"""
Protective exit management (Milestone 2, PART 13).

THE RULE
--------
"A position must never be considered 'managed' simply because the intended stop
exists in the database. Confirm broker acceptance."

Milestone 1 wrote `stop` and `targets` onto the Order row and submitted a bare
market entry. Nothing was ever transmitted to the broker. Any position it opened
was, in reality, unprotected, while the journal displayed a stop price. That is
the most dangerous class of bug in this whole system: not a missing feature, but
a feature that appears present.

So this module distinguishes three states explicitly, and the operator-facing
name of the middle one is chosen to be alarming:

    INTENDED      we know what stop we want; the broker has not been told
    UNPROTECTED   we have a position and no broker-side protection exists
    BROKER_HELD   the broker has acknowledged a working protective order

`ProtectionStatus.is_protected` is True only for BROKER_HELD, and only when the
broker returned a live order id for the protective leg.

PREFERENCE ORDER
----------------
Broker-native first, because a protective order living on the broker's books
survives this process crashing, losing its network, or being killed:

  1. BRACKET on entry -- one submission, entry + stop + target, broker-managed.
     Requires BOTH legs (Alpaca's validator enforces it), whole-share quantity,
     and no extended_hours.
  2. OCO attached to an existing position -- when a position already exists
     (e.g. after a partial fill, or after restart-with-position recovery).
  3. Local synthetic stop -- LAST resort, and reported as DEGRADED protection,
     never as protection. It only works while this process is alive and polling.

Time-based and end-of-session exits are necessarily local (no broker primitive
expresses "close at 15:55"), so they are implemented as monitored rules whose
enforcement action is an ungated `close_position`.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from enum import Enum

from app.broker.base import BrokerError, OrderRequest

log = logging.getLogger("aegis.execution.exits")


class ProtectionState(str, Enum):
    INTENDED = "INTENDED"
    UNPROTECTED = "UNPROTECTED"
    BROKER_HELD = "BROKER_HELD"
    LOCAL_SYNTHETIC = "LOCAL_SYNTHETIC"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    PROFIT_TARGET = "profit_target"
    TRAILING_STOP = "trailing_stop"
    TIME_LIMIT = "time_limit"
    END_OF_SESSION = "end_of_session"
    INVALIDATION = "invalidation"
    CIRCUIT_BREAKER_FLATTEN = "circuit_breaker_flatten"
    OPERATOR_MANUAL = "operator_manual"
    RECONCILIATION_HALT = "reconciliation_halt"


@dataclasses.dataclass(frozen=True)
class ProtectionPlan:
    """What we intend. Holding one of these protects nothing."""

    ticker: str
    direction: str            # long | short
    qty: int
    entry: float
    stop: float
    targets: tuple[float, ...]
    max_holding_minutes: int | None = None
    close_at_session_end: bool = True
    trail_percent: float | None = None

    def __post_init__(self):
        if self.qty <= 0:
            raise ValueError(f"ProtectionPlan qty must be positive, got {self.qty}")
        if self.direction == "long" and self.stop >= self.entry:
            raise ValueError(
                f"Long protection plan has stop {self.stop} at or above entry "
                f"{self.entry}. A stop that cannot be hit by adverse movement is "
                f"not a stop.")
        if self.direction == "short" and self.stop <= self.entry:
            raise ValueError(
                f"Short protection plan has stop {self.stop} at or below entry "
                f"{self.entry}.")

    @property
    def primary_target(self) -> float | None:
        return self.targets[0] if self.targets else None

    @property
    def exit_side(self) -> str:
        return "SELL" if self.direction == "long" else "COVER"

    def as_record(self) -> dict:
        return dataclasses.asdict(self) | {"targets": list(self.targets)}


@dataclasses.dataclass(frozen=True)
class ProtectionStatus:
    """What is ACTUALLY true about a position's protection right now."""

    ticker: str
    state: ProtectionState
    plan: ProtectionPlan | None
    broker_order_ids: tuple[str, ...]
    detail: str
    checked_at: dt.datetime

    @property
    def is_protected(self) -> bool:
        """Broker-side protection only.

        LOCAL_SYNTHETIC returns False on purpose. A stop that only exists in
        this process is not protection against the scenarios protection is for
        -- crash, hang, power loss, network partition -- so calling it protected
        would make the health snapshot lie.
        """
        return self.state is ProtectionState.BROKER_HELD and bool(self.broker_order_ids)

    @property
    def requires_operator_attention(self) -> bool:
        return self.state in (ProtectionState.UNPROTECTED,
                              ProtectionState.LOCAL_SYNTHETIC,
                              ProtectionState.INTENDED)

    def as_record(self) -> dict:
        return {
            "ticker": self.ticker, "state": self.state.value,
            "is_protected": self.is_protected,
            "requires_operator_attention": self.requires_operator_attention,
            "broker_order_ids": list(self.broker_order_ids),
            "detail": self.detail,
            "plan": self.plan.as_record() if self.plan else None,
            "checked_at": self.checked_at.isoformat(),
        }


class ProtectiveExitManager:
    """Builds bracket entries, verifies broker acceptance, and enforces the
    exits no broker primitive can express.

    Args:
        execution_engine: used for ungated protective actions (close, cancel).
        broker: used for status queries.
        allow_local_synthetic_fallback: when False (the default), a position
            whose broker-side protection could not be established is CLOSED
            immediately rather than held behind a local-only stop. Flat is a
            safer state than unprotected-and-hoping.
    """

    def __init__(self, execution_engine, broker,
                 allow_local_synthetic_fallback: bool = False):
        self.engine = execution_engine
        self.broker = broker
        self.allow_local_synthetic_fallback = allow_local_synthetic_fallback

    # -- building the entry ------------------------------------------------
    def build_bracket_entry(self, plan: ProtectionPlan, *,
                            limit_price: float | None = None,
                            time_in_force: str = "day") -> OrderRequest:
        """Entry order with broker-managed stop and target attached.

        Refuses to build a bracket without a target, because Alpaca rejects a
        one-legged bracket anyway and a locally-tracked "bracket" that the broker
        never accepted is exactly the illusion this module exists to prevent.
        """
        target = plan.primary_target
        if target is None:
            raise BrokerError(
                f"Cannot build a bracket entry for {plan.ticker} without a "
                f"profit target: bracket and OCO orders require BOTH a "
                f"take-profit and a stop leg. Either supply a target or use an "
                f"entry plus a separate stop order and accept that the two are "
                f"not atomic."
            )
        _assert_target_side(plan, target)
        return OrderRequest(
            ticker=plan.ticker,
            side="BUY" if plan.direction == "long" else "SHORT",
            qty=plan.qty,
            order_type="bracket",
            limit_price=limit_price,
            stop_price=plan.stop,
            take_profit_price=target,
            time_in_force=time_in_force,
            extended_hours=False,   # brackets + extended hours are rejected
        )

    # -- verifying protection ---------------------------------------------
    def verify_protection(self, plan: ProtectionPlan,
                          now: dt.datetime | None = None) -> ProtectionStatus:
        """Ask the BROKER what protective orders exist for this ticker.

        Never consults the local database. The whole point is to find out
        whether the broker agrees with what the database claims.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            open_orders = self.broker.get_open_orders()
        except Exception as exc:  # noqa: BLE001
            return ProtectionStatus(
                ticker=plan.ticker, state=ProtectionState.UNPROTECTED, plan=plan,
                broker_order_ids=(), checked_at=now,
                detail=(f"could not query broker for protective orders ({exc}). "
                        f"Protection status is unverifiable, which must be treated "
                        f"as UNPROTECTED, not as fine."),
            )

        protective_ids = []
        for status in open_orders:
            raw = status.raw or {}
            if (raw.get("symbol") or "").upper() != plan.ticker.upper():
                continue
            if self._is_protective(raw, plan):
                protective_ids.append(status.broker_order_id)
            for leg in raw.get("legs") or []:
                if self._leg_is_protective(leg, plan):
                    protective_ids.append(leg["broker_order_id"])

        if protective_ids:
            return ProtectionStatus(
                ticker=plan.ticker, state=ProtectionState.BROKER_HELD, plan=plan,
                broker_order_ids=tuple(dict.fromkeys(protective_ids)), checked_at=now,
                detail=(f"broker holds {len(set(protective_ids))} working protective "
                        f"order(s) for {plan.ticker}; protection survives this "
                        f"process dying"),
            )

        return ProtectionStatus(
            ticker=plan.ticker, state=ProtectionState.UNPROTECTED, plan=plan,
            broker_order_ids=(), checked_at=now,
            detail=(f"broker reports NO working protective order for {plan.ticker}. "
                    f"The intended stop of {plan.stop} exists only in local records, "
                    f"which does not protect the position."),
        )

    @staticmethod
    def _is_protective(raw: dict, plan: ProtectionPlan) -> bool:
        order_type = (raw.get("type") or "").lower()
        side = (raw.get("side") or "").lower()
        expected_side = "sell" if plan.direction == "long" else "buy"
        return (order_type in ("stop", "stop_limit", "trailing_stop", "limit")
                and side == expected_side
                and (raw.get("stop_price") is not None
                     or raw.get("limit_price") is not None
                     or order_type == "trailing_stop"))

    @staticmethod
    def _leg_is_protective(leg: dict, plan: ProtectionPlan) -> bool:
        leg_type = (leg.get("type") or "").lower()
        leg_status = (leg.get("status") or "").lower()
        working = leg_status in ("new", "accepted", "held", "pending_new",
                                "partially_filled")
        return working and leg_type in ("stop", "stop_limit", "limit", "trailing_stop")

    def ensure_protected_or_flatten(self, plan: ProtectionPlan,
                                    now: dt.datetime | None = None) -> ProtectionStatus:
        """Verify protection, and if absent, take the safe action.

        Attempts to attach an OCO first. If that fails, closes the position --
        unless `allow_local_synthetic_fallback` was explicitly enabled, in which
        case the position is retained under DEGRADED, clearly-labelled local-only
        protection.
        """
        status = self.verify_protection(plan, now=now)
        if status.is_protected:
            return status

        log.error("Position %s is UNPROTECTED: %s", plan.ticker, status.detail)

        attached = self.attach_oco(plan)
        if attached.is_protected:
            return attached

        if self.allow_local_synthetic_fallback:
            log.warning(
                "Falling back to LOCAL SYNTHETIC protection for %s. This is "
                "DEGRADED: the stop exists only while this process runs.",
                plan.ticker)
            return dataclasses.replace(
                attached, state=ProtectionState.LOCAL_SYNTHETIC,
                detail=("broker-side protection could not be established; a "
                        "local-only stop is being monitored. This does not "
                        "survive a crash and is NOT counted as protected."),
            )

        log.critical("Closing %s because broker-side protection could not be "
                     "established. Flat is safer than unprotected.", plan.ticker)
        try:
            self.engine.close_position(
                plan.ticker,
                reason=("no broker-side protective order could be established; "
                        "closing rather than holding an unprotected position"))
            return dataclasses.replace(
                attached, state=ProtectionState.CLOSED,
                detail=("position closed because broker-side protection could not "
                        "be established"),
            )
        except Exception as exc:  # noqa: BLE001
            log.critical("Could not close unprotected position %s: %s", plan.ticker, exc)
            return dataclasses.replace(
                attached, state=ProtectionState.UNPROTECTED,
                detail=(f"position is UNPROTECTED and the attempt to close it also "
                        f"failed ({exc}). OPERATOR INTERVENTION REQUIRED."),
            )

    def attach_oco(self, plan: ProtectionPlan,
                   now: dt.datetime | None = None) -> ProtectionStatus:
        """Attach a broker-native OCO (stop + target) to an existing position.

        Used after a partial fill, and during restart-with-position recovery.
        Requires no ExecutionGrant: this reduces risk on a position that already
        exists, and gating it behind entry authorization would mean a tripped
        breaker prevented us from protecting an open position.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        target = plan.primary_target
        if target is None:
            return ProtectionStatus(
                ticker=plan.ticker, state=ProtectionState.UNPROTECTED, plan=plan,
                broker_order_ids=(), checked_at=now,
                detail=("cannot attach an OCO without a profit target; OCO requires "
                        "both legs"),
            )
        try:
            _assert_target_side(plan, target)
            req = OrderRequest(
                ticker=plan.ticker, side=plan.exit_side, qty=plan.qty,
                order_type="oco", stop_price=plan.stop, take_profit_price=target,
                time_in_force="gtc",
            )
            status = self.broker.submit_protective_order(req) if hasattr(
                self.broker, "submit_protective_order") else None
            if status is None:
                return ProtectionStatus(
                    ticker=plan.ticker, state=ProtectionState.UNPROTECTED, plan=plan,
                    broker_order_ids=(), checked_at=now,
                    detail=(f"{type(self.broker).__name__} exposes no ungated "
                            f"protective-order path, so an OCO could not be "
                            f"attached without an entry grant. Treating as "
                            f"UNPROTECTED rather than assuming."),
                )
            return ProtectionStatus(
                ticker=plan.ticker, state=ProtectionState.BROKER_HELD, plan=plan,
                broker_order_ids=(status.broker_order_id,), checked_at=now,
                detail=f"OCO accepted by broker (id={status.broker_order_id})",
            )
        except Exception as exc:  # noqa: BLE001
            return ProtectionStatus(
                ticker=plan.ticker, state=ProtectionState.UNPROTECTED, plan=plan,
                broker_order_ids=(), checked_at=now,
                detail=f"broker refused the protective OCO: {exc}",
            )

    # -- exits no broker primitive expresses -------------------------------
    def evaluate_time_exit(self, plan: ProtectionPlan, *, entry_time: dt.datetime,
                           now: dt.datetime | None = None) -> ExitReason | None:
        if plan.max_holding_minutes is None:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        held = (now - entry_time).total_seconds() / 60.0
        if held >= plan.max_holding_minutes:
            log.info("Time exit for %s: held %.1f min, limit %d",
                     plan.ticker, held, plan.max_holding_minutes)
            return ExitReason.TIME_LIMIT
        return None

    def evaluate_session_exit(self, plan: ProtectionPlan, *, session_close: dt.datetime | None,
                              flatten_buffer_minutes: int = 5,
                              now: dt.datetime | None = None) -> ExitReason | None:
        """Close before the session ends.

        `session_close` comes from the market calendar, not from a hard-coded
        16:00. Early-close days are exactly when a hard-coded time silently
        holds a position past the bell.

        A missing `session_close` returns None rather than guessing a time. The
        caller is expected to treat an unknown session as a reason not to be in
        a position at all, which is a decision for the session gate, not here.
        """
        if not plan.close_at_session_end or session_close is None:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        if now >= session_close - dt.timedelta(minutes=flatten_buffer_minutes):
            log.info("End-of-session exit for %s: now=%s close=%s buffer=%dmin",
                     plan.ticker, now.isoformat(), session_close.isoformat(),
                     flatten_buffer_minutes)
            return ExitReason.END_OF_SESSION
        return None

    def execute_exit(self, ticker: str, reason: ExitReason) -> dict:
        """Perform an exit. Ungated by design."""
        log.warning("Exiting %s: %s", ticker, reason.value)
        result = {"ticker": ticker, "reason": reason.value,
                  "at": dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            status = self.engine.close_position(ticker, reason=reason.value)
            result["broker_order_id"] = status.broker_order_id
            result["broker_status"] = str(status.status)
            result["succeeded"] = True
        except Exception as exc:  # noqa: BLE001
            result["succeeded"] = False
            result["error"] = str(exc)
            log.critical("EXIT FAILED for %s (%s): %s. Position remains open.",
                         ticker, reason.value, exc)
        return result


def _assert_target_side(plan: ProtectionPlan, target: float) -> None:
    if plan.direction == "long" and target <= plan.entry:
        raise BrokerError(
            f"Long target {target} is at or below entry {plan.entry}; a "
            f"take-profit that triggers on adverse movement is not a target.")
    if plan.direction == "short" and target >= plan.entry:
        raise BrokerError(
            f"Short target {target} is at or above entry {plan.entry}.")

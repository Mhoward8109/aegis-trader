"""
Persistent, system-level circuit breaker (Milestone 2, PART 15).

WHY THIS REPLACES THE MILESTONE 1 BREAKER
-----------------------------------------
`app/risk/circuit_breaker.py` held its tripped state in a plain in-memory dict.
Two consequences were confirmed by inspection (docs/AUDIT_MILESTONE2.md):

  1. **A restart cleared it.** The single most likely thing to happen right
     after a critical fault -- a crash, an operator restart, a supervisor
     relaunch -- silently erased the protection. A breaker that forgets is not a
     breaker.
  2. **`clear_for_new_session(today)` could un-trip the current session.** The
     docstring said it was "only meaningful for a session_date that has not
     started yet", but nothing enforced that. It popped whatever key it was
     given.

Additionally the old breaker had zero call sites anywhere in `app/`, and the
dashboard hard-coded `"circuit_breaker_tripped": False`.

DESIGN
------
* State lives in its own SQLite file, separate from the trade journal, so a
  corrupted or deleted journal DB cannot clear the breaker, and vice versa.
* Every read hits the database. There is no cached "not tripped" answer that
  could survive a trip written by another process.
* Writes are committed immediately, before the caller is told the trip
  succeeded.
* **Trips are append-only.** Clearing writes a new row; it never deletes
  history. The full trip/reset ledger is retained for the operator.
* Resetting the CURRENT session requires a `BreakerReset` object which can only
  be constructed with a module-level seal. Strategy, risk, and execution code do
  not import that seal, and a repo-scan test enforces it. This is the same
  honesty caveat as the execution grant: Python cannot make it unforgeable, but
  it makes bypass a deliberate, greppable, test-detectable act rather than an
  accident.

WHAT A TRIPPED BREAKER DOES AND DOES NOT BLOCK
----------------------------------------------
Prohibits: new entries.
Permits:  protective exits, position closes, order cancellation.

That asymmetry is deliberate and is the whole point. A breaker that also blocked
exits would strand a live position behind its own safety mechanism -- the
failure mode is worse than the one it protects against. `permits_entry()` and
`permits_protective_exit()` are separate methods so no caller has to reason
about it.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sqlite3
import uuid
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Reset seal. Only app/cli.py's explicit operator reset command imports this.
# tests/test_authorization_invariants.py enforces that no strategy, risk,
# execution, scanner, or orchestration module imports it.
# ---------------------------------------------------------------------------
_RESET_SEAL = object()


class BreakerTrigger(str, Enum):
    """Every condition that can trip the breaker.

    Enumerated rather than free-form strings so the operator-facing health
    snapshot and the reset audit trail cannot drift apart from the code that
    trips.
    """

    DAILY_LOSS_LIMIT = "daily_loss_limit"
    WEEKLY_LOSS_LIMIT = "weekly_loss_limit"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    STALE_MARKET_DATA = "stale_market_data"
    BROKER_DISCONNECTED = "broker_disconnected"
    REPEATED_ORDER_REJECTIONS = "repeated_order_rejections"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    UNEXPECTED_POSITION = "unexpected_position"
    CORRUPTED_STATE = "corrupted_state"
    EXCESSIVE_SLIPPAGE = "excessive_slippage"
    CRITICAL_EXCEPTION = "critical_exception"
    OPERATOR_MANUAL_HALT = "operator_manual_halt"


class BreakerSealError(Exception):
    """Raised when a BreakerReset is constructed without the module seal."""


class BreakerTrippedError(Exception):
    """Raised by `assert_entries_permitted()`.

    Callers that must abort use this; callers on the authorization path use the
    boolean from `state()` as evidence instead.
    """

    def __init__(self, message: str, state: BreakerState):
        super().__init__(message)
        self.state = state


@dataclasses.dataclass(frozen=True)
class TripRecord:
    trip_id: str
    session_date: str
    trigger: BreakerTrigger
    detail: str
    context: dict
    tripped_at: dt.datetime
    cleared_at: dt.datetime | None
    cleared_by: str | None
    clear_reason: str | None

    @property
    def active(self) -> bool:
        return self.cleared_at is None

    def as_record(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "session_date": self.session_date,
            "trigger": self.trigger.value,
            "detail": self.detail,
            "context": self.context,
            "tripped_at": self.tripped_at.isoformat(),
            "cleared_at": self.cleared_at.isoformat() if self.cleared_at else None,
            "cleared_by": self.cleared_by,
            "clear_reason": self.clear_reason,
            "active": self.active,
        }


@dataclasses.dataclass(frozen=True)
class BreakerState:
    """The operator-facing answer to "can this system trade right now?"."""

    tripped: bool
    active_trips: tuple[TripRecord, ...]
    checked_at: dt.datetime

    @property
    def reason(self) -> str:
        if not self.tripped:
            return "Circuit breaker clear."
        parts = [f"{t.trigger.value}: {t.detail}" for t in self.active_trips]
        return "CIRCUIT BREAKER TRIPPED -- " + "; ".join(parts)

    def permits_entry(self) -> bool:
        """New positions. Blocked whenever the breaker is tripped."""
        return not self.tripped

    def permits_protective_exit(self) -> bool:
        """Stops, targets, closes, cancels.

        Always True. A tripped breaker must never strand an open position
        behind its own safety mechanism.
        """
        return True

    def as_record(self) -> dict:
        return {
            "tripped": self.tripped,
            "reason": self.reason,
            "checked_at": self.checked_at.isoformat(),
            "active_trips": [t.as_record() for t in self.active_trips],
        }


@dataclasses.dataclass(frozen=True)
class BreakerReset:
    """Authorization to clear a trip.

    Constructing this requires `_RESET_SEAL`, which lives only in this module
    and is imported only by the operator CLI reset command. `same_session` must
    be explicitly True to clear a trip for a session that has already begun --
    the common case (a new trading day) does not need it, so the dangerous
    option is never the default.
    """

    operator: str
    reason: str
    same_session: bool
    issued_at: dt.datetime

    def __init__(self, seal: object, *, operator: str, reason: str,
                 same_session: bool = False,
                 issued_at: dt.datetime | None = None):
        if seal is not _RESET_SEAL:
            raise BreakerSealError(
                "BreakerReset must be constructed with the module reset seal. "
                "Strategy, risk, and execution code cannot reset the circuit "
                "breaker; only the operator CLI reset command can."
            )
        if not operator or not operator.strip():
            raise ValueError("A breaker reset must record WHO performed it.")
        if not reason or len(reason.strip()) < 10:
            raise ValueError(
                "A breaker reset must record a substantive reason (>=10 chars) "
                "explaining what was investigated and fixed. A reset without a "
                "diagnosis is how a breaker becomes decorative."
            )
        object.__setattr__(self, "operator", operator.strip())
        object.__setattr__(self, "reason", reason.strip())
        object.__setattr__(self, "same_session", same_session)
        object.__setattr__(self, "issued_at",
                           issued_at or dt.datetime.now(dt.timezone.utc))


def issue_operator_reset(*, operator: str, reason: str,
                         same_session: bool = False) -> BreakerReset:
    """The ONLY supported way to obtain a BreakerReset.

    Intentionally a module-level function rather than a method on the breaker,
    so that holding a `PersistentCircuitBreaker` instance does not by itself
    give a caller the ability to reset it.
    """
    return BreakerReset(_RESET_SEAL, operator=operator, reason=reason,
                        same_session=same_session)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS breaker_trips (
    trip_id       TEXT PRIMARY KEY,
    session_date  TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    detail        TEXT NOT NULL,
    context_json  TEXT NOT NULL,
    tripped_at    TEXT NOT NULL,
    cleared_at    TEXT,
    cleared_by    TEXT,
    clear_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_breaker_active
    ON breaker_trips (cleared_at);
"""


class PersistentCircuitBreaker:
    """Durable system-level kill switch.

    Thread/process safety: SQLite handles the concurrency. Every method opens a
    short-lived connection rather than holding one, so a long-running process
    and a one-shot CLI command see the same state without a shared handle.
    """

    def __init__(self, db_path: str | Path, cfg: dict | None = None):
        self.db_path = Path(db_path)
        self.cfg = cfg or {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0,
                               isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # -- reading ---------------------------------------------------------
    def state(self, now: dt.datetime | None = None) -> BreakerState:
        """Read current state FROM DISK. Never cached."""
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT trip_id, session_date, trigger, detail, context_json,"
                    " tripped_at, cleared_at, cleared_by, clear_reason"
                    " FROM breaker_trips WHERE cleared_at IS NULL"
                    " ORDER BY tripped_at"
                ).fetchall()
        except sqlite3.Error as exc:
            # A breaker whose own store is unreadable must report TRIPPED. The
            # alternative -- assuming "clear" because we could not check -- is
            # exactly the failure this class exists to prevent.
            synthetic = TripRecord(
                trip_id="unreadable-store",
                session_date=now.date().isoformat(),
                trigger=BreakerTrigger.CORRUPTED_STATE,
                detail=(f"circuit breaker state store could not be read ({exc}). "
                        f"Failing closed: entries are prohibited until the "
                        f"operator resolves this."),
                context={"db_path": str(self.db_path), "error": str(exc)},
                tripped_at=now, cleared_at=None, cleared_by=None,
                clear_reason=None,
            )
            return BreakerState(tripped=True, active_trips=(synthetic,),
                                checked_at=now)

        trips = tuple(self._row_to_trip(r) for r in rows)
        return BreakerState(tripped=bool(trips), active_trips=trips,
                            checked_at=now)

    def is_tripped(self) -> bool:
        return self.state().tripped

    def permits_entry(self) -> bool:
        return self.state().permits_entry()

    def assert_entries_permitted(self) -> BreakerState:
        st = self.state()
        if not st.permits_entry():
            raise BreakerTrippedError(st.reason, st)
        return st

    def history(self, limit: int = 100) -> list[TripRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trip_id, session_date, trigger, detail, context_json,"
                " tripped_at, cleared_at, cleared_by, clear_reason"
                " FROM breaker_trips ORDER BY tripped_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_trip(r) for r in rows]

    @staticmethod
    def _row_to_trip(r: tuple) -> TripRecord:
        try:
            context = json.loads(r[4])
        except (json.JSONDecodeError, TypeError):
            context = {"_unparseable_context": r[4]}
        return TripRecord(
            trip_id=r[0], session_date=r[1],
            trigger=_coerce_trigger(r[2]), detail=r[3], context=context,
            tripped_at=_parse_ts(r[5]),
            cleared_at=_parse_ts(r[6]) if r[6] else None,
            cleared_by=r[7], clear_reason=r[8],
        )

    # -- tripping --------------------------------------------------------
    def trip(self, trigger: BreakerTrigger, detail: str,
             context: dict | None = None,
             session_date: str | None = None,
             now: dt.datetime | None = None) -> TripRecord:
        """Trip the breaker and PERSIST before returning.

        Idempotent per (session_date, trigger): re-tripping the same trigger in
        the same session returns the existing active record rather than
        multiplying rows, so a tight retry loop cannot flood the ledger.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        session_date = session_date or now.date().isoformat()
        context = context or {}

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT trip_id, session_date, trigger, detail, context_json,"
                " tripped_at, cleared_at, cleared_by, clear_reason"
                " FROM breaker_trips"
                " WHERE cleared_at IS NULL AND session_date = ? AND trigger = ?",
                (session_date, trigger.value),
            ).fetchone()
            if existing:
                return self._row_to_trip(existing)

            trip_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO breaker_trips (trip_id, session_date, trigger,"
                " detail, context_json, tripped_at) VALUES (?,?,?,?,?,?)",
                (trip_id, session_date, trigger.value, detail,
                 json.dumps(context, default=str), now.isoformat()),
            )

        return TripRecord(
            trip_id=trip_id, session_date=session_date, trigger=trigger,
            detail=detail, context=context, tripped_at=now,
            cleared_at=None, cleared_by=None, clear_reason=None,
        )

    # -- resetting -------------------------------------------------------
    def reset(self, reset: BreakerReset, *, session_date: str | None = None,
              trip_id: str | None = None,
              now: dt.datetime | None = None) -> list[TripRecord]:
        """Clear active trips under an explicit operator authorization.

        Guards, in order:
          * `reset` must be a real `BreakerReset` (seal-constructed).
          * Clearing a trip whose `session_date` is the current session requires
            `reset.same_session is True`.
          * Clearing is recorded, never silent: `cleared_at`, `cleared_by`, and
            `clear_reason` are written and the row is retained.

        Returns the trips that were cleared. An empty list means nothing
        matched -- which is itself informative and is not an error.
        """
        if not isinstance(reset, BreakerReset):
            raise BreakerSealError(
                "reset() requires a BreakerReset issued via "
                "issue_operator_reset(). Passing a bare string, dict, or bool "
                "is not a valid operator authorization."
            )
        now = now or dt.datetime.now(dt.timezone.utc)
        today = now.date().isoformat()

        state = self.state(now=now)
        candidates = [t for t in state.active_trips
                      if (trip_id is None or t.trip_id == trip_id)
                      and (session_date is None or t.session_date == session_date)]

        blocked = [t for t in candidates
                   if t.session_date >= today and not reset.same_session]
        if blocked:
            names = ", ".join(f"{t.trigger.value}@{t.session_date}" for t in blocked)
            raise BreakerSealError(
                f"Refusing to clear trips for the current or a future session "
                f"({names}) without an explicit same-session reset. The default "
                f"reset path is for starting a NEW session; clearing today's "
                f"breaker mid-session requires "
                f"issue_operator_reset(..., same_session=True) and is expected "
                f"to follow an actual investigation."
            )

        cleared: list[TripRecord] = []
        with self._connect() as conn:
            for t in candidates:
                conn.execute(
                    "UPDATE breaker_trips SET cleared_at=?, cleared_by=?,"
                    " clear_reason=? WHERE trip_id=? AND cleared_at IS NULL",
                    (now.isoformat(), reset.operator, reset.reason, t.trip_id),
                )
                cleared.append(dataclasses.replace(
                    t, cleared_at=now, cleared_by=reset.operator,
                    clear_reason=reset.reason,
                ))
        return cleared

    # -- trigger evaluators ---------------------------------------------
    #
    # Each returns the TripRecord if it tripped, else None. They are separate
    # named methods (rather than one generic `check(threshold, value)`) so that
    # the health snapshot and the tests can refer to a specific safety property
    # by name.
    def check_daily_loss(self, *, realized_pnl_today: float, equity: float,
                         max_daily_loss_pct: float,
                         session_date: str | None = None) -> TripRecord | None:
        if equity <= 0:
            return self.trip(
                BreakerTrigger.CORRUPTED_STATE,
                f"account equity reported as {equity}, which cannot be used to "
                f"evaluate a percentage loss limit",
                {"equity": equity}, session_date=session_date,
            )
        limit = -(max_daily_loss_pct / 100.0) * equity
        if realized_pnl_today <= limit:
            return self.trip(
                BreakerTrigger.DAILY_LOSS_LIMIT,
                f"realized P&L {realized_pnl_today:.2f} breached the daily loss "
                f"limit of {limit:.2f} ({max_daily_loss_pct}% of {equity:.2f})",
                {"realized_pnl_today": realized_pnl_today, "limit": limit,
                 "equity": equity, "max_daily_loss_pct": max_daily_loss_pct},
                session_date=session_date,
            )
        return None

    def check_weekly_loss(self, *, realized_pnl_week: float, equity: float,
                          max_weekly_loss_pct: float,
                          session_date: str | None = None) -> TripRecord | None:
        if equity <= 0:
            return self.trip(
                BreakerTrigger.CORRUPTED_STATE,
                f"account equity reported as {equity}, which cannot be used to "
                f"evaluate a percentage loss limit",
                {"equity": equity}, session_date=session_date,
            )
        limit = -(max_weekly_loss_pct / 100.0) * equity
        if realized_pnl_week <= limit:
            return self.trip(
                BreakerTrigger.WEEKLY_LOSS_LIMIT,
                f"weekly realized P&L {realized_pnl_week:.2f} breached the "
                f"weekly loss limit of {limit:.2f}",
                {"realized_pnl_week": realized_pnl_week, "limit": limit,
                 "equity": equity, "max_weekly_loss_pct": max_weekly_loss_pct},
                session_date=session_date,
            )
        return None

    def check_consecutive_losses(self, *, consecutive_losses: int,
                                 max_allowed: int,
                                 session_date: str | None = None) -> TripRecord | None:
        if consecutive_losses >= max_allowed:
            return self.trip(
                BreakerTrigger.CONSECUTIVE_LOSSES,
                f"{consecutive_losses} consecutive losing trades reached the "
                f"limit of {max_allowed}",
                {"consecutive_losses": consecutive_losses,
                 "max_allowed": max_allowed},
                session_date=session_date,
            )
        return None

    def check_freshness_report(self, report, *,
                               session_date: str | None = None) -> TripRecord | None:
        """Trip on stale data.

        Takes a `FreshnessReport` (app.marketdata.freshness) rather than a raw
        age, so the recorded context names every stale source instead of only
        the first one that failed.
        """
        if report is None:
            return self.trip(
                BreakerTrigger.STALE_MARKET_DATA,
                "no freshness report was produced for this cycle, so data age "
                "is unknown; treating unknown as stale",
                {}, session_date=session_date,
            )
        if getattr(report, "all_required_fresh", False):
            return None
        stale = getattr(report, "stale_required_sources", [])
        return self.trip(
            BreakerTrigger.STALE_MARKET_DATA,
            getattr(report, "detail", "required market data was stale"),
            {"stale_sources": [dataclasses.asdict(s) for s in stale]},
            session_date=session_date,
        )

    def check_broker_connected(self, *, connected: bool, detail: str = "",
                               session_date: str | None = None) -> TripRecord | None:
        if not connected:
            return self.trip(
                BreakerTrigger.BROKER_DISCONNECTED,
                detail or "broker connectivity check failed",
                {"detail": detail}, session_date=session_date,
            )
        return None

    def check_repeated_rejections(self, *, rejection_count: int,
                                  threshold: int | None = None,
                                  session_date: str | None = None) -> TripRecord | None:
        threshold = (threshold if threshold is not None
                     else self.cfg.get("repeated_rejected_orders_trip", 3))
        if rejection_count >= threshold:
            return self.trip(
                BreakerTrigger.REPEATED_ORDER_REJECTIONS,
                f"{rejection_count} order rejections reached the threshold of "
                f"{threshold}; the system is being told 'no' repeatedly and "
                f"should stop rather than keep asking",
                {"rejection_count": rejection_count, "threshold": threshold},
                session_date=session_date,
            )
        return None

    def check_reconciliation(self, *, discrepancies: list | None,
                             session_date: str | None = None) -> TripRecord | None:
        if discrepancies:
            return self.trip(
                BreakerTrigger.RECONCILIATION_FAILURE,
                f"{len(discrepancies)} unresolved discrepancy/discrepancies "
                f"between local records and broker state",
                {"discrepancies": discrepancies}, session_date=session_date,
            )
        return None

    def check_unexpected_position(self, *, unexpected: list | None,
                                  session_date: str | None = None) -> TripRecord | None:
        if unexpected:
            return self.trip(
                BreakerTrigger.UNEXPECTED_POSITION,
                f"broker reports {len(unexpected)} position(s) this system has "
                f"no record of opening: {unexpected}",
                {"unexpected_positions": unexpected}, session_date=session_date,
            )
        return None

    def check_slippage(self, *, slippage_pct: float,
                       threshold_pct: float | None = None,
                       session_date: str | None = None) -> TripRecord | None:
        threshold_pct = (threshold_pct if threshold_pct is not None
                         else self.cfg.get("excessive_slippage_trip_pct"))
        if threshold_pct is None:
            return None
        if slippage_pct > threshold_pct:
            return self.trip(
                BreakerTrigger.EXCESSIVE_SLIPPAGE,
                f"fill slippage {slippage_pct:.3f}% exceeded the "
                f"{threshold_pct:.3f}% threshold, indicating execution "
                f"conditions the sizing model did not assume",
                {"slippage_pct": slippage_pct, "threshold_pct": threshold_pct},
                session_date=session_date,
            )
        return None

    def trip_on_critical_exception(self, exc: BaseException, *, where: str,
                                   session_date: str | None = None) -> TripRecord:
        return self.trip(
            BreakerTrigger.CRITICAL_EXCEPTION,
            f"unhandled exception in {where}: {type(exc).__name__}: {exc}",
            {"where": where, "exception_type": type(exc).__name__,
             "exception": str(exc)},
            session_date=session_date,
        )


def _parse_ts(raw: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _coerce_trigger(raw: str) -> BreakerTrigger:
    try:
        return BreakerTrigger(raw)
    except ValueError:
        # An unrecognised trigger string in the store is itself a corrupted
        # state condition; do not silently drop the row's meaning.
        return BreakerTrigger.CORRUPTED_STATE


def default_breaker_path(data_dir: str | Path = "data") -> Path:
    """Breaker state file location.

    Deliberately separate from the journal DB: deleting or recreating the trade
    journal must not clear the breaker.
    """
    return Path(os.environ.get("AEGIS_BREAKER_DB",
                               str(Path(data_dir) / "circuit_breaker.db")))

"""
Order lifecycle management and broker reconciliation (Milestone 2, PART 12).

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
Broker-confirmed state is authoritative. `submit_order()` returning is NOT a
fill.

Milestone 1 violated this in one line:

    journal.update_order_state(order,
        OrderState.FILLED if status.status == "filled" else OrderState.SUBMITTED,
        ...)

Three separate defects in that expression:
  1. It read a status string from the submit response -- an acknowledgement --
     and treated `"filled"` as a completed trade, with no check that
     `filled_qty > 0` or that an average fill price existed.
  2. It transitioned PROPOSED -> SUBMITTED and PROPOSED -> FILLED directly, both
     of which the state machine's own table forbids. Nothing called
     `assert_transition_allowed`, so the table was decorative.
  3. It never asked the broker again. Whatever the submit response happened to
     say became permanent local truth.

`OrderLifecycleManager` below routes every state change through
`assert_transition_allowed`, and derives state from a FRESH broker query rather
than from the submit response.

RECONCILIATION
--------------
`reconcile()` compares local records against the broker's own orders and
positions and classifies every disagreement. Per the brief, unexplained
discrepancies must block new trading -- so `ReconciliationReport.blocks_trading`
is True whenever any discrepancy is of a kind that means we do not know our own
exposure. The pipeline feeds that into the circuit breaker.

Deliberately, an unexpected broker position blocks trading but does NOT get
auto-closed. Closing a position this system did not open is a real-money action
based on a guess about its provenance; the correct response is to halt and tell
the operator.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from enum import Enum

from app.broker.base import BrokerAdapter, BrokerOrderStatus
from app.common.db import OrderState
from app.execution.order_state_machine import (
    assert_transition_allowed,
    is_terminal,
)

log = logging.getLogger("aegis.execution.lifecycle")


# ---------------------------------------------------------------------------
# Broker status -> our OrderState.
#
# Alpaca has 18 order statuses (research §2.5). Mapping them explicitly, with a
# deliberate default of UNKNOWN, means a status we have never seen halts and
# reconciles instead of being coerced into a plausible-looking state.
# ---------------------------------------------------------------------------
BROKER_STATUS_MAP: dict[str, OrderState] = {
    # accepted / working
    "new": OrderState.ACKNOWLEDGED,
    "accepted": OrderState.ACKNOWLEDGED,
    "pending_new": OrderState.SUBMITTED,
    "accepted_for_bidding": OrderState.ACKNOWLEDGED,
    "held": OrderState.ACKNOWLEDGED,
    "pending_replace": OrderState.ACKNOWLEDGED,
    "replaced": OrderState.ACKNOWLEDGED,
    "pending_cancel": OrderState.ACKNOWLEDGED,
    # fills
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    # terminal-ish
    "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED,
    "expired": OrderState.EXPIRED,
    "done_for_day": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
    # states whose meaning for us is genuinely unclear -> UNKNOWN, not a guess
    "suspended": OrderState.UNKNOWN,
    "pending_review": OrderState.UNKNOWN,
    "stopped": OrderState.UNKNOWN,
    "calculated": OrderState.UNKNOWN,
}


def map_broker_status(status: str, *, filled_qty: float = 0.0,
                      filled_avg_price: float | None = None) -> OrderState:
    """Translate a broker status string into an OrderState.

    A broker saying "filled" is necessary but not sufficient. If the broker
    claims a fill but reports zero quantity or no average price, we return
    UNKNOWN and reconcile, because those three facts disagreeing means we cannot
    compute a real entry price, slippage, or P&L -- and a trade whose entry price
    we invented is worse than a trade we admit we cannot account for.
    """
    key = str(status).strip().lower()
    mapped = BROKER_STATUS_MAP.get(key)
    if mapped is None:
        log.warning("Unrecognised broker order status %r -> UNKNOWN", status)
        return OrderState.UNKNOWN
    if mapped is OrderState.FILLED and (filled_qty <= 0 or filled_avg_price is None):
        log.error("Broker reports 'filled' but filled_qty=%s avg_price=%s. "
                  "Refusing to record a fill we cannot price.",
                  filled_qty, filled_avg_price)
        return OrderState.UNKNOWN
    if mapped is OrderState.PARTIALLY_FILLED and filled_qty <= 0:
        return OrderState.ACKNOWLEDGED
    return mapped


class DiscrepancyKind(str, Enum):
    """Every way local records and broker state can disagree."""

    UNEXPECTED_BROKER_POSITION = "unexpected_broker_position"
    MISSING_BROKER_POSITION = "missing_broker_position"
    POSITION_QTY_MISMATCH = "position_qty_mismatch"
    UNEXPECTED_BROKER_ORDER = "unexpected_broker_order"
    MISSING_BROKER_ORDER = "missing_broker_order"
    ORDER_STATE_MISMATCH = "order_state_mismatch"
    BROKER_UNREACHABLE = "broker_unreachable"


#: Discrepancies that mean we do not know our own exposure. Any of these blocks
#: new entries. `ORDER_STATE_MISMATCH` is included because a local FILLED
#: against a broker CANCELLED means our position accounting is wrong.
BLOCKING_DISCREPANCIES = {
    DiscrepancyKind.UNEXPECTED_BROKER_POSITION,
    DiscrepancyKind.MISSING_BROKER_POSITION,
    DiscrepancyKind.POSITION_QTY_MISMATCH,
    DiscrepancyKind.UNEXPECTED_BROKER_ORDER,
    DiscrepancyKind.ORDER_STATE_MISMATCH,
    DiscrepancyKind.BROKER_UNREACHABLE,
}


@dataclasses.dataclass(frozen=True)
class Discrepancy:
    kind: DiscrepancyKind
    ticker: str | None
    detail: str
    local: dict | None = None
    broker: dict | None = None

    @property
    def blocks_trading(self) -> bool:
        return self.kind in BLOCKING_DISCREPANCIES

    def as_record(self) -> dict:
        return {
            "kind": self.kind.value, "ticker": self.ticker, "detail": self.detail,
            "local": self.local, "broker": self.broker,
            "blocks_trading": self.blocks_trading,
        }


@dataclasses.dataclass(frozen=True)
class ReconciliationReport:
    checked_at: dt.datetime
    discrepancies: tuple[Discrepancy, ...]
    local_position_count: int
    broker_position_count: int
    local_open_order_count: int
    broker_open_order_count: int

    @property
    def clean(self) -> bool:
        return not self.discrepancies

    @property
    def blocks_trading(self) -> bool:
        return any(d.blocks_trading for d in self.discrepancies)

    @property
    def detail(self) -> str:
        if self.clean:
            return (f"Reconciled clean: {self.broker_position_count} position(s), "
                    f"{self.broker_open_order_count} open order(s) agree with "
                    f"local records.")
        return "; ".join(f"{d.kind.value}({d.ticker}): {d.detail}"
                         for d in self.discrepancies)

    def as_record(self) -> dict:
        return {
            "checked_at": self.checked_at.isoformat(),
            "clean": self.clean,
            "blocks_trading": self.blocks_trading,
            "detail": self.detail,
            "local_position_count": self.local_position_count,
            "broker_position_count": self.broker_position_count,
            "local_open_order_count": self.local_open_order_count,
            "broker_open_order_count": self.broker_open_order_count,
            "discrepancies": [d.as_record() for d in self.discrepancies],
        }

    def blocking_records(self) -> list[dict]:
        return [d.as_record() for d in self.discrepancies if d.blocks_trading]


class OrderLifecycleManager:
    """Owns every state change on an order row.

    Args:
        broker: used for FRESH status queries. State is never derived from a
            cached submit response.
        journal: TradeJournal. Persistence only; this class owns legality.
    """

    def __init__(self, broker: BrokerAdapter, journal):
        self.broker = broker
        self.journal = journal

    # -- transitions -------------------------------------------------------
    def transition(self, order, target: OrderState, reason: str,
                   *, broker_response: dict | None = None,
                   data_timestamp: dt.datetime | None = None, **fields) -> None:
        """Apply a state change, enforcing the transition table FIRST.

        Milestone 1 called `journal.update_order_state()` directly and never
        consulted the table. Every caller now comes through here.
        """
        current = order.state
        if current is target and target in (OrderState.PARTIALLY_FILLED,):
            pass  # repeated partial fills are legal per the table
        assert_transition_allowed(current, target)
        self.journal.update_order_state(order, target, reason, **fields)
        if broker_response is not None or data_timestamp is not None:
            self.journal.record_order_event(
                order.id, current, target, reason,
                broker_response=broker_response, data_timestamp=data_timestamp)

    def mark_risk_approved(self, order, detail: str) -> None:
        self.transition(order, OrderState.RISK_APPROVED, f"risk approved: {detail}")

    def mark_risk_rejected(self, order, rule: str) -> None:
        self.transition(order, OrderState.RISK_REJECTED, f"risk rejected: {rule}")

    def mark_submitted(self, order, receipt) -> None:
        """Record that a submission was ACKNOWLEDGED -- not that it filled.

        Deliberately transitions only as far as SUBMITTED, regardless of what
        the submit response said. Learning the real state is
        `refresh_from_broker`'s job, and it asks the broker again to do it.
        """
        self.transition(
            order, OrderState.SUBMITTED,
            f"submitted to {receipt.broker_environment.value} broker "
            f"(coid={receipt.client_order_id}); broker acknowledgement is not a fill",
            broker_response=receipt.as_record(),
            broker_order_id=receipt.broker_order_id,
        )

    def mark_submission_uncertain(self, order, detail: str) -> None:
        """Submission raised after the request may have left. UNKNOWN is the
        honest state and it is reachable from SUBMITTED."""
        if order.state is OrderState.RISK_APPROVED:
            # We must pass through SUBMITTED to reach UNKNOWN legally, and
            # SUBMITTED is the truthful intermediate: we did attempt it.
            self.transition(order, OrderState.SUBMITTED,
                            f"submission attempted, outcome unknown: {detail}")
        self.transition(order, OrderState.UNKNOWN,
                        f"submission state UNKNOWN: {detail}. Reconcile before "
                        f"any further action on this order.")

    def refresh_from_broker(self, order) -> OrderState:
        """Query the broker and move the local row to whatever the broker says.

        This is the only supported way an order becomes FILLED.
        """
        broker_order_id = getattr(order, "broker_order_id", None)
        if not broker_order_id:
            log.warning("Order %s has no broker_order_id; cannot refresh", order.id)
            if order.state not in (OrderState.UNKNOWN,) and not is_terminal(order.state):
                self.transition(order, OrderState.UNKNOWN,
                                "no broker_order_id recorded, so broker state "
                                "cannot be queried")
            return order.state

        try:
            status = self.broker.get_order_status(broker_order_id)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not refresh order %s from broker: %s", broker_order_id, exc)
            if not is_terminal(order.state) and order.state is not OrderState.UNKNOWN:
                self.transition(order, OrderState.UNKNOWN,
                                f"broker status query failed: {exc}")
            return order.state

        target = map_broker_status(
            status.status, filled_qty=status.filled_qty,
            filled_avg_price=status.filled_avg_price)

        if target is order.state:
            return order.state

        try:
            assert_transition_allowed(order.state, target)
        except Exception as exc:  # noqa: BLE001
            # The broker reports a state our table says we cannot reach from
            # here. The broker is authoritative about the ORDER, but our local
            # row is now provably wrong, so we go to UNKNOWN rather than forcing
            # an illegal transition or silently ignoring the broker.
            log.error("Broker state %s unreachable from local %s for order %s: %s",
                      target.value, order.state.value, broker_order_id, exc)
            if order.state is not OrderState.UNKNOWN:
                self.transition(
                    order, OrderState.UNKNOWN,
                    f"broker reports {status.status!r} -> {target.value}, which is "
                    f"not reachable from local state {order.state.value}. Local "
                    f"record is untrustworthy.",
                    broker_response=status.raw)
            return order.state

        fields = {}
        if status.filled_qty:
            fields["filled_qty"] = status.filled_qty
        if status.filled_avg_price is not None:
            fields["filled_avg_price"] = status.filled_avg_price

        self.transition(order, target,
                        f"broker-confirmed status={status.status!r}",
                        broker_response=status.raw, **fields)
        return target

    # -- reconciliation ----------------------------------------------------
    def reconcile(self, local_open_orders: list, local_positions: dict[str, float],
                  now: dt.datetime | None = None) -> ReconciliationReport:
        """Compare local records against the broker.

        Args:
            local_open_orders: Order rows we believe are still working.
            local_positions: {ticker: signed_qty} we believe we hold.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        discrepancies: list[Discrepancy] = []

        try:
            broker_positions = {p.ticker: float(p.qty) for p in self.broker.get_positions()}
            broker_orders: list[BrokerOrderStatus] = self.broker.get_open_orders()
        except Exception as exc:  # noqa: BLE001
            # Cannot reconcile means cannot know exposure means cannot trade.
            return ReconciliationReport(
                checked_at=now,
                discrepancies=(Discrepancy(
                    kind=DiscrepancyKind.BROKER_UNREACHABLE, ticker=None,
                    detail=(f"broker could not be queried for reconciliation "
                            f"({type(exc).__name__}: {exc}). Exposure is unknown, "
                            f"so new entries must stop."),
                ),),
                local_position_count=len(local_positions), broker_position_count=-1,
                local_open_order_count=len(local_open_orders), broker_open_order_count=-1,
            )

        # positions
        for ticker, broker_qty in broker_positions.items():
            local_qty = local_positions.get(ticker)
            if local_qty is None:
                discrepancies.append(Discrepancy(
                    kind=DiscrepancyKind.UNEXPECTED_BROKER_POSITION, ticker=ticker,
                    detail=(f"broker holds {broker_qty} shares of {ticker} that this "
                            f"system has no record of opening. NOT auto-closing: "
                            f"closing a position of unknown provenance is itself a "
                            f"real trade. Halting new entries for the operator."),
                    local=None, broker={"qty": broker_qty},
                ))
            elif abs(local_qty - broker_qty) > 1e-6:
                discrepancies.append(Discrepancy(
                    kind=DiscrepancyKind.POSITION_QTY_MISMATCH, ticker=ticker,
                    detail=(f"local records show {local_qty} shares, broker shows "
                            f"{broker_qty}. Position sizing and risk limits are "
                            f"computed from the local number, so this must be "
                            f"resolved before sizing anything else."),
                    local={"qty": local_qty}, broker={"qty": broker_qty},
                ))

        for ticker, local_qty in local_positions.items():
            if ticker not in broker_positions and abs(local_qty) > 1e-6:
                discrepancies.append(Discrepancy(
                    kind=DiscrepancyKind.MISSING_BROKER_POSITION, ticker=ticker,
                    detail=(f"local records show {local_qty} shares of {ticker} but "
                            f"the broker reports no such position. Either it was "
                            f"closed outside this system or a fill was recorded that "
                            f"did not happen."),
                    local={"qty": local_qty}, broker=None,
                ))

        # orders
        local_by_broker_id = {
            str(getattr(o, "broker_order_id", "")): o
            for o in local_open_orders if getattr(o, "broker_order_id", None)
        }
        broker_ids = {s.broker_order_id for s in broker_orders}

        for status in broker_orders:
            if status.broker_order_id not in local_by_broker_id:
                discrepancies.append(Discrepancy(
                    kind=DiscrepancyKind.UNEXPECTED_BROKER_ORDER,
                    ticker=status.raw.get("symbol") if status.raw else None,
                    detail=(f"broker has working order {status.broker_order_id} "
                            f"({status.status}) that this system does not track. It "
                            f"could fill at any moment and create exposure the risk "
                            f"engine has not accounted for."),
                    broker={"broker_order_id": status.broker_order_id,
                            "status": status.status},
                ))

        for o in local_open_orders:
            bid = getattr(o, "broker_order_id", None)
            if bid and str(bid) not in broker_ids:
                discrepancies.append(Discrepancy(
                    kind=DiscrepancyKind.MISSING_BROKER_ORDER,
                    ticker=getattr(o, "ticker", None),
                    detail=(f"local order {o.id} (state {o.state.value}) references "
                            f"broker order {bid}, which the broker no longer lists as "
                            f"open. It may have filled, been cancelled, or expired; "
                            f"refresh it individually."),
                    local={"order_id": o.id, "state": o.state.value,
                           "broker_order_id": str(bid)},
                ))

        return ReconciliationReport(
            checked_at=now, discrepancies=tuple(discrepancies),
            local_position_count=len(local_positions),
            broker_position_count=len(broker_positions),
            local_open_order_count=len(local_open_orders),
            broker_open_order_count=len(broker_orders),
        )

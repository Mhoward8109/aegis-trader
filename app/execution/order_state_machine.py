"""
Deterministic order state machine (spec §12).

    PROPOSED -> RISK_APPROVED -> SUBMITTED -> ACKNOWLEDGED
             -> PARTIALLY_FILLED -> FILLED -> EXIT_PENDING -> CLOSED

Side branches: RISK_REJECTED, REJECTED, CANCELLED, EXPIRED, UNKNOWN.

Rules encoded here, not left to convention:
  - "Never assume an order filled simply because it was submitted." ->
    transition into FILLED requires an explicit broker-confirmed fill event;
    there is no direct SUBMITTED -> FILLED transition allowed.
  - "Broker-confirmed state is authoritative." -> UNKNOWN is reachable from
    almost any state, representing "we do not currently trust our own state";
    callers must reconcile before transitioning out of UNKNOWN.
  - Illegal transitions raise instead of silently no-op-ing, so a bug shows
    up immediately instead of producing a corrupted-looking order row.
"""
from __future__ import annotations

from app.common.db import OrderState

_ALLOWED: dict[OrderState, set[OrderState]] = {
    OrderState.PROPOSED: {OrderState.RISK_APPROVED, OrderState.RISK_REJECTED, OrderState.CANCELLED},
    OrderState.RISK_APPROVED: {OrderState.SUBMITTED, OrderState.CANCELLED},
    OrderState.RISK_REJECTED: set(),  # terminal
    OrderState.SUBMITTED: {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.UNKNOWN, OrderState.CANCELLED},
    OrderState.ACKNOWLEDGED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.UNKNOWN,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED,  # additional partial fills
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.UNKNOWN,
    },
    OrderState.FILLED: {OrderState.EXIT_PENDING, OrderState.UNKNOWN},
    OrderState.EXIT_PENDING: {OrderState.CLOSED, OrderState.UNKNOWN},
    OrderState.CLOSED: set(),          # terminal
    OrderState.REJECTED: set(),        # terminal
    OrderState.CANCELLED: set(),       # terminal
    OrderState.EXPIRED: set(),         # terminal
    # UNKNOWN can resolve to any non-terminal state once reconciled; this is
    # the ONE deliberate exception to strict adjacency, because "unknown" by
    # definition means we don't yet know which real state we're in.
    OrderState.UNKNOWN: {
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CLOSED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    },
}

TERMINAL_STATES = {
    OrderState.CLOSED,
    OrderState.REJECTED,
    OrderState.CANCELLED,
    OrderState.EXPIRED,
    OrderState.RISK_REJECTED,
}


class IllegalOrderTransition(Exception):
    pass


def assert_transition_allowed(current: OrderState, target: OrderState) -> None:
    allowed = _ALLOWED.get(current, set())
    if target not in allowed:
        raise IllegalOrderTransition(
            f"Illegal order state transition: {current.value} -> {target.value}. "
            f"Allowed from {current.value}: {sorted(s.value for s in allowed) or 'NONE (terminal state)'}"
        )


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES


class OrderStateMachine:
    """Wraps a single Order row's lifecycle. Every transition is recorded by
    the caller as an OrderEvent — this class only enforces legality, it does
    not persist anything itself (keeps it trivially unit-testable)."""

    def __init__(self, initial: OrderState = OrderState.PROPOSED):
        self.state = initial
        self.history: list[tuple[OrderState, OrderState]] = []

    def transition(self, target: OrderState) -> None:
        assert_transition_allowed(self.state, target)
        self.history.append((self.state, target))
        self.state = target

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)

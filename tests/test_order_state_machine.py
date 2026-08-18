import pytest

from app.common.db import OrderState
from app.execution.order_state_machine import IllegalOrderTransition, OrderStateMachine, is_terminal


def test_happy_path_full_lifecycle():
    sm = OrderStateMachine()
    sm.transition(OrderState.RISK_APPROVED)
    sm.transition(OrderState.SUBMITTED)
    sm.transition(OrderState.ACKNOWLEDGED)
    sm.transition(OrderState.PARTIALLY_FILLED)
    sm.transition(OrderState.FILLED)
    sm.transition(OrderState.EXIT_PENDING)
    sm.transition(OrderState.CLOSED)
    assert sm.state == OrderState.CLOSED
    assert sm.is_terminal


def test_submitted_may_reach_filled_because_acknowledged_may_never_be_observed():
    """SUBMITTED -> FILLED is legal, and that is deliberate.

    This test replaces an earlier one that asserted the opposite. The earlier
    invariant was wrong, and wrong in the unsafe direction: ACKNOWLEDGED is a
    state the BROKER passes through, not one this system can require it to
    report. A market order can fill between submission and the first status
    poll, so the first status ever observed is "filled". Forbidding the edge did
    not prevent anything -- it routed every fast fill to UNKNOWN, and UNKNOWN
    suppresses protective-exit attachment, so the stricter table left real
    positions open with no stop at the broker.

    The invariant that actually matters is enforced elsewhere and tested below:
    a fill state may only be entered from a broker-reported status, never from
    the local belief that submit_order() returned successfully.
    """
    sm = OrderStateMachine()
    sm.transition(OrderState.RISK_APPROVED)
    sm.transition(OrderState.SUBMITTED)
    sm.transition(OrderState.FILLED)
    assert sm.state == OrderState.FILLED


def test_risk_gate_cannot_be_skipped_on_the_way_to_a_broker():
    """The transition that must stay illegal: PROPOSED straight to SUBMITTED.

    Milestone 1 performed exactly this transition, which is how an order could
    reach a broker without the risk engine having approved it.
    """
    for target in (OrderState.SUBMITTED, OrderState.FILLED,
                   OrderState.PARTIALLY_FILLED, OrderState.ACKNOWLEDGED):
        sm = OrderStateMachine()
        assert sm.state == OrderState.PROPOSED
        with pytest.raises(IllegalOrderTransition):
            sm.transition(target)


def test_risk_rejected_is_terminal_so_a_rejection_cannot_be_walked_back():
    sm = OrderStateMachine()
    sm.transition(OrderState.RISK_REJECTED)
    for target in (OrderState.RISK_APPROVED, OrderState.SUBMITTED,
                   OrderState.FILLED):
        with pytest.raises(IllegalOrderTransition):
            sm.transition(target)


def test_terminal_states_have_no_outgoing_transitions():
    sm = OrderStateMachine(initial=OrderState.CLOSED)
    with pytest.raises(IllegalOrderTransition):
        sm.transition(OrderState.SUBMITTED)


def test_unknown_state_reachable_and_resolvable():
    sm = OrderStateMachine()
    sm.transition(OrderState.RISK_APPROVED)
    sm.transition(OrderState.SUBMITTED)
    sm.transition(OrderState.UNKNOWN)
    assert not is_terminal(sm.state)
    sm.transition(OrderState.ACKNOWLEDGED)  # reconciliation resolves it
    assert sm.state == OrderState.ACKNOWLEDGED


def test_risk_rejected_is_terminal():
    sm = OrderStateMachine()
    sm.transition(OrderState.RISK_REJECTED)
    assert sm.is_terminal
    with pytest.raises(IllegalOrderTransition):
        sm.transition(OrderState.SUBMITTED)

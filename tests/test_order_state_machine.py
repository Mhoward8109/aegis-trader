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


def test_cannot_skip_submitted_to_filled_directly():
    sm = OrderStateMachine()
    sm.transition(OrderState.RISK_APPROVED)
    sm.transition(OrderState.SUBMITTED)
    with pytest.raises(IllegalOrderTransition):
        sm.transition(OrderState.FILLED)  # must go through ACKNOWLEDGED first


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

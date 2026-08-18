"""Persistent circuit-breaker and operator-reset safety invariants."""
from __future__ import annotations

import pytest

from app.broker.base import BrokerError, OrderRequest
from app.broker.shadow_adapter import ShadowBroker
from app.risk.persistent_circuit_breaker import (
    BreakerSealError, BreakerTrippedError, BreakerTrigger, PersistentCircuitBreaker,
    issue_operator_reset,
)
from tests.invariant_support import OneTickerProvider


def test_breaker_trip_persists_across_fresh_process_object(tmp_path):
    """A breaker trip must persist across a fresh object, or restarting after a fault could silently resume entries."""
    path = tmp_path / "breaker.db"
    PersistentCircuitBreaker(path).trip(BreakerTrigger.CRITICAL_EXCEPTION, "test crash")
    restarted = PersistentCircuitBreaker(path)
    assert restarted.is_tripped()
    with pytest.raises(BreakerTrippedError):
        restarted.assert_entries_permitted()


def test_tripped_breaker_blocks_entries_but_permits_protective_exits(tmp_path):
    """A tripped breaker must block entries but permit protective exits, or the halt could strand an existing position unprotected."""
    breaker = PersistentCircuitBreaker(tmp_path / "breaker.db")
    breaker.trip(BreakerTrigger.OPERATOR_MANUAL_HALT, "halt for test")
    assert not breaker.permits_entry()
    assert breaker.state().permits_protective_exit()
    broker = ShadowBroker(10_000, lambda ticker: OneTickerProvider().get_quote(ticker))
    status = broker.submit_protective_order(OrderRequest("SAFE", "SELL", 1, "stop", stop_price=99))
    assert status.status == "accepted"


def test_plain_code_cannot_reset_breaker_without_operator_authorization(tmp_path):
    """Strategy, risk, and execution-like code cannot reset with ordinary data, or a trading path could clear its own halt."""
    breaker = PersistentCircuitBreaker(tmp_path / "breaker.db")
    breaker.trip(BreakerTrigger.DAILY_LOSS_LIMIT, "daily loss test")
    with pytest.raises(BreakerSealError):
        breaker.reset({"operator": "strategy"})  # type: ignore[arg-type]
    assert breaker.is_tripped()


def test_reset_requires_explicit_operator_path_and_same_session_opt_in(tmp_path):
    """Reset requires the explicit operator path and same-session opt-in, or a generic restart could erase a current trading halt."""
    breaker = PersistentCircuitBreaker(tmp_path / "breaker.db")
    trip = breaker.trip(BreakerTrigger.RECONCILIATION_FAILURE, "reconciliation test")
    ordinary_reset = issue_operator_reset(operator="operator", reason="Investigated root cause and corrected it.")
    with pytest.raises(BreakerSealError):
        breaker.reset(ordinary_reset, session_date=trip.session_date)
    explicit_reset = issue_operator_reset(
        operator="operator", reason="Investigated root cause and corrected it.", same_session=True
    )
    cleared = breaker.reset(explicit_reset, session_date=trip.session_date)
    assert len(cleared) == 1
    assert not breaker.is_tripped()

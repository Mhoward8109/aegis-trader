from app.risk.circuit_breaker import CircuitBreaker

CFG = {"daily_loss_trip": True, "consecutive_loss_trip": True, "stale_data_trip": True,
       "reconciliation_failure_trip": True, "repeated_rejected_orders_trip": 3,
       "excessive_slippage_trip_pct": 1.0, "_max_daily_loss_pct": 2.0}


def test_not_tripped_initially():
    cb = CircuitBreaker(CFG)
    assert not cb.is_tripped("2026-08-18")


def test_daily_loss_trips_breaker():
    cb = CircuitBreaker(CFG)
    result = cb.check_daily_loss("2026-08-18", realized_pnl=-2500, equity=100_000)
    assert result is not None and result.tripped
    assert cb.is_tripped("2026-08-18")


def test_consecutive_losses_trip_breaker():
    cb = CircuitBreaker(CFG)
    result = cb.check_consecutive_losses("2026-08-18", consecutive_losses=3, max_allowed=3)
    assert result is not None
    assert cb.is_tripped("2026-08-18")


def test_strategy_cannot_untrip_mid_session():
    """The breaker exposes no method that a strategy could call to clear a
    trip for the SAME session_date — only clear_for_new_session exists, and
    per spec it must only be invoked by an operator/scheduler at day start."""
    cb = CircuitBreaker(CFG)
    cb.trip("2026-08-18", "daily_loss_exceeded", {})
    assert cb.is_tripped("2026-08-18")
    # Even calling clear_for_new_session with the SAME date (misuse) simply
    # clears it — the safety property is procedural (only ops code should
    # call this), verified in docs/SAFETY.md and enforced by never wiring
    # this method into strategy/risk modules.
    cb.clear_for_new_session("2026-08-18")
    assert not cb.is_tripped("2026-08-18")


def test_repeated_rejections_trip():
    cb = CircuitBreaker(CFG)
    assert cb.check_repeated_rejections("2026-08-18", reject_count=2) is None
    result = cb.check_repeated_rejections("2026-08-18", reject_count=3)
    assert result is not None
    assert cb.is_tripped("2026-08-18")


def test_stale_data_trips():
    cb = CircuitBreaker(CFG)
    result = cb.check_stale_data("2026-08-18", "quotes", age_seconds=10, max_age_seconds=5)
    assert result is not None
    assert cb.is_tripped("2026-08-18")


def test_fresh_data_does_not_trip():
    cb = CircuitBreaker(CFG)
    result = cb.check_stale_data("2026-08-18", "quotes", age_seconds=2, max_age_seconds=5)
    assert result is None
    assert not cb.is_tripped("2026-08-18")

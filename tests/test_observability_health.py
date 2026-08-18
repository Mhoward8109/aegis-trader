import datetime as dt

from fastapi.testclient import TestClient

from app.broker.base import Quote
from app.common.modes import Mode
from app.dashboard.server import app
from app.marketdata.freshness import FreshnessGate
from app.observability.health import BLOCKED, HEALTHY, UNAVAILABLE, UNKNOWN, build_health_snapshot
from app.risk.persistent_circuit_breaker import BreakerTrigger, PersistentCircuitBreaker


UTC = dt.timezone.utc


def _breaker(tmp_path):
    return PersistentCircuitBreaker(tmp_path / "breaker.sqlite")


def test_snapshot_reports_real_tripped_breaker_reason(tmp_path):
    now = dt.datetime(2026, 8, 18, 15, 30, tzinfo=UTC)
    breaker = _breaker(tmp_path)
    breaker.trip(BreakerTrigger.DAILY_LOSS_LIMIT, "daily loss limit exceeded", now=now)

    snapshot = build_health_snapshot(mode=Mode.SHADOW, circuit_breaker=breaker, now=now)
    record = snapshot.as_record()["circuit_breaker_state"]

    assert record["availability"] == "AVAILABLE"
    assert record["value"]["tripped"] is True
    assert record["value"]["state"] == "TRIPPED"
    assert "daily loss limit exceeded" in record["value"]["reason"]
    assert record["value"]["trip_time"] == now.isoformat()


def test_unavailable_inputs_are_marked_and_never_healthy():
    snapshot = build_health_snapshot(now=dt.datetime(2026, 8, 18, tzinfo=UTC))
    record = snapshot.as_record()

    assert record["mode"]["availability"] == UNKNOWN
    assert record["circuit_breaker_state"]["availability"] == UNKNOWN
    assert record["last_quote_timestamp"]["availability"] == UNAVAILABLE
    assert record["open_positions"]["availability"] == UNAVAILABLE
    assert record["status"] != HEALTHY


def test_status_is_blocked_when_breaker_is_tripped(tmp_path):
    breaker = _breaker(tmp_path)
    breaker.trip(BreakerTrigger.STALE_MARKET_DATA, "required quote feed is stale")

    snapshot = build_health_snapshot(mode=Mode.SHADOW, circuit_breaker=breaker)

    assert snapshot.status == BLOCKED
    assert any("Circuit breaker is TRIPPED" in reason for reason in snapshot.blocking_reasons)


def test_stale_last_quote_is_not_healthy(tmp_path):
    now = dt.datetime(2026, 8, 18, 15, 30, tzinfo=UTC)
    breaker = _breaker(tmp_path)
    freshness = FreshnessGate(now=now)
    freshness.require("quote", now - dt.timedelta(seconds=60))
    quote = Quote("AEGIS", 10.0, 10.1, 10.05, now - dt.timedelta(seconds=60), "test")

    snapshot = build_health_snapshot(
        mode=Mode.SHADOW,
        circuit_breaker=breaker,
        freshness_report=freshness.report(),
        last_quote=quote,
        now=now,
    )

    assert snapshot.status != HEALTHY
    assert snapshot.status == BLOCKED
    assert snapshot.last_quote_age_seconds["value"] == 60.0


def test_health_endpoint_returns_all_required_fields():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    payload = response.json()
    required = {
        "generated_at", "status", "blocking_reasons", "mode", "broker_environment",
        "broker_adapter_enabled", "market_data_provider", "market_data_health",
        "last_quote_timestamp", "last_quote_age_seconds", "last_successful_data_update",
        "reconciliation_state", "circuit_breaker_state", "active_strategies",
        "open_positions", "open_orders", "realized_pnl", "unrealized_pnl",
        "remaining_daily_risk_budget", "latest_candidate_considered",
        "latest_rejection_reason", "latest_execution_event",
    }
    assert required <= payload.keys()

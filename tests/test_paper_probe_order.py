from __future__ import annotations

import datetime as dt
import dataclasses

import pytest

from app.broker.base import AccountSnapshot, BrokerError, BrokerOrderStatus, Position, Quote
from app.common.modes import Mode
from app.execution.authorization import BrokerEnvironment, ExecutionAuthorizer
from app.marketdata.session import CLOSED, SessionState
from app.verification.order_probe import (
    ACKNOWLEDGEMENT_FLAG,
    OrderProbeConfig,
    OrderProbeRefused,
    run_order_probe,
)
from tests.invariant_support import RISK_CFG, journal_for
from tests.probe_support import (
    MarketData,
    NOW,
    RecordingBroker,
    SessionService,
    runtime_for,
)


CONFIG = OrderProbeConfig(
    max_spread_pct=0.5,
    min_liquidity_avg_dollar_vol=5_000_000,
    risk_config=RISK_CFG,
)


def _run(tmp_path, **changes):
    broker = changes.pop("broker", RecordingBroker())
    market = changes.pop("market_data", MarketData())
    runtime_changes = changes.pop("runtime_changes", {})
    runtime = runtime_for(
        tmp_path, broker=broker, market_data=market, **runtime_changes
    )
    return run_order_probe(
        runtime,
        journal=journal_for(tmp_path),
        symbol=changes.pop("symbol", "SPY"),
        qty=changes.pop("qty", 1),
        acknowledged=changes.pop("acknowledged", True),
        configured_mode=changes.pop("configured_mode", Mode.PAPER),
        config_mode_source=changes.pop("config_mode_source", "test"),
        config=changes.pop("config", CONFIG),
        now=changes.pop("now", NOW),
        expected_broker_type=changes.pop("expected_broker_type", RecordingBroker),
        expected_market_data_type=changes.pop("expected_market_data_type", MarketData),
        **changes,
    )


def test_missing_operator_acknowledgement_refuses_before_any_mutation(tmp_path):
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match=ACKNOWLEDGEMENT_FLAG):
        _run(tmp_path, broker=broker, acknowledged=False)
    assert broker.mutations == []


@pytest.mark.parametrize("qty", [0, -1, 1.5, True])
def test_invalid_quantity_refuses(tmp_path, qty):
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match="positive whole number"):
        _run(tmp_path, broker=broker, qty=qty)
    assert broker.mutations == []


@pytest.mark.parametrize("symbol", ["", "../SPY", "SPY;BUY"])
def test_malformed_symbol_refuses(tmp_path, symbol):
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match="symbol is malformed"):
        _run(tmp_path, broker=broker, symbol=symbol)
    assert broker.mutations == []


def test_shadow_or_generic_broker_subclass_is_structurally_rejected(tmp_path):
    class ShadowLike(RecordingBroker):
        pass

    broker = ShadowLike()
    with pytest.raises(OrderProbeRefused, match="exact AlpacaPaperBroker"):
        _run(tmp_path, broker=broker)
    assert broker.mutations == []


def test_live_environment_is_structurally_rejected(tmp_path):
    class LiveBroker(RecordingBroker):
        environment = BrokerEnvironment.LIVE

    broker = LiveBroker()
    with pytest.raises(OrderProbeRefused, match="exact AlpacaPaperBroker"):
        _run(tmp_path, broker=broker, expected_broker_type=LiveBroker)
    assert broker.mutations == []


def test_mock_market_provider_is_structurally_rejected(tmp_path):
    class MockProvider(MarketData):
        pass

    market = MockProvider()
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match="exact AlpacaMarketDataProvider"):
        _run(tmp_path, broker=broker, market_data=market)
    assert broker.mutations == []


def test_tripped_breaker_refuses_before_submission(tmp_path):
    runtime = runtime_for(tmp_path)
    runtime.circuit_breaker.trip_on_critical_exception(RuntimeError("test"), where="test")
    with pytest.raises(OrderProbeRefused, match="breaker is tripped"):
        run_order_probe(
            runtime,
            journal=journal_for(tmp_path),
            symbol="SPY",
            qty=1,
            acknowledged=True,
            configured_mode=Mode.PAPER,
            config_mode_source="test",
            config=CONFIG,
            now=NOW,
            expected_broker_type=RecordingBroker,
            expected_market_data_type=MarketData,
        )
    assert runtime.broker.mutations == []


def test_dirty_reconciliation_refuses_before_submission(tmp_path):
    position = Position("SPY", 1, 500, 500, 0, "long", NOW)
    broker = RecordingBroker(positions=[position])
    with pytest.raises(OrderProbeRefused, match="reconciliation is not clean"):
        _run(tmp_path, broker=broker)
    assert broker.mutations == []


def test_closed_session_refuses_before_submission(tmp_path):
    service = SessionService(SessionState(CLOSED, NOW, is_open=False))
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match="does not permit"):
        _run(tmp_path, broker=broker, runtime_changes={"session_service": service})
    assert broker.mutations == []


@pytest.mark.parametrize("source", ["account", "quote", "bars"])
def test_stale_required_data_refuses_before_submission(tmp_path, source):
    stale = NOW - dt.timedelta(hours=1)
    broker = RecordingBroker()
    market = MarketData()
    if source == "account":
        broker.account.timestamp = stale
    elif source == "quote":
        broker.quote.timestamp = stale
    else:
        market.now = stale
    with pytest.raises(OrderProbeRefused, match="stale or incoherent"):
        _run(tmp_path, broker=broker, market_data=market)
    assert broker.mutations == []


def test_insufficient_buying_power_refuses(tmp_path):
    account = AccountSnapshot(100_000, 1, 1, "USD", NOW)
    broker = RecordingBroker(account=account)
    with pytest.raises(OrderProbeRefused, match="buying power is insufficient"):
        _run(tmp_path, broker=broker)
    assert broker.mutations == []


def test_excessive_spread_refuses(tmp_path):
    quote = Quote("SPY", 490, 500, 495, NOW, "test")
    broker = RecordingBroker(quote=quote)
    with pytest.raises(OrderProbeRefused, match="spread"):
        _run(tmp_path, broker=broker)
    assert broker.mutations == []


def test_insufficient_liquidity_refuses(tmp_path):
    market = MarketData(volume=1)
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match="liquidity"):
        _run(tmp_path, broker=broker, market_data=market)
    assert broker.mutations == []


def test_production_risk_engine_rejection_refuses_before_submission(tmp_path):
    risk_config = {**RISK_CFG, "max_trades_per_day": 0}
    config = dataclasses.replace(CONFIG, risk_config=risk_config)
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match="risk engine rejected probe"):
        _run(tmp_path, broker=broker, config=config)
    assert broker.mutations == []


def test_wrong_configured_mode_refuses_without_submission(tmp_path):
    broker = RecordingBroker()
    with pytest.raises(OrderProbeRefused, match="configured mode is SHADOW"):
        _run(tmp_path, broker=broker, configured_mode=Mode.SHADOW)
    assert broker.mutations == []


def test_wrong_mode_authorizer_refuses_without_submission(tmp_path):
    broker = RecordingBroker()
    with pytest.raises(Exception, match="authorizer built for SHADOW"):
        _run(
            tmp_path,
            broker=broker,
            runtime_changes={"authorizer": ExecutionAuthorizer(target_mode=Mode.SHADOW)},
        )
    assert broker.mutations == []


def test_broker_rejection_is_not_reported_as_success(tmp_path):
    rejected = BrokerOrderStatus("probe-1", "rejected", 0, None, {"symbol": "SPY"})
    broker = RecordingBroker(status_sequence=[rejected])
    with pytest.raises(OrderProbeRefused, match="ended in REJECTED"):
        _run(tmp_path, broker=broker)
    assert [name for name, _ in broker.mutations] == ["submit_order"]


def test_timeout_before_receipt_trips_breaker_and_never_retries(tmp_path):
    broker = RecordingBroker(submit_error=TimeoutError("unknown submission"))
    runtime = runtime_for(tmp_path, broker=broker)
    with pytest.raises(OrderProbeRefused, match="outcome is unknown"):
        run_order_probe(
            runtime,
            journal=journal_for(tmp_path),
            symbol="SPY",
            qty=1,
            acknowledged=True,
            configured_mode=Mode.PAPER,
            config_mode_source="test",
            config=CONFIG,
            now=NOW,
            expected_broker_type=RecordingBroker,
            expected_market_data_type=MarketData,
        )
    assert [name for name, _ in broker.mutations] == ["submit_order"]
    assert not runtime.circuit_breaker.permits_entry()


def test_timeout_after_possible_submission_becomes_unknown_and_trips_breaker(tmp_path):
    broker = RecordingBroker(status_sequence=[TimeoutError("status unavailable")])
    runtime = runtime_for(tmp_path, broker=broker)
    with pytest.raises(OrderProbeRefused, match="status after submission is UNKNOWN"):
        run_order_probe(
            runtime,
            journal=journal_for(tmp_path),
            symbol="SPY",
            qty=1,
            acknowledged=True,
            configured_mode=Mode.PAPER,
            config_mode_source="test",
            config=CONFIG,
            now=NOW,
            expected_broker_type=RecordingBroker,
            expected_market_data_type=MarketData,
        )
    assert [name for name, _ in broker.mutations] == ["submit_order"]
    assert not runtime.circuit_breaker.permits_entry()


def test_partial_fill_cancels_remaining_quantity_and_refuses_success(tmp_path):
    partial = BrokerOrderStatus("probe-1", "partially_filled", 0.5, 500.0, {"symbol": "SPY", "legs": [{}]})
    broker = RecordingBroker(status_sequence=[partial])
    with pytest.raises(OrderProbeRefused, match="partial fill"):
        _run(tmp_path, broker=broker)
    assert [name for name, _ in broker.mutations] == ["submit_order", "cancel_order"]


def test_immediate_fill_requires_broker_confirmed_protective_legs(tmp_path):
    filled = BrokerOrderStatus("probe-1", "filled", 1, 500.0, {"symbol": "SPY"})
    broker = RecordingBroker(status_sequence=[filled])
    with pytest.raises(OrderProbeRefused, match="without broker-confirmed protective"):
        _run(tmp_path, broker=broker)


def test_immediate_fill_with_bracket_legs_passes_and_submits_exactly_once(tmp_path):
    filled = BrokerOrderStatus(
        "probe-1", "filled", 1, 500.0, {"symbol": "SPY", "legs": [{"id": "stop"}, {"id": "target"}]}
    )
    broker = RecordingBroker(status_sequence=[filled])
    report = _run(tmp_path, broker=broker)
    assert report.passed
    assert [name for name, _ in broker.mutations] == ["submit_order"]
    assert len(broker.submissions) == 1
    assert broker.submissions[0].order_type == "bracket"


def test_unfilled_order_is_cancelled_and_confirmed(tmp_path):
    broker = RecordingBroker()
    report = _run(tmp_path, broker=broker)
    assert report.passed
    assert [name for name, _ in broker.mutations] == ["submit_order", "cancel_order"]
    assert "Unfilled order cancelled" in report.render()


def test_submit_receipt_is_never_reported_as_fill(tmp_path):
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus("probe-1", "filled", 1, 500.0, {"legs": [{}, {}]}),
        status_sequence=[BrokerOrderStatus("probe-1", "accepted", 0, None, {"symbol": "SPY"})],
    )
    report = _run(tmp_path, broker=broker)
    assert report.passed
    assert "Unfilled order cancelled" in report.render()


def test_broker_error_during_submit_is_treated_as_uncertain_not_definite_reject(tmp_path):
    broker = RecordingBroker(submit_error=BrokerError("transport failed"))
    runtime = runtime_for(tmp_path, broker=broker)
    with pytest.raises(OrderProbeRefused, match="outcome is unknown"):
        run_order_probe(
            runtime,
            journal=journal_for(tmp_path),
            symbol="SPY",
            qty=1,
            acknowledged=True,
            configured_mode=Mode.PAPER,
            config_mode_source="test",
            config=CONFIG,
            now=NOW,
            expected_broker_type=RecordingBroker,
            expected_market_data_type=MarketData,
        )
    assert len(broker.submissions) == 1

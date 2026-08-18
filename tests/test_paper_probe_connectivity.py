from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.broker.base import BrokerOrderStatus, Position, Quote
from app.marketdata.session import SessionState, UNKNOWN
from app.risk.persistent_circuit_breaker import BreakerTrigger
from app.verification.connectivity import run_connectivity_probe
from tests.probe_support import (
    MarketData,
    NOW,
    RecordingBroker,
    RegimeEngine,
    SecProvider,
    SessionService,
    runtime_for,
)


def test_connectivity_probe_passes_all_required_read_operations(tmp_path):
    runtime = runtime_for(tmp_path)
    report = run_connectivity_probe(runtime, now=NOW)
    assert report.passed
    assert "OVERALL: PASS" in report.render()
    assert runtime.broker.mutations == []


@pytest.mark.parametrize(
    "runtime_change,expected_check",
    [
        (lambda p: {"broker": RecordingBroker(read_error=TimeoutError("broker"))}, "Broker account"),
        (lambda p: {"market_data": MarketData(error=TimeoutError("data"))}, "SPY bars"),
        (lambda p: {"session_service": SessionService(error=TimeoutError("clock"))}, "Market clock/session"),
        (lambda p: {"session_service": SessionService(SessionState(UNKNOWN, None, reason="bad clock"))}, "Market clock/session"),
        (lambda p: {"regime_engine": RegimeEngine(error=TimeoutError("regime"))}, "SPY/QQQ/IWM regime inputs"),
        (lambda p: {"regime_engine": RegimeEngine(unknown=True)}, "SPY/QQQ/IWM regime inputs"),
        (lambda p: {"sec_provider": SecProvider(error=TimeoutError("sec"))}, "SEC EDGAR"),
    ],
)
def test_required_connectivity_failures_fail_overall(
    tmp_path, runtime_change, expected_check
):
    runtime = runtime_for(tmp_path, **runtime_change(tmp_path))
    report = run_connectivity_probe(runtime, now=NOW)
    assert not report.passed
    assert any(c.name == expected_check and c.status.value == "FAIL" for c in report.checks)
    assert runtime.broker.mutations == []


@pytest.mark.parametrize("stale_source", ["account", "quote", "bars"])
def test_stale_or_missing_required_timestamps_fail_closed(tmp_path, stale_source):
    stale = NOW - dt.timedelta(hours=1)
    broker = RecordingBroker()
    market = MarketData()
    if stale_source == "account":
        broker.account.timestamp = stale
    elif stale_source == "quote":
        broker.quote.timestamp = stale
    else:
        market.now = stale
    runtime = runtime_for(tmp_path, broker=broker, market_data=market)
    report = run_connectivity_probe(runtime, now=NOW)
    assert not report.passed
    assert "Required data freshness" in report.render()
    assert broker.mutations == []


@pytest.mark.parametrize(
    "quote",
    [
        Quote("SPY", -1, 500, 500, NOW, "test"),
        Quote("SPY", 501, 500, 500, NOW, "test"),
        Quote("SPY", float("nan"), 500, 500, NOW, "test"),
    ],
)
def test_malformed_quote_fails(tmp_path, quote):
    broker = RecordingBroker(quote=quote)
    report = run_connectivity_probe(runtime_for(tmp_path, broker=broker), now=NOW)
    assert not report.passed
    assert "malformed bid/ask/last" in report.render()
    assert broker.mutations == []


def test_empty_bars_fail(tmp_path):
    market = MarketData()
    market.get_bars = lambda *args: pd.DataFrame()
    report = run_connectivity_probe(runtime_for(tmp_path, market_data=market), now=NOW)
    assert not report.passed
    assert "no bars returned" in report.render()


def test_reconciliation_disagreement_blocks_probe(tmp_path):
    position = Position("SPY", 1, 500, 500, 0, "long", NOW)
    broker = RecordingBroker(positions=[position])
    report = run_connectivity_probe(runtime_for(tmp_path, broker=broker), now=NOW)
    assert not report.passed
    assert "unexpected_broker_position" in report.render()
    assert broker.mutations == []


def test_unexpected_open_order_blocks_probe(tmp_path):
    status = BrokerOrderStatus("outside", "accepted", 0, None, {"symbol": "SPY"})
    broker = RecordingBroker(open_orders=[status])
    report = run_connectivity_probe(runtime_for(tmp_path, broker=broker), now=NOW)
    assert not report.passed
    assert "unexpected_broker_order" in report.render()


def test_tripped_breaker_blocks_probe(tmp_path):
    runtime = runtime_for(tmp_path)
    runtime.circuit_breaker.trip(BreakerTrigger.CRITICAL_EXCEPTION, "test")
    report = run_connectivity_probe(runtime, now=NOW)
    assert not report.passed
    assert "Circuit breaker" in report.render()
    assert "BLOCKED" in report.render()


def test_report_does_not_print_sensitive_account_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_API_KEY_ID", "SUPER-SECRET-KEY")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET_KEY", "SUPER-SECRET-VALUE")
    text = run_connectivity_probe(runtime_for(tmp_path), now=NOW).render()
    assert "SUPER-SECRET" not in text
    assert "100000" not in text


def test_sec_without_source_metadata_fails(tmp_path):
    class NoMetadataSec:
        def research(self, ticker, since):
            return []

    report = run_connectivity_probe(
        runtime_for(tmp_path, sec_provider=NoMetadataSec()), now=NOW
    )
    assert not report.passed
    assert "missing source metadata" in report.render()

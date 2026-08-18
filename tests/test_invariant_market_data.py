"""Market-data integrity and freshness safety invariants."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from types import SimpleNamespace

from app.catalyst.engine import CatalystEngine, NullNewsProvider
from app.common.db import Candidate
from app.common.modes import Mode
from app.execution.authorization import ExecutionAuthorizer
from app.orchestration.pipeline import run_pipeline
from app.risk.engine import RiskEngine
from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
from app.scanner.base import ScanCriteria
from app.strategy.scoring import OpportunityScorer
from tests.invariant_support import AlwaysSetup, NOW, OneTickerProvider, RISK_CFG, TestBroker, WEIGHTS, journal_for


def _run(tmp_path, provider, broker=None, **kwargs):
    journal = journal_for(tmp_path)
    broker = broker or TestBroker()
    result = run_pipeline(
        mode=Mode.PAPER, provider=provider, criteria=ScanCriteria(), strategies=[AlwaysSetup({})],
        scorer=OpportunityScorer(WEIGHTS), catalyst_engine=CatalystEngine([NullNewsProvider()]),
        risk_engine=RiskEngine(RISK_CFG), risk_cfg=RISK_CFG, broker=broker, journal=journal,
        authorizer=ExecutionAuthorizer(target_mode=Mode.PAPER),
        circuit_breaker=PersistentCircuitBreaker(tmp_path / "breaker.db"),
        config_mode=Mode.PAPER, config_mode_source="default.yaml", min_score_to_consider=0,
        session_service=SimpleNamespace(
            current_session=lambda: SimpleNamespace(
                session="REGULAR", reason="", scheduled_close=None, is_unknown=False
            ),
            permits_orders=lambda *_: True,
        ),
        **kwargs,
    )
    return result, broker, journal


def _bars(timestamp):
    return pd.DataFrame([{
        "timestamp": timestamp, "open": 99.5, "high": 100.5, "low": 99,
        "close": 100, "volume": 1_000_000,
    }])


@pytest.mark.parametrize(
    ("name", "provider_factory", "broker_factory"),
    [
        ("stale quote", lambda: OneTickerProvider(quote=__import__("app.broker.base", fromlist=["Quote"]).Quote(
            "SAFE", 99.9, 100.1, 100, NOW - dt.timedelta(minutes=10), "test")), lambda: TestBroker()),
        ("stale bars", lambda: OneTickerProvider(bars=_bars(NOW - dt.timedelta(minutes=10))), lambda: TestBroker()),
        ("missing quote", lambda: OneTickerProvider(quote=None), lambda: TestBroker()),
        ("none quote timestamp", lambda: OneTickerProvider(quote=__import__("app.broker.base", fromlist=["Quote"]).Quote(
            "SAFE", 99.9, 100.1, 100, None, "test")), lambda: TestBroker()),
        ("non datetime quote timestamp", lambda: OneTickerProvider(quote=__import__("app.broker.base", fromlist=["Quote"]).Quote(
            "SAFE", 99.9, 100.1, 100, "not-a-time", "test")), lambda: TestBroker()),
        ("malformed bars", lambda: OneTickerProvider(bars=_bars("not-a-datetime")), lambda: TestBroker()),
    ],
)
def test_unusable_market_data_refuses_submission_and_is_journaled(tmp_path, name, provider_factory, broker_factory):
    """Unusable required market data must refuse and journal the decision, or orders could be placed on unknown inputs."""
    provider, broker = provider_factory(), broker_factory()
    if name == "missing quote":
        provider.quote = None
        broker.get_quote = lambda ticker: None
    result, broker, journal = _run(tmp_path, provider, broker)
    assert not broker.submissions, name
    assert journal.session.query(Candidate).count() >= 1, name
    assert result.outcomes[0].stage_reached in {"not_authorized", "data_incoherent"}, name


@pytest.mark.parametrize("exc", [TimeoutError("feed timeout"), RuntimeError("provider failed")])
def test_provider_failure_or_timeout_fails_closed_and_is_journaled(tmp_path, exc):
    """A provider failure or timeout must refuse and journal the decision, or an outage could become an unrecorded unsafe path."""
    result, broker, journal = _run(tmp_path, OneTickerProvider(bars_error=exc))
    assert not broker.submissions
    assert journal.session.query(Candidate).count() >= 1
    # `market_data_unavailable` is the stage the provider-failure boundary now
    # reports. It is deliberately distinct from `not_authorized`: an operator
    # reading the journal needs to see "the feed failed", not "authorization
    # declined", because those call for completely different remedies.
    assert result.outcomes[0].stage_reached == "market_data_unavailable"
    assert result.outcomes[0].rejection_reason == "provider_failure"


def test_quote_bar_price_scale_disagreement_refuses_and_journals(tmp_path):
    """Quote and bar prices must agree within tolerance, or split-scale data could generate a falsely plausible order."""
    from app.broker.base import Quote

    provider = OneTickerProvider(
        quote=Quote("SAFE", 199.9, 200.1, 200, NOW, "test"),
        bars=_bars(NOW),
    )
    result, broker, journal = _run(tmp_path, provider, quote_bar_tolerance_pct=1.0)
    assert not broker.submissions
    assert result.outcomes[0].stage_reached == "data_incoherent"
    assert journal.session.query(Candidate).count() == 1

"""
End-to-end proof of the spec's first milestone: scanner finds candidates ->
catalyst engine attaches sources -> strategy produces a Setup -> scorer
scores it -> risk engine approves/rejects -> (in SHADOW) ShadowBroker
"fills" it with zero network calls -> TradeJournal records every stage,
including rejections. All offline / deterministic (MockProvider + seeded
strategies) so this runs in CI with no credentials.
"""
import datetime as dt

from app.broker.shadow_adapter import ShadowBroker
from app.catalyst.engine import CatalystEngine, NullNewsProvider
from app.common.db import Candidate, init_db
from app.common.modes import Mode
from app.journal.store import TradeJournal
from app.execution.authorization import ExecutionAuthorizer
from app.orchestration.pipeline import run_pipeline
from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
from app.risk.engine import RiskEngine
from app.scanner.base import ScanCriteria
from app.scanner.mock_provider import MockProvider
from app.strategy.opening_range_breakout import OpeningRangeBreakout
from app.strategy.vwap_reclaim import VwapReclaim
from app.strategy.scoring import OpportunityScorer
from sqlalchemy.orm import Session

RISK_CFG = {
    "max_risk_per_trade_pct": 0.5, "max_risk_per_trade_usd": None, "max_daily_loss_pct": 2.0,
    "max_daily_loss_usd": None, "max_weekly_loss_pct": 5.0, "max_trades_per_day": 10,
    "max_concurrent_positions": 5, "max_position_pct_of_account": 20.0,
    "max_sector_exposure_pct": 40.0, "max_spread_pct": 1.0, "min_liquidity_avg_dollar_vol": 1_000_000,
    "max_slippage_pct": 1.0, "max_consecutive_losses": 3,
}
WEIGHTS = {
    "catalyst_quality": 15, "catalyst_freshness": 10, "relative_volume": 15, "liquidity": 10,
    "spread_quality": 5, "technical_alignment": 15, "market_trend": 10, "reward_risk": 10,
    "data_confidence": 5, "historical_strategy_performance": 5,
}


def make_journal(tmp_path):
    engine = init_db(str(tmp_path / "journal.db"))
    return TradeJournal(Session(engine))


def quote_source_factory(mock_provider: MockProvider):
    def _q(ticker):
        from app.broker.base import Quote
        rows = mock_provider.scan(ScanCriteria())
        row = next((r for r in rows if r.ticker == ticker), None)
        price = row.fields["price"] if row else 100.0
        return Quote(ticker=ticker, bid=price * 0.999, ask=price * 1.001, last=price,
                     timestamp=dt.datetime.now(dt.timezone.utc), source="mock")
    return _q


def test_shadow_mode_pipeline_runs_end_to_end_and_journals_everything(tmp_path):
    journal = make_journal(tmp_path)
    provider = MockProvider(seed=7)
    broker = ShadowBroker(starting_equity=100_000, quote_source=quote_source_factory(provider))
    risk_engine = RiskEngine(RISK_CFG)
    scorer = OpportunityScorer(WEIGHTS)
    catalyst_engine = CatalystEngine([NullNewsProvider()])
    strategies = [OpeningRangeBreakout({"min_rvol": 0.5, "max_spread_pct": 5.0}),
                  VwapReclaim({"max_spread_pct": 5.0})]
    criteria = ScanCriteria(price_min=1, price_max=500, rvol_min=0.5, dollar_volume_min=100_000)

    result = run_pipeline(
        mode=Mode.SHADOW, provider=provider, criteria=criteria, strategies=strategies,
        scorer=scorer, catalyst_engine=catalyst_engine, risk_engine=risk_engine, risk_cfg=RISK_CFG,
        broker=broker, journal=journal, min_score_to_consider=0.0,
        authorizer=ExecutionAuthorizer(target_mode=Mode.SHADOW),
        circuit_breaker=PersistentCircuitBreaker(tmp_path / "breaker.db"),
        config_mode=Mode.SHADOW, config_mode_source="default.yaml",
    )

    # 0. The run was not halted by a system-level gate, and no candidate died of
    #    an unhandled exception. Asserted FIRST and loudly, because an earlier
    #    version of this test passed while all 10 candidates were crashing in
    #    the freshness gate: every later assertion is satisfied by an empty or
    #    all-errored result set. A test that cannot distinguish "nothing traded
    #    because the gates said no" from "nothing traded because the code threw"
    #    is not evidence of anything.
    assert not result.halted, result.halt_reason
    errored = [o for o in result.outcomes if o.stage_reached == "error"]
    assert not errored, (
        "candidates raised unhandled exceptions: "
        + "; ".join(f"{o.ticker}: {o.rejection_reason} {o.detail}" for o in errored)
    )

    # 0b. Every outcome must be one of the stages this pipeline can legitimately
    #     reach. "error" is excluded above; anything unrecognised means a stage
    #     label was introduced without this test being told about it.
    #
    #     Honest note about what this fixture does and does not prove: with
    #     MockProvider(seed=7) the random walk trends DOWN, so neither
    #     OpeningRangeBreakout nor VwapReclaim ever triggers and every candidate
    #     legitimately ends at "strategy_no_setup". This test therefore proves
    #     the scan/catalyst/strategy/journal path and the absence of crashes -- it
    #     does NOT prove the submission path. That is proved separately by
    #     test_pipeline_submission.py, which uses a fixture engineered to trigger.
    legitimate_stages = {
        "scored", "scored_research_only", "strategy_no_setup",
        "risk_rejected", "not_authorized", "submitted", "submission_unknown",
    }
    stages_reached = {o.stage_reached for o in result.outcomes}
    assert stages_reached <= legitimate_stages, (
        f"unrecognised stage(s): {stages_reached - legitimate_stages}"
    )

    # 1. The scanner actually found candidates.
    assert result.candidates_scanned > 0

    # 2. Every candidate reached SOME recorded stage (nothing silently dropped).
    assert len(result.outcomes) == result.candidates_scanned
    stages = {o.stage_reached for o in result.outcomes}
    assert stages  # non-empty

    # 3. Every scored candidate (reached "scored" or later) was journaled as
    #    a Candidate row with a non-opaque score breakdown (spec: "Do NOT
    #    allow the AI to produce an unexplained number").
    scored_or_later = [o for o in result.outcomes if o.score is not None]
    candidates_in_db = journal.session.query(Candidate).all()
    assert len(candidates_in_db) == len(scored_or_later)
    for c in candidates_in_db:
        assert c.score_breakdown_json  # never empty/opaque

    # 4. SHADOW mode must never have touched a real broker: ShadowBroker has
    #    no network client at all (see test_shadow_broker.py), and every
    #    order actually submitted went through it.
    if result.orders_submitted > 0:
        assert len(broker.get_trade_history()) == result.orders_submitted

    # 5. Rejected candidates are recorded too (spec: "Rejected trades are
    #    important training information") - decision must not be silently
    #    left PENDING for anything risk touched.
    for c in candidates_in_db:
        assert c.decision in ("APPROVED", "REJECTED")


def test_research_mode_never_submits_orders_even_with_zero_score_threshold(tmp_path):
    journal = make_journal(tmp_path)
    provider = MockProvider(seed=7)
    broker = ShadowBroker(starting_equity=100_000, quote_source=quote_source_factory(provider))
    risk_engine = RiskEngine(RISK_CFG)
    scorer = OpportunityScorer(WEIGHTS)
    catalyst_engine = CatalystEngine([NullNewsProvider()])
    strategies = [OpeningRangeBreakout({"min_rvol": 0.5, "max_spread_pct": 5.0}),
                  VwapReclaim({"max_spread_pct": 5.0})]
    criteria = ScanCriteria(price_min=1, price_max=500, rvol_min=0.5, dollar_volume_min=100_000)

    result = run_pipeline(
        mode=Mode.RESEARCH, provider=provider, criteria=criteria, strategies=strategies,
        scorer=scorer, catalyst_engine=catalyst_engine, risk_engine=risk_engine, risk_cfg=RISK_CFG,
        broker=broker, journal=journal, min_score_to_consider=0.0,
        authorizer=ExecutionAuthorizer(target_mode=Mode.RESEARCH),
        circuit_breaker=PersistentCircuitBreaker(tmp_path / "breaker.db"),
        config_mode=Mode.RESEARCH, config_mode_source="default.yaml",
    )
    assert result.orders_submitted == 0
    assert len(broker.get_trade_history()) == 0

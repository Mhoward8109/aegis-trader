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
from app.marketdata.regime import build_regime_snapshot
from app.orchestration.pipeline import run_pipeline
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
    regime = build_regime_snapshot(spy_pct=0.5, qqq_pct=0.4, iwm_pct=0.3, vix_level=14,
                                     breadth=1.2, spy_range_pct=2.0, as_of="test")
    criteria = ScanCriteria(price_min=1, price_max=500, rvol_min=0.5, dollar_volume_min=100_000)

    result = run_pipeline(
        mode=Mode.SHADOW, provider=provider, criteria=criteria, strategies=strategies,
        scorer=scorer, catalyst_engine=catalyst_engine, risk_engine=risk_engine, risk_cfg=RISK_CFG,
        broker=broker, journal=journal, regime=regime, min_score_to_consider=0.0,
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
    regime = build_regime_snapshot(spy_pct=0.5, qqq_pct=0.4, iwm_pct=0.3, vix_level=14,
                                     breadth=1.2, spy_range_pct=2.0, as_of="test")
    criteria = ScanCriteria(price_min=1, price_max=500, rvol_min=0.5, dollar_volume_min=100_000)

    result = run_pipeline(
        mode=Mode.RESEARCH, provider=provider, criteria=criteria, strategies=strategies,
        scorer=scorer, catalyst_engine=catalyst_engine, risk_engine=risk_engine, risk_cfg=RISK_CFG,
        broker=broker, journal=journal, regime=regime, min_score_to_consider=0.0,
    )
    assert result.orders_submitted == 0
    assert len(broker.get_trade_history()) == 0

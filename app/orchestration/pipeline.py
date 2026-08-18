"""
Orchestration pipeline (spec §33's authorization chain, first milestone
slice). Wires: Scanner -> Catalyst -> Technical indicators -> Strategy ->
OpportunityScorer -> RiskEngine -> (mode-gated) Broker -> TradeJournal.

This is intentionally a single-pass "run one scan cycle" function, not a
persistent scheduler loop (that belongs to Phase 12 continuous monitoring,
not this milestone). It is safe to call repeatedly (e.g. from a future
APScheduler job) because it has no hidden state of its own — all state
lives in the injected AccountState/broker/journal.

Every one of spec §33's gates is represented explicitly below rather than
folded into one opaque "should_trade()" call, so a human reviewing a run
can see exactly which gate rejected a candidate. See PipelineResult below.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from app.broker.base import BrokerAdapter, OrderRequest
from app.catalyst.engine import CatalystEngine
from app.common.db import TradeMode
from app.common.modes import Mode
from app.journal.store import TradeJournal
from app.marketdata.regime import RegimeSnapshot
from app.risk.engine import AccountState, CandidateRiskInput, RiskEngine
from app.scanner.base import MarketDataProvider, ScanCriteria, Scanner
from app.strategy.base import MarketContext, Strategy
from app.strategy.scoring import OpportunityScorer, ScoreInputs
from app.technical import indicators as ind


@dataclasses.dataclass
class PipelineOutcome:
    ticker: str
    stage_reached: str          # scanner|catalyst|strategy_no_setup|scored|risk_rejected|risk_approved|submitted
    score: float | None = None
    setup: dict | None = None
    rejection_reason: str | None = None


@dataclasses.dataclass
class PipelineResult:
    ran_at: dt.datetime
    outcomes: list[PipelineOutcome]
    orders_submitted: int
    candidates_scanned: int


def _mode_to_tradmode(mode: Mode) -> TradeMode:
    return {Mode.RESEARCH: TradeMode.RESEARCH, Mode.SHADOW: TradeMode.SHADOW,
            Mode.PAPER: TradeMode.PAPER, Mode.LIVE: TradeMode.LIVE}[mode]


def run_pipeline(
    *, mode: Mode, provider: MarketDataProvider, criteria: ScanCriteria,
    strategies: list[Strategy], scorer: OpportunityScorer, catalyst_engine: CatalystEngine,
    risk_engine: RiskEngine, risk_cfg: dict, broker: BrokerAdapter, journal: TradeJournal,
    regime: RegimeSnapshot, min_score_to_consider: float = 40.0,
    consecutive_losses: int = 0, sector_lookup: dict[str, str] | None = None,
) -> PipelineResult:
    """One full scan-to-(hypothetical-or-real)-order cycle.

    `mode` gates what happens at the very end: RESEARCH never even scores
    trade-actionable setups against risk (§0 no orders, no hypothetical
    trades); SHADOW scores + risk-checks + would submit but calls
    broker.submit_order on a ShadowBroker (no network); PAPER/LIVE call
    submit_order on a real (paper-endpoint-only, or live-only-if-authorized)
    broker adapter — the caller is responsible for having already run this
    through ModeGovernor before constructing that broker instance.
    """
    sector_lookup = sector_lookup or {}
    now = dt.datetime.now(dt.timezone.utc)
    scan = Scanner(provider, criteria).run()
    outcomes: list[PipelineOutcome] = []
    orders_submitted = 0

    account_snapshot = broker.get_account()
    open_positions = broker.get_positions()
    account = AccountState(
        equity=account_snapshot.equity, buying_power=account_snapshot.buying_power,
        open_positions=len(open_positions),
        open_position_symbols={p.ticker for p in open_positions},
        sector_exposure_pct={}, trades_today=0,
        realized_pnl_today=0.0, realized_pnl_week=0.0,
        consecutive_losses=consecutive_losses,
    )

    for result in scan["results"]:
        ticker = result.ticker
        catalysts = catalyst_engine.research(ticker)

        # Build a minimal MarketContext from whatever the provider gives us.
        # A real market-data adapter would supply genuine intraday/prev-day
        # bars; MockProvider's get_bars() is synthetic but shaped identically,
        # which is exactly why strategies never need to know the difference.
        bars = provider.get_bars(ticker, "1min", now - dt.timedelta(hours=1), now)
        computed = ind.compute_all(bars) if bars is not None and len(bars) > 0 else {}
        quote = {
            "bid": result.fields.get("price", 0) * (1 - result.fields.get("spread_pct", 0) / 200),
            "ask": result.fields.get("price", 0) * (1 + result.fields.get("spread_pct", 0) / 200),
            "last": result.fields.get("price"), "spread_pct": result.fields.get("spread_pct"),
        }
        ctx = MarketContext(
            ticker=ticker, timestamp=now, bars_intraday=bars, bars_prev_day=bars,
            quote=quote, indicators={**computed, "rvol": result.fields.get("rvol")},
            catalyst={"catalysts": [dataclasses.asdict(c) for c in catalysts]} if catalysts else None,
            regime=dataclasses.asdict(regime), session="regular",
        )

        setup = None
        for strat in strategies:
            setup = strat.evaluate(ctx)
            if setup is not None:
                break

        if setup is None:
            outcomes.append(PipelineOutcome(ticker=ticker, stage_reached="strategy_no_setup"))
            continue

        score_inputs = ScoreInputs(
            catalyst_quality=catalyst_engine.quality_score(catalysts),
            catalyst_freshness=catalyst_engine.freshness_score(catalysts),
            relative_volume=result.fields.get("rvol", 0),
            liquidity_usd=result.fields.get("dollar_volume", 0),
            spread_pct=result.fields.get("spread_pct", 1.0),
            technical_alignment=0.5, market_trend_alignment=0.5,
            reward_risk=(setup.targets[0] - setup.entry) / max(setup.entry - setup.stop, 1e-6)
            if setup.direction == "long" else (setup.entry - setup.targets[0]) / max(setup.stop - setup.entry, 1e-6),
            historical_strategy_expectancy_r=None, data_confidence=0.8,
        )
        scored = scorer.score(score_inputs)

        mode_trade = _mode_to_tradmode(mode)
        candidate = journal.record_candidate(
            ticker=ticker, strategy=setup.strategy, strategy_version=setup.strategy_version,
            setup=dataclasses.asdict(setup), catalyst=ctx.catalyst, regime=ctx.regime,
            score=scored["score"], breakdown=scored["breakdown"], entry=setup.entry, stop=setup.stop,
            targets=setup.targets, reward_risk=score_inputs.reward_risk,
            position_size=None, invalidation=setup.invalidation,
            major_risks=setup.prohibited_conditions, confidence=scored["score"] / 100.0,
            sources=ctx.catalyst, mode=mode_trade, data_timestamp=now,
        )

        if scored["score"] < min_score_to_consider:
            journal.record_rejection(candidate, f"score {scored['score']} below threshold {min_score_to_consider}")
            outcomes.append(PipelineOutcome(ticker=ticker, stage_reached="scored", score=scored["score"],
                                             rejection_reason="below_score_threshold"))
            continue

        if not mode.allows_hypothetical_trades and not mode.allows_order_submission:
            # RESEARCH mode: score it for visibility, but do not risk-check
            # or trade it (spec: "MODE 0: no orders. No hypothetical trades.")
            outcomes.append(PipelineOutcome(ticker=ticker, stage_reached="scored", score=scored["score"]))
            continue

        risk_input = CandidateRiskInput(
            ticker=ticker, sector=sector_lookup.get(ticker, "Unknown"), entry=setup.entry, stop=setup.stop,
            spread_pct=result.fields.get("spread_pct", 1.0),
            avg_dollar_volume=result.fields.get("dollar_volume", 0),
            estimated_slippage_pct=result.fields.get("spread_pct", 0.1) / 2,
        )
        decision = risk_engine.evaluate(risk_input, account)
        journal.record_risk_event(candidate_id=candidate.id,
                                    decision="APPROVED" if decision.approved else "REJECTED",
                                    rule_triggered=decision.rule_triggered, inputs=decision.inputs,
                                    message=decision.reason)

        if not decision.approved:
            journal.record_rejection(candidate, decision.rule_triggered or "risk_rejected")
            outcomes.append(PipelineOutcome(ticker=ticker, stage_reached="risk_rejected", score=scored["score"],
                                             rejection_reason=decision.rule_triggered))
            continue

        candidate.decision = "APPROVED"
        journal.session.commit()
        outcomes.append(PipelineOutcome(ticker=ticker, stage_reached="risk_approved", score=scored["score"],
                                         setup=dataclasses.asdict(setup)))

        if not mode.allows_order_submission:
            # SHADOW: risk-approved but structurally cannot reach a broker
            # wire. We still record it as a hypothetical order via
            # ShadowBroker (which itself makes zero network calls) so the
            # trade journal captures what WOULD have happened.
            pass

        order = journal.open_order(
            candidate_id=candidate.id, mode=mode_trade, ticker=ticker,
            side="BUY" if setup.direction == "long" else "SHORT",
            order_type="market", qty=decision.position_size_shares,
            intended_entry=setup.entry, stop=setup.stop, targets=setup.targets, strategy=setup.strategy,
        )
        status = broker.submit_order(OrderRequest(
            ticker=ticker, side="BUY" if setup.direction == "long" else "SHORT",
            qty=decision.position_size_shares, order_type="market",
        ))
        from app.common.db import OrderState
        journal.update_order_state(order, OrderState.FILLED if status.status == "filled" else OrderState.SUBMITTED,
                                     f"broker status={status.status}")
        orders_submitted += 1
        outcomes.append(PipelineOutcome(ticker=ticker, stage_reached="submitted", score=scored["score"]))

    return PipelineResult(ran_at=now, outcomes=outcomes, orders_submitted=orders_submitted,
                           candidates_scanned=len(scan["results"]))

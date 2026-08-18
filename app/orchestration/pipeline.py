"""
Orchestration pipeline — the authorization chain, wired for real.

    Scanner -> Catalyst -> Technicals -> Strategy -> Scorer
            -> RiskEngine -> ExecutionAuthorizer -> ExecutionEngine
            -> BrokerAdapter -> OrderLifecycleManager -> Journal

WHAT CHANGED FROM MILESTONE 1 AND WHY
-------------------------------------
The Milestone 1 version of this file was where the system's documented safety
properties went to die. Concretely (docs/AUDIT_MILESTONE2.md §3):

1. The SHADOW guard was literally `pass`, then execution fell through to
   `broker.submit_order()` unconditionally in every mode. RESEARCH was protected
   only by an earlier `continue`; SHADOW was protected only by the caller having
   happened to pass a ShadowBroker.
2. Four `AccountState` fields were hard-coded literals, so four risk limits
   could never fire. See app/risk/account_state_builder.py.
3. `bars_prev_day=bars` passed intraday bars as previous-day bars, so any
   strategy comparing today to yesterday was comparing today to itself.
4. Fills were inferred from `status.status == "filled"` on the submit response.
5. Four safety subsystems — freshness, circuit breaker, order state machine,
   event calendar — had zero call sites anywhere in the codebase.
6. Both `risk_approved` and `submitted` outcomes were appended for one candidate,
   so the run summary double-counted.

Every gate below is a separate, named, journaled step. The ordering is not
cosmetic: system-level halts are evaluated ONCE before the candidate loop, so a
tripped breaker or a failed reconciliation cannot be re-litigated per ticker.

FAIL-CLOSED IS THE DEFAULT EVERYWHERE
-------------------------------------
Any gate that cannot determine its answer refuses. `AuthorizationEvidence` uses
a not-supplied sentinel rather than boolean defaults, so a field this function
forgets to populate produces a FAILED check, not a skipped one.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from app.broker.base import BrokerAdapter, OrderRequest
from app.catalyst.engine import CatalystEngine
from app.common.db import OrderState, TradeMode
from app.common.modes import Mode
from app.execution.authorization import (
    AuthorizationEvidence,
    ExecutionAuthorizer,
    ExecutionIntent,
    ExecutionNotAuthorizedError,
)
from app.execution.engine import ExecutionEngine, SubmissionUncertainError
from app.execution.exits import ProtectionPlan, ProtectiveExitManager
from app.execution.lifecycle import OrderLifecycleManager
from app.journal.store import TradeJournal
from app.marketdata.freshness import FreshnessGate
from app.risk.account_state_builder import build_account_state
from app.risk.engine import CandidateRiskInput, RiskEngine
from app.risk.persistent_circuit_breaker import BreakerTrigger
from app.risk.shortability import ShortabilityGate
from app.scanner.base import MarketDataProvider, ScanCriteria, Scanner
from app.strategy.base import MarketContext, Strategy
from app.strategy.scoring import OpportunityScorer, ScoreInputs
from app.technical import indicators as ind

log = logging.getLogger("aegis.pipeline")


@dataclasses.dataclass
class PipelineOutcome:
    """One terminal result per candidate. Exactly one is appended per ticker."""

    ticker: str
    stage_reached: str
    score: float | None = None
    setup: dict | None = None
    rejection_reason: str | None = None
    gate: str | None = None
    detail: str | None = None

    def as_record(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class PipelineResult:
    ran_at: dt.datetime
    outcomes: list[PipelineOutcome]
    orders_submitted: int
    candidates_scanned: int
    halted: bool = False
    halt_reason: str | None = None
    system_gates: dict | None = None

    @property
    def rejections_by_gate(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            if o.gate:
                counts[o.gate] = counts.get(o.gate, 0) + 1
        return counts

    def as_record(self) -> dict:
        return {
            "ran_at": self.ran_at.isoformat(),
            "halted": self.halted, "halt_reason": self.halt_reason,
            "candidates_scanned": self.candidates_scanned,
            "orders_submitted": self.orders_submitted,
            "rejections_by_gate": self.rejections_by_gate,
            "system_gates": self.system_gates,
            "outcomes": [o.as_record() for o in self.outcomes],
        }


def _mode_to_trademode(mode: Mode) -> TradeMode:
    return {Mode.RESEARCH: TradeMode.RESEARCH, Mode.SHADOW: TradeMode.SHADOW,
            Mode.PAPER: TradeMode.PAPER, Mode.LIVE: TradeMode.LIVE}[mode]


def run_pipeline(
    *,
    mode: Mode,
    provider: MarketDataProvider,
    criteria: ScanCriteria,
    strategies: list[Strategy],
    scorer: OpportunityScorer,
    catalyst_engine: CatalystEngine,
    risk_engine: RiskEngine,
    risk_cfg: dict,
    broker: BrokerAdapter,
    journal: TradeJournal,
    # -- Milestone 2 required collaborators -------------------------------
    authorizer: ExecutionAuthorizer,
    circuit_breaker,
    config_mode: Mode,
    config_mode_source: str | None,
    live_config_from_permitted_source: bool = False,
    operator_live_flag_present: bool = False,
    regime_engine=None,
    session_service=None,
    allowed_sessions: tuple[str, ...] = ("REGULAR",),
    # Maximum tolerated disagreement between the latest quote price and the
    # latest bar close, as a percent of the bar close. This is a data-integrity
    # threshold, not a strategy tunable: exceeding it means entry and stop would
    # come from inconsistent price sources.
    quote_bar_tolerance_pct: float = 5.0,
    freshness_max_ages: dict[str, float] | None = None,
    event_guard=None,
    min_score_to_consider: float = 40.0,
    sector_lookup: dict[str, str] | None = None,
    long_only: bool = True,
    require_easy_to_borrow: bool = True,
    allow_local_synthetic_stops: bool = False,
) -> PipelineResult:
    """Run one scan-to-order cycle with every gate enforced.

    Requires an `authorizer` and a `circuit_breaker`. They have no defaults on
    purpose: a caller that has not supplied them cannot form a valid call, so
    "forgot to wire the safety subsystem" is a TypeError at the call site rather
    than a silently unprotected run. This is the same reasoning that put the
    `grant` argument on `submit_order()`.
    """
    now = dt.datetime.now(dt.timezone.utc)
    sector_lookup = sector_lookup or {}
    trade_mode = _mode_to_trademode(mode)
    session_date = now.date().isoformat()
    outcomes: list[PipelineOutcome] = []
    orders_submitted = 0
    system_gates: dict = {}

    def halt(reason: str) -> PipelineResult:
        log.critical("PIPELINE HALTED: %s", reason)
        return PipelineResult(ran_at=now, outcomes=outcomes, orders_submitted=0,
                              candidates_scanned=0, halted=True,
                              halt_reason=reason, system_gates=system_gates)

    # =====================================================================
    # SYSTEM-LEVEL GATES — evaluated ONCE, before any candidate is considered.
    #
    # Per-ticker evaluation would be wrong as well as wasteful: a tripped
    # breaker is a property of the system, and re-asking per ticker invites a
    # future refactor to "skip this ticker" instead of "stop trading".
    # =====================================================================

    # --- Gate S1: circuit breaker ---------------------------------------
    breaker_state = circuit_breaker.state(now=now)
    system_gates["circuit_breaker"] = breaker_state.as_record()
    if not breaker_state.permits_entry():
        return halt(f"circuit breaker is TRIPPED: {breaker_state.reason}. "
                    f"No new entries. Protective exits remain permitted. An "
                    f"explicit operator reset is required.")

    # --- Gate S2: market session ----------------------------------------
    session_state = None
    session_permits_orders = None
    session_detail = ""
    if session_service is not None:
        try:
            session_state = session_service.current_session()
            session_permits_orders = session_service.permits_orders(
                session_state, allowed_sessions)
            session_detail = (f"session={session_state.session} "
                              f"allowed={sorted(allowed_sessions)} "
                              f"{session_state.reason or ''}".strip())
            if session_state.is_unknown:
                session_permits_orders = False
                session_detail = (f"market session is UNKNOWN "
                                  f"({session_state.reason}). An unknown session must "
                                  f"not permit orders: a holiday or a halt looks "
                                  f"exactly like a quiet day from price data alone.")
        except Exception as exc:  # noqa: BLE001
            session_permits_orders = False
            session_detail = (f"market session could not be determined ({exc}). "
                              f"Failing closed.")
        system_gates["session"] = {
            "permits_orders": session_permits_orders, "detail": session_detail,
            "state": getattr(session_state, "session", None),
            "scheduled_close": (session_state.scheduled_close.isoformat()
                                if getattr(session_state, "scheduled_close", None)
                                else None),
        }
    elif mode.allows_order_submission:
        # No session service in an order-submitting mode is a wiring failure, not
        # an optional feature. Milestone 1 passed session="regular" as a literal.
        return halt("no market session service was supplied, so the pipeline "
                    "cannot know whether the market is open. Refusing to submit "
                    "orders on the assumption that every weekday is a normal "
                    "trading session.")
    elif mode is Mode.SHADOW:
        # SHADOW's whole purpose is to generate and journal hypothetical trades
        # offline, including outside market hours. Permitting them here is safe
        # for a structural reason, not a trusting one: Mode.SHADOW requires a
        # broker adapter reporting BrokerEnvironment.SHADOW, and ShadowBroker
        # holds no network client at all (proved by
        # test_shadow_broker_has_no_http_client_attribute). So this branch cannot
        # widen what reaches a real venue. It is recorded, not silent.
        session_permits_orders = True
        session_detail = ("no session service supplied; SHADOW mode permits "
                          "hypothetical trades at any hour because they reach "
                          "only the networkless ShadowBroker. NOT a real fill.")
    else:
        session_permits_orders = False
        session_detail = ("no session service supplied; RESEARCH mode submits "
                          "nothing, so this is recorded rather than fatal")

    # --- Gate S3: economic event guard ----------------------------------
    if event_guard is not None:
        try:
            blackout = event_guard.current_blackout(now=now)
        except Exception as exc:  # noqa: BLE001
            return halt(f"economic event guard could not be evaluated ({exc}). "
                        f"Refusing to trade through a possible FOMC/CPI window.")
        system_gates["event_guard"] = (
            blackout.as_record() if hasattr(blackout, "as_record")
            else {"blocked": bool(blackout)})
        if blackout is not None and getattr(blackout, "blocks_new_entries", False):
            return halt(f"economic event blackout active: {blackout.detail}")

    # --- Gate S4: broker connectivity -----------------------------------
    broker_connected = False
    try:
        broker_connected = bool(broker.is_connected()) if hasattr(
            broker, "is_connected") else True
    except Exception as exc:  # noqa: BLE001
        broker_connected = False
        log.error("broker connectivity check raised: %s", exc)
    system_gates["broker"] = {"connected": broker_connected,
                              "environment": getattr(broker.environment, "value", None),
                              "adapter": type(broker).__name__}
    if mode.allows_order_submission and not broker_connected:
        circuit_breaker.check_broker_connected(
            connected=False, detail="pre-run connectivity check failed",
            session_date=session_date)
        return halt("broker is not reachable; no orders can be submitted or "
                    "confirmed, so trading stops rather than firing blind.")

    # --- Gate S5: reconciliation ----------------------------------------
    lifecycle = OrderLifecycleManager(broker, journal)
    reconciliation = None
    if mode.allows_order_submission:
        local_open = journal.open_orders()
        local_positions = _local_positions(journal)
        reconciliation = lifecycle.reconcile(local_open, local_positions, now=now)
        system_gates["reconciliation"] = reconciliation.as_record()
        if reconciliation.blocks_trading:
            circuit_breaker.check_reconciliation(
                discrepancies=reconciliation.blocking_records(),
                session_date=session_date)
            return halt(f"reconciliation failed: {reconciliation.detail}. New "
                        f"entries are blocked until the operator resolves the "
                        f"discrepancy. Unexpected positions are deliberately NOT "
                        f"auto-closed.")

    # --- Gate S6: account state -----------------------------------------
    account_build = build_account_state(broker=broker, journal=journal,
                                        mode=trade_mode,
                                        sector_lookup=sector_lookup, now=now)
    system_gates["account_state"] = account_build.as_record()
    # This pre-loop snapshot is used ONLY for the system-level gate record above.
    # It is deliberately NOT the snapshot the risk engine sees: the candidate loop
    # rebuilds account state per candidate so that positions opened earlier in
    # this same run are counted. See the comment at that rebuild.
    account_state_valid = account_build.complete
    account_state_detail = account_build.detail

    # --- Gate S7: market regime -----------------------------------------
    regime_record: dict | None = None
    if regime_engine is not None:
        try:
            regime_snapshot = regime_engine.build(now=now)
            regime_record = (regime_snapshot.as_record()
                             if hasattr(regime_snapshot, "as_record")
                             else dataclasses.asdict(regime_snapshot))
        except Exception as exc:  # noqa: BLE001
            regime_record = {"regime": "UNKNOWN",
                             "detail": f"regime engine failed: {exc}"}
    else:
        regime_record = {"regime": "UNKNOWN",
                         "detail": "no regime engine supplied; recorded as UNKNOWN "
                                   "rather than assumed neutral"}
    system_gates["regime"] = regime_record

    # --- collaborators for the candidate loop ---------------------------
    execution_engine = ExecutionEngine(
        broker,
        on_submission_uncertain=lambda exc, client_order_id, intent: (
            circuit_breaker.trip_on_critical_exception(
                exc,
                where=(f"order submission for {intent.ticker} "
                       f"(coid={client_order_id}); submission state is UNKNOWN so "
                       f"trading halts rather than retrying, because a retry "
                       f"could open a double position"),
                session_date=session_date)
        ),
    )
    exit_manager = ProtectiveExitManager(
        execution_engine, broker,
        allow_local_synthetic_fallback=allow_local_synthetic_stops)
    shortability = ShortabilityGate(broker, long_only=long_only,
                                    require_easy_to_borrow=require_easy_to_borrow)

    # =====================================================================
    # CANDIDATE LOOP
    # =====================================================================
    scan = Scanner(provider, criteria).run()
    results = scan["results"]

    for result in results:
        ticker = result.ticker
        try:
            # ----------------------------------------------------------------
            # REBUILD account state for EVERY candidate.
            #
            # Milestone 1 (and the first Milestone 2 draft) built this once
            # before the loop and reused it for all candidates. Every risk limit
            # that COUNTS something -- max_concurrent_positions, max_trades_per_day,
            # buying power, sector exposure, daily loss -- was therefore evaluated
            # against a snapshot taken before any of this run's orders existed.
            #
            # Observed effect: a single scan submitted 10 positions with
            # max_concurrent_positions set to 5, because candidate #10 was still
            # being told that zero positions were open. Each order was
            # individually within limits and the portfolio was not.
            #
            # Rebuilt from the BROKER each time, not incremented locally, because
            # broker-confirmed state is authoritative and a local counter would
            # drift the moment a fill was partial or an order was rejected.
            # ----------------------------------------------------------------
            account_build = build_account_state(broker=broker, journal=journal,
                                                mode=trade_mode,
                                                sector_lookup=sector_lookup,
                                                now=now)
            account = account_build.state
            account_state_valid = account_build.complete
            account_state_detail = account_build.detail

            outcome, submitted = _process_candidate(
                result=result, ticker=ticker, mode=mode, trade_mode=trade_mode,
                now=now, provider=provider, strategies=strategies, scorer=scorer,
                catalyst_engine=catalyst_engine, risk_engine=risk_engine,
                account=account, journal=journal, authorizer=authorizer,
                execution_engine=execution_engine, lifecycle=lifecycle,
                exit_manager=exit_manager, shortability=shortability,
                freshness_max_ages=freshness_max_ages,
                circuit_breaker=circuit_breaker, session_date=session_date,
                quote_bar_tolerance_pct=quote_bar_tolerance_pct,
                broker=broker, regime_record=regime_record,
                session_state=session_state,
                session_permits_orders=session_permits_orders,
                session_detail=session_detail,
                account_state_valid=account_state_valid,
                account_state_detail=account_state_detail,
                broker_connected=broker_connected,
                config_mode=config_mode, config_mode_source=config_mode_source,
                live_config_from_permitted_source=live_config_from_permitted_source,
                operator_live_flag_present=operator_live_flag_present,
                min_score_to_consider=min_score_to_consider,
                sector_lookup=sector_lookup,
            )
        except SubmissionUncertainError as exc:
            # The breaker was already tripped by the engine's callback. Stop the
            # whole run: we do not know our exposure.
            outcomes.append(PipelineOutcome(
                ticker=ticker, stage_reached="submission_unknown",
                gate="submission_state", rejection_reason="submission_state_unknown",
                detail=str(exc)))
            return halt(f"submission state for {ticker} is UNKNOWN: {exc}")
        except Exception as exc:  # noqa: BLE001
            # One bad ticker must not silently abort the run, but it also must
            # not vanish. It is journaled and the loop continues.
            log.exception("candidate %s raised", ticker)
            outcomes.append(PipelineOutcome(
                ticker=ticker, stage_reached="error", gate="unhandled_exception",
                rejection_reason=type(exc).__name__, detail=str(exc)))
            continue

        outcomes.append(outcome)
        orders_submitted += 1 if submitted else 0

    return PipelineResult(ran_at=now, outcomes=outcomes,
                          orders_submitted=orders_submitted,
                          candidates_scanned=len(results),
                          system_gates=system_gates)


# ---------------------------------------------------------------------------
def _process_candidate(*, result, ticker, mode, trade_mode, now, provider,
                       strategies, scorer, catalyst_engine, risk_engine, account,
                       journal, authorizer, execution_engine, lifecycle,
                       exit_manager, shortability, freshness_max_ages, circuit_breaker,
                       session_date, quote_bar_tolerance_pct,
                       broker, regime_record, session_state, session_permits_orders,
                       session_detail, account_state_valid, account_state_detail,
                       broker_connected, config_mode, config_mode_source,
                       live_config_from_permitted_source, operator_live_flag_present,
                       min_score_to_consider, sector_lookup
                       ) -> tuple[PipelineOutcome, bool]:
    """Evaluate one ticker. Returns (outcome, submitted). Exactly one outcome."""

    catalyst_result = catalyst_engine.research(ticker)
    catalysts = getattr(catalyst_result, "catalysts", catalyst_result) or []

    # --- market data ----------------------------------------------------
    # bars_prev_day is fetched SEPARATELY. Milestone 1 passed `bars_prev_day=bars`,
    # handing intraday bars to strategies expecting yesterday's session — so any
    # gap or prior-range comparison silently compared today against itself.
    #
    # The market-data fetch is wrapped because a provider timeout or HTTP error
    # previously propagated out of here into the broad per-candidate exception
    # handler. That handler kept the run alive, which looks like the right
    # outcome, but it produced NO market-data refusal record: the candidate was
    # logged as a generic error rather than as "refused because data was
    # unusable". A feed that is timing out is exactly the condition the freshness
    # design exists to catch, and it was the one condition that bypassed it.
    try:
        bars = provider.get_bars(ticker, "1min", now - dt.timedelta(hours=8), now)
        bars_prev = _fetch_prev_day_bars(provider, ticker, now)
    except Exception as exc:  # noqa: BLE001 - any provider failure is a data fault
        detail = (f"market-data provider failed for {ticker}: "
                  f"{type(exc).__name__}: {exc}")
        log.error("%s — refusing this candidate; no order can be built without "
                  "bars.", detail)
        if mode.allows_order_submission:
            # A provider that is erroring is a property of the feed, not of the
            # symbol, so the breaker is tripped rather than merely skipping to the
            # next ticker and failing identically on every one of them.
            circuit_breaker.trip(
                trigger=BreakerTrigger.STALE_MARKET_DATA,
                detail=detail,
                context={"ticker": ticker, "exception": type(exc).__name__},
                session_date=session_date,
            )
        _journal_data_fault(journal, ticker, mode, now, detail,
                            {"provider_error": type(exc).__name__,
                             "message": str(exc)})
        return PipelineOutcome(
            ticker=ticker, stage_reached="market_data_unavailable",
            gate="market_data", rejection_reason="provider_failure",
            detail=detail), False

    computed = ind.compute_all(bars) if bars is not None and len(bars) > 0 else {}

    quote_obj = _get_quote(provider, broker, ticker)
    quote = _quote_to_dict(quote_obj, result)

    # --- Gate C1: freshness (hard gate) ---------------------------------
    #
    # A NEW gate per candidate. FreshnessGate accumulates the sources registered
    # on it, so reusing one instance across the loop would carry an earlier
    # ticker's stale quote into every later ticker's report — one bad symbol
    # would block the whole scan, and the recorded reason would name the wrong
    # ticker.
    freshness_gate = FreshnessGate(max_ages=freshness_max_ages, now=now)
    freshness_gate.require("quote", getattr(quote_obj, "timestamp", None))
    freshness_gate.require("bars", _last_bar_timestamp(bars))
    account_snapshot = _safe_account(broker)
    if mode.allows_order_submission:
        freshness_gate.require("account_snapshot",
                               getattr(account_snapshot, "timestamp", None))
    else:
        freshness_gate.observe("account_snapshot",
                               getattr(account_snapshot, "timestamp", None))
    freshness_report = freshness_gate.report()

    # --- Gate C1b: quote/bar coherence (hard gate) -----------------------
    #
    # Freshness proves the data is RECENT. It does not prove the quote and the
    # bar series describe the same instrument at the same scale. Integration
    # testing produced setups where entry came from a quote near 230 and the stop
    # from bars near 38 -- both perfectly fresh, both internally consistent, and
    # jointly meaningless. Every downstream gate passed and the position was
    # sized to 2 shares and submitted.
    #
    # Real-world causes are not exotic: an unadjusted split in one feed, a
    # crossed/erroneous print, a symbol change, or two providers disagreeing.
    # Checked here, before indicators reach a strategy, because after that point
    # the mismatch is laundered into a plausible-looking Setup.
    coherence = _quote_bar_coherence(quote_obj, bars,
                                     tolerance_pct=quote_bar_tolerance_pct)
    if not coherence["coherent"]:
        detail = coherence["detail"]
        if mode.allows_order_submission:
            # Tripped, not skipped. Two feeds disagreeing about price is a
            # property of the DATA PLUMBING, not of this symbol, so moving to the
            # next ticker would keep trading on the same broken inputs.
            circuit_breaker.trip(
                trigger=BreakerTrigger.CORRUPTED_STATE,
                detail=f"{ticker}: {detail}",
                context=coherence,
                session_date=session_date,
            )
        _journal_data_fault(journal, ticker, mode, now, detail, coherence)
        return PipelineOutcome(
            ticker=ticker, stage_reached="data_incoherent", gate="data_coherence",
            rejection_reason="quote_bar_incoherent", detail=detail), False

    if not freshness_report.all_required_fresh:
        if mode.allows_order_submission:
            # Trip the breaker, not just skip the ticker. Stale data is a
            # property of the feed, not of the symbol, so continuing to the next
            # ticker would keep trading on the same broken feed. The trip is what
            # actually stops the order: the authorization evidence built below
            # re-reads the breaker, so a tripped breaker fails authorization.
            circuit_breaker.check_freshness_report(freshness_report,
                                                   session_date=session_date)
        else:
            # SHADOW. Previously this branch did not exist, so the freshness gate
            # applied ONLY to order-submitting modes and a SHADOW run recorded
            # hypothetical trades from stale data without comment. That is not a
            # money-safety bug, but it is a data-integrity bug with real
            # consequences: SHADOW output is the evidence base used to decide
            # whether a strategy deserves promotion (PART 21), and a hypothetical
            # fill priced off a stale quote is indistinguishable in the journal
            # from one priced off a good quote. Poisoned evidence is worse than
            # absent evidence, because it survives into a promotion decision.
            #
            # The persistent breaker is deliberately NOT tripped here: SHADOW
            # places nothing at a broker, so there is no exposure to arrest, and
            # letting an offline run latch a persistent breaker would block later
            # real runs over a fault that risked nothing.
            stale_detail = ("stale required data in a hypothetical-trade mode: "
                            + str(freshness_report.detail))
            _journal_data_fault(
                journal, ticker, mode, now, stale_detail,
                {"stale_required_sources":
                     [str(getattr(s, "name", s))
                      for s in freshness_report.stale_required_sources]})
            return PipelineOutcome(
                # No score is reported: this return happens BEFORE scoring, and
                # emitting a score here would mean inventing one.
                ticker=ticker, stage_reached="stale_data", gate="freshness",
                rejection_reason="stale_required_data",
                detail=stale_detail), False

    ctx = MarketContext(
        ticker=ticker, timestamp=now, bars_intraday=bars, bars_prev_day=bars_prev,
        quote=quote,
        indicators={**computed, "rvol": result.fields.get("rvol")},
        catalyst=_catalyst_context(catalysts, catalyst_result),
        regime=regime_record,
        session=(getattr(session_state, "session", None) or "UNKNOWN"),
    )

    # --- strategy -------------------------------------------------------
    setup = None
    for strat in strategies:
        setup = strat.evaluate(ctx)
        if setup is not None:
            break
    if setup is None:
        return PipelineOutcome(ticker=ticker, stage_reached="strategy_no_setup",
                               gate="strategy"), False

    score_inputs = _build_score_inputs(setup, result, catalyst_engine, catalysts,
                                       freshness_report)
    scored = scorer.score(score_inputs)

    candidate = journal.record_candidate(
        ticker=ticker, strategy=setup.strategy, strategy_version=setup.strategy_version,
        setup=dataclasses.asdict(setup), catalyst=ctx.catalyst, regime=ctx.regime,
        score=scored["score"], breakdown=scored["breakdown"], entry=setup.entry,
        stop=setup.stop, targets=setup.targets, reward_risk=score_inputs.reward_risk,
        position_size=None, invalidation=setup.invalidation,
        major_risks=setup.prohibited_conditions,
        confidence=scored["score"] / 100.0, sources=ctx.catalyst, mode=trade_mode,
        data_timestamp=now,
    )

    if scored["score"] < min_score_to_consider:
        reason = (f"score {scored['score']} below threshold {min_score_to_consider}")
        journal.record_rejection(candidate, reason)
        return PipelineOutcome(ticker=ticker, stage_reached="scored",
                               score=scored["score"], gate="score_threshold",
                               rejection_reason="below_score_threshold",
                               detail=reason), False

    if not mode.allows_hypothetical_trades and not mode.allows_order_submission:
        # RESEARCH: score for visibility, then stop. No risk check, no order,
        # not even a hypothetical one.
        return PipelineOutcome(ticker=ticker, stage_reached="scored_research_only",
                               score=scored["score"], gate="mode_research",
                               detail="RESEARCH mode: no orders and no "
                                      "hypothetical trades"), False

    # --- Gate C2: risk engine -------------------------------------------
    risk_input = CandidateRiskInput(
        ticker=ticker, sector=sector_lookup.get(ticker, "Unknown"),
        entry=setup.entry, stop=setup.stop,
        spread_pct=_spread_pct(quote, result),
        avg_dollar_volume=result.fields.get("dollar_volume", 0),
        estimated_slippage_pct=_spread_pct(quote, result) / 2,
        direction=setup.direction,
    )
    decision = risk_engine.evaluate(risk_input, account)
    journal.record_risk_event(
        candidate_id=candidate.id,
        decision="APPROVED" if decision.approved else "REJECTED",
        rule_triggered=decision.rule_triggered, inputs=decision.inputs,
        message=decision.reason)

    order = journal.open_order(
        candidate_id=candidate.id, mode=trade_mode, ticker=ticker,
        side="BUY" if setup.direction == "long" else "SHORT",
        order_type="bracket" if setup.targets else "market",
        qty=decision.position_size_shares, intended_entry=setup.entry,
        stop=setup.stop, targets=setup.targets, strategy=setup.strategy,
    )

    if not decision.approved:
        lifecycle.mark_risk_rejected(order, decision.rule_triggered or "risk_rejected")
        journal.record_rejection(candidate, decision.rule_triggered or "risk_rejected")
        return PipelineOutcome(ticker=ticker, stage_reached="risk_rejected",
                               score=scored["score"], gate="risk_engine",
                               rejection_reason=decision.rule_triggered,
                               detail=decision.reason), False

    lifecycle.mark_risk_approved(order, decision.reason)
    candidate.decision = "APPROVED"
    journal.session.commit()

    # --- Gate C3: shortability ------------------------------------------
    short_verdict = shortability.verify(ticker, setup.direction, now=now)

    # --- Gate C4: execution authorization -------------------------------
    intent = ExecutionIntent(
        mode=mode, ticker=ticker,
        side="BUY" if setup.direction == "long" else "SHORT",
        qty=decision.position_size_shares,
        order_type="bracket" if setup.targets else "market",
    )
    evidence = AuthorizationEvidence(
        config_mode=config_mode,
        config_mode_source=config_mode_source,
        live_config_from_permitted_source=live_config_from_permitted_source,
        operator_live_flag_present=operator_live_flag_present,
        risk_approved=decision.approved,
        risk_detail=decision.reason,
        data_fresh=freshness_report.all_required_fresh,
        freshness_detail=freshness_report.detail,
        broker_environment=broker.environment,
        broker_connected=broker_connected,
        account_state_valid=account_state_valid,
        account_state_detail=account_state_detail,
        # Re-read the breaker from disk here rather than reusing the pre-loop
        # state. A protective exit or another process may have tripped it since
        # the run began, and an authorization decision must reflect the breaker
        # as it is at the moment of authorization.
        circuit_breaker_tripped=not circuit_breaker.state(now=now).permits_entry(),
        circuit_breaker_detail=circuit_breaker.state(now=now).reason,
        session_permits_orders=bool(session_permits_orders),
        session_detail=session_detail,
        short_sale_verified=(True if short_verdict.permits_short else
                             (None if short_verdict.is_data_failure else False)),
        short_sale_detail=short_verdict.reason,
    )
    auth = authorizer.evaluate(intent, evidence)
    journal.record_order_event(order.id, order.state, order.state,
                               reason=f"authorization: {auth.first_failure_reason or 'all checks passed'}",
                               broker_response=auth.as_record(),
                               data_timestamp=now)

    if not auth.authorized:
        journal.record_rejection(candidate,
                                 auth.first_failure_reason or "not_authorized")
        lifecycle.transition(order, OrderState.CANCELLED,
                             f"execution not authorized: {auth.first_failure_reason}")
        return PipelineOutcome(ticker=ticker, stage_reached="not_authorized",
                               score=scored["score"], gate="execution_authorization",
                               rejection_reason=auth.first_failure_reason,
                               detail="; ".join(f"{c.name}: {c.detail}"
                                                for c in auth.failed)), False

    grant = auth.grant

    # --- protective plan ------------------------------------------------
    plan = ProtectionPlan(
        ticker=ticker, direction=setup.direction, qty=decision.position_size_shares,
        entry=setup.entry, stop=setup.stop, targets=tuple(setup.targets or ()),
        close_at_session_end=True,
    )

    # --- submit ---------------------------------------------------------
    if plan.targets:
        req = exit_manager.build_bracket_entry(plan)
    else:
        # No target means no bracket is possible, so protection has to be
        # attached in a second step and is therefore NOT atomic with the entry.
        # Recorded explicitly so nobody reads this path as equivalent.
        log.warning("%s has no profit target, so entry and stop cannot be "
                    "submitted atomically as a bracket. Protection will be "
                    "attached separately and verified.", ticker)
        req = OrderRequest(ticker=ticker, side=intent.side,
                           qty=decision.position_size_shares, order_type="market")

    try:
        receipt = execution_engine.submit(req, grant, intent)
    except ExecutionNotAuthorizedError as exc:
        lifecycle.transition(order, OrderState.CANCELLED,
                             f"execution engine refused: {exc}")
        return PipelineOutcome(ticker=ticker, stage_reached="not_authorized",
                               score=scored["score"], gate="execution_engine",
                               rejection_reason="engine_refused",
                               detail=str(exc)), False
    except SubmissionUncertainError as exc:
        lifecycle.mark_submission_uncertain(order, str(exc))
        raise

    lifecycle.mark_submitted(order, receipt)

    # --- broker-confirmed state, NOT the submit response ----------------
    confirmed_state = lifecycle.refresh_from_broker(order)

    # --- verify protection actually exists at the broker ----------------
    protection = None
    if confirmed_state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
        protection = exit_manager.ensure_protected_or_flatten(plan, now=now)
        journal.record_order_event(
            order.id, confirmed_state, confirmed_state,
            reason=f"protection: {protection.state.value} — {protection.detail}",
            broker_response=protection.as_record(), data_timestamp=now)

    detail = (f"broker-confirmed state={confirmed_state.value}; "
              f"protection={protection.state.value if protection else 'n/a'}")
    return PipelineOutcome(ticker=ticker, stage_reached="submitted",
                           score=scored["score"],
                           setup=dataclasses.asdict(setup), detail=detail), True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _local_positions(journal) -> dict[str, float]:
    """Positions this system believes it holds, from filled-not-closed orders."""
    positions: dict[str, float] = {}
    from app.common.db import Order as OrderModel
    rows = (journal.session.query(OrderModel)
            .filter(OrderModel.state.in_((OrderState.FILLED,
                                          OrderState.PARTIALLY_FILLED,
                                          OrderState.EXIT_PENDING)))
            .all())
    for o in rows:
        signed = o.qty if o.side in ("BUY", "COVER") else -o.qty
        positions[o.ticker] = positions.get(o.ticker, 0.0) + signed
    return {k: v for k, v in positions.items() if abs(v) > 1e-9}


def _fetch_prev_day_bars(provider, ticker, now):
    """Previous session's bars, fetched as their own request.

    Returns None on failure rather than falling back to intraday bars. A
    strategy receiving None can decline; a strategy receiving today's bars
    labelled as yesterday's cannot tell that it has been lied to.
    """
    try:
        start = now - dt.timedelta(days=5)
        return provider.get_bars(ticker, "1day", start, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("previous-day bars unavailable for %s: %s", ticker, exc)
        return None


def _get_quote(provider, broker, ticker):
    for source in (provider, broker):
        getter = getattr(source, "get_quote", None)
        if getter is None:
            continue
        try:
            return getter(ticker)
        except Exception as exc:  # noqa: BLE001
            log.warning("quote from %s failed for %s: %s",
                        type(source).__name__, ticker, exc)
    return None


def _safe_account(broker):
    try:
        return broker.get_account()
    except Exception:  # noqa: BLE001
        return None


def _quote_to_dict(quote_obj, result) -> dict:
    """Build the quote dict strategies read.

    Milestone 1 SYNTHESISED bid and ask from a scanner price and a spread
    percentage. Those were not quotes; they were arithmetic dressed as market
    data, and no freshness check could have caught it because they carried no
    timestamp. When a real quote is unavailable now, bid/ask are None and the
    freshness gate rejects, rather than a plausible number being invented.
    """
    if quote_obj is not None:
        return {
            "bid": quote_obj.bid, "ask": quote_obj.ask, "last": quote_obj.last,
            "timestamp": getattr(quote_obj, "timestamp", None),
            "source": getattr(quote_obj, "source", None),
            "spread_pct": (((quote_obj.ask - quote_obj.bid) / quote_obj.ask * 100.0)
                           if quote_obj.ask else None),
        }
    return {"bid": None, "ask": None, "last": result.fields.get("price"),
            "timestamp": None, "source": "unavailable",
            "spread_pct": result.fields.get("spread_pct")}


def _spread_pct(quote: dict, result) -> float:
    for value in (quote.get("spread_pct"), result.fields.get("spread_pct")):
        if value is not None:
            return float(value)
    # An unknown spread must not read as a tight one, or the max-spread limit
    # becomes a no-op on exactly the illiquid names it exists to exclude.
    return float("inf")


def _last_bar_timestamp(bars):
    """The timestamp of the most recent bar, or None if it cannot be determined.

    Deliberately picky about where it reads the timestamp from. The first version
    of this helper used `bars.index[-1]`, which on a DataFrame with a default
    RangeIndex returns the integer row number. That is not a timestamp, and
    feeding it to the freshness gate raised on `.tzinfo` for every candidate.
    Returning None here is the correct fallback because the gate treats a missing
    timestamp as stale, so an unreadable bar series blocks trading instead of
    passing an integer off as a time.
    """
    if bars is None or len(bars) == 0:
        return None

    def _coerce(value):
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        return value if isinstance(value, dt.datetime) else None

    try:
        # 1. an explicit timestamp column (what MockProvider and the Alpaca
        #    provider both produce)
        columns = getattr(bars, "columns", None)
        if columns is not None and "timestamp" in columns:
            got = _coerce(bars["timestamp"].iloc[-1])
            if got is not None:
                return got
        # 2. a genuine DatetimeIndex
        index = getattr(bars, "index", None)
        if index is not None and hasattr(index, "tz") or (
                index is not None and getattr(index, "inferred_type", None) == "datetime64"):
            got = _coerce(index[-1])
            if got is not None:
                return got
        # 3. a plain list of mappings
        row = bars[-1] if isinstance(bars, (list, tuple)) else None
        if isinstance(row, dict):
            return _coerce(row.get("timestamp"))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read a bar timestamp: %s", exc)
    return None


def _catalyst_context(catalysts, catalyst_result) -> dict | None:
    if not catalysts:
        return {"catalysts": [], "no_verified_catalyst": True,
                "reason": getattr(catalyst_result, "reason",
                                  "no verified catalyst found")}
    return {"catalysts": [dataclasses.asdict(c) if dataclasses.is_dataclass(c) else c
                          for c in catalysts],
            "no_verified_catalyst": False}


def _build_score_inputs(setup, result, catalyst_engine, catalysts,
                        freshness_report) -> ScoreInputs:
    if setup.direction == "long":
        reward_risk = ((setup.targets[0] - setup.entry)
                       / max(setup.entry - setup.stop, 1e-6)) if setup.targets else 0.0
    else:
        reward_risk = ((setup.entry - setup.targets[0])
                       / max(setup.stop - setup.entry, 1e-6)) if setup.targets else 0.0
    return ScoreInputs(
        catalyst_quality=catalyst_engine.quality_score(catalysts),
        catalyst_freshness=catalyst_engine.freshness_score(catalysts),
        relative_volume=result.fields.get("rvol", 0),
        liquidity_usd=result.fields.get("dollar_volume", 0),
        spread_pct=result.fields.get("spread_pct", 1.0),
        technical_alignment=0.5, market_trend_alignment=0.5,
        reward_risk=reward_risk, historical_strategy_expectancy_r=None,
        # Data confidence is now DERIVED from the freshness report rather than
        # being the hard-coded 0.8 Milestone 1 used. A stale-data run should not
        # score as confidently as a fresh one.
        data_confidence=1.0 if freshness_report.all_required_fresh else 0.3,
    )


def _quote_bar_coherence(quote_obj, bars, *, tolerance_pct: float) -> dict:
    """Check that the latest quote and the latest bar close agree on price scale.

    Returns a dict rather than raising so the caller can journal the numbers that
    produced the verdict. `coherent` is True when the check PASSES or when there
    is not enough data to judge -- an absent quote or absent bars is already the
    freshness gate's job, and failing here too would report a misleading reason.
    """
    last_price = None
    for attr in ("last", "last_price", "mid", "ask", "bid"):
        value = getattr(quote_obj, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            last_price = float(value)
            break

    bar_close = None
    if bars is not None and len(bars) > 0:
        try:
            bar_close = float(bars["close"].iloc[-1])
        except Exception:  # noqa: BLE001 - shape varies by provider
            try:
                bar_close = float(bars[-1]["close"])
            except Exception:  # noqa: BLE001
                bar_close = None

    if last_price is None or bar_close is None or bar_close <= 0:
        return {"coherent": True, "detail": "insufficient data to judge coherence",
                "quote_price": last_price, "bar_close": bar_close,
                "deviation_pct": None, "tolerance_pct": tolerance_pct}

    deviation_pct = abs(last_price - bar_close) / bar_close * 100.0
    coherent = deviation_pct <= tolerance_pct
    return {
        "coherent": coherent,
        "quote_price": last_price,
        "bar_close": bar_close,
        "deviation_pct": round(deviation_pct, 4),
        "tolerance_pct": tolerance_pct,
        "detail": (
            f"quote/bar price disagreement: quote={last_price:.4f} vs last bar "
            f"close={bar_close:.4f} ({deviation_pct:.2f}% apart, tolerance "
            f"{tolerance_pct:.2f}%). Entry and stop would be derived from "
            f"inconsistent price data; refusing to build a setup."
        ) if not coherent else "coherent",
    }


def _journal_data_fault(journal, ticker, mode, now, detail, context) -> None:
    """Record a data-integrity refusal.

    A refusal that is not written down is indistinguishable from a symbol that
    simply had no setup, and the spec is explicit that rejected setups are
    training information.
    """
    journal.record_data_fault(ticker=ticker, mode=_mode_to_trademode(mode),
                              detected_at=now, detail=detail, context=context)

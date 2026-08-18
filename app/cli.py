"""
Aegis Trader CLI — the only supported entry point into the system.

    python -m app.cli status
    python -m app.cli demo-scan
    python -m app.cli run --mode research
    python -m app.cli run --mode shadow
    python -m app.cli run --mode paper
    python -m app.cli breaker status
    python -m app.cli breaker reset --operator NAME --reason "..."
    python -m app.cli dashboard

MODE SAFETY
-----------
This file is the ONE place that constructs a BrokerAdapter. It is not, however,
the thing that makes execution safe: `ExecutionAuthorizer` is, and it is
enforced inside the broker adapters themselves, so bypassing this CLI does not
bypass authorization. See app/execution/authorization.py and
tests/test_invariant_authorization.py.

`run --mode live` refuses to execute in this milestone. That refusal is
deliberate and is not a missing feature -- see PART 3 of the milestone brief and
docs/SAFETY.md. AlpacaLiveBroker additionally refuses to construct at all.

NO SILENT FALLBACK
------------------
If a PAPER dependency (credentials, broker, market data, clock) cannot be
constructed, this CLI EXITS. It never substitutes MockProvider, ShadowBroker, a
neutral regime, or a live endpoint for a missing paper dependency. A run that
prints trades from synthetic data while claiming to be PAPER is the single most
dangerous failure mode available to this program.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.common.modes import (
    InvalidModeError,
    LiveTradingNotAuthorizedError,
    Mode,
    ModeGovernor,
)
from app.config.loader import LOCAL_CONFIG_PATH, load_config

# Default universe for PAPER. Alpaca's snapshot endpoint is a symbol lookup, not
# a market-wide scanner, so a universe must be supplied explicitly rather than
# pretending the provider can scan "the market".
DEFAULT_PAPER_UNIVERSE = (
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "META", "GOOGL",
    "SMCI", "PLTR", "COIN", "MARA", "SOFI", "RIVN", "INTC", "MU",
)


def _breaker_db_path(cfg) -> Path:
    return Path(cfg.get("logging.dir") or "data") / "circuit_breaker.db"


def _journal_db_path(cfg) -> Path:
    return Path(cfg.get("logging.dir") or "data") / "journal.db"


# ---------------------------------------------------------------------------
def cmd_status(args):
    cfg = load_config(args.config)
    print("=" * 68)
    print(f" AEGIS TRADER — MODE: {cfg.mode.display_banner}")
    print("=" * 68)
    print(f"Config sources: {cfg.source_files}")
    print(f"Mode declared by: {cfg.mode_source or '(not declared; using default)'}")
    print(f"Risk: max_risk_per_trade_pct={cfg.get('risk.max_risk_per_trade_pct')} "
          f"max_daily_loss_pct={cfg.get('risk.max_daily_loss_pct')}")
    print(f"Broker configured: {cfg.get('broker.active')}")
    print(f"Strategies enabled: {cfg.get('strategies.enabled')}")

    # Credential PRESENCE only. Never print a key, a prefix, or a length.
    print("\nCredential presence (values are never printed):")
    for var in ("ALPACA_PAPER_API_KEY_ID", "ALPACA_PAPER_API_SECRET_KEY",
                "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY",
                "SEC_EDGAR_CONTACT_EMAIL"):
        print(f"  {var:<32} {'SET' if os.environ.get(var) else 'not set'}")

    breaker_db = _breaker_db_path(cfg)
    if breaker_db.exists():
        from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
        state = PersistentCircuitBreaker(breaker_db).state()
        print(f"\nCircuit breaker: {'TRIPPED' if not state.permits_entry() else 'clear'}")
        if not state.permits_entry():
            print(f"  reason: {state.reason}")
            print("  New entries are blocked. Protective exits remain permitted.")
            print("  Reset with: python -m app.cli breaker reset --operator NAME "
                  "--reason \"...\"")
    else:
        print(f"\nCircuit breaker: no state file yet at {breaker_db} (never tripped)")

    if cfg.mode is Mode.LIVE:
        print("\n*** LIVE MODE IS SET IN CONFIG. `run --mode live` still refuses to")
        print("*** execute in this milestone; LIVE is architecturally anticipated")
        print("*** but operationally disabled. See docs/SAFETY.md.")


def cmd_demo_scan(args):
    """Scanner + catalyst engine + scoring against the offline MockProvider.

    Zero network calls, zero broker interaction, safe in any mode. This is a
    DEMO of the scoring pipeline, not a market scan -- MockProvider generates
    synthetic prices.
    """
    from app.catalyst.engine import CatalystEngine, NullNewsProvider
    from app.scanner.base import ScanCriteria, Scanner
    from app.scanner.mock_provider import MockProvider
    from app.strategy.scoring import OpportunityScorer, ScoreInputs

    cfg = load_config(args.config)
    provider = MockProvider()
    criteria = _criteria_from_cfg(cfg)
    scan = Scanner(provider, criteria).run()
    scorer = OpportunityScorer(cfg.get("scoring.weights"))
    catalyst_engine = CatalystEngine([NullNewsProvider()])

    print(f"Scanner unsupported filters for provider: "
          f"{scan['criteria_unsupported_by_provider']}")
    rows = []
    for r in scan["results"]:
        catalysts = catalyst_engine.research(r.ticker)
        inputs = ScoreInputs(
            catalyst_quality=catalyst_engine.quality_score(catalysts),
            catalyst_freshness=catalyst_engine.freshness_score(catalysts),
            relative_volume=r.fields.get("rvol", 0),
            liquidity_usd=r.fields.get("dollar_volume", 0),
            spread_pct=r.fields.get("spread_pct", 1.0),
            technical_alignment=0.5, market_trend_alignment=0.5,
            reward_risk=2.0, historical_strategy_expectancy_r=None,
            data_confidence=0.8,
        )
        rows.append((r.ticker, scorer.score(inputs)["score"], r.fields))

    rows.sort(key=lambda x: -x[1])
    print(f"\n{'Ticker':<8}{'Score':<8}{'Price':<10}{'%Chg':<8}{'RVOL':<8}{'$Vol':<14}")
    for ticker, score, fields in rows:
        print(f"{ticker:<8}{score:<8}{fields.get('price', ''):<10}"
              f"{fields.get('pct_change', ''):<8}{fields.get('rvol', ''):<8}"
              f"{fields.get('dollar_volume', 0):<14,.0f}")
    print("\n(MockProvider — offline synthetic data. This is NOT a market scan.)")


# ---------------------------------------------------------------------------
def cmd_breaker(args):
    """Operator control surface for the persistent circuit breaker.

    Reset lives HERE, in the operator-facing CLI, and nowhere else. The reset
    token is minted by `issue_operator_reset()`, a module-level function rather
    than a breaker method, so that holding a PersistentCircuitBreaker instance
    does not by itself confer the ability to reset it. Strategy, risk, and
    execution code hold instances; none of them can clear a trip.
    """
    from app.risk.persistent_circuit_breaker import (
        PersistentCircuitBreaker,
        issue_operator_reset,
    )

    cfg = load_config(args.config)
    breaker = PersistentCircuitBreaker(_breaker_db_path(cfg))

    if args.breaker_command == "status":
        state = breaker.state()
        tripped = not state.permits_entry()
        print(f"Circuit breaker: {'TRIPPED' if tripped else 'CLEAR'}")
        if tripped:
            print(f"  reason:            {state.reason}")
            print(f"  entries permitted: {state.permits_entry()}")
            print(f"  exits permitted:   {state.permits_protective_exit()}")
        print("\nRecent history (most recent first):")
        history = breaker.history(10)
        if not history:
            print("  (none)")
        for row in history:
            print(f"  {row}")
        return

    if args.breaker_command == "reset":
        state = breaker.state()
        if state.permits_entry():
            print("Breaker is not tripped; nothing to reset.")
            return
        print(f"About to clear a TRIPPED breaker.\n  reason: {state.reason}")
        if not args.yes:
            # A trip means the system already decided something was wrong.
            # Clearing it is an operator judgement, so it is confirmed
            # interactively unless --yes is passed deliberately.
            answer = input("Type CLEAR to confirm: ").strip()
            if answer != "CLEAR":
                print("Aborted. Breaker remains tripped.")
                sys.exit(1)
        reset = issue_operator_reset(operator=args.operator, reason=args.reason,
                                    same_session=args.same_session)
        breaker.reset(reset)
        print("Breaker cleared. New entries are permitted again.")
        print("The trip and this reset both remain in the audit history.")


# ---------------------------------------------------------------------------
def cmd_run(args):
    cfg = load_config(args.config)
    governor = ModeGovernor(
        config_mode=cfg.mode,
        cli_live_flag_present=args.i_understand_this_is_live_trading,
        local_config_path_exists=LOCAL_CONFIG_PATH.exists(),
    )
    try:
        target_mode = Mode(args.mode.upper())
    except ValueError:
        print(f"REFUSING TO START: unknown mode {args.mode!r}", file=sys.stderr)
        sys.exit(1)

    try:
        governor.assert_execution_allowed(target_mode)
    except (InvalidModeError, LiveTradingNotAuthorizedError) as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Starting Aegis Trader in {target_mode.display_banner}")

    if target_mode is Mode.LIVE:
        # PART 3: LIVE stays operationally disabled. This refusal happens BEFORE
        # any broker construction, and AlpacaLiveBroker refuses to construct even
        # if this branch were removed -- two independent stops, not one.
        print("!!! LIVE MODE CONFIRMED BY OPERATOR — and still refused. !!!",
              file=sys.stderr)
        print("LIVE execution is operationally disabled in this milestone: "
              "AlpacaLiveBroker.OPERATIONALLY_ENABLED is False and this command "
              "does not construct it. See docs/SAFETY.md.", file=sys.stderr)
        sys.exit(1)

    if target_mode in (Mode.RESEARCH, Mode.SHADOW):
        _run_offline_cycle(cfg, target_mode)
    elif target_mode is Mode.PAPER:
        _run_paper_cycle(cfg, args)


def _criteria_from_cfg(cfg):
    from app.scanner.base import ScanCriteria
    return ScanCriteria(
        price_min=cfg.get("scanner.price_min"),
        price_max=cfg.get("scanner.price_max"),
        rvol_min=cfg.get("scanner.rvol_min"),
        dollar_volume_min=cfg.get("scanner.dollar_volume_min"),
        max_spread_pct=cfg.get("scanner.max_spread_pct"),
    )


def _strategies_from_cfg(cfg):
    from app.strategy.opening_range_breakout import OpeningRangeBreakout
    from app.strategy.vwap_reclaim import VwapReclaim
    return [
        OpeningRangeBreakout(cfg.get("strategies.opening_range_breakout") or {}),
        VwapReclaim(cfg.get("strategies.vwap_reclaim") or {}),
    ]


def _journal_from_cfg(cfg):
    from app.common.db import get_session_factory
    from app.journal.store import TradeJournal
    path = _journal_db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    return TradeJournal(get_session_factory(str(path))())


def _print_result(result, *, provenance: str):
    print(f"\nScanned {result.candidates_scanned} candidates → "
          f"{len(result.outcomes)} outcomes journaled → "
          f"{result.orders_submitted} orders submitted.")
    if getattr(result, "halted", False):
        print(f"RUN HALTED: {result.halt_reason}")
    for o in result.outcomes:
        score_str = f"score={o.score:.1f}" if o.score is not None else "score=n/a"
        reason = o.rejection_reason or ""
        print(f"  {o.ticker:<6} stage={o.stage_reached:<18} {score_str:<12} {reason}")
    print(f"\n{provenance}")


# ---------------------------------------------------------------------------
def _run_offline_cycle(cfg, target_mode: Mode):
    """RESEARCH / SHADOW: MockProvider + ShadowBroker. No network, no credentials.

    Regime is reported as UNKNOWN rather than as a fabricated flat/neutral
    reading. Milestone 1 passed a hand-built "flat" regime snapshot here, which
    meant every SHADOW trade was tagged with a market regime that had never been
    measured -- and PART 21 requires regime to be recorded per trade for later
    analysis. A wrong label is worse than an absent one, because it looks usable.
    """
    from app.broker.shadow_adapter import ShadowBroker
    from app.catalyst.engine import CatalystEngine, NullNewsProvider
    from app.execution.authorization import ExecutionAuthorizer
    from app.orchestration.pipeline import run_pipeline
    from app.risk.engine import RiskEngine
    from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
    from app.scanner.mock_provider import MockProvider
    from app.strategy.scoring import OpportunityScorer

    criteria = _criteria_from_cfg(cfg)
    provider = MockProvider()
    risk_cfg = cfg.get("risk")

    broker = ShadowBroker(
        starting_equity=cfg.get("account.starting_equity_usd"),
        quote_source=provider.get_quote,
    )

    breaker_path = _breaker_db_path(cfg)
    breaker_path.parent.mkdir(parents=True, exist_ok=True)

    result = run_pipeline(
        mode=target_mode,
        provider=provider,
        criteria=criteria,
        strategies=_strategies_from_cfg(cfg),
        scorer=OpportunityScorer(cfg.get("scoring.weights")),
        catalyst_engine=CatalystEngine([NullNewsProvider()]),
        risk_engine=RiskEngine(risk_cfg),
        risk_cfg=risk_cfg,
        broker=broker,
        journal=_journal_from_cfg(cfg),
        authorizer=ExecutionAuthorizer(target_mode=target_mode),
        circuit_breaker=PersistentCircuitBreaker(breaker_path, cfg=cfg.get("circuit_breaker")),
        config_mode=cfg.mode,
        config_mode_source=cfg.mode_source or "default.yaml",
        min_score_to_consider=cfg.get("scoring.min_score_to_display") or 40.0,
        # No session service offline: there is no authoritative clock without a
        # broker connection, and inventing one would defeat PART 10.
        session_service=None,
        regime_engine=None,
    )

    _print_result(result, provenance=(
        "(MockProvider synthetic data + ShadowBroker + regime UNKNOWN — offline, "
        "zero network calls, zero credentials. These are NOT real market results "
        "and the fills are simulated in memory.)"))


# ---------------------------------------------------------------------------
def _missing_paper_credentials() -> list[str]:
    """Which credential pair is missing, if any.

    The paper-specific names are preferred. The unprefixed pair is accepted as a
    fallback because Alpaca's own dashboard emits those names, but note that the
    unprefixed pair cannot be *verified* to be a paper key -- see the limitation
    printed by _run_paper_cycle.
    """
    have_paper = (os.environ.get("ALPACA_PAPER_API_KEY_ID")
                  and os.environ.get("ALPACA_PAPER_API_SECRET_KEY"))
    have_plain = (os.environ.get("ALPACA_API_KEY_ID")
                  and os.environ.get("ALPACA_API_SECRET_KEY"))
    if have_paper or have_plain:
        return []
    return ["ALPACA_PAPER_API_KEY_ID", "ALPACA_PAPER_API_SECRET_KEY"]


def _refuse(message: str) -> None:
    print(f"REFUSING TO RUN PAPER: {message}", file=sys.stderr)
    sys.exit(1)


def _run_paper_cycle(cfg, args):
    """PAPER: Alpaca's official paper endpoint, real market data, real clock.

    Every dependency here is constructed BEFORE the pipeline runs, and any
    failure exits. There is deliberately no `except` that falls back to
    MockProvider or ShadowBroker: a PAPER run that silently becomes a SHADOW run
    would report simulated fills under a heading that says real ones.
    """
    from app.broker.alpaca_adapter import AlpacaPaperBroker
    from app.catalyst.engine import CatalystEngine, NullNewsProvider
    from app.execution.authorization import ExecutionAuthorizer
    from app.marketdata.alpaca_provider import AlpacaMarketDataProvider
    from app.marketdata.regime_engine import MarketRegimeEngine
    from app.marketdata.session import MarketSessionService
    from app.orchestration.pipeline import run_pipeline
    from app.risk.engine import RiskEngine
    from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
    from app.strategy.scoring import OpportunityScorer

    missing = _missing_paper_credentials()
    if missing:
        _refuse(
            "paper trading credentials are not set.\n"
            f"  Required: {missing[0]} and {missing[1]}\n"
            "  (the unprefixed ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY pair is\n"
            "   also accepted, but the prefixed names are strongly preferred so a\n"
            "   paper key can never be confused with a live key)\n"
            "  Where to get them: https://alpaca.markets — sign up (email only, no\n"
            "   funding required), then Dashboard → API Keys with the Paper toggle ON.\n"
            "  Minimum permissions: trading only. Do NOT enable transfers/withdrawals.\n"
            "  Set them as environment variables. Never put them in a config file."
        )

    universe = tuple(cfg.get("scanner.universe") or DEFAULT_PAPER_UNIVERSE)

    # --- broker (paper endpoint, hard-coded in the adapter) --------------
    try:
        broker = AlpacaPaperBroker()
    except Exception as exc:  # noqa: BLE001
        _refuse(f"could not construct the Alpaca PAPER broker: {exc}")

    # --- market data ----------------------------------------------------
    try:
        provider = AlpacaMarketDataProvider(symbols=universe)
    except Exception as exc:  # noqa: BLE001
        _refuse(f"could not construct the Alpaca market-data provider: {exc}")

    # --- authoritative clock/calendar (PART 10) -------------------------
    try:
        session_service = MarketSessionService(client=broker.trading_client)
    except Exception as exc:  # noqa: BLE001
        _refuse(
            f"could not construct the market session service: {exc}\n"
            "  Session state must come from the broker clock/calendar. Assuming "
            "every weekday is a normal session is exactly what PART 10 forbids."
        )

    # --- market regime (PART 9) -----------------------------------------
    try:
        regime_engine = MarketRegimeEngine(provider)
    except Exception as exc:  # noqa: BLE001
        _refuse(f"could not construct the market regime engine: {exc}")

    # --- catalyst research (PARTS 7/8) ----------------------------------
    if args.no_catalyst_research:
        catalyst_engine = CatalystEngine([NullNewsProvider()])
        print("\n" + "!" * 68)
        print("DEGRADED: catalyst research is DISABLED by --no-catalyst-research.")
        print("Every candidate will be recorded as 'no verified catalyst found'.")
        print("Catalyst-derived scores will be 0. This run does NOT demonstrate")
        print("catalyst research capability.")
        print("!" * 68)
    elif not os.environ.get("SEC_EDGAR_CONTACT_EMAIL"):
        _refuse(
            "SEC_EDGAR_CONTACT_EMAIL is not set.\n"
            "  SEC EDGAR is free and needs no API key, but it requires a contact\n"
            "  email in the User-Agent header; requests without one get blocked.\n"
            "  Set SEC_EDGAR_CONTACT_EMAIL to an address you monitor.\n"
            "  To run PAPER without catalyst research at all, pass\n"
            "  --no-catalyst-research and accept the degraded banner."
        )
    else:
        from app.catalyst.sec_edgar import SecEdgarFilingProvider
        catalyst_engine = CatalystEngine([SecEdgarFilingProvider()])

    risk_cfg = cfg.get("risk")
    breaker_path = _breaker_db_path(cfg)
    breaker_path.parent.mkdir(parents=True, exist_ok=True)
    breaker = PersistentCircuitBreaker(breaker_path, cfg=cfg.get("circuit_breaker"))

    if not breaker.permits_entry():
        _refuse(
            f"the circuit breaker is TRIPPED: {breaker.state().reason}\n"
            "  New entries are blocked until an operator clears it deliberately:\n"
            "    python -m app.cli breaker reset --operator NAME --reason \"...\"\n"
            "  Protective exits remain permitted while tripped."
        )

    print("\n" + "=" * 68)
    print(" PAPER MODE — Alpaca paper endpoint")
    print("=" * 68)
    print(f" broker environment : {broker.environment.value}")
    print(f" endpoint           : {broker.base_url_in_use}")
    print(f" market data        : Alpaca (feed={provider.feed}) over {len(universe)} symbols")
    print(" LIMITATION: Alpaca exposes no server-side paper-vs-live signal.")
    print("   `paper=True` is client-side URL selection only. The compensating")
    print("   controls are: AlpacaPaperBroker hard-codes paper=True with no")
    print("   parameter that can change it, exposes no url_override, declares")
    print("   BrokerEnvironment.PAPER, and the ExecutionAuthorizer requires the")
    print("   grant's environment to match. See docs/SAFETY.md.")
    print("=" * 68)

    result = run_pipeline(
        mode=Mode.PAPER,
        provider=provider,
        criteria=_criteria_from_cfg(cfg),
        strategies=_strategies_from_cfg(cfg),
        scorer=OpportunityScorer(cfg.get("scoring.weights")),
        catalyst_engine=catalyst_engine,
        risk_engine=RiskEngine(risk_cfg),
        risk_cfg=risk_cfg,
        broker=broker,
        journal=_journal_from_cfg(cfg),
        authorizer=ExecutionAuthorizer(target_mode=Mode.PAPER),
        circuit_breaker=breaker,
        config_mode=cfg.mode,
        config_mode_source=cfg.mode_source or "default.yaml",
        min_score_to_consider=cfg.get("scoring.min_score_to_display") or 40.0,
        session_service=session_service,
        regime_engine=regime_engine,
        allowed_sessions=tuple(cfg.get("sessions.allowed") or ("REGULAR",)),
    )

    _print_result(result, provenance=(
        "(Alpaca PAPER endpoint, real market data, real broker clock. Fills are "
        "real paper fills confirmed by the broker, not simulated locally.)"))


def cmd_dashboard(args):
    import uvicorn
    uvicorn.run("app.dashboard.server:app", host="127.0.0.1", port=args.port,
                reload=False)


def _paper_probe_runtime(cfg, *, symbol: str, require_sec: bool):
    from app.paper_runtime import build_paper_runtime

    symbols = tuple(dict.fromkeys((symbol.upper(), "SPY", "QQQ", "IWM")))
    return build_paper_runtime(
        symbols=symbols,
        breaker_path=_breaker_db_path(cfg),
        breaker_config=cfg.get("circuit_breaker"),
        require_sec=require_sec,
    )


def _paper_probe_local_state(journal):
    # Re-use the production journal interpretation so the probe cannot present
    # an empty local state while durable orders or positions actually exist.
    from app.orchestration.pipeline import _local_positions

    return journal.open_orders(), _local_positions(journal)


def _paper_probe_freshness(cfg) -> dict[str, float]:
    return {
        "quote": float(cfg.get("data_freshness.quote_max_age_seconds")),
        "bars": float(cfg.get("data_freshness.bar_max_age_seconds")),
        "account": float(cfg.get("data_freshness.account_max_age_seconds")),
    }


def cmd_paper_probe(args):
    """Dedicated verification path; never invokes scanners or strategies."""
    from app.paper_runtime import PaperRuntimeError

    cfg = load_config(args.config)
    if args.probe_command == "order":
        if not args.i_understand_this_submits_a_paper_order:
            print(
                "PAPER ORDER PROBE REFUSED: missing required acknowledgement "
                "--i-understand-this-submits-a-paper-order",
                file=sys.stderr,
            )
            sys.exit(1)
        if cfg.mode is not Mode.PAPER:
            print(
                f"PAPER ORDER PROBE REFUSED: configured mode is {cfg.mode.value}, "
                "not PAPER",
                file=sys.stderr,
            )
            sys.exit(1)
    try:
        runtime = _paper_probe_runtime(
            cfg,
            symbol=args.symbol,
            require_sec=args.probe_command == "connectivity",
        )
    except PaperRuntimeError as exc:
        print(f"PAPER PROBE REFUSED: {exc}", file=sys.stderr)
        sys.exit(1)

    journal = _journal_from_cfg(cfg)
    local_open_orders, local_positions = _paper_probe_local_state(journal)
    if args.probe_command == "connectivity":
        from app.verification.connectivity import run_connectivity_probe

        report = run_connectivity_probe(
            runtime,
            symbol=args.symbol,
            local_open_orders=local_open_orders,
            local_positions=local_positions,
            freshness_max_ages=_paper_probe_freshness(cfg),
        )
    else:
        from app.verification.order_probe import (
            OrderProbeConfig,
            OrderProbeRefused,
            run_order_probe,
        )

        risk_cfg = cfg.get("risk")
        try:
            report = run_order_probe(
                runtime,
                journal=journal,
                symbol=args.symbol,
                qty=args.qty,
                acknowledged=args.i_understand_this_submits_a_paper_order,
                configured_mode=cfg.mode,
                config_mode_source=cfg.mode_source,
                config=OrderProbeConfig(
                    max_spread_pct=risk_cfg["max_spread_pct"],
                    min_liquidity_avg_dollar_vol=risk_cfg[
                        "min_liquidity_avg_dollar_vol"
                    ],
                    risk_config=risk_cfg,
                    allowed_sessions=tuple(
                        cfg.get("sessions.allowed") or ("REGULAR",)
                    ),
                ),
                local_open_orders=local_open_orders,
                local_positions=local_positions,
                freshness_max_ages=_paper_probe_freshness(cfg),
            )
        except OrderProbeRefused as exc:
            print(f"PAPER ORDER PROBE REFUSED: {exc}", file=sys.stderr)
            sys.exit(1)

    print(report.render())
    if not report.passed:
        sys.exit(1)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(prog="aegis-trader")
    parser.add_argument("--config", default=None,
                       help="Path to an additional config YAML to layer on top. "
                            "NOTE: this overlay cannot escalate mode to LIVE.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("demo-scan").set_defaults(func=cmd_demo_scan)

    run_p = sub.add_parser("run")
    run_p.add_argument("--mode", required=True,
                      choices=["research", "shadow", "paper", "live"])
    run_p.add_argument("--i-understand-this-is-live-trading", action="store_true",
                      help="Required for LIVE, and still insufficient: LIVE is "
                           "operationally disabled in this milestone.")
    run_p.add_argument("--no-catalyst-research", action="store_true",
                      help="Run PAPER with catalyst research disabled. Prints a "
                           "degraded banner; every candidate is recorded as "
                           "'no verified catalyst found'.")
    run_p.set_defaults(func=cmd_run)

    breaker_p = sub.add_parser("breaker",
                               help="Inspect or reset the persistent circuit breaker.")
    breaker_sub = breaker_p.add_subparsers(dest="breaker_command", required=True)
    breaker_sub.add_parser("status")
    reset_p = breaker_sub.add_parser("reset")
    reset_p.add_argument("--operator", required=True,
                        help="Who is clearing the breaker. Recorded in the audit trail.")
    reset_p.add_argument("--reason", required=True,
                        help="Why it is safe to clear. Recorded in the audit trail.")
    reset_p.add_argument("--same-session", action="store_true",
                        help="Permit trading again within the SAME session that "
                             "tripped. Off by default.")
    reset_p.add_argument("--yes", action="store_true",
                        help="Skip the interactive confirmation.")
    breaker_p.set_defaults(func=cmd_breaker)

    dash_p = sub.add_parser("dashboard")
    dash_p.add_argument("--port", type=int, default=8080)
    dash_p.set_defaults(func=cmd_dashboard)

    probe_p = sub.add_parser(
        "paper-probe",
        help="Operator-controlled PAPER verification; separate from strategies.",
    )
    probe_sub = probe_p.add_subparsers(dest="probe_command", required=True)
    connectivity_p = probe_sub.add_parser(
        "connectivity", help="Read-only infrastructure verification."
    )
    connectivity_p.add_argument("--symbol", default="SPY")
    connectivity_p.set_defaults(func=cmd_paper_probe)
    order_p = probe_sub.add_parser(
        "order", help="Submit at most one supervised PAPER bracket order."
    )
    order_p.add_argument("--symbol", required=True)
    order_p.add_argument("--qty", type=int, default=1)
    order_p.add_argument(
        "--i-understand-this-submits-a-paper-order",
        action="store_true",
        help="Required PAPER-only acknowledgement. It grants no LIVE authority.",
    )
    order_p.set_defaults(func=cmd_paper_probe)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

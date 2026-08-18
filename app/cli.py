"""
Aegis Trader CLI — the only supported entry point into the system.

    python -m app.cli status
    python -m app.cli demo-scan
    python -m app.cli run --mode shadow
    python -m app.cli dashboard

MODE SAFETY: this file is the ONE place that constructs a BrokerAdapter and
decides whether live trading is reachable. See ModeGovernor in
app/common/modes.py. `run --mode live` additionally requires
--i-understand-this-is-live-trading; without it the process exits before
constructing anything broker-related.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.common.modes import LiveTradingNotAuthorizedError, Mode, ModeGovernor, InvalidModeError
from app.config.loader import LOCAL_CONFIG_PATH, load_config


def cmd_status(args):
    cfg = load_config(args.config)
    print("=" * 60)
    print(f" AEGIS TRADER — MODE: {cfg.mode.display_banner}")
    print("=" * 60)
    print(f"Config sources: {cfg.source_files}")
    print(f"Risk: max_risk_per_trade_pct={cfg.get('risk.max_risk_per_trade_pct')} "
          f"max_daily_loss_pct={cfg.get('risk.max_daily_loss_pct')}")
    print(f"Broker configured: {cfg.get('broker.active')}")
    print(f"Strategies enabled: {cfg.get('strategies.enabled')}")
    if cfg.mode is Mode.LIVE:
        print("\n*** LIVE MODE IS SET IN CONFIG. Real orders are possible if this")
        print("*** process is also started with --i-understand-this-is-live-trading.")


def cmd_demo_scan(args):
    """Runs the scanner + catalyst engine + scoring end to end against the
    offline MockProvider, with zero network calls and zero broker
    interaction. Safe to run anywhere, anytime, in any mode."""
    from app.scanner.base import ScanCriteria, Scanner
    from app.scanner.mock_provider import MockProvider
    from app.strategy.scoring import OpportunityScorer, ScoreInputs
    from app.catalyst.engine import CatalystEngine, NullNewsProvider

    cfg = load_config(args.config)
    provider = MockProvider()
    criteria = ScanCriteria(
        price_min=cfg.get("scanner.price_min"), price_max=cfg.get("scanner.price_max"),
        rvol_min=cfg.get("scanner.rvol_min"), dollar_volume_min=cfg.get("scanner.dollar_volume_min"),
        max_spread_pct=cfg.get("scanner.max_spread_pct"),
    )
    scan = Scanner(provider, criteria).run()
    scorer = OpportunityScorer(cfg.get("scoring.weights"))
    catalyst_engine = CatalystEngine([NullNewsProvider()])

    print(f"Scanner unsupported filters for provider: {scan['criteria_unsupported_by_provider']}")
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
            reward_risk=2.0, historical_strategy_expectancy_r=None, data_confidence=0.8,
        )
        s = scorer.score(inputs)
        rows.append((r.ticker, s["score"], r.fields))

    rows.sort(key=lambda x: -x[1])
    print(f"\n{'Ticker':<8}{'Score':<8}{'Price':<10}{'%Chg':<8}{'RVOL':<8}{'$Vol':<14}")
    for ticker, score, fields in rows:
        print(f"{ticker:<8}{score:<8}{fields.get('price', ''):<10}{fields.get('pct_change', ''):<8}"
              f"{fields.get('rvol', ''):<8}{fields.get('dollar_volume', ''):<14,.0f}")
    print("\n(This used MockProvider — offline synthetic data. Wire a real "
          "market-data adapter in app/scanner/ to scan the live market.)")


def cmd_run(args):
    cfg = load_config(args.config)
    governor = ModeGovernor(
        config_mode=cfg.mode,
        cli_live_flag_present=args.i_understand_this_is_live_trading,
        local_config_path_exists=LOCAL_CONFIG_PATH.exists(),
    )
    target_mode = Mode(args.mode.upper())
    try:
        governor.assert_execution_allowed(target_mode)
    except (InvalidModeError, LiveTradingNotAuthorizedError) as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Starting Aegis Trader in {target_mode.display_banner}")
    if target_mode in (Mode.RESEARCH, Mode.SHADOW):
        _run_one_pipeline_cycle(cfg, target_mode)
    elif target_mode is Mode.PAPER:
        print("PAPER mode: orders will be sent ONLY to the broker's paper endpoint. "
              "Requires ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY in the environment. "
              "Broker/market-data adapters for PAPER are not wired into `run` yet "
              "(Phase 9) — refusing to proceed rather than pretend to place orders.")
        sys.exit(1)
    elif target_mode is Mode.LIVE:
        print("!!! LIVE MODE CONFIRMED BY OPERATOR. Real broker, real money. !!!")
        print("LIVE execution is not implemented in `run` in this milestone — "
              "refusing to proceed. See docs/SAFETY.md.")
        sys.exit(1)


def _run_one_pipeline_cycle(cfg, target_mode):
    """Wires one real run_pipeline() cycle for RESEARCH/SHADOW using
    MockProvider (no real market-data adapter exists yet — see
    docs/ARCHITECTURE.md §7) and ShadowBroker (no network client at all,
    see app/broker/shadow_adapter.py), so this is safe in any environment
    with zero credentials."""
    import datetime as dt

    from app.broker.base import Quote
    from app.broker.shadow_adapter import ShadowBroker
    from app.catalyst.engine import CatalystEngine, NullNewsProvider
    from app.common.db import get_session_factory
    from app.journal.store import TradeJournal
    from app.marketdata.regime import build_regime_snapshot
    from app.orchestration.pipeline import run_pipeline
    from app.risk.engine import RiskEngine
    from app.scanner.base import ScanCriteria
    from app.scanner.mock_provider import MockProvider
    from app.strategy.opening_range_breakout import OpeningRangeBreakout
    from app.strategy.scoring import OpportunityScorer
    from app.strategy.vwap_reclaim import VwapReclaim

    provider = MockProvider()
    criteria = ScanCriteria(
        price_min=cfg.get("scanner.price_min"), price_max=cfg.get("scanner.price_max"),
        rvol_min=cfg.get("scanner.rvol_min"), dollar_volume_min=cfg.get("scanner.dollar_volume_min"),
        max_spread_pct=cfg.get("scanner.max_spread_pct"),
    )
    strategies = [
        OpeningRangeBreakout(cfg.get("strategies.opening_range_breakout") or {}),
        VwapReclaim(cfg.get("strategies.vwap_reclaim") or {}),
    ]
    scorer = OpportunityScorer(cfg.get("scoring.weights"))
    catalyst_engine = CatalystEngine([NullNewsProvider()])
    risk_cfg = cfg.get("risk")
    risk_engine = RiskEngine(risk_cfg)

    def _quote(ticker):
        rows = provider.scan(criteria)
        row = next((r for r in rows if r.ticker == ticker), None)
        price = row.fields["price"] if row else 100.0
        return Quote(ticker=ticker, bid=price * 0.999, ask=price * 1.001, last=price,
                     timestamp=dt.datetime.now(dt.timezone.utc), source="mock")

    broker = ShadowBroker(starting_equity=cfg.get("account.starting_equity_usd"), quote_source=_quote)

    session_factory = get_session_factory(str(Path(cfg.get("logging.dir") or "data") / "journal.db"))
    journal = TradeJournal(session_factory())

    # Regime is stubbed flat/neutral here — no live SPY/QQQ/VIX feed exists
    # yet (see docs/ARCHITECTURE.md §7). Wiring a real regime feed is a
    # documented follow-up, not silently faked as "real."
    regime = build_regime_snapshot(spy_pct=0.0, qqq_pct=0.0, iwm_pct=0.0, vix_level=None,
                                     breadth=None, spy_range_pct=1.0, as_of="stub-no-live-feed")

    result = run_pipeline(
        mode=target_mode, provider=provider, criteria=criteria, strategies=strategies,
        scorer=scorer, catalyst_engine=catalyst_engine, risk_engine=risk_engine, risk_cfg=risk_cfg,
        broker=broker, journal=journal, regime=regime,
        min_score_to_consider=cfg.get("scoring.min_score_to_display") or 40.0,
    )

    print(f"\nScanned {result.candidates_scanned} candidates → "
          f"{len(result.outcomes)} outcomes journaled → {result.orders_submitted} orders submitted.")
    for o in result.outcomes:
        score_str = f"score={o.score:.1f}" if o.score is not None else "score=n/a"
        print(f"  {o.ticker:<6} stage={o.stage_reached:<16} {score_str}")
    print("\n(MockProvider + stubbed flat regime + ShadowBroker — offline, zero "
          "network calls. Wire a real market-data/regime feed before this output "
          "reflects the live market.)")


def cmd_dashboard(args):
    import uvicorn
    uvicorn.run("app.dashboard.server:app", host="127.0.0.1", port=args.port, reload=False)


def main():
    parser = argparse.ArgumentParser(prog="aegis-trader")
    parser.add_argument("--config", default=None, help="Path to an additional config YAML to layer on top.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("demo-scan").set_defaults(func=cmd_demo_scan)

    run_p = sub.add_parser("run")
    run_p.add_argument("--mode", required=True, choices=["research", "shadow", "paper", "live"])
    run_p.add_argument("--i-understand-this-is-live-trading", action="store_true")
    run_p.set_defaults(func=cmd_run)

    dash_p = sub.add_parser("dashboard")
    dash_p.add_argument("--port", type=int, default=8080)
    dash_p.set_defaults(func=cmd_dashboard)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

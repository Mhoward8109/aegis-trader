# Aegis Trader — Strategy Development Guide

## The Contract

Every strategy subclasses `app.strategy.base.Strategy` and implements six
gate methods, called in this exact order by `Strategy.evaluate()` (which
you should not override):

1. `market_conditions_ok(ctx)` — broad regime filter (e.g. "only trade
   breakouts when `ctx.regime['trend_vs_range'] == 'trending'`").
2. `candidate_criteria_met(ctx)` — cheap pre-filter (price/rvol/liquidity
   thresholds) before doing more expensive checks.
3. `setup_conditions_met(ctx)` — the actual pattern condition (e.g. price
   above opening-range high).
4. `confirmation_met(ctx)` — a second, independent confirmation (e.g.
   volume confirmation, multi-bar hold).
5. `entry_trigger(ctx)` — the precise trigger condition for *this* bar.
6. `build_setup(ctx)` — only called if all five gates above passed;
   returns a `Setup` (entry/stop/targets/invalidation) or `None`.

`None` at any stage is a valid, expected, non-error outcome — "no trade"
is itself information (spec §35), and the orchestration pipeline journals
it as `strategy_no_setup` rather than treating it as a failure.

A strategy **never**:
- calls a broker directly
- decides position size or risk budget (that's `RiskEngine`'s job — see
  `docs/SAFETY.md` §4)
- sees account equity, buying power, or other strategies' open positions
  (that context isn't passed into `MarketContext`, by design)

This separation is what makes "performance is not permission" (spec §34)
structurally enforced rather than a promise: even a strategy tuned to
100% backtest win rate cannot skip the risk engine, because it has no
reference to a broker or account state to skip it *with*.

## `MarketContext` — What a Strategy Can See

```python
MarketContext(
    ticker: str, timestamp: datetime,
    bars_intraday: pd.DataFrame,   # OHLCV bars, assembled by the orchestrator
    bars_prev_day: pd.DataFrame,
    quote: dict,                    # {bid, ask, last, spread_pct}
    indicators: dict,                # output of app/technical/indicators.py::compute_all()
    catalyst: dict | None,           # {catalysts: [...]} from CatalystEngine, or None
    regime: dict,                    # RegimeSnapshot as dict — spy/qqq/iwm direction, vix regime, etc.
    session: str,                    # premarket | regular | postmarket
)
```

Because this is a plain dataclass with no live connections inside it,
strategies are trivially backtestable: feed a `Strategy.evaluate()` a
recorded `MarketContext` from any point in history and it behaves
identically to live. This is also what makes it safe to run the exact
same strategy code in SHADOW and PAPER/LIVE.

## Reference Implementations

- `app/strategy/opening_range_breakout.py` — breakout above the first N
  minutes' high, with an rvol and spread gate.
- `app/strategy/vwap_reclaim.py` — reclaim of VWAP after a dip, with a
  multi-bar confirmation. **Known minor issue**: references
  `ctx.raw_signals_history`, an attribute that doesn't exist on
  `MarketContext` — currently dead code inside a conditional branch that
  isn't exercised by the current test suite; flagged here rather than
  silently left for a future contributor to trip over. Fix before relying
  on this strategy's confirmation logic in PAPER/LIVE.

## Versioning

Every `Setup` records `strategy` and `strategy_version` explicitly. When
you materially change a gate's logic, bump `Strategy.version` — the
journal keys backtest/paper performance history by
`(strategy, strategy_version)`, so silently mutating a strategy in place
would corrupt the very performance evidence the promotion checklist in
`docs/OPERATOR_MANUAL.md` depends on.

## Scoring

A strategy producing a `Setup` does not by itself mean "trade it" — the
`Setup` is fed into `OpportunityScorer` (`app/strategy/scoring.py`) along
with catalyst/technical/liquidity signals to produce a 0-100 score with a
full, non-opaque breakdown by component (catalyst quality, relative
volume, liquidity, spread quality, technical alignment, market trend,
reward:risk, data confidence, historical strategy performance). Only
scores above `scoring.min_score_to_trade` in config are even passed to the
risk engine for a trade decision.

## Adding a New Strategy — Checklist

1. Subclass `Strategy`, implement all six gate methods, set `name` and
   `version`.
2. Register it in `config/*.yaml`'s `strategies.enabled` list and add a
   `strategies.<name>` params block.
3. Wire it into the strategies list passed to `run_pipeline()` (currently
   done in `app/cli.py::_run_one_pipeline_cycle`).
4. Write a unit test exercising each gate independently (see
   `tests/test_indicators.py` and `tests/test_scoring.py` for the style
   used elsewhere in this repo) plus at least one full `evaluate()` test
   with a synthetic `MarketContext` that should produce a `Setup`, and one
   that should correctly return `None`.
5. Backtest it (`app/backtest/engine.py`) with a proper train/validation/
   out-of-sample split before ever running it in PAPER — see
   `docs/ARCHITECTURE.md` and the spec's overfitting-rejection requirement
   (`is_likely_overfit()`).
6. Never let it run in PAPER/LIVE until it has a SHADOW track record you
   can point to in the journal DB.

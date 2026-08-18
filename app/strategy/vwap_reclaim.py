"""
VWAP Reclaim (spec §7 example strategy). Hypothesis: a stock that lost VWAP
intraday and then reclaims it with confirmation tends to continue upward.
Independent, separately measurable module — see docs/STRATEGY_GUIDE.md for
how to add strategies like this one.
"""
from __future__ import annotations

from app.strategy.base import MarketContext, Setup, Strategy


class VwapReclaim(Strategy):
    name = "vwap_reclaim"
    version = "1.0.0"

    def market_conditions_ok(self, ctx: MarketContext) -> bool:
        return True  # regime-agnostic by design; regime is tagged for later analysis instead of gating

    def candidate_criteria_met(self, ctx: MarketContext) -> bool:
        return ctx.indicators.get("vwap") is not None

    def setup_conditions_met(self, ctx: MarketContext) -> bool:
        # requires that price was below vwap recently and is now above it
        history = ctx.raw_signals_history if hasattr(ctx, "raw_signals_history") else None
        lost_vwap_recently = ctx.indicators.get("lost_vwap_recently", False)
        return bool(lost_vwap_recently)

    def confirmation_met(self, ctx: MarketContext) -> bool:
        bars_above = ctx.indicators.get("bars_above_vwap", 0)
        return bars_above >= self.params.get("reclaim_confirmation_bars", 2)

    def entry_trigger(self, ctx: MarketContext) -> bool:
        last = ctx.quote.get("last")
        vwap = ctx.indicators.get("vwap")
        return last is not None and vwap is not None and last > vwap

    def build_setup(self, ctx: MarketContext) -> Setup | None:
        last = ctx.quote.get("last")
        vwap = ctx.indicators.get("vwap")
        atr14 = ctx.indicators.get("atr14")
        low_of_day = ctx.indicators.get("low_of_day")
        if last is None or vwap is None or atr14 is None:
            return None

        entry = last
        stop = min(vwap - atr14 * 0.5, low_of_day) if low_of_day else vwap - atr14 * 0.5
        risk = entry - stop
        if risk <= 0:
            return None
        targets = [entry + risk * 1.5, entry + risk * 2.5]

        return Setup(
            ticker=ctx.ticker, strategy=self.name, strategy_version=self.version,
            direction="long", entry=entry, stop=stop, targets=targets,
            invalidation=f"Price closes back below VWAP ({vwap:.2f}).",
            confirmation_notes=f"Reclaimed VWAP after {ctx.indicators.get('bars_above_vwap')} confirming bars.",
            max_spread_pct=self.params.get("max_spread_pct", 0.4),
            min_liquidity_usd=self.params.get("min_liquidity_usd", 5_000_000),
            permitted_sessions=["regular"],
            prohibited_conditions=["trading_halt_recent"],
            raw_signals={"vwap": vwap, "atr14": atr14, "low_of_day": low_of_day},
        )

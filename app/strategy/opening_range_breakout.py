"""
Opening Range Breakout (spec §7 example strategy). Hypothesis, not a
proven edge: "break of the defined opening range with liquidity and
volume confirmation continues in the breakout direction." Must be
backtested and walk-forward validated (spec §16-17) before any promotion
consideration — this class only encodes the rule, it does not claim it works.
"""
from __future__ import annotations

from app.strategy.base import MarketContext, Setup, Strategy


class OpeningRangeBreakout(Strategy):
    name = "opening_range_breakout"
    version = "1.0.0"

    def market_conditions_ok(self, ctx: MarketContext) -> bool:
        return ctx.regime.get("trend_vs_range") != "choppy_range"

    def candidate_criteria_met(self, ctx: MarketContext) -> bool:
        rvol = ctx.indicators.get("rvol")
        return rvol is not None and rvol >= self.params.get("min_rvol", 2.0)

    def setup_conditions_met(self, ctx: MarketContext) -> bool:
        orange = ctx.indicators.get("opening_range")
        return bool(orange and orange.get("high") is not None and orange.get("low") is not None)

    def confirmation_met(self, ctx: MarketContext) -> bool:
        spread_pct = ctx.quote.get("spread_pct")
        if spread_pct is None:
            return False
        return spread_pct <= self.params.get("max_spread_pct", 0.3)

    def entry_trigger(self, ctx: MarketContext) -> bool:
        orange = ctx.indicators.get("opening_range") or {}
        last = ctx.quote.get("last")
        if last is None or orange.get("high") is None:
            return False
        return last > orange["high"]

    def build_setup(self, ctx: MarketContext) -> Setup | None:
        orange = ctx.indicators.get("opening_range") or {}
        atr14 = ctx.indicators.get("atr14")
        last = ctx.quote.get("last")
        if last is None or orange.get("high") is None or orange.get("low") is None or atr14 is None:
            return None  # NO TRADE: insufficient data to define entry/stop (spec §35)

        entry = last
        stop = orange["low"]
        risk = entry - stop
        if risk <= 0:
            return None
        targets = [entry + risk * 1.5, entry + risk * 2.5]

        return Setup(
            ticker=ctx.ticker, strategy=self.name, strategy_version=self.version,
            direction="long", entry=entry, stop=stop, targets=targets,
            invalidation=f"Price closes back below opening-range low ({orange['low']:.2f}).",
            confirmation_notes=f"Broke opening range high {orange['high']:.2f} on RVOL "
                                f"{ctx.indicators.get('rvol')}.",
            max_spread_pct=self.params.get("max_spread_pct", 0.3),
            min_liquidity_usd=self.params.get("min_liquidity_usd", 5_000_000),
            permitted_sessions=["regular"],
            prohibited_conditions=["trading_halt_recent", "no_catalyst_and_low_confidence"],
            raw_signals={"opening_range": orange, "rvol": ctx.indicators.get("rvol"), "atr14": atr14},
        )

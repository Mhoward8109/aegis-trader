"""
Market Regime Engine (spec §14). Computes broad-tape context so no setup is
evaluated in isolation, and tags every trade with the regime that was active
(so strategy-vs-regime performance can be measured later, spec §14/§20).
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class RegimeSnapshot:
    spy_direction: str        # up | down | flat
    qqq_direction: str
    iwm_direction: str
    vix_level: float | None
    vix_regime: str           # low | normal | elevated | extreme
    breadth: float | None     # advancers/decliners ratio, if available
    trend_vs_range: str       # trending | choppy_range
    risk_on_off: str          # risk_on | risk_off | neutral
    as_of: str


def classify_direction(pct_change: float, flat_band: float = 0.15) -> str:
    if pct_change > flat_band:
        return "up"
    if pct_change < -flat_band:
        return "down"
    return "flat"


def classify_vix(vix_level: float | None) -> str:
    if vix_level is None:
        return "unknown"
    if vix_level < 15:
        return "low"
    if vix_level < 20:
        return "normal"
    if vix_level < 30:
        return "elevated"
    return "extreme"


def classify_trend_vs_range(bars_high_low_range_pct: float, threshold: float = 1.5) -> str:
    """Cheap proxy: if the day's high-low range as % of price is below the
    threshold, call it choppy/range; strategies can use this as a gate."""
    return "trending" if bars_high_low_range_pct >= threshold else "choppy_range"


def build_regime_snapshot(spy_pct: float, qqq_pct: float, iwm_pct: float, vix_level: float | None,
                            breadth: float | None, spy_range_pct: float, as_of: str) -> RegimeSnapshot:
    vix_regime = classify_vix(vix_level)
    risk_on_off = "neutral"
    if vix_regime in ("elevated", "extreme") and spy_pct < 0:
        risk_on_off = "risk_off"
    elif vix_regime == "low" and spy_pct > 0:
        risk_on_off = "risk_on"

    return RegimeSnapshot(
        spy_direction=classify_direction(spy_pct), qqq_direction=classify_direction(qqq_pct),
        iwm_direction=classify_direction(iwm_pct), vix_level=vix_level, vix_regime=vix_regime,
        breadth=breadth, trend_vs_range=classify_trend_vs_range(spy_range_pct), risk_on_off=risk_on_off,
        as_of=as_of,
    )

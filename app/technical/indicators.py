"""
Technical Analysis Engine (spec §6). Pure functions over pandas DataFrames of
OHLCV bars (columns: timestamp, open, high, low, close, volume). No I/O, no
broker/data-provider dependency — this module is deliberately easy to unit
test with synthetic data (see tests/test_indicators.py).

Per spec §6: "Indicators should support decision-making but should not
automatically become trading signals simply because they exist." Accordingly
this module only ever returns numbers/series; whether/how a strategy acts on
them is entirely the strategy module's decision (app/strategy/*).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum()
    cum_vol_price = (typical * df["volume"]).cumsum()
    return cum_vol_price / cum_vol.replace(0, np.nan)


def anchored_vwap(df: pd.DataFrame, anchor_index: int) -> pd.Series:
    sub = df.iloc[anchor_index:].copy()
    out = pd.Series(index=df.index, dtype=float)
    out.iloc[anchor_index:] = vwap(sub).values
    return out


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder-style RSI with correct edge-case handling: when avg_loss is
    exactly 0 but avg_gain > 0 (a pure up-move over the window), RSI must
    be 100, not the neutral midpoint — a naive `avg_gain/avg_loss` division
    produces NaN in that case and silently masking it as 50 would hide a
    real (and useful) signal from every strategy gate that reads RSI.
    Only a truly flat window (avg_gain == 0 AND avg_loss == 0, i.e. no
    price movement at all, or not enough history yet) falls back to the
    neutral midpoint.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=window).mean()
    avg_loss = loss.rolling(window=window).mean()

    out = pd.Series(index=series.index, dtype=float)
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    loss_zero_gain_positive = (avg_loss == 0) & (avg_gain > 0)
    normal = ~both_zero & ~loss_zero_gain_positive

    rs = avg_gain[normal] / avg_loss[normal]
    out[normal] = 100 - (100 / (1 + rs))
    out[loss_zero_gain_positive] = 100.0
    out[both_zero] = 50.0
    return out.fillna(50.0)  # NaN only for the initial window before enough history exists


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=window).mean()


def relative_volume(df: pd.DataFrame, lookback_bars: int) -> pd.Series:
    """Current cumulative volume vs the trailing average cumulative volume
    over the same intraday window on prior days. Caller must pass a frame
    already restricted to a single day's bars for `current`; for a proper
    RVOL you typically compare df['volume'].sum() so far today against a
    20-day average for the same elapsed time — that comparison itself lives
    in the scanner, which has access to multi-day history. This helper
    provides the rolling-bar version usable intraday."""
    roll_avg = df["volume"].rolling(window=lookback_bars).mean()
    return df["volume"] / roll_avg.replace(0, np.nan)


def opening_range(df: pd.DataFrame, minutes: int, bar_minutes: int = 1) -> dict:
    n_bars = max(1, minutes // bar_minutes)
    window = df.iloc[:n_bars]
    if window.empty:
        return {"high": None, "low": None, "bars_used": 0}
    return {"high": float(window["high"].max()), "low": float(window["low"].min()), "bars_used": len(window)}


def previous_day_levels(prev_day_df: pd.DataFrame) -> dict:
    if prev_day_df.empty:
        return {"high": None, "low": None, "close": None}
    return {
        "high": float(prev_day_df["high"].max()),
        "low": float(prev_day_df["low"].min()),
        "close": float(prev_day_df["close"].iloc[-1]),
    }


def premarket_levels(premarket_df: pd.DataFrame) -> dict:
    if premarket_df.empty:
        return {"high": None, "low": None}
    return {"high": float(premarket_df["high"].max()), "low": float(premarket_df["low"].min())}


def high_low_of_day(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"high": None, "low": None}
    return {"high": float(df["high"].max()), "low": float(df["low"].min())}


def distance_from(price: float, reference: float | None) -> float | None:
    if reference is None or reference == 0:
        return None
    return (price - reference) / reference * 100.0


def support_resistance_levels(df: pd.DataFrame, window: int = 20, prominence: float = 0.5) -> dict:
    """Very lightweight local-extrema based S/R finder — good enough for
    'is price near a prior swing level' checks, not a substitute for a full
    market-profile engine. Returns lists of candidate support/resistance
    prices sorted by recency."""
    highs = df["high"]
    lows = df["low"]
    resistances, supports = [], []
    for i in range(window, len(df) - window):
        local_high = highs.iloc[i - window:i + window]
        local_low = lows.iloc[i - window:i + window]
        if highs.iloc[i] == local_high.max():
            resistances.append(float(highs.iloc[i]))
        if lows.iloc[i] == local_low.min():
            supports.append(float(lows.iloc[i]))
    return {"resistance": sorted(set(resistances), reverse=True)[:5], "support": sorted(set(supports), reverse=True)[:5]}


def compute_all(df: pd.DataFrame, cfg: dict | None = None) -> dict:
    """Convenience bundle used by the scanner/candidate builder. Returns the
    latest value of each indicator plus a couple of structural levels.
    Gracefully omits anything that can't be computed from insufficient data
    (spec §4: 'Gracefully identify unavailable information')."""
    result: dict = {}
    close = df["close"]

    def last_or_none(series: pd.Series):
        if series is None or series.empty or pd.isna(series.iloc[-1]):
            return None
        return float(series.iloc[-1])

    result["vwap"] = last_or_none(vwap(df))
    result["ema9"] = last_or_none(ema(close, 9))
    result["ema20"] = last_or_none(ema(close, 20))
    result["ema50"] = last_or_none(ema(close, 50)) if len(df) >= 50 else None
    result["sma20"] = last_or_none(sma(close, 20))
    result["sma50"] = last_or_none(sma(close, 50)) if len(df) >= 50 else None
    result["rsi14"] = last_or_none(rsi(close, 14))
    macd_df = macd(close)
    result["macd"] = last_or_none(macd_df["macd"])
    result["macd_signal"] = last_or_none(macd_df["signal"])
    result["atr14"] = last_or_none(atr(df, 14))
    result["high_of_day"] = high_low_of_day(df)["high"]
    result["low_of_day"] = high_low_of_day(df)["low"]

    # ------------------------------------------------------------------
    # Structural / stateful signals the shipped strategies gate on.
    #
    # These were MISSING from compute_all in Milestone 1, and their absence was
    # silent rather than loud: ctx.indicators.get("opening_range") returned
    # None, setup_conditions_met returned False, and the strategy reported
    # "no setup" -- indistinguishable from "conditions genuinely not met". The
    # practical effect was that OpeningRangeBreakout and VwapReclaim were BOTH
    # structurally incapable of ever producing a Setup through the pipeline,
    # while the end-to-end test still passed because its only order-count
    # assertion sat behind an `if orders_submitted > 0` guard.
    #
    # Computed here in deterministic code so a strategy never has to invent a
    # value it was not given.
    # ------------------------------------------------------------------
    cfg = cfg or {}
    bar_minutes = int(cfg.get("bar_minutes", 1))
    result["opening_range"] = opening_range(
        df, int(cfg.get("opening_range_minutes", 15)), bar_minutes=bar_minutes)

    vwap_series = vwap(df)
    result["bars_above_vwap"] = _trailing_bars_above(close, vwap_series)
    result["lost_vwap_recently"] = _lost_vwap_recently(
        close, vwap_series,
        lookback_bars=int(cfg.get("vwap_reclaim_lookback_bars", 30)),
    )
    return result


def _trailing_bars_above(close: pd.Series, vwap_series: pd.Series) -> int:
    """How many consecutive most-recent bars closed above VWAP.

    Returns 0 rather than None when price is not above VWAP: "zero confirming
    bars" is a true statement, whereas None would make a `>=` comparison inside
    a strategy raise and be swallowed as a generic error.
    """
    above = (close > vwap_series).fillna(False).tolist()
    count = 0
    for flag in reversed(above):
        if not flag:
            break
        count += 1
    return count


def _lost_vwap_recently(close: pd.Series, vwap_series: pd.Series,
                        *, lookback_bars: int) -> bool:
    """True when price closed BELOW VWAP within the lookback and is now above it.

    This is the actual "reclaim" precondition. Without the earlier loss, price
    being above VWAP is just an uptrend, not a reclaim; treating the two as the
    same would let the strategy fire on a setup it was never designed for.
    """
    if len(close) < 2:
        return False
    above = (close > vwap_series).fillna(False).tolist()
    if not above[-1]:
        return False
    window = above[-lookback_bars:] if lookback_bars > 0 else above
    return any(not flag for flag in window[:-1])

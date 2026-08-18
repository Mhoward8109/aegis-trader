import numpy as np
import pandas as pd

from app.technical import indicators as ind


def make_bars(n=60, seed=1):
    rng = np.random.RandomState(seed)
    idx = pd.date_range("2026-08-18 09:30", periods=n, freq="1min")
    closes = 100 + np.cumsum(rng.normal(0, 0.3, n))
    highs = closes + abs(rng.normal(0, 0.1, n))
    lows = closes - abs(rng.normal(0, 0.1, n))
    opens = closes - rng.normal(0, 0.1, n)
    vols = rng.uniform(1000, 5000, n)
    return pd.DataFrame({"timestamp": idx, "open": opens, "high": highs, "low": lows,
                          "close": closes, "volume": vols})


def test_vwap_is_between_low_and_high_of_day():
    df = make_bars()
    v = ind.vwap(df)
    assert v.iloc[-1] >= df["low"].min() - 1e-6
    assert v.iloc[-1] <= df["high"].max() + 1e-6


def test_ema_reacts_faster_than_sma_to_a_shock():
    df = make_bars()
    df.loc[df.index[-1], "close"] = df["close"].iloc[-2] + 20  # shock up
    e9 = ind.ema(df["close"], 9).iloc[-1]
    s20 = ind.sma(df["close"], 20).iloc[-1]
    # EMA(9) should move closer to the new price than SMA(20)
    assert abs(e9 - df["close"].iloc[-1]) < abs(s20 - df["close"].iloc[-1])


def test_rsi_bounded_0_100():
    df = make_bars()
    r = ind.rsi(df["close"])
    assert (r.dropna() >= 0).all() and (r.dropna() <= 100).all()


def test_rsi_high_on_strictly_increasing_series():
    closes = pd.Series(np.arange(1, 30, dtype=float))
    r = ind.rsi(closes)
    assert r.iloc[-1] > 90  # monotonic up-move -> RSI near 100


def test_atr_non_negative():
    df = make_bars()
    a = ind.atr(df)
    assert (a.dropna() >= 0).all()


def test_opening_range_uses_only_first_n_minutes():
    df = make_bars()
    orange = ind.opening_range(df, minutes=5, bar_minutes=1)
    assert orange["bars_used"] == 5
    assert orange["high"] == df.iloc[:5]["high"].max()
    assert orange["low"] == df.iloc[:5]["low"].min()


def test_compute_all_handles_short_history_gracefully():
    df = make_bars(n=10)  # too short for ema50/sma50
    result = ind.compute_all(df)
    assert result["ema50"] is None
    assert result["sma50"] is None
    assert result["vwap"] is not None

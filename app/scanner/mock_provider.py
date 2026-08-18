"""
Mock market-data provider — deterministic, offline, used for demo/dev/tests
and for exercising the dashboard end-to-end without any live API key. Never
imported by anything that could run in PAPER/LIVE mode (see app/cli.py wiring).
"""
from __future__ import annotations

import datetime as dt
import random

from app.scanner.base import MarketDataProvider, ScanCriteria, ScanResult

_DEMO_TICKERS = ["AAPL", "TSLA", "NVDA", "SMCI", "AMD", "PLTR", "COIN", "MARA", "SOFI", "RIVN"]


class MockProvider(MarketDataProvider):
    supported_fields = {
        "price_min", "price_max", "pct_change_min", "gap_pct_min", "current_volume_min",
        "avg_daily_volume_min", "rvol_min", "dollar_volume_min", "max_spread_pct",
    }

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)

    def scan(self, criteria: ScanCriteria) -> list[ScanResult]:
        now = dt.datetime.now(dt.timezone.utc)
        out = []
        for t in _DEMO_TICKERS:
            price = round(self._rng.uniform(2, 300), 2)
            pct_change = round(self._rng.uniform(-8, 15), 2)
            rvol = round(self._rng.uniform(0.5, 6.0), 2)
            avg_dollar_vol = round(self._rng.uniform(1_000_000, 80_000_000), 0)
            spread_pct = round(self._rng.uniform(0.02, 0.8), 3)
            fields = {
                "price": price, "pct_change": pct_change, "rvol": rvol,
                "dollar_volume": avg_dollar_vol, "spread_pct": spread_pct,
                "gap_pct": round(self._rng.uniform(-5, 10), 2),
                "avg_daily_volume": round(self._rng.uniform(200_000, 20_000_000)),
                "current_volume": round(self._rng.uniform(100_000, 30_000_000)),
            }
            if criteria.price_min is not None and price < criteria.price_min:
                continue
            if criteria.price_max is not None and price > criteria.price_max:
                continue
            if criteria.rvol_min is not None and rvol < criteria.rvol_min:
                continue
            if criteria.dollar_volume_min is not None and avg_dollar_vol < criteria.dollar_volume_min:
                continue
            if criteria.max_spread_pct is not None and spread_pct > criteria.max_spread_pct:
                continue
            out.append(ScanResult(ticker=t, fields=fields, unavailable_fields=[], data_timestamp=now, source="mock"))
        return out

    def get_bars(self, ticker: str, timeframe: str, start, end):
        """Uses a numpy Generator (seeded from self._rng) for vectorized
        array draws — the plain stdlib `random.Random.uniform(a, b)` used
        for scan() only supports scalar draws, not a `size=` array arg."""
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(self._rng.randint(0, 2**32 - 1))
        n = 60
        idx = pd.date_range(end=dt.datetime.now(dt.timezone.utc), periods=n, freq="1min")
        base = rng.uniform(10, 200)
        closes = base + np.cumsum(rng.uniform(-0.5, 0.5, n))
        highs = closes + abs(rng.uniform(0, 0.3, n))
        lows = closes - abs(rng.uniform(0, 0.3, n))
        opens = closes - rng.uniform(-0.2, 0.2, n)
        vols = rng.uniform(1000, 50000, n)
        return pd.DataFrame({"timestamp": idx, "open": opens, "high": highs, "low": lows,
                              "close": closes, "volume": vols})

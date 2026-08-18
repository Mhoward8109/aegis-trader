"""
Mock market-data provider — deterministic, offline, used for demo/dev/tests
and for exercising the dashboard end-to-end without any live API key. Never
imported by anything that could run in PAPER/LIVE mode (see app/cli.py wiring).

WHY THIS FILE WAS REWRITTEN IN MILESTONE 2
------------------------------------------
The Milestone 1 version drew its prices twice, independently:

    scan():     price = self._rng.uniform(2, 300)
    get_bars(): base  = rng.uniform(10, 200)      # unrelated to `price`

Nothing tied the two together, so for a given ticker the quote could say 230
while the bars sat near 38. Every consumer downstream then combined them: the
strategies took `entry` from the quote and `stop` from bar-derived levels, and
produced setups with stops 84% away from entry. The risk engine sized those to
1-3 shares and the pipeline submitted them.

That made this file the single largest source of false confidence in the test
suite. Any test that used MockProvider to check sizing, stop distance, reward:
risk, or position value was measuring an artifact of two unrelated random draws.

Two changes fix it:

1. **One price per ticker.** A per-ticker anchor price is derived deterministically
   from (seed, ticker) and used by scan(), get_bars(), and get_quote() alike, so
   the quote is consistent with the last bar close by construction.
2. **Explicit drift instead of seed roulette.** The old random walk trended
   whichever way the seed happened to send it, so "does a strategy ever fire?"
   depended on luck. `drift_per_bar` makes the direction a stated parameter.

The mock is still deliberately unrealistic in ways that do not matter (fake
tickers, uniform noise). It is now at least *self-consistent*, which is the
property tests were implicitly relying on.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random

from app.scanner.base import MarketDataProvider, ScanCriteria, ScanResult

_DEMO_TICKERS = ["AAPL", "TSLA", "NVDA", "SMCI", "AMD", "PLTR", "COIN", "MARA", "SOFI", "RIVN"]


class MockProvider(MarketDataProvider):
    supported_fields = {
        "price_min", "price_max", "pct_change_min", "gap_pct_min", "current_volume_min",
        "avg_daily_volume_min", "rvol_min", "dollar_volume_min", "max_spread_pct",
    }

    def __init__(self, seed: int = 42, *, drift_per_bar: float = 0.0,
                 bars: int = 60):
        """
        drift_per_bar
            Deterministic per-bar price drift as a FRACTION of the anchor price.
            0.0 reproduces a directionless walk. A positive value produces a
            reliable uptrend, which is what a breakout/reclaim strategy needs in
            order to fire at all. Stated explicitly so a test asserting "this
            setup triggers" does not depend on which way a seeded walk wandered.
        bars
            Number of 1-minute bars returned by get_bars(). 60 keeps the default
            behaviour; tests that need a longer opening range can raise it.
        """
        self._seed = seed
        self._rng = random.Random(seed)
        self._drift_per_bar = drift_per_bar
        self._bars = bars

    # -- deterministic per-ticker anchor -----------------------------------
    def _anchor_price(self, ticker: str) -> float:
        """The single price this provider believes `ticker` trades at.

        Derived from a hash of (seed, ticker) rather than from self._rng so it
        does NOT depend on how many other calls have advanced the RNG. Without
        that property, get_bars(ticker) would return a different anchor depending
        on whether scan() ran first, and the coherence guarantee would hold only
        by accident.
        """
        digest = hashlib.sha256(f"{self._seed}:{ticker}".encode()).digest()
        # map the first 4 bytes into a plausible equity price range
        raw = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return round(5.0 + raw * 295.0, 2)

    def _ticker_rng(self, ticker: str, purpose: str) -> random.Random:
        """A stable RNG per (seed, ticker, purpose) — order-independent, like the anchor."""
        return random.Random(f"{self._seed}:{ticker}:{purpose}")

    def scan(self, criteria: ScanCriteria) -> list[ScanResult]:
        now = dt.datetime.now(dt.timezone.utc)
        out = []
        for t in _DEMO_TICKERS:
            rng = self._ticker_rng(t, "scan")
            price = self._anchor_price(t)
            pct_change = round(rng.uniform(-8, 15), 2)
            rvol = round(rng.uniform(0.5, 6.0), 2)
            avg_dollar_vol = round(rng.uniform(1_000_000, 80_000_000), 0)
            spread_pct = round(rng.uniform(0.02, 0.8), 3)
            fields = {
                "price": price, "pct_change": pct_change, "rvol": rvol,
                "dollar_volume": avg_dollar_vol, "spread_pct": spread_pct,
                "gap_pct": round(rng.uniform(-5, 10), 2),
                "avg_daily_volume": round(rng.uniform(200_000, 20_000_000)),
                "current_volume": round(rng.uniform(100_000, 30_000_000)),
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
            out.append(ScanResult(ticker=t, fields=fields, unavailable_fields=[],
                                  data_timestamp=now, source="mock"))
        return out

    def get_bars(self, ticker: str, timeframe: str, start, end):
        """Bars whose LAST CLOSE lands on the ticker's anchor price.

        Built backwards from the anchor rather than forwards from an arbitrary
        base, so `bars["close"].iloc[-1] ≈ scan()["price"] ≈ get_quote().last` by
        construction. That equality is what the pipeline's quote/bar coherence
        gate checks, and it is the property the Milestone 1 mock violated.
        """
        import numpy as np
        import pandas as pd

        anchor = self._anchor_price(ticker)
        n = self._bars
        rng = np.random.default_rng(
            int.from_bytes(hashlib.sha256(f"{self._seed}:{ticker}:bars".encode()).digest()[:8], "big")
            % (2**32))

        idx = pd.date_range(end=dt.datetime.now(dt.timezone.utc), periods=n, freq="1min")

        # Noise scaled to the anchor so a $6 stock and a $300 stock get
        # comparable PERCENTAGE volatility. The old version used a flat +/-0.5
        # absolute step, which is a rounding error on a $300 name and a 10%
        # move on a $6 one.
        noise_scale = anchor * 0.001
        steps = rng.uniform(-noise_scale, noise_scale, n)
        steps += anchor * self._drift_per_bar

        # cumulative path, then shifted so the FINAL close equals the anchor
        path = np.cumsum(steps)
        closes = anchor + (path - path[-1])

        wick = anchor * 0.0008
        highs = closes + np.abs(rng.uniform(0, wick, n))
        lows = closes - np.abs(rng.uniform(0, wick, n))
        opens = closes - rng.uniform(-wick, wick, n)
        vols = rng.uniform(1000, 50000, n)

        # Guarantee the OHLC relationships the indicators assume, rather than
        # hoping the noise happened to respect them.
        highs = np.maximum.reduce([highs, closes, opens])
        lows = np.minimum.reduce([lows, closes, opens])

        return pd.DataFrame({"timestamp": idx, "open": opens, "high": highs,
                             "low": lows, "close": closes, "volume": vols})

    def get_quote(self, ticker: str):
        """A quote consistent with the anchor and with the last bar close.

        Returned as a Quote so callers do not have to special-case the mock.
        """
        from app.broker.base import Quote

        anchor = self._anchor_price(ticker)
        rng = self._ticker_rng(ticker, "quote")
        half_spread = anchor * rng.uniform(0.0001, 0.0015)
        return Quote(
            ticker=ticker,
            bid=round(anchor - half_spread, 4),
            ask=round(anchor + half_spread, 4),
            last=anchor,
            timestamp=dt.datetime.now(dt.timezone.utc),
            # Labelled "mock" so a quote from this provider is identifiable in
            # the journal and can never be mistaken for a real feed after the fact.
            source="mock",
        )

"""Build a market regime from real snapshot inputs without flat fallbacks."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.marketdata.freshness import check_freshness
from app.marketdata.regime import RegimeSnapshot, build_regime_snapshot

REQUIRED_SYMBOLS = ("SPY", "QQQ", "IWM")
DEFAULT_OPTIONAL_SYMBOLS = ("VIX", "VIXY")


class MarketRegimeEngine:
    """Construct a regime from SPY/QQQ/IWM snapshots.

    Required percent changes are calculated exclusively as
    ``daily_bar.close`` versus ``previous_daily_bar.close``.  If a required
    snapshot, daily bar, prior close, or real API timestamp is missing/stale,
    the returned regime is explicitly UNKNOWN; the engine never injects a
    zero/flat stand-in.  Optional VIX/VIXY and sector inputs are fetched only as
    supplemental context and cannot make a missing required input acceptable.
    """

    def __init__(
        self,
        snapshot_source: Any,
        *,
        max_age_seconds: float = 300.0,
        optional_symbols: Iterable[str] = DEFAULT_OPTIONAL_SYMBOLS,
        sector_symbols: Iterable[str] = (),
    ) -> None:
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative")
        self.snapshot_source = snapshot_source
        self.max_age_seconds = max_age_seconds
        self.optional_symbols = tuple(dict.fromkeys(symbol.upper() for symbol in optional_symbols))
        self.sector_symbols = tuple(dict.fromkeys(symbol.upper() for symbol in sector_symbols))

    def build(self, *, now: dt.datetime | None = None) -> RegimeSnapshot:
        """Return a valid snapshot or a conservatively unknown one."""
        if now is None:
            now = dt.datetime.now(dt.timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=dt.timezone.utc)
        symbols = REQUIRED_SYMBOLS + self.optional_symbols + self.sector_symbols
        try:
            snapshots = self._fetch_snapshots(symbols)
        except Exception as exc:
            return self._unknown(f"snapshot fetch failed: {type(exc).__name__}")

        values: dict[str, tuple[float, Any]] = {}
        for symbol in REQUIRED_SYMBOLS:
            snapshot = snapshots.get(symbol)
            parsed = self._required_values(snapshot, now)
            if parsed is None:
                return self._unknown(f"required {symbol} snapshot missing, malformed, or stale")
            values[symbol] = parsed

        spy_pct, spy_daily = values["SPY"]
        qqq_pct, _ = values["QQQ"]
        iwm_pct, _ = values["IWM"]
        vix_level = self._vix_level(snapshots.get("VIX"), now)
        spy_range_pct = self._range_pct(spy_daily)
        if spy_range_pct is None:
            return self._unknown("required SPY daily range missing or malformed")
        as_of = self._as_of(values.values())
        return build_regime_snapshot(
            spy_pct=spy_pct,
            qqq_pct=qqq_pct,
            iwm_pct=iwm_pct,
            vix_level=vix_level,
            breadth=None,
            spy_range_pct=spy_range_pct,
            as_of=as_of,
        )

    def _fetch_snapshots(self, symbols: tuple[str, ...]) -> Mapping[str, Any]:
        source = self.snapshot_source
        if hasattr(source, "get_snapshots"):
            result = source.get_snapshots(symbols)
        elif callable(source):
            result = source(symbols)
        else:
            raise TypeError("snapshot_source must expose get_snapshots(symbols) or be callable")
        if not isinstance(result, Mapping):
            raise TypeError("snapshot source returned a non-mapping")
        return {str(symbol).upper(): snapshot for symbol, snapshot in result.items()}

    def _required_values(self, snapshot: Any, now: dt.datetime) -> tuple[float, Any] | None:
        if snapshot is None:
            return None
        daily = getattr(snapshot, "daily_bar", None)
        previous = getattr(snapshot, "previous_daily_bar", None)
        close = self._number(getattr(daily, "close", None))
        previous_close = self._number(getattr(previous, "close", None))
        if daily is None or previous is None or close is None or previous_close is None or previous_close == 0:
            return None
        timestamp = self._snapshot_timestamp(snapshot)
        if timestamp is None:
            return None
        if not check_freshness("market-regime snapshot", timestamp, self.max_age_seconds, now).fresh:
            return None
        return ((close - previous_close) / previous_close * 100, daily)

    def _vix_level(self, snapshot: Any, now: dt.datetime) -> float | None:
        """Use actual VIX only; VIXY's ETF price is not a VIX level substitute."""
        if snapshot is None:
            return None
        timestamp = self._snapshot_timestamp(snapshot)
        if timestamp is None or not check_freshness("VIX snapshot", timestamp, self.max_age_seconds, now).fresh:
            return None
        trade = self._number(getattr(getattr(snapshot, "latest_trade", None), "price", None))
        return trade if trade is not None else self._number(getattr(getattr(snapshot, "daily_bar", None), "close", None))

    @staticmethod
    def _range_pct(daily_bar: Any) -> float | None:
        high = MarketRegimeEngine._number(getattr(daily_bar, "high", None))
        low = MarketRegimeEngine._number(getattr(daily_bar, "low", None))
        close = MarketRegimeEngine._number(getattr(daily_bar, "close", None))
        if high is None or low is None or close is None or close == 0:
            return None
        return ((high - low) / close) * 100

    @staticmethod
    def _snapshot_timestamp(snapshot: Any) -> dt.datetime | None:
        candidates = (
            getattr(getattr(snapshot, "latest_trade", None), "timestamp", None),
            getattr(getattr(snapshot, "latest_quote", None), "timestamp", None),
            getattr(getattr(snapshot, "minute_bar", None), "timestamp", None),
            getattr(getattr(snapshot, "daily_bar", None), "timestamp", None),
        )
        valid = []
        for timestamp in candidates:
            if isinstance(timestamp, dt.datetime):
                valid.append(timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=dt.timezone.utc))
        return max(valid) if valid else None

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None

    @staticmethod
    def _as_of(values: Iterable[tuple[float, Any]]) -> str:
        timestamps = [getattr(daily, "timestamp", None) for _, daily in values]
        real = [timestamp for timestamp in timestamps if isinstance(timestamp, dt.datetime)]
        return max(real).isoformat() if real else "unknown"

    @staticmethod
    def _unknown(reason: str) -> RegimeSnapshot:
        """Explicitly unknown fields prevent a neutral/flat safety bypass."""
        return RegimeSnapshot(
            spy_direction="unknown",
            qqq_direction="unknown",
            iwm_direction="unknown",
            vix_level=None,
            vix_regime="unknown",
            breadth=None,
            trend_vs_range="unknown",
            risk_on_off="unknown",
            as_of=f"unknown: {reason}",
        )


# Short alias for callers that prefer a generic engine name.
RegimeEngine = MarketRegimeEngine

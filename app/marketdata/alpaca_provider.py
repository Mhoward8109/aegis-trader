"""Alpaca market-data adapter with explicit data-quality disclosures.

The Basic Alpaca feed is IEX-only.  IEX volume is not consolidated-tape volume,
so all volume-based values (including client-side derived average daily volume,
relative volume, and dollar volume) are marked ``DEGRADED``.  Alpaca does not
expose average daily volume, relative volume, or premarket high/low as snapshot
fields: this adapter derives the first two from daily bars only when requested;
premarket high/low would require filtering minute bars client-side and is not
returned by ``scan``.
"""
from __future__ import annotations

import datetime as dt
import enum
import os
from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame

from app.scanner.base import MarketDataProvider, ScanCriteria, ScanResult


class FieldAvailability(str, enum.Enum):
    """Whether a returned value exists and is suitable for its named purpose."""

    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class MarketDataUnavailableError(RuntimeError):
    """Raised when a market-data response cannot safely be used."""


class AlpacaMarketDataProvider(MarketDataProvider):
    """IEX-backed Alpaca provider.

    ``scan`` is intentionally limited to a caller-provided universe; Alpaca's
    snapshot endpoint is a symbol lookup, not a complete market scanner.  One
    ``StockSnapshotRequest`` is made for the requested universe, yielding the
    latest trade, quote, minute bar, daily bar, and previous daily bar per
    symbol.  No credential is read from a configuration file.
    """

    supported_fields = {
        "price_min",
        "price_max",
        "pct_change_min",
        "gap_pct_min",
        "current_volume_min",
        "avg_daily_volume_min",
        "rvol_min",
        "dollar_volume_min",
        "max_spread_pct",
    }

    _BASE_AVAILABILITY = {
        "price": FieldAvailability.AVAILABLE,
        "pct_change": FieldAvailability.AVAILABLE,
        "gap_pct": FieldAvailability.AVAILABLE,
        "spread_pct": FieldAvailability.AVAILABLE,
    }

    def __init__(
        self,
        symbols: Iterable[str] = (),
        *,
        client: StockHistoricalDataClient | None = None,
        feed: DataFeed = DataFeed.IEX,
        average_volume_lookback: int = 20,
    ) -> None:
        self.symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        self.feed = feed
        self.average_volume_lookback = average_volume_lookback
        if average_volume_lookback < 1:
            raise ValueError("average_volume_lookback must be at least 1")
        self.client = client or self._client_from_environment()

    @staticmethod
    def _client_from_environment() -> StockHistoricalDataClient:
        key = os.getenv("ALPACA_PAPER_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
        secret = os.getenv("ALPACA_PAPER_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError(
                "Alpaca market data requires ALPACA_PAPER_API_KEY_ID and "
                "ALPACA_PAPER_API_SECRET_KEY (or ALPACA_API_KEY_ID and "
                "ALPACA_API_SECRET_KEY). Credentials are read only from environment variables."
            )
        return StockHistoricalDataClient(api_key=key, secret_key=secret)

    def field_availability(self) -> dict[str, FieldAvailability]:
        """Return baseline availability; per-result metadata records actual gaps."""
        availability = dict(self._BASE_AVAILABILITY)
        # Basic accounts use this class's default IEX feed.  Its volume is a
        # small venue subset, not consolidated-tape volume.  A caller that
        # deliberately selects a consolidated feed gets an accurate (possibly
        # delayed) tape volume rather than an incorrect IEX warning.
        volume_status = FieldAvailability.DEGRADED if self.feed == DataFeed.IEX else FieldAvailability.AVAILABLE
        availability.update(
            {
                "current_volume": volume_status,
                "dollar_volume": volume_status,
                "avg_daily_volume": volume_status,
                "rvol": volume_status,
                "premarket_high": volume_status,
                "premarket_low": volume_status,
            }
        )
        return availability

    def get_snapshots(self, symbols: Iterable[str]) -> dict[str, Any]:
        """Fetch snapshots in one request, returning only API-supplied objects.

        A snapshot includes ``latest_trade``, ``latest_quote``, ``minute_bar``,
        ``daily_bar``, and ``previous_daily_bar``.  It is also the input used by
        :class:`app.marketdata.regime_engine.MarketRegimeEngine`.
        """
        requested = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not requested:
            return {}
        try:
            response = self.client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=list(requested), feed=self.feed)
            )
        except Exception as exc:
            raise MarketDataUnavailableError("Alpaca snapshot request failed; refusing to use substitute data") from exc
        if not isinstance(response, Mapping):
            raise MarketDataUnavailableError("Alpaca snapshot response was malformed")
        return {symbol: response.get(symbol) for symbol in requested if response.get(symbol) is not None}

    def scan(self, criteria: ScanCriteria) -> list[ScanResult]:
        """Evaluate only values actually present in IEX snapshots/bars.

        If Alpaca times out, returns malformed data, or supplies no real API
        timestamp, this returns no candidates rather than inventing a current
        value or timestamp.  Volume values remain explicitly ``DEGRADED``.
        """
        if not self.symbols:
            return []
        try:
            snapshots = self.get_snapshots(self.symbols)
        except MarketDataUnavailableError:
            return []

        need_volume_history = criteria.avg_daily_volume_min is not None or criteria.rvol_min is not None
        historical_volumes = self._average_daily_volumes(self.symbols) if need_volume_history else {}
        results: list[ScanResult] = []
        for symbol in self.symbols:
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                continue
            try:
                result = self._scan_result(symbol, snapshot, need_volume_history, historical_volumes.get(symbol))
            except (TypeError, ValueError, AttributeError):
                # A malformed model is not evidence for a trade candidate.
                continue
            if result is not None and self._matches(result.fields, criteria):
                results.append(result)
        return results

    def _scan_result(
        self,
        symbol: str,
        snapshot: Any,
        need_volume_history: bool,
        average_volume: float | None,
    ) -> ScanResult | None:
        latest_trade = getattr(snapshot, "latest_trade", None)
        latest_quote = getattr(snapshot, "latest_quote", None)
        daily_bar = getattr(snapshot, "daily_bar", None)
        previous_daily_bar = getattr(snapshot, "previous_daily_bar", None)
        timestamp = self._snapshot_timestamp(latest_trade, latest_quote, getattr(snapshot, "minute_bar", None), daily_bar)
        if timestamp is None:
            return None

        availability = self.field_availability()
        price = self._number(getattr(latest_trade, "price", None))
        if price is None:
            availability["price"] = FieldAvailability.UNAVAILABLE

        daily_close = self._number(getattr(daily_bar, "close", None))
        previous_close = self._number(getattr(previous_daily_bar, "close", None))
        daily_open = self._number(getattr(daily_bar, "open", None))
        pct_change = self._pct_change(daily_close, previous_close)
        gap_pct = self._pct_change(daily_open, previous_close)
        if pct_change is None:
            availability["pct_change"] = FieldAvailability.UNAVAILABLE
        if gap_pct is None:
            availability["gap_pct"] = FieldAvailability.UNAVAILABLE

        current_volume = self._number(getattr(daily_bar, "volume", None))
        if current_volume is None:
            availability["current_volume"] = FieldAvailability.UNAVAILABLE
        dollar_volume = price * current_volume if price is not None and current_volume is not None else None
        if dollar_volume is None:
            availability["dollar_volume"] = FieldAvailability.UNAVAILABLE

        bid = self._number(getattr(latest_quote, "bid_price", None))
        ask = self._number(getattr(latest_quote, "ask_price", None))
        spread_pct = None
        if bid is not None and ask is not None and bid >= 0 and ask > 0:
            midpoint = (bid + ask) / 2
            if midpoint > 0:
                spread_pct = ((ask - bid) / midpoint) * 100
        if spread_pct is None:
            availability["spread_pct"] = FieldAvailability.UNAVAILABLE

        rvol = None
        if need_volume_history:
            if average_volume is None:
                availability["avg_daily_volume"] = FieldAvailability.UNAVAILABLE
                availability["rvol"] = FieldAvailability.UNAVAILABLE
            elif current_volume is not None:
                rvol = current_volume / average_volume
            else:
                availability["rvol"] = FieldAvailability.UNAVAILABLE
        else:
            availability["avg_daily_volume"] = FieldAvailability.UNAVAILABLE
            availability["rvol"] = FieldAvailability.UNAVAILABLE

        fields = {
            "price": price,
            "pct_change": pct_change,
            "gap_pct": gap_pct,
            "current_volume": current_volume,
            "dollar_volume": dollar_volume,
            "spread_pct": spread_pct,
            "avg_daily_volume": average_volume,
            "rvol": rvol,
            "field_availability": availability,
        }
        return ScanResult(
            ticker=symbol,
            fields=fields,
            unavailable_fields=sorted(name for name, status in availability.items() if status is FieldAvailability.UNAVAILABLE),
            data_timestamp=timestamp,
            source=self._source_name,
        )

    def _average_daily_volume(self, symbol: str) -> float | None:
        """Derive historical ADV from daily IEX bars; never substitute zero."""
        return self._average_daily_volumes((symbol.upper(),)).get(symbol.upper())

    def _average_daily_volumes(self, symbols: Iterable[str]) -> dict[str, float | None]:
        """Derive ADV in one batched daily-bar request to conserve API calls."""
        requested = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not requested:
            return {}
        try:
            response = self.client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=list(requested),
                    timeframe=TimeFrame.Day,
                    # Request one extra daily bar so the current, incomplete
                    # session does not contaminate a historical ADV/RVOL base.
                    limit=self.average_volume_lookback + 1,
                    feed=self.feed,
                )
            )
        except Exception:
            return {symbol: None for symbol in requested}
        averages: dict[str, float | None] = {}
        for symbol in requested:
            try:
                bars = self._bars_for_symbol(response, symbol)
            except (TypeError, ValueError, AttributeError):
                averages[symbol] = None
                continue
            dated_bars = [
                (self._api_timestamp(getattr(bar, "timestamp", None)), bar)
                for bar in bars
            ]
            dated_bars = [(timestamp, bar) for timestamp, bar in dated_bars if timestamp is not None]
            if len(dated_bars) < 2:
                averages[symbol] = None
                continue
            historical = [
                bar
                for _, bar in sorted(dated_bars, key=lambda item: item[0])[:-1][
                    -self.average_volume_lookback :
                ]
            ]
            volumes = [self._number(getattr(bar, "volume", None)) for bar in historical]
            valid = [volume for volume in volumes if volume is not None]
            averages[symbol] = sum(valid) / len(valid) if valid else None
        return averages

    def get_bars(self, ticker: str, timeframe: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        """Return API bars with their API timestamps and availability metadata.

        ``timestamp`` is copied exclusively from each Alpaca ``Bar.timestamp``.
        ``data_timestamp`` is the newest API bar timestamp, not wall-clock time.
        With the default Basic IEX feed, volumes are marked ``DEGRADED``.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        try:
            response = self.client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=ticker.upper(),
                    timeframe=self._timeframe(timeframe),
                    start=start,
                    end=end,
                    feed=self.feed,
                )
            )
            bars = self._bars_for_symbol(response, ticker.upper())
        except Exception as exc:
            raise MarketDataUnavailableError("Alpaca bar request failed; no bars were fabricated") from exc

        rows = []
        for bar in bars:
            timestamp = self._api_timestamp(getattr(bar, "timestamp", None))
            if timestamp is None:
                continue
            values = {name: self._number(getattr(bar, name, None)) for name in ("open", "high", "low", "close", "volume")}
            if any(values[name] is None for name in ("open", "high", "low", "close")):
                continue
            rows.append({"timestamp": timestamp, **values})
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        availability = {column: FieldAvailability.AVAILABLE for column in ("open", "high", "low", "close")}
        availability["volume"] = FieldAvailability.DEGRADED if self.feed == DataFeed.IEX else FieldAvailability.AVAILABLE
        frame.attrs["field_availability"] = availability
        frame.attrs["data_timestamp"] = max(frame["timestamp"]) if not frame.empty else None
        frame.attrs["source"] = self._source_name
        return frame

    def get_premarket_high_low(self, ticker: str, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
        """Derive premarket high/low from API minute bars, never from defaults.

        Alpaca has no ``premarket_high``/``premarket_low`` response fields.
        This method filters returned one-minute bars to 04:00–09:30 Eastern.
        If that interval has no timestamped bars, both values are explicitly
        unavailable.  On the default IEX feed the result is venue-only and
        therefore ``DEGRADED`` rather than a consolidated premarket range.
        """
        bars = self.get_bars(ticker, "1min", start, end)
        if bars.empty:
            return {
                "premarket_high": None,
                "premarket_low": None,
                "data_timestamp": None,
                "field_availability": {
                    "premarket_high": FieldAvailability.UNAVAILABLE,
                    "premarket_low": FieldAvailability.UNAVAILABLE,
                },
            }
        # Convert explicitly via zoneinfo so daylight-saving transitions are
        # respected rather than treating Eastern time as a fixed UTC offset.
        from zoneinfo import ZoneInfo

        eastern_times = bars["timestamp"].map(lambda timestamp: timestamp.astimezone(ZoneInfo("America/New_York")).time())
        in_premarket = (eastern_times >= dt.time(4)) & (eastern_times < dt.time(9, 30))
        premarket = bars.loc[in_premarket]
        if premarket.empty:
            return {
                "premarket_high": None,
                "premarket_low": None,
                "data_timestamp": None,
                "field_availability": {
                    "premarket_high": FieldAvailability.UNAVAILABLE,
                    "premarket_low": FieldAvailability.UNAVAILABLE,
                },
            }
        status = FieldAvailability.DEGRADED if self.feed == DataFeed.IEX else FieldAvailability.AVAILABLE
        return {
            "premarket_high": float(premarket["high"].max()),
            "premarket_low": float(premarket["low"].min()),
            "data_timestamp": max(premarket["timestamp"]),
            "field_availability": {"premarket_high": status, "premarket_low": status},
        }

    @staticmethod
    def _bars_for_symbol(response: Any, symbol: str) -> list[Any]:
        data = getattr(response, "data", response)
        if not isinstance(data, Mapping):
            raise ValueError("bar response is not a mapping")
        bars = data.get(symbol, [])
        return list(bars) if isinstance(bars, (list, tuple)) else []

    @property
    def _source_name(self) -> str:
        feed_name = getattr(self.feed, "value", str(self.feed))
        return f"alpaca-{feed_name}"

    @staticmethod
    def _timeframe(value: str) -> TimeFrame:
        normalized = value.strip().lower()
        choices = {
            "1min": TimeFrame.Minute,
            "minute": TimeFrame.Minute,
            "1hour": TimeFrame.Hour,
            "hour": TimeFrame.Hour,
            "1day": TimeFrame.Day,
            "day": TimeFrame.Day,
            "1week": TimeFrame.Week,
            "week": TimeFrame.Week,
            "1month": TimeFrame.Month,
            "month": TimeFrame.Month,
        }
        if normalized not in choices:
            raise ValueError(f"unsupported Alpaca timeframe: {value!r}")
        return choices[normalized]

    @staticmethod
    def _api_timestamp(value: Any) -> dt.datetime | None:
        if not isinstance(value, dt.datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)

    @classmethod
    def _snapshot_timestamp(cls, *models: Any) -> dt.datetime | None:
        timestamps = [cls._api_timestamp(getattr(model, "timestamp", None)) for model in models if model is not None]
        real = [timestamp for timestamp in timestamps if timestamp is not None]
        return max(real) if real else None

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None  # Reject NaN without a default.

    @staticmethod
    def _pct_change(current: float | None, previous: float | None) -> float | None:
        if current is None or previous is None or previous == 0:
            return None
        return ((current - previous) / previous) * 100

    @staticmethod
    def _matches(fields: dict[str, Any], criteria: ScanCriteria) -> bool:
        checks = (
            (criteria.price_min, fields["price"], lambda value, limit: value >= limit),
            (criteria.price_max, fields["price"], lambda value, limit: value <= limit),
            (criteria.pct_change_min, fields["pct_change"], lambda value, limit: value >= limit),
            (criteria.gap_pct_min, fields["gap_pct"], lambda value, limit: value >= limit),
            (criteria.current_volume_min, fields["current_volume"], lambda value, limit: value >= limit),
            (criteria.avg_daily_volume_min, fields["avg_daily_volume"], lambda value, limit: value >= limit),
            (criteria.rvol_min, fields["rvol"], lambda value, limit: value >= limit),
            (criteria.dollar_volume_min, fields["dollar_volume"], lambda value, limit: value >= limit),
            (criteria.max_spread_pct, fields["spread_pct"], lambda value, limit: value <= limit),
        )
        for requested, actual, comparison in checks:
            if requested is not None and (actual is None or not comparison(actual, requested)):
                return False
        return True

"""Pure unit tests for market-data safety properties; no network clients are used."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from app.marketdata.alpaca_provider import AlpacaMarketDataProvider, FieldAvailability, MarketDataUnavailableError
from app.marketdata.regime_engine import MarketRegimeEngine
from app.marketdata.session import (
    AFTER_HOURS,
    EARLY_CLOSE,
    HOLIDAY,
    PREMARKET,
    REGULAR,
    UNKNOWN,
    MarketSessionService,
)
from app.scanner.base import ScanCriteria

UTC = dt.timezone.utc
ET = ZoneInfo("America/New_York")


def bar(timestamp, *, open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0):
    return NS(timestamp=timestamp, open=open, high=high, low=low, close=close, volume=volume)


def snapshot(timestamp, *, close=101.0, previous_close=100.0, volume=1000.0, price=101.0):
    return NS(
        latest_trade=NS(timestamp=timestamp, price=price),
        latest_quote=NS(timestamp=timestamp, bid_price=100.9, ask_price=101.1),
        minute_bar=bar(timestamp, close=price, volume=100),
        daily_bar=bar(timestamp, close=close, volume=volume),
        previous_daily_bar=bar(timestamp - dt.timedelta(days=1), close=previous_close, volume=900),
    )


class FakeDataClient:
    def __init__(self, snapshots=None, bars=None, error=None):
        self.snapshots = snapshots or {}
        self.bars = bars or {}
        self.error = error
        self.snapshot_requests = []

    def get_stock_snapshot(self, request):
        self.snapshot_requests.append(request)
        if self.error:
            raise self.error
        return self.snapshots

    def get_stock_bars(self, request):
        if self.error:
            raise self.error
        symbol = request.symbol_or_symbols
        if isinstance(symbol, list):
            return {ticker: self.bars.get(ticker, []) for ticker in symbol}
        return {symbol: self.bars.get(symbol, [])}


def test_iex_volume_is_marked_degraded():
    api_time = dt.datetime(2026, 8, 18, 14, 31, tzinfo=UTC)
    provider = AlpacaMarketDataProvider(["SPY"], client=FakeDataClient({"SPY": snapshot(api_time)}))

    results = provider.scan(ScanCriteria())

    assert len(results) == 1
    availability = results[0].fields["field_availability"]
    assert availability["current_volume"] is FieldAvailability.DEGRADED
    assert availability["dollar_volume"] is FieldAvailability.DEGRADED
    assert provider.field_availability()["rvol"] is FieldAvailability.DEGRADED


def test_rvol_is_derived_from_historical_daily_bars_without_current_session_volume():
    api_time = dt.datetime(2026, 8, 18, 14, 31, tzinfo=UTC)
    historical = [
        bar(api_time - dt.timedelta(days=offset), volume=1000 if offset == 0 else 100)
        for offset in range(21)
    ]
    provider = AlpacaMarketDataProvider(
        ["SPY"],
        client=FakeDataClient({"SPY": snapshot(api_time, volume=1000)}, bars={"SPY": historical}),
        average_volume_lookback=20,
    )

    result = provider.scan(ScanCriteria(rvol_min=5))[0]

    assert result.fields["avg_daily_volume"] == 100
    assert result.fields["rvol"] == 10
    assert result.fields["field_availability"]["rvol"] is FieldAvailability.DEGRADED


def test_provider_uses_api_timestamp_not_wall_clock():
    api_time = dt.datetime(2022, 1, 3, 15, 59, tzinfo=UTC)
    provider = AlpacaMarketDataProvider(["SPY"], client=FakeDataClient({"SPY": snapshot(api_time)}))

    result = provider.scan(ScanCriteria())[0]

    assert result.data_timestamp == api_time
    assert result.data_timestamp != dt.datetime.now(UTC)


def test_missing_quote_marks_spread_unavailable_without_inventing_value():
    api_time = dt.datetime(2026, 8, 18, 14, 31, tzinfo=UTC)
    data = snapshot(api_time)
    data.latest_quote = None
    provider = AlpacaMarketDataProvider(["SPY"], client=FakeDataClient({"SPY": data}))

    result = provider.scan(ScanCriteria())[0]

    assert result.fields["spread_pct"] is None
    assert result.fields["field_availability"]["spread_pct"] is FieldAvailability.UNAVAILABLE
    assert "spread_pct" in result.unavailable_fields


def test_missing_timestamp_refuses_candidate_instead_of_using_wall_clock():
    data = snapshot(dt.datetime(2026, 8, 18, 14, 31, tzinfo=UTC))
    data.latest_trade.timestamp = None
    data.latest_quote.timestamp = None
    data.minute_bar.timestamp = None
    data.daily_bar.timestamp = None
    provider = AlpacaMarketDataProvider(["SPY"], client=FakeDataClient({"SPY": data}))

    assert provider.scan(ScanCriteria()) == []


def test_provider_timeout_returns_no_candidates():
    provider = AlpacaMarketDataProvider(["SPY"], client=FakeDataClient(error=TimeoutError("timed out")))

    assert provider.scan(ScanCriteria()) == []


def test_malformed_snapshot_refuses_candidate():
    provider = AlpacaMarketDataProvider(["SPY"], client=FakeDataClient({"SPY": object()}))

    assert provider.scan(ScanCriteria()) == []


def test_bar_request_exception_is_explicit_not_synthetic():
    provider = AlpacaMarketDataProvider(["SPY"], client=FakeDataClient(error=TimeoutError("timed out")))
    with pytest.raises(MarketDataUnavailableError):
        provider.get_bars("SPY", "1min", dt.datetime(2026, 8, 18, tzinfo=UTC), dt.datetime(2026, 8, 19, tzinfo=UTC))


def test_regime_is_unknown_when_spy_missing():
    now = dt.datetime(2026, 8, 18, 14, 31, tzinfo=UTC)
    source = {"QQQ": snapshot(now), "IWM": snapshot(now)}

    regime = MarketRegimeEngine(lambda _: source).build(now=now)

    assert regime.spy_direction == "unknown"
    assert regime.risk_on_off == "unknown"
    assert regime.as_of.startswith("unknown:")


def test_regime_is_unknown_when_required_data_is_stale():
    now = dt.datetime(2026, 8, 18, 14, 31, tzinfo=UTC)
    stale = now - dt.timedelta(minutes=6)
    source = {symbol: snapshot(stale) for symbol in ("SPY", "QQQ", "IWM")}

    regime = MarketRegimeEngine(lambda _: source, max_age_seconds=300).build(now=now)

    assert regime.spy_direction == "unknown"
    assert regime.trend_vs_range == "unknown"


def test_regime_percent_change_comes_from_daily_bar_and_previous_daily_bar():
    now = dt.datetime(2026, 8, 18, 14, 31, tzinfo=UTC)
    source = {
        "SPY": snapshot(now, close=102, previous_close=100, price=1),
        "QQQ": snapshot(now, close=98, previous_close=100, price=999),
        "IWM": snapshot(now, close=100, previous_close=100, price=2),
    }

    regime = MarketRegimeEngine(lambda _: source).build(now=now)

    assert regime.spy_direction == "up"
    assert regime.qqq_direction == "down"
    assert regime.iwm_direction == "flat"


def make_session_service(timestamp, *, is_open, calendar_entries):
    return MarketSessionService(
        clock_source=lambda: NS(timestamp=timestamp, is_open=is_open),
        calendar_source=lambda _: calendar_entries,
    )


def calendar_entry(date, open_time=dt.time(9, 30), close_time=dt.time(16)):
    return NS(
        date=date,
        open=dt.datetime.combine(date, open_time, tzinfo=ET),
        close=dt.datetime.combine(date, close_time, tzinfo=ET),
    )


def test_unknown_session_does_not_permit_orders():
    service = MarketSessionService(clock_source=lambda: (_ for _ in ()).throw(TimeoutError()), calendar_source=lambda _: [])

    state = service.current_session()

    assert state.session == UNKNOWN
    assert state.is_unknown
    assert not service.permits_orders(state, [REGULAR, PREMARKET])


def test_early_close_detected_from_calendar_close_time():
    date = dt.date(2026, 11, 27)
    clock_time = dt.datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 ET
    service = make_session_service(clock_time, is_open=True, calendar_entries=[calendar_entry(date, close_time=dt.time(13))])

    state = service.current_session()

    assert state.session == EARLY_CLOSE
    assert state.scheduled_close == dt.datetime(2026, 11, 27, 13, 0, tzinfo=ET)


def test_calendar_absence_is_holiday_not_normal_weekday_session():
    timestamp = dt.datetime(2026, 12, 25, 16, 0, tzinfo=UTC)
    service = make_session_service(timestamp, is_open=False, calendar_entries=[])

    assert service.current_session().session == HOLIDAY


def test_calendar_drives_premarket_and_after_hours_classification():
    date = dt.date(2026, 8, 18)
    premarket = make_session_service(dt.datetime(2026, 8, 18, 12, 30, tzinfo=UTC), is_open=False, calendar_entries=[calendar_entry(date)])
    after_hours = make_session_service(dt.datetime(2026, 8, 18, 21, 0, tzinfo=UTC), is_open=False, calendar_entries=[calendar_entry(date)])

    assert premarket.current_session().session == PREMARKET
    assert after_hours.current_session().session == AFTER_HOURS

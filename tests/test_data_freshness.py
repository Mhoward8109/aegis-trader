import datetime as dt

import pytest

from app.marketdata.freshness import StaleDataError, assert_fresh, check_freshness


def test_fresh_data_passes():
    now = dt.datetime(2026, 8, 18, 15, 30, tzinfo=dt.timezone.utc)
    ts = now - dt.timedelta(seconds=2)
    check = check_freshness("quotes", ts, max_age_seconds=5, now=now)
    assert check.fresh


def test_stale_data_fails_closed():
    now = dt.datetime(2026, 8, 18, 15, 30, tzinfo=dt.timezone.utc)
    ts = now - dt.timedelta(seconds=30)
    check = check_freshness("quotes", ts, max_age_seconds=5, now=now)
    assert not check.fresh


def test_assert_fresh_raises_on_stale_data():
    now = dt.datetime(2026, 8, 18, 15, 30, tzinfo=dt.timezone.utc)
    ts = now - dt.timedelta(seconds=100)
    with pytest.raises(StaleDataError):
        assert_fresh("bars", ts, max_age_seconds=60, now=now)


def test_assert_fresh_passes_silently_when_fresh():
    now = dt.datetime(2026, 8, 18, 15, 30, tzinfo=dt.timezone.utc)
    ts = now - dt.timedelta(seconds=1)
    result = assert_fresh("bars", ts, max_age_seconds=60, now=now)
    assert result.fresh

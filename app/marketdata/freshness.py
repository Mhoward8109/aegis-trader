"""
Data Freshness (spec §11). "Never trade from stale data... If market data
becomes stale: FAIL CLOSED. Do not trade." This module is the single place
that decision makes; every other module that needs a freshness verdict calls
into here rather than re-implementing the comparison.
"""
from __future__ import annotations

import dataclasses
import datetime as dt


class StaleDataError(Exception):
    """Raised (not just logged) so callers cannot accidentally continue past
    a stale-data condition without an explicit except clause."""


@dataclasses.dataclass
class FreshnessCheck:
    source: str
    age_seconds: float
    max_age_seconds: float
    fresh: bool


def check_freshness(source: str, data_timestamp: dt.datetime, max_age_seconds: float,
                     now: dt.datetime | None = None) -> FreshnessCheck:
    now = now or dt.datetime.now(dt.timezone.utc)
    if data_timestamp.tzinfo is None:
        data_timestamp = data_timestamp.replace(tzinfo=dt.timezone.utc)
    age = (now - data_timestamp).total_seconds()
    return FreshnessCheck(source=source, age_seconds=age, max_age_seconds=max_age_seconds, fresh=age <= max_age_seconds)


def assert_fresh(source: str, data_timestamp: dt.datetime, max_age_seconds: float,
                  now: dt.datetime | None = None) -> FreshnessCheck:
    check = check_freshness(source, data_timestamp, max_age_seconds, now)
    if not check.fresh:
        raise StaleDataError(
            f"{source} data is {check.age_seconds:.1f}s old (limit {max_age_seconds}s). "
            f"FAIL CLOSED per spec §11 — no trade will be evaluated on this data."
        )
    return check


def assert_all_fresh(checks: list[tuple[str, dt.datetime, float]], now: dt.datetime | None = None) -> list[FreshnessCheck]:
    return [assert_fresh(source, ts, max_age, now) for source, ts, max_age in checks]

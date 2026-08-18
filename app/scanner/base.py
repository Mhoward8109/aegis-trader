"""
Market Scanner (spec §4). The scanner's job is purely to narrow the full
U.S.-listed universe down to a candidate list using cheap, configurable
filters — it does not decide whether anything is tradeable (that's the
strategy + risk engine's job downstream).

Every field a filter references is declared in ScanCriteria with a default
of None ("not applied"), and MarketDataProvider adapters declare which
fields they can actually supply via `supported_fields`. The scanner cross-
checks requested filters against supported_fields and reports unavailable
ones instead of silently ignoring or (worse) fabricating them — spec §4:
"Do not assume every data provider supplies every field. Gracefully
identify unavailable information."
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt


@dataclasses.dataclass
class ScanCriteria:
    price_min: float | None = None
    price_max: float | None = None
    pct_change_min: float | None = None
    gap_pct_min: float | None = None
    premarket_change_min: float | None = None
    postmarket_change_min: float | None = None
    current_volume_min: float | None = None
    avg_daily_volume_min: float | None = None
    rvol_min: float | None = None
    dollar_volume_min: float | None = None
    max_spread_pct: float | None = None
    min_float: float | None = None
    max_float: float | None = None
    max_market_cap: float | None = None
    min_market_cap: float | None = None


@dataclasses.dataclass
class ScanResult:
    ticker: str
    fields: dict                        # every field actually returned
    unavailable_fields: list[str]       # requested filters this provider could not evaluate
    data_timestamp: dt.datetime
    source: str


class MarketDataProvider(abc.ABC):
    supported_fields: set[str] = set()

    @abc.abstractmethod
    def scan(self, criteria: ScanCriteria) -> list[ScanResult]: ...

    @abc.abstractmethod
    def get_bars(self, ticker: str, timeframe: str, start: dt.datetime, end: dt.datetime): ...

    def unsupported_filters(self, criteria: ScanCriteria) -> list[str]:
        requested = {k for k, v in dataclasses.asdict(criteria).items() if v is not None}
        return sorted(requested - self.supported_fields)


class Scanner:
    def __init__(self, provider: MarketDataProvider, criteria: ScanCriteria):
        self.provider = provider
        self.criteria = criteria

    def run(self) -> dict:
        missing = self.provider.unsupported_filters(self.criteria)
        results = self.provider.scan(self.criteria)
        return {
            "results": results,
            "criteria_unsupported_by_provider": missing,
            "provider": type(self.provider).__name__,
        }

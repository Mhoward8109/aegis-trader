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


# ---------------------------------------------------------------------------
# MILESTONE 2 ADDITION (PART 6).
#
# The functions above existed in Milestone 1 and were called from NOWHERE -- a
# mechanical grep for `assert_fresh` across app/ returned zero call sites
# (docs/AUDIT_MILESTONE2.md section 2). The gate below exists so that "was this
# data fresh?" becomes a structured, journalable artifact that the execution
# authorizer consumes as evidence, rather than an exception that a caller might
# never trigger because it never asked.
#
# Design decisions that matter:
#   * A MISSING timestamp is treated as STALE, not as "skip the check". A feed
#     that fails to report when its data is from is exactly the feed you should
#     not trade on.
#   * A FUTURE timestamp beyond a small tolerance is also treated as invalid --
#     it usually means a timezone bug or clock skew, and both mean the age
#     computation cannot be trusted.
#   * The report is built for ALL required sources before any verdict is
#     returned, so the journal records every stale source, not just the first.
# ---------------------------------------------------------------------------

#: A timestamp may be at most this far in the future before we treat it as a
#: clock/timezone fault rather than a fresh reading.
FUTURE_TOLERANCE_SECONDS = 5.0


@dataclasses.dataclass(frozen=True)
class SourceFreshness:
    source: str
    required: bool
    age_seconds: float | None
    max_age_seconds: float
    fresh: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class FreshnessReport:
    """Verdict over every data source a trade decision depended on."""

    checked_at: dt.datetime
    sources: tuple[SourceFreshness, ...]

    @property
    def all_required_fresh(self) -> bool:
        return all(s.fresh for s in self.sources if s.required)

    @property
    def stale_required_sources(self) -> list[SourceFreshness]:
        return [s for s in self.sources if s.required and not s.fresh]

    @property
    def detail(self) -> str:
        if not self.sources:
            return "No data sources were registered for freshness checking."
        if self.all_required_fresh:
            ages = ", ".join(
                f"{s.source}={s.age_seconds:.1f}s"
                for s in self.sources
                if s.age_seconds is not None
            )
            return f"All required market data within freshness limits ({ages})."
        problems = "; ".join(f"{s.source}: {s.detail}" for s in self.stale_required_sources)
        return f"FAIL CLOSED -- stale or unusable required data. {problems}"

    def as_record(self) -> dict:
        return {
            "checked_at": self.checked_at.isoformat(),
            "all_required_fresh": self.all_required_fresh,
            "detail": self.detail,
            "sources": [dataclasses.asdict(s) for s in self.sources],
        }

    def assert_fresh(self) -> None:
        """Raise if any REQUIRED source is stale.

        Callers on the order path pass `all_required_fresh` to the
        ExecutionAuthorizer as evidence; callers that must abort outright use
        this.
        """
        if not self.all_required_fresh:
            raise StaleDataError(self.detail)


class FreshnessGate:
    """Builds a FreshnessReport from a set of declared requirements.

    Usage on the order path::

        gate = FreshnessGate(max_ages=cfg.marketdata_freshness)
        gate.require("quote", quote.timestamp)
        gate.require("bars", last_bar.timestamp)
        gate.require("account", account.as_of)
        report = gate.report()

    `report` is handed to the ExecutionAuthorizer as evidence. There is no way
    to reach a submission without it, because the authorizer treats an absent
    evidence field as a FAILED check rather than as an unknown to ignore.
    """

    #: Conservative defaults in seconds, overridable from config
    #: (`marketdata.freshness`) because an appropriate quote age differs
    #: enormously between a momentum scalp and a swing entry.
    DEFAULT_MAX_AGES: dict[str, float] = {
        "quote": 10.0,
        "trade": 10.0,
        "bars": 120.0,
        "snapshot": 30.0,
        "account": 60.0,
        "positions": 60.0,
        "market_state": 300.0,
        "regime": 900.0,
    }

    #: Used when a caller registers a source name we have no default for. Kept
    #: deliberately tight so an unrecognised source is not silently lenient.
    FALLBACK_MAX_AGE = 60.0

    def __init__(self, max_ages: dict[str, float] | None = None,
                 now: dt.datetime | None = None):
        self.max_ages = {**self.DEFAULT_MAX_AGES, **(max_ages or {})}
        self._now = now
        self._sources: list[SourceFreshness] = []

    @property
    def now(self) -> dt.datetime:
        return self._now or dt.datetime.now(dt.timezone.utc)

    def require(self, source: str, data_timestamp: dt.datetime | None,
                max_age_seconds: float | None = None) -> SourceFreshness:
        """Register a source whose staleness BLOCKS trading."""
        return self._add(source, data_timestamp, max_age_seconds, required=True)

    def observe(self, source: str, data_timestamp: dt.datetime | None,
                max_age_seconds: float | None = None) -> SourceFreshness:
        """Register a source we want recorded but which does not block."""
        return self._add(source, data_timestamp, max_age_seconds, required=False)

    def _add(self, source: str, data_timestamp: dt.datetime | None,
             max_age_seconds: float | None, *, required: bool) -> SourceFreshness:
        limit = (max_age_seconds if max_age_seconds is not None
                 else self.max_ages.get(source, self.FALLBACK_MAX_AGE))

        if data_timestamp is None:
            entry = SourceFreshness(
                source=source, required=required, age_seconds=None,
                max_age_seconds=limit, fresh=False,
                detail=("no timestamp supplied -- a feed that cannot say when its "
                        "data is from is treated as stale, never as exempt"),
            )
            self._sources.append(entry)
            return entry

        if not isinstance(data_timestamp, dt.datetime):
            # A non-datetime value is a wiring bug, not a fresh reading. Raising
            # here would let a caller's broad `except` turn a data fault into a
            # generic error and lose the fail-closed verdict, so this records an
            # explicit stale entry instead. The case is real: a caller once
            # passed a DataFrame RangeIndex position (an int) as a timestamp.
            entry = SourceFreshness(
                source=source, required=required, age_seconds=None,
                max_age_seconds=limit, fresh=False,
                detail=(f"timestamp was {type(data_timestamp).__name__} "
                        f"({data_timestamp!r}), not a datetime. Treated as stale: "
                        f"a value that is not a time cannot prove data is recent."),
            )
            self._sources.append(entry)
            return entry

        ts = data_timestamp
        if ts.tzinfo is None:
            # A naive timestamp is assumed UTC, consistent with check_freshness
            # above, but we record that we had to assume.
            ts = ts.replace(tzinfo=dt.timezone.utc)

        age = (self.now - ts).total_seconds()

        if age < -FUTURE_TOLERANCE_SECONDS:
            entry = SourceFreshness(
                source=source, required=required, age_seconds=age,
                max_age_seconds=limit, fresh=False,
                detail=(f"timestamp is {abs(age):.1f}s in the FUTURE, beyond the "
                        f"{FUTURE_TOLERANCE_SECONDS}s tolerance. This indicates a "
                        f"clock or timezone fault, so the age cannot be trusted"),
            )
        elif age > limit:
            entry = SourceFreshness(
                source=source, required=required, age_seconds=age,
                max_age_seconds=limit, fresh=False,
                detail=f"{age:.1f}s old, limit {limit:.1f}s",
            )
        else:
            entry = SourceFreshness(
                source=source, required=required, age_seconds=age,
                max_age_seconds=limit, fresh=True,
                detail=f"{age:.1f}s old, within {limit:.1f}s limit",
            )
        self._sources.append(entry)
        return entry

    def report(self) -> FreshnessReport:
        return FreshnessReport(checked_at=self.now, sources=tuple(self._sources))

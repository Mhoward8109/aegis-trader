"""
Economic/Event Risk calendar (spec §15). "Never rely on a permanently
hard-coded calendar" — events live in a data file (config/events.yaml by
default) that the operator refreshes, not in Python source. A live-calendar
API adapter can be dropped in later behind the same EventCalendarProvider
interface (see docs/ARCHITECTURE.md for FRED/Trading-Economics options).
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt

import yaml


@dataclasses.dataclass
class ScheduledEvent:
    name: str
    event_type: str        # FOMC|CPI|PPI|NFP|GDP|FED_SPEECH|EARNINGS
    scheduled_at: dt.datetime
    high_impact: bool = True


class EventCalendarProvider(abc.ABC):
    @abc.abstractmethod
    def upcoming_events(self, window_start: dt.datetime, window_end: dt.datetime) -> list[ScheduledEvent]: ...


class YamlFileEventCalendar(EventCalendarProvider):
    def __init__(self, path: str):
        self.path = path

    def _load(self) -> list[ScheduledEvent]:
        try:
            with open(self.path) as f:
                raw = yaml.safe_load(f) or []
        except FileNotFoundError:
            return []
        out = []
        for row in raw:
            out.append(ScheduledEvent(
                name=row["name"], event_type=row["event_type"],
                scheduled_at=dt.datetime.fromisoformat(row["scheduled_at"]),
                high_impact=row.get("high_impact", True),
            ))
        return out

    def upcoming_events(self, window_start: dt.datetime, window_end: dt.datetime) -> list[ScheduledEvent]:
        return [e for e in self._load() if window_start <= e.scheduled_at <= window_end]


class EventRiskGate:
    def __init__(self, provider: EventCalendarProvider, blackout_before_min: int, blackout_after_min: int,
                 relevant_types: set[str]):
        self.provider = provider
        self.before = dt.timedelta(minutes=blackout_before_min)
        self.after = dt.timedelta(minutes=blackout_after_min)
        self.relevant_types = relevant_types

    def in_blackout(self, now: dt.datetime) -> tuple[bool, ScheduledEvent | None]:
        window_start = now - self.after
        window_end = now + self.before
        events = self.provider.upcoming_events(window_start, window_end)
        for e in events:
            if e.high_impact and e.event_type in self.relevant_types:
                return True, e
        return False, None

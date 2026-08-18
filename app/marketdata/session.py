"""Exchange-session classification using Alpaca's authoritative clock/calendar.

The calendar is required because a weekday alone does not prove that markets are
open, and Alpaca represents early closes through ``Calendar.close`` rather than
a separate boolean.  If either clock/calendar lookup fails, the state is unknown
and order permission is denied.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
from collections.abc import Callable, Iterable
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

EASTERN = ZoneInfo("America/New_York")
CLOSED = "CLOSED"
PREMARKET = "PREMARKET"
REGULAR = "REGULAR"
AFTER_HOURS = "AFTER_HOURS"
HOLIDAY = "HOLIDAY"
EARLY_CLOSE = "EARLY_CLOSE"
UNKNOWN = "UNKNOWN"


@dataclasses.dataclass(frozen=True)
class SessionState:
    """Current market session, including evidence used to make the decision."""

    session: str
    timestamp: dt.datetime | None
    calendar_date: dt.date | None = None
    scheduled_open: dt.datetime | None = None
    scheduled_close: dt.datetime | None = None
    is_open: bool | None = None
    reason: str | None = None

    @property
    def is_unknown(self) -> bool:
        """Unknown data fails closed; callers must never permit its orders."""
        return self.session == UNKNOWN


class MarketSessionService:
    """Classify market time from ``TradingClient.get_clock/get_calendar``.

    ``clock_source`` and ``calendar_source`` can be injected callables for tests
    or a different authoritative transport.  The callables receive no argument
    and a ``date`` argument respectively.  The standard Alpaca implementation
    uses ``get_clock()`` and ``get_calendar(GetCalendarRequest(...))``.
    """

    def __init__(
        self,
        client: TradingClient | None = None,
        *,
        clock_source: Callable[[], Any] | None = None,
        calendar_source: Callable[[dt.date], Iterable[Any]] | None = None,
    ) -> None:
        if clock_source is not None or calendar_source is not None:
            if clock_source is None or calendar_source is None:
                raise ValueError("inject both clock_source and calendar_source, or neither")
            self._clock_source = clock_source
            self._calendar_source = calendar_source
            return
        self.client = client or self._client_from_environment()
        self._clock_source = self.client.get_clock
        self._calendar_source = self._calendar_for_date

    @staticmethod
    def _client_from_environment() -> TradingClient:
        key = os.getenv("ALPACA_PAPER_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
        secret = os.getenv("ALPACA_PAPER_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError(
                "Market session lookup requires ALPACA_PAPER_API_KEY_ID and "
                "ALPACA_PAPER_API_SECRET_KEY (or ALPACA_API_KEY_ID and "
                "ALPACA_API_SECRET_KEY) in the environment."
            )
        return TradingClient(api_key=key, secret_key=secret, paper=True)

    def _calendar_for_date(self, date: dt.date) -> Iterable[Any]:
        return self.client.get_calendar(GetCalendarRequest(start=date, end=date))

    def current_session(self) -> SessionState:
        """Return a session state or explicit unknown when Alpaca is unreachable."""
        try:
            clock = self._clock_source()
            timestamp = self._aware_timestamp(getattr(clock, "timestamp", None))
            if timestamp is None:
                return SessionState(UNKNOWN, None, reason="Alpaca clock did not include a valid timestamp")
            session_date = timestamp.astimezone(EASTERN).date()
            calendar = list(self._calendar_source(session_date))
            today = next((entry for entry in calendar if getattr(entry, "date", None) == session_date), None)
            if today is None:
                return SessionState(HOLIDAY, timestamp, calendar_date=session_date, is_open=False, reason="no Alpaca calendar session")
            scheduled_open = self._calendar_datetime(getattr(today, "open", None), session_date)
            scheduled_close = self._calendar_datetime(getattr(today, "close", None), session_date)
            if scheduled_open is None or scheduled_close is None or scheduled_close <= scheduled_open:
                return SessionState(UNKNOWN, timestamp, calendar_date=session_date, reason="Alpaca calendar entry was malformed")
            return self._classify(timestamp, bool(getattr(clock, "is_open", False)), session_date, scheduled_open, scheduled_close)
        except Exception as exc:
            return SessionState(UNKNOWN, None, reason=f"clock/calendar lookup failed: {type(exc).__name__}")

    def _classify(
        self,
        timestamp: dt.datetime,
        clock_is_open: bool,
        session_date: dt.date,
        scheduled_open: dt.datetime,
        scheduled_close: dt.datetime,
    ) -> SessionState:
        local = timestamp.astimezone(EASTERN)
        local_open = scheduled_open.astimezone(EASTERN)
        local_close = scheduled_close.astimezone(EASTERN)
        normal_close = dt.datetime.combine(session_date, dt.time(16), tzinfo=EASTERN)
        is_early_close = local_close < normal_close
        base = dict(
            timestamp=timestamp,
            calendar_date=session_date,
            scheduled_open=scheduled_open,
            scheduled_close=scheduled_close,
            is_open=clock_is_open,
        )
        if clock_is_open:
            # The exchange clock is authoritative for actual regular trading.
            return SessionState(EARLY_CLOSE if is_early_close else REGULAR, **base)
        premarket_start = dt.datetime.combine(session_date, dt.time(4), tzinfo=EASTERN)
        after_hours_end = dt.datetime.combine(session_date, dt.time(20), tzinfo=EASTERN)
        if premarket_start <= local < local_open:
            return SessionState(PREMARKET, **base)
        if local_close <= local < after_hours_end:
            return SessionState(AFTER_HOURS, **base)
        return SessionState(CLOSED, **base)

    @staticmethod
    def permits_orders(session: SessionState | str, allowed_sessions: Iterable[str]) -> bool:
        """Allow only explicitly configured, known session states."""
        return permits_orders(session, allowed_sessions)

    @staticmethod
    def _aware_timestamp(value: Any) -> dt.datetime | None:
        if not isinstance(value, dt.datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)

    @classmethod
    def _calendar_datetime(cls, value: Any, date: dt.date) -> dt.datetime | None:
        if isinstance(value, dt.datetime):
            return cls._aware_timestamp(value)
        # Defensive fallback for fake clients. Alpaca's Calendar model is a datetime.
        if isinstance(value, dt.time):
            return dt.datetime.combine(date, value, tzinfo=EASTERN)
        return None


def permits_orders(session: SessionState | str, allowed_sessions: Iterable[str]) -> bool:
    """Allow orders only in an explicitly allowed, known session.

    This module-level form is provided for order paths that do not retain the
    service instance after acquiring a :class:`SessionState`.
    """
    value = session.session if isinstance(session, SessionState) else str(session)
    if value == UNKNOWN:
        return False
    return value in {str(allowed).upper() for allowed in allowed_sessions}

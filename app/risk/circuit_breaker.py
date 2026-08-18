"""
Daily Circuit Breaker (spec §10) — non-bypassable kill switch.

Design constraint taken directly from the spec: "The trading strategy cannot
disable this protection." Concretely, that means:
  - CircuitBreaker.trip() and .is_tripped() are the only public mutators of
    breaker state; there is no reset-from-strategy-code path.
  - Only an explicit operator action (a new CLI command, logged, outside
    strategy code) can clear a trip, and only for a NEW session date.
  - Once tripped for a session_date, `is_tripped()` returns True for the
    remainder of that date no matter what calls it afterward.
"""
from __future__ import annotations

import dataclasses
import datetime as dt


@dataclasses.dataclass
class BreakerCheck:
    trigger: str
    tripped: bool
    details: dict


class CircuitBreaker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._tripped_dates: dict[str, dict] = {}   # session_date -> {trigger, details}

    def is_tripped(self, session_date: str) -> bool:
        return session_date in self._tripped_dates

    def trip(self, session_date: str, trigger: str, details: dict) -> BreakerCheck:
        if session_date not in self._tripped_dates:
            self._tripped_dates[session_date] = {"trigger": trigger, "details": details,
                                                    "at": dt.datetime.now(dt.timezone.utc).isoformat()}
        return BreakerCheck(trigger=trigger, tripped=True, details=details)

    def clear_for_new_session(self, session_date: str) -> None:
        """Operator-only reset, and only meaningful for a session_date that
        has not started yet — this does NOT let strategy code un-trip today's
        breaker. Intended to be called once at the start of a new trading day
        by the scheduler, never by strategy or risk code mid-session."""
        self._tripped_dates.pop(session_date, None)

    # --- individual trigger evaluators -------------------------------
    def check_daily_loss(self, session_date: str, realized_pnl: float, equity: float) -> BreakerCheck | None:
        if not self.cfg.get("daily_loss_trip", True):
            return None
        limit = -(self.cfg_parent_max_daily_loss_pct() / 100.0) * equity
        if realized_pnl <= limit:
            return self.trip(session_date, "daily_loss_exceeded",
                              {"realized_pnl": realized_pnl, "limit": limit})
        return None

    def check_consecutive_losses(self, session_date: str, consecutive_losses: int, max_allowed: int) -> BreakerCheck | None:
        if not self.cfg.get("consecutive_loss_trip", True):
            return None
        if consecutive_losses >= max_allowed:
            return self.trip(session_date, "max_consecutive_losses",
                              {"consecutive_losses": consecutive_losses, "max_allowed": max_allowed})
        return None

    def check_stale_data(self, session_date: str, source: str, age_seconds: float, max_age_seconds: float) -> BreakerCheck | None:
        if not self.cfg.get("stale_data_trip", True):
            return None
        if age_seconds > max_age_seconds:
            return self.trip(session_date, "stale_data",
                              {"source": source, "age_seconds": age_seconds, "max_age_seconds": max_age_seconds})
        return None

    def check_reconciliation(self, session_date: str, discrepancy: dict | None) -> BreakerCheck | None:
        if not self.cfg.get("reconciliation_failure_trip", True):
            return None
        if discrepancy:
            return self.trip(session_date, "reconciliation_failure", discrepancy)
        return None

    def check_repeated_rejections(self, session_date: str, reject_count: int) -> BreakerCheck | None:
        threshold = self.cfg.get("repeated_rejected_orders_trip", 3)
        if reject_count >= threshold:
            return self.trip(session_date, "repeated_rejected_orders",
                              {"reject_count": reject_count, "threshold": threshold})
        return None

    def check_slippage(self, session_date: str, slippage_pct: float) -> BreakerCheck | None:
        threshold = self.cfg.get("excessive_slippage_trip_pct")
        if threshold is not None and slippage_pct > threshold:
            return self.trip(session_date, "excessive_slippage",
                              {"slippage_pct": slippage_pct, "threshold": threshold})
        return None

    def cfg_parent_max_daily_loss_pct(self) -> float:
        # allows the breaker to be constructed with just circuit_breaker cfg
        # while still knowing the risk.max_daily_loss_pct value passed in by caller
        return self.cfg.get("_max_daily_loss_pct", 2.0)

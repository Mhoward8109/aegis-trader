"""Fail-closed operator health snapshot.

A field is never represented by a comforting default.  Each observable is an
explicit record with ``availability`` (``AVAILABLE``, ``UNAVAILABLE``, or
``UNKNOWN``), ``value``, and ``reason``.  This lets an operator distinguish
"zero" from "we did not manage to measure it" in a single JSON response.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from app.common.db import Candidate, OrderEvent, TradeMode
from app.common.modes import Mode
from app.risk.account_state_builder import build_account_state


AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
UNKNOWN = "UNKNOWN"
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
BLOCKED = "BLOCKED"


def _available(value: Any, reason: str | None = None) -> dict[str, Any]:
    return {"availability": AVAILABLE, "value": value, "reason": reason}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"availability": UNAVAILABLE, "value": None, "reason": reason}


def _unknown(reason: str) -> dict[str, Any]:
    return {"availability": UNKNOWN, "value": None, "reason": reason}


def _has_unavailable_value(value: Any) -> bool:
    """Return whether an explicit availability marker is not AVAILABLE."""
    if isinstance(value, dict):
        if "availability" in value and value["availability"] != AVAILABLE:
            return True
        return any(_has_unavailable_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_unavailable_value(item) for item in value)
    return False


def _utc_now(now: dt.datetime | None) -> dt.datetime:
    value = now or dt.datetime.now(dt.timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _timestamp_record(value: dt.datetime | None, *, now: dt.datetime,
                      label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if value is None:
        reason = f"{label} was not supplied by a live data source."
        return _unavailable(reason), _unavailable(f"Cannot calculate age: {reason}")
    if not isinstance(value, dt.datetime):
        reason = f"{label} was {type(value).__name__}, not a datetime."
        return _unknown(reason), _unknown(f"Cannot calculate age: {reason}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return _available(value.isoformat()), _available((now - value).total_seconds())


def _mode_to_trade_mode(mode: Mode | str | None) -> TradeMode | None:
    if mode is None:
        return None
    raw = mode.value if isinstance(mode, Mode) else str(mode)
    try:
        return TradeMode(raw)
    except ValueError:
        return None


def _strategy_records(strategies: Any) -> dict[str, Any]:
    if strategies is None:
        return _unavailable("No active strategy registry was supplied.")
    records: list[dict[str, Any]] = []
    try:
        for strategy in strategies:
            if isinstance(strategy, dict):
                name = strategy.get("name") or strategy.get("strategy")
                version = strategy.get("version")
            elif isinstance(strategy, tuple) and len(strategy) == 2:
                name, version = strategy
            else:
                name = getattr(strategy, "name", type(strategy).__name__)
                version = getattr(strategy, "version", None)
            name_record = (_available(str(name)) if name else
                           _unknown("Strategy has no name."))
            version_record = (_available(str(version)) if version is not None else
                              _unavailable(
                                  f"Strategy {name or type(strategy).__name__} does not expose a version."))
            records.append({"name": name_record, "version": version_record})
    except Exception as exc:  # noqa: BLE001
        return _unknown(f"Active strategy enumeration failed: {type(exc).__name__}: {exc}")
    return _available(records, "No strategies are active." if not records else None)


def _latest_journal_records(journal: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if journal is None:
        reason = "No journal was supplied, so persisted candidate and execution history cannot be read."
        return _unavailable(reason), _unavailable(reason), _unavailable(reason)
    session = getattr(journal, "session", None)
    if session is None:
        reason = "Journal has no SQLAlchemy session, so persisted history cannot be read."
        return _unknown(reason), _unknown(reason), _unknown(reason)
    try:
        candidate = session.query(Candidate).order_by(Candidate.created_at.desc()).first()
        if candidate is None:
            latest_candidate = _unavailable("The journal contains no candidates yet.")
        else:
            latest_candidate = _available({
                "id": candidate.id, "ticker": candidate.ticker,
                "strategy": candidate.strategy, "strategy_version": candidate.strategy_version,
                "decision": candidate.decision,
                "considered_at": candidate.created_at.isoformat(),
            })
    except Exception as exc:  # noqa: BLE001
        latest_candidate = _unknown(f"Latest candidate query failed: {type(exc).__name__}: {exc}")
    try:
        rejected = (session.query(Candidate)
                    .filter(Candidate.rejection_reason.isnot(None))
                    .order_by(Candidate.created_at.desc()).first())
        if rejected is None:
            latest_rejection = _unavailable("The journal contains no rejection reason yet.")
        else:
            latest_rejection = _available({
                "candidate_id": rejected.id, "ticker": rejected.ticker,
                "reason": rejected.rejection_reason,
                "rejected_at": rejected.created_at.isoformat(),
            })
    except Exception as exc:  # noqa: BLE001
        latest_rejection = _unknown(f"Latest rejection query failed: {type(exc).__name__}: {exc}")
    try:
        event = session.query(OrderEvent).order_by(OrderEvent.at.desc()).first()
        if event is None:
            latest_execution = _unavailable("The journal contains no execution event yet.")
        else:
            latest_execution = _available({
                "id": event.id, "order_id": event.order_id,
                "from_state": event.from_state, "to_state": event.to_state,
                "reason": event.reason, "at": event.at.isoformat(),
            })
    except Exception as exc:  # noqa: BLE001
        latest_execution = _unknown(f"Latest execution-event query failed: {type(exc).__name__}: {exc}")
    return latest_candidate, latest_rejection, latest_execution


@dataclasses.dataclass(frozen=True)
class HealthSnapshot:
    """One complete, serializable operator health response."""

    generated_at: str
    status: str
    blocking_reasons: list[str]
    mode: dict[str, Any]
    broker_environment: dict[str, Any]
    broker_adapter_enabled: dict[str, Any]
    market_data_provider: dict[str, Any]
    market_data_health: dict[str, Any]
    last_quote_timestamp: dict[str, Any]
    last_quote_age_seconds: dict[str, Any]
    last_successful_data_update: dict[str, Any]
    reconciliation_state: dict[str, Any]
    circuit_breaker_state: dict[str, Any]
    active_strategies: dict[str, Any]
    open_positions: dict[str, Any]
    open_orders: dict[str, Any]
    realized_pnl: dict[str, Any]
    unrealized_pnl: dict[str, Any]
    remaining_daily_risk_budget: dict[str, Any]
    latest_candidate_considered: dict[str, Any]
    latest_rejection_reason: dict[str, Any]
    latest_execution_event: dict[str, Any]

    def as_record(self) -> dict[str, Any]:
        """Return JSON-safe data without losing explicit availability reasons."""
        return dataclasses.asdict(self)


def build_health_snapshot(
    *,
    mode: Mode | str | None = None,
    broker: Any | None = None,
    broker_adapter_enabled: bool | None = None,
    broker_adapter_enabled_reason: str | None = None,
    market_data_provider: Any | None = None,
    freshness_report: Any | None = None,
    last_quote: Any | None = None,
    last_successful_data_update: dt.datetime | None = None,
    reconciliation: Any | None = None,
    circuit_breaker: Any | None = None,
    strategies: Any | None = None,
    journal: Any | None = None,
    sector_lookup: dict[str, str] | None = None,
    daily_risk_limit_usd: float | None = None,
    latest_candidate: Any | None = None,
    latest_rejection: Any | None = None,
    latest_execution_event: Any | None = None,
    quote_max_age_seconds: float = 10.0,
    now: dt.datetime | None = None,
) -> HealthSnapshot:
    """Build an honest health response from currently available collaborators.

    The function accepts optional collaborators so the dashboard can run before
    every production adapter exists.  Omission is not treated as a benign zero:
    it becomes an explicit UNAVAILABLE/UNKNOWN record and blocks new entries.
    """
    checked_at = _utc_now(now)
    blocking: list[str] = []

    if mode is None:
        mode_record = _unknown("Operating mode was not supplied.")
        trade_mode = None
        blocking.append("Operating mode is UNKNOWN; entry authorization cannot be evaluated.")
    else:
        raw_mode = mode.value if isinstance(mode, Mode) else str(mode)
        try:
            Mode(raw_mode)
            mode_record = _available(raw_mode)
        except ValueError:
            mode_record = _unknown(f"Unsupported operating mode {raw_mode!r}.")
            blocking.append("Operating mode is UNKNOWN; entry authorization cannot be evaluated.")
        trade_mode = _mode_to_trade_mode(mode)

    if broker is None:
        broker_environment = _unavailable("No broker adapter is instantiated.")
    else:
        environment = getattr(broker, "environment", None)
        raw_environment = getattr(environment, "value", environment)
        broker_environment = (_available(str(raw_environment)) if raw_environment in {"SHADOW", "PAPER", "LIVE"}
                              else _unknown("Broker adapter does not declare a recognized environment."))

    if broker_adapter_enabled is None:
        adapter_enabled = _unknown(
            "Operational enablement was not supplied; adapter presence alone does not prove it is enabled.")
    else:
        adapter_enabled = _available(bool(broker_adapter_enabled), broker_adapter_enabled_reason)

    provider_name = None
    if isinstance(market_data_provider, str):
        provider_name = market_data_provider
    elif market_data_provider is not None:
        provider_name = getattr(market_data_provider, "name", type(market_data_provider).__name__)
    market_provider_record = (_available(provider_name) if provider_name else
                              _unavailable("No market-data provider is instantiated."))

    if freshness_report is None:
        market_health = _unavailable("No FreshnessReport was supplied by the market-data path.")
        blocking.append("Market-data freshness is UNAVAILABLE; new entries are fail-closed.")
    else:
        try:
            sources = getattr(freshness_report, "sources")
            fresh = bool(getattr(freshness_report, "all_required_fresh"))
            detail = str(getattr(freshness_report, "detail"))
            report_record = (freshness_report.as_record() if hasattr(freshness_report, "as_record")
                             else {"all_required_fresh": fresh, "detail": detail})
            if not sources:
                market_health = _unknown("FreshnessReport contained no registered data sources.")
                blocking.append("Market-data freshness has no registered sources; new entries are fail-closed.")
            elif fresh:
                market_health = _available(report_record)
            else:
                market_health = _available(report_record, detail)
                blocking.append(f"Required market data is stale or unusable: {detail}")
        except Exception as exc:  # noqa: BLE001
            market_health = _unknown(f"FreshnessReport could not be interpreted: {type(exc).__name__}: {exc}")
            blocking.append("Market-data freshness is UNKNOWN; new entries are fail-closed.")

    quote_timestamp = getattr(last_quote, "timestamp", None) if last_quote is not None else None
    quote_ts_record, quote_age_record = _timestamp_record(
        quote_timestamp, now=checked_at, label="Last quote timestamp")
    if quote_age_record["availability"] == AVAILABLE:
        age = quote_age_record["value"]
        if age > quote_max_age_seconds or age < -5.0:
            blocking.append(
                f"Last quote age is {age:.1f}s, outside the usable range (maximum {quote_max_age_seconds:.1f}s).")
    else:
        blocking.append("Last quote timestamp is unavailable or invalid; new entries are fail-closed.")

    update_record, _ = _timestamp_record(last_successful_data_update, now=checked_at,
                                         label="Last successful data update")

    if reconciliation is None:
        reconciliation_record = _unavailable("No reconciliation report was supplied.")
        blocking.append("Reconciliation state is UNAVAILABLE; exposure cannot be verified for new entries.")
    else:
        try:
            blocks = bool(getattr(reconciliation, "blocks_trading"))
            detail = str(getattr(reconciliation, "detail"))
            value = reconciliation.as_record() if hasattr(reconciliation, "as_record") else {
                "blocks_trading": blocks, "detail": detail}
            reconciliation_record = _available(value, detail if blocks else None)
            if blocks:
                blocking.append(f"Reconciliation blocks new entries: {detail}")
        except Exception as exc:  # noqa: BLE001
            reconciliation_record = _unknown(f"Reconciliation report could not be interpreted: {type(exc).__name__}: {exc}")
            blocking.append("Reconciliation state is UNKNOWN; exposure cannot be verified for new entries.")

    if circuit_breaker is None:
        breaker_record = _unknown("No persistent circuit breaker was supplied.")
        blocking.append("Circuit-breaker state is UNKNOWN; new entries are fail-closed.")
    else:
        try:
            breaker_state = circuit_breaker.state(now=checked_at)
            tripped = bool(breaker_state.tripped)
            active_trips = list(getattr(breaker_state, "active_trips", ()))
            trip_times = [getattr(t, "tripped_at", None) for t in active_trips]
            latest_trip_time = max((t for t in trip_times if isinstance(t, dt.datetime)), default=None)
            value = {
                "state": "TRIPPED" if tripped else "CLEAR",
                "tripped": tripped,
                "reason": breaker_state.reason,
                "trip_time": latest_trip_time.isoformat() if latest_trip_time else None,
                "checked_at": getattr(breaker_state, "checked_at", checked_at).isoformat(),
                "permits_entry": bool(breaker_state.permits_entry()),
                "active_trips": [t.as_record() if hasattr(t, "as_record") else str(t)
                                 for t in active_trips],
            }
            breaker_record = _available(value)
            if tripped or not breaker_state.permits_entry():
                blocking.append(f"Circuit breaker is TRIPPED: {breaker_state.reason}")
        except Exception as exc:  # noqa: BLE001
            breaker_record = _unknown(f"Persistent circuit-breaker state could not be read: {type(exc).__name__}: {exc}")
            blocking.append("Circuit-breaker state is UNKNOWN; new entries are fail-closed.")

    strategy_record = _strategy_records(strategies)

    account_build = None
    if broker is None or journal is None or trade_mode is None:
        needed = []
        if broker is None:
            needed.append("broker")
        if journal is None:
            needed.append("journal")
        if trade_mode is None:
            needed.append("valid mode")
        account_reason = "Cannot build account state; missing " + ", ".join(needed) + "."
        open_positions = _unavailable(account_reason)
        realized_pnl = _unavailable(account_reason)
        blocking.append("Account risk state is UNAVAILABLE; risk limits cannot approve new entries.")
    else:
        try:
            account_build = build_account_state(broker=broker, journal=journal,
                                                mode=trade_mode, sector_lookup=sector_lookup,
                                                now=checked_at)
            state = account_build.state
            open_positions = (_available(state.open_positions, account_build.detail)
                              if state.open_positions is not None else
                              _unavailable(account_build.detail))
            realized_pnl = (_available({"today": state.realized_pnl_today,
                                        "week": state.realized_pnl_week}, account_build.detail)
                            if state.realized_pnl_today is not None and state.realized_pnl_week is not None else
                            _unavailable(account_build.detail))
            if not account_build.complete:
                blocking.append("Account risk state is incomplete; risk limits cannot approve new entries.")
        except Exception as exc:  # noqa: BLE001
            account_reason = f"Account state build failed: {type(exc).__name__}: {exc}"
            open_positions = _unknown(account_reason)
            realized_pnl = _unknown(account_reason)
            blocking.append("Account risk state is UNKNOWN; risk limits cannot approve new entries.")

    if broker is None:
        open_orders = _unavailable("No broker adapter is instantiated.")
        unrealized_pnl = _unavailable("No broker adapter is instantiated.")
    else:
        try:
            orders = broker.get_open_orders()
            open_orders = _available(len(orders))
        except Exception as exc:  # noqa: BLE001
            open_orders = _unknown(f"Broker open-order query failed: {type(exc).__name__}: {exc}")
        try:
            positions = broker.get_positions()
            unrealized_pnl = _available(sum(float(p.unrealized_pnl) for p in positions))
        except Exception as exc:  # noqa: BLE001
            unrealized_pnl = _unknown(f"Broker position query failed: {type(exc).__name__}: {exc}")

    if daily_risk_limit_usd is None:
        budget = _unavailable("No dollar daily-risk limit was supplied; remaining budget cannot be calculated.")
    elif realized_pnl["availability"] != AVAILABLE or realized_pnl["value"]["today"] is None:
        budget = _unavailable("Today's realized P&L is unavailable, so remaining daily-risk budget cannot be calculated.")
    else:
        limit = float(daily_risk_limit_usd)
        remaining = limit + float(realized_pnl["value"]["today"])
        budget = _available(max(0.0, remaining),
                            f"Computed as daily loss limit {limit:.2f} plus today's realized P&L.")

    journal_candidate, journal_rejection, journal_execution = _latest_journal_records(journal)
    candidate_record = _available(latest_candidate) if latest_candidate is not None else journal_candidate
    rejection_record = _available(latest_rejection) if latest_rejection is not None else journal_rejection
    execution_record = (_available(latest_execution_event) if latest_execution_event is not None
                        else journal_execution)

    mode_value = mode_record.get("value")
    if mode_value in {"PAPER", "LIVE"}:
        if adapter_enabled["availability"] != AVAILABLE or not adapter_enabled["value"]:
            blocking.append("Broker adapter is not proven operationally enabled for an order-submitting mode.")
        if broker_environment["availability"] != AVAILABLE:
            blocking.append("Broker environment is UNKNOWN for an order-submitting mode.")

    # Preserve first occurrence while keeping concise, independently actionable reasons.
    blocking = list(dict.fromkeys(blocking))
    observability_fields = (
        mode_record, broker_environment, adapter_enabled, market_provider_record,
        market_health, quote_ts_record, quote_age_record, update_record,
        reconciliation_record, breaker_record, strategy_record, open_positions,
        open_orders, realized_pnl, unrealized_pnl, budget, candidate_record,
        rejection_record, execution_record,
    )
    if blocking:
        status = BLOCKED
    elif any(_has_unavailable_value(field) for field in observability_fields):
        # These gaps did not block a presently valid entry gate, but an operator
        # cannot call the picture fully healthy while they remain unmeasured.
        status = DEGRADED
    else:
        status = HEALTHY

    return HealthSnapshot(
        generated_at=checked_at.isoformat(), status=status, blocking_reasons=blocking,
        mode=mode_record, broker_environment=broker_environment,
        broker_adapter_enabled=adapter_enabled, market_data_provider=market_provider_record,
        market_data_health=market_health, last_quote_timestamp=quote_ts_record,
        last_quote_age_seconds=quote_age_record,
        last_successful_data_update=update_record,
        reconciliation_state=reconciliation_record, circuit_breaker_state=breaker_record,
        active_strategies=strategy_record, open_positions=open_positions,
        open_orders=open_orders, realized_pnl=realized_pnl, unrealized_pnl=unrealized_pnl,
        remaining_daily_risk_budget=budget,
        latest_candidate_considered=candidate_record,
        latest_rejection_reason=rejection_record,
        latest_execution_event=execution_record,
    )

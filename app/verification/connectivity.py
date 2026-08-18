"""Read-only Alpaca PAPER connectivity probe implementation."""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable

from app.execution.lifecycle import OrderLifecycleManager
from app.marketdata.freshness import FreshnessGate
from app.verification.readonly import ReadOnlyBroker
from app.verification.report import CheckStatus, ProbeCheck, ProbeReport


def _attempt(name: str, operation: Callable[[], object]) -> tuple[ProbeCheck, object | None]:
    try:
        value = operation()
    except Exception as exc:  # noqa: BLE001 - report boundary
        # External exceptions can embed URLs, headers, or account identifiers.
        # The operator report needs the failure class, not its raw payload.
        return ProbeCheck(name, CheckStatus.FAIL, f"{type(exc).__name__}"), None
    if value is None:
        return ProbeCheck(name, CheckStatus.FAIL, "no value returned"), None
    return ProbeCheck(name, CheckStatus.PASS), value


def run_connectivity_probe(
    runtime,
    *,
    symbol: str = "SPY",
    local_open_orders: list | None = None,
    local_positions: dict[str, float] | None = None,
    now: dt.datetime | None = None,
    freshness_max_ages: dict[str, float] | None = None,
) -> ProbeReport:
    """Exercise real PAPER infrastructure without possessing mutation methods."""
    now = now or dt.datetime.now(dt.timezone.utc)
    broker = ReadOnlyBroker(runtime.broker)
    checks: list[ProbeCheck] = []

    check, account = _attempt("Broker account", broker.get_account)
    checks.append(check)
    check, buying_power = _attempt("Buying power", broker.get_buying_power)
    checks.append(check)
    if buying_power is not None and float(buying_power) < 0:
        checks[-1] = ProbeCheck("Buying power", CheckStatus.FAIL, "negative value")
    check, positions = _attempt("Positions", broker.get_positions)
    checks.append(check)
    check, open_orders = _attempt("Open orders", broker.get_open_orders)
    checks.append(check)
    check, session = _attempt("Market clock/session", runtime.session_service.current_session)
    checks.append(check)
    if session is not None and getattr(session, "is_unknown", False):
        checks[-1] = ProbeCheck(
            "Market clock/session", CheckStatus.FAIL, getattr(session, "reason", "UNKNOWN")
        )

    check, quote = _attempt(f"{symbol.upper()} quote", lambda: broker.get_quote(symbol))
    checks.append(check)
    if quote is not None:
        try:
            bid, ask, last = float(quote.bid), float(quote.ask), float(quote.last)
            valid_quote = (
                all(math.isfinite(value) and value > 0 for value in (bid, ask, last))
                and ask >= bid
            )
        except (AttributeError, TypeError, ValueError):
            valid_quote = False
        if not valid_quote:
            checks[-1] = ProbeCheck(
                f"{symbol.upper()} quote", CheckStatus.FAIL, "malformed bid/ask/last"
            )
    start = now - dt.timedelta(minutes=10)
    check, bars = _attempt(
        f"{symbol.upper()} bars",
        lambda: runtime.market_data.get_bars(symbol, "1Min", start, now),
    )
    checks.append(check)
    if bars is not None and bool(getattr(bars, "empty", True)):
        checks[-1] = ProbeCheck(f"{symbol.upper()} bars", CheckStatus.FAIL, "no bars returned")

    gate = FreshnessGate(max_ages=freshness_max_ages, now=now)
    gate.require("account", getattr(account, "timestamp", None))
    gate.require("quote", getattr(quote, "timestamp", None))
    bar_timestamp = getattr(bars, "attrs", {}).get("data_timestamp") if bars is not None else None
    gate.require("bars", bar_timestamp)
    freshness = gate.report()
    checks.append(
        ProbeCheck(
            "Required data freshness",
            CheckStatus.PASS if freshness.all_required_fresh else CheckStatus.FAIL,
            freshness.detail,
        )
    )

    check, regime = _attempt("SPY/QQQ/IWM regime inputs", runtime.regime_engine.build)
    checks.append(check)
    required_directions = (
        getattr(regime, "spy_direction", "unknown"),
        getattr(regime, "qqq_direction", "unknown"),
        getattr(regime, "iwm_direction", "unknown"),
    ) if regime is not None else ("unknown",) * 3
    if any(str(value).lower() == "unknown" for value in required_directions):
        checks[-1] = ProbeCheck(
            "SPY/QQQ/IWM regime inputs", CheckStatus.FAIL, "required regime is UNKNOWN"
        )

    if runtime.sec_provider is None:
        checks.append(ProbeCheck("SEC EDGAR", CheckStatus.FAIL, "provider not constructed"))
    else:
        check, sec = _attempt(
            "SEC EDGAR",
            lambda: runtime.sec_provider.research(
                symbol, now - dt.timedelta(days=7)
            ),
        )
        checks.append(check)
        if sec is not None and not hasattr(sec, "source_url"):
            checks[-1] = ProbeCheck("SEC EDGAR", CheckStatus.FAIL, "missing source metadata")

    manager = OrderLifecycleManager(broker, journal=None)
    reconciliation = manager.reconcile(
        local_open_orders or [], local_positions or {}, now=now
    )
    checks.append(
        ProbeCheck(
            "Read-only reconciliation",
            CheckStatus.PASS if reconciliation.clean else CheckStatus.FAIL,
            reconciliation.detail,
        )
    )
    clear = runtime.circuit_breaker.permits_entry()
    checks.append(
        ProbeCheck(
            "Circuit breaker",
            CheckStatus.PASS if clear else CheckStatus.BLOCKED,
            "clear" if clear else "tripped",
        )
    )
    return ProbeReport("AEGIS PAPER CONNECTIVITY PROBE", tuple(checks))

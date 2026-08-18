"""Explicitly acknowledged, single-order Alpaca PAPER verification probe."""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

from app.broker.base import OrderRequest
from app.common.db import OrderState, TradeMode
from app.common.modes import Mode
from app.execution.authorization import (
    AuthorizationEvidence,
    BrokerEnvironment,
    ExecutionIntent,
)
from app.execution.engine import ExecutionEngine, SubmissionUncertainError
from app.execution.lifecycle import OrderLifecycleManager
from app.marketdata.freshness import FreshnessGate
from app.marketdata.session import permits_orders
from app.verification.report import CheckStatus, ProbeCheck, ProbeReport


ACKNOWLEDGEMENT_FLAG = "--i-understand-this-submits-a-paper-order"


class OrderProbeRefused(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class OrderProbeConfig:
    max_spread_pct: float
    min_liquidity_avg_dollar_vol: float
    risk_config: dict
    allowed_sessions: tuple[str, ...] = ("REGULAR",)
    sector: str = "Broad Market ETF"
    stop_pct: float = 1.0
    target_pct: float = 2.0


def _bar_timestamp(bars):
    return getattr(bars, "attrs", {}).get("data_timestamp")


def _average_dollar_volume(bars) -> float | None:
    try:
        if bars is None or bars.empty:
            return None
        values = (bars["close"].astype(float) * bars["volume"].astype(float)).dropna()
        return float(values.mean()) if len(values) else None
    except Exception:  # noqa: BLE001 - malformed data is a refusal
        return None


def _local_positions_from_journal(journal) -> dict[str, float]:
    from app.common.db import Order

    rows = (
        journal.session.query(Order)
        .filter(
            Order.state.in_(
                (OrderState.FILLED, OrderState.PARTIALLY_FILLED, OrderState.EXIT_PENDING)
            )
        )
        .all()
    )
    positions: dict[str, float] = {}
    for row in rows:
        signed = row.qty if row.side in {"BUY", "COVER"} else -row.qty
        positions[row.ticker] = positions.get(row.ticker, 0.0) + signed
    return {ticker: qty for ticker, qty in positions.items() if abs(qty) > 1e-9}


def _assert_real_paper_broker(broker, *, expected_type=None) -> None:
    production_check = expected_type is None
    if expected_type is None:
        from app.broker.alpaca_adapter import AlpacaPaperBroker, PAPER_BASE_URL
        expected_type = AlpacaPaperBroker
    if type(broker) is not expected_type or broker.environment is not BrokerEnvironment.PAPER:
        raise OrderProbeRefused(
            "order probe requires the exact AlpacaPaperBroker type with PAPER environment"
        )
    if production_check:
        if getattr(broker, "base_url_in_use", None) != PAPER_BASE_URL:
            raise OrderProbeRefused("paper broker endpoint does not match PAPER_BASE_URL")
        if hasattr(broker, "url_override"):
            raise OrderProbeRefused("broker URL overrides are prohibited")


def _assert_real_market_data(provider, *, expected_type=None) -> None:
    if expected_type is None:
        from app.marketdata.alpaca_provider import AlpacaMarketDataProvider
        expected_type = AlpacaMarketDataProvider
    if type(provider) is not expected_type:
        raise OrderProbeRefused(
            "order probe requires the exact AlpacaMarketDataProvider type; "
            "mock/offline providers are prohibited"
        )


def run_order_probe(
    runtime,
    *,
    journal,
    symbol: str,
    qty: int,
    acknowledged: bool,
    configured_mode: Mode,
    config_mode_source: str | None,
    config: OrderProbeConfig,
    local_open_orders: list | None = None,
    local_positions: dict[str, float] | None = None,
    now: dt.datetime | None = None,
    expected_broker_type=None,
    expected_market_data_type=None,
    freshness_max_ages: dict[str, float] | None = None,
) -> ProbeReport:
    """Submit at most one broker-native bracket order after every gate passes."""
    if not acknowledged:
        raise OrderProbeRefused(f"missing required acknowledgement {ACKNOWLEDGEMENT_FLAG}")
    if configured_mode is not Mode.PAPER:
        raise OrderProbeRefused(
            f"configured mode is {configured_mode.value}, not PAPER"
        )
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise OrderProbeRefused("quantity must be a positive whole number")
    symbol = symbol.strip().upper()
    if not symbol or not symbol.replace(".", "").isalnum():
        raise OrderProbeRefused("symbol is malformed")
    _assert_real_paper_broker(runtime.broker, expected_type=expected_broker_type)
    _assert_real_market_data(
        runtime.market_data, expected_type=expected_market_data_type
    )

    checks: list[ProbeCheck] = []
    if not runtime.circuit_breaker.permits_entry():
        raise OrderProbeRefused("circuit breaker is tripped")
    checks.append(ProbeCheck("Circuit breaker", CheckStatus.PASS, "clear"))

    reconciliation = OrderLifecycleManager(runtime.broker, journal).reconcile(
        local_open_orders or [], local_positions or {}, now=now
    )
    if not reconciliation.clean:
        raise OrderProbeRefused(f"reconciliation is not clean: {reconciliation.detail}")
    checks.append(ProbeCheck("Reconciliation", CheckStatus.PASS, reconciliation.detail))

    session = runtime.session_service.current_session()
    session_ok = permits_orders(session, config.allowed_sessions)
    if not session_ok:
        raise OrderProbeRefused(
            f"session {getattr(session, 'session', 'UNKNOWN')} does not permit this order"
        )
    checks.append(ProbeCheck("Market session", CheckStatus.PASS, session.session))

    account = runtime.broker.get_account()
    quote = runtime.broker.get_quote(symbol)
    now = now or dt.datetime.now(dt.timezone.utc)
    bars = runtime.market_data.get_bars(
        symbol, "1Min", now - dt.timedelta(minutes=30), now
    )
    liquidity_bars = runtime.market_data.get_bars(
        symbol, "1Day", now - dt.timedelta(days=30), now
    )
    gate = FreshnessGate(max_ages=freshness_max_ages, now=now)
    gate.require("account", getattr(account, "timestamp", None))
    gate.require("quote", getattr(quote, "timestamp", None))
    gate.require("bars", _bar_timestamp(bars))
    freshness = gate.report()
    if not freshness.all_required_fresh:
        raise OrderProbeRefused(f"required data is stale or incoherent: {freshness.detail}")
    checks.append(ProbeCheck("Required data freshness", CheckStatus.PASS, freshness.detail))

    bid, ask = float(quote.bid), float(quote.ask)
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask)) or ask < bid:
        raise OrderProbeRefused("quote is malformed")
    midpoint = (bid + ask) / 2.0
    spread_pct = (ask - bid) / midpoint * 100.0
    if spread_pct > config.max_spread_pct:
        raise OrderProbeRefused(
            f"spread {spread_pct:.4f}% exceeds {config.max_spread_pct:.4f}%"
        )
    liquidity = _average_dollar_volume(liquidity_bars)
    if liquidity is None or liquidity < config.min_liquidity_avg_dollar_vol:
        raise OrderProbeRefused(
            f"observed dollar-volume liquidity {liquidity!r} is below "
            f"{config.min_liquidity_avg_dollar_vol}"
        )
    notional = ask * qty
    if float(account.buying_power) < notional:
        raise OrderProbeRefused(
            f"buying power is insufficient for ${notional:.2f} notional"
        )
    checks.append(
        ProbeCheck("Spread/liquidity/buying-power gates", CheckStatus.PASS)
    )

    stop = round(ask * (1.0 - config.stop_pct / 100.0), 2)
    target = round(ask * (1.0 + config.target_pct / 100.0), 2)
    from app.risk.account_state_builder import build_account_state
    from app.risk.engine import CandidateRiskInput, RiskEngine

    account_build = build_account_state(
        broker=runtime.broker,
        journal=journal,
        mode=TradeMode.PAPER,
        sector_lookup={position.ticker: config.sector for position in runtime.broker.get_positions()},
        now=now,
    )
    if not account_build.complete:
        raise OrderProbeRefused(account_build.detail)
    risk_decision = RiskEngine(config.risk_config).evaluate(
        CandidateRiskInput(
            ticker=symbol,
            sector=config.sector,
            entry=ask,
            stop=stop,
            spread_pct=spread_pct,
            avg_dollar_volume=liquidity,
            estimated_slippage_pct=0.0,
            direction="long",
        ),
        account_build.state,
    )
    if not risk_decision.approved:
        raise OrderProbeRefused(
            f"risk engine rejected probe: {risk_decision.rule_triggered}: "
            f"{risk_decision.reason}"
        )
    if qty > risk_decision.position_size_shares:
        raise OrderProbeRefused(
            f"requested quantity {qty} exceeds risk-approved size "
            f"{risk_decision.position_size_shares}"
        )
    checks.append(
        ProbeCheck(
            "Production risk engine",
            CheckStatus.PASS,
            f"approved_size={risk_decision.position_size_shares}",
        )
    )
    if not runtime.circuit_breaker.permits_entry():
        raise OrderProbeRefused("circuit breaker tripped during preflight")
    late_reconciliation = OrderLifecycleManager(runtime.broker, journal).reconcile(
        journal.open_orders(), _local_positions_from_journal(journal), now=now
    )
    if late_reconciliation.blocks_trading:
        raise OrderProbeRefused(
            f"reconciliation changed during preflight: {late_reconciliation.detail}"
        )
    intent = ExecutionIntent(
        mode=Mode.PAPER,
        ticker=symbol,
        side="BUY",
        qty=qty,
        order_type="bracket",
        entry=ask,
        stop=stop,
    )
    evidence = AuthorizationEvidence(
        config_mode=configured_mode,
        config_mode_source=config_mode_source,
        risk_approved=True,
        risk_detail=(
            f"qty={qty}, notional={notional:.2f}, spread={spread_pct:.4f}%, "
            f"liquidity={liquidity:.2f}, risk_rule=approved, "
            f"risk_size={risk_decision.position_size_shares}"
        ),
        data_fresh=True,
        freshness_detail=freshness.detail,
        broker_environment=runtime.broker.environment,
        broker_connected=True,
        account_state_valid=True,
        account_state_detail="fresh account and sufficient buying power",
        circuit_breaker_tripped=False,
        session_permits_orders=True,
        session_detail=session.session,
    )
    grant = runtime.authorizer.authorize(intent, evidence)
    checks.append(ProbeCheck("Execution authorization", CheckStatus.PASS))

    order = journal.open_order(
        candidate_id=None,
        mode=TradeMode.PAPER,
        ticker=symbol,
        side="BUY",
        order_type="bracket",
        qty=qty,
        intended_entry=ask,
        stop=stop,
        targets=[target],
        strategy="paper-probe",
    )
    lifecycle = OrderLifecycleManager(runtime.broker, journal)
    lifecycle.mark_risk_approved(order, "paper probe preflight passed")
    engine = ExecutionEngine(runtime.broker)
    request = OrderRequest(
        ticker=symbol,
        side="BUY",
        qty=qty,
        order_type="bracket",
        stop_price=stop,
        take_profit_price=target,
        time_in_force="day",
    )
    try:
        receipt = engine.submit(request, grant, intent)
    except SubmissionUncertainError as exc:
        lifecycle.mark_submission_uncertain(order, str(exc))
        runtime.circuit_breaker.trip_on_critical_exception(
            RuntimeError(type(exc).__name__), where="paper order probe"
        )
        raise OrderProbeRefused(
            "submission outcome is unknown; breaker tripped and retry prohibited"
        ) from exc

    lifecycle.mark_submitted(order, receipt)
    state = lifecycle.refresh_from_broker(order)
    if state is OrderState.UNKNOWN:
        failure = OrderProbeRefused(
            "broker status after submission is UNKNOWN; reconcile before any retry"
        )
        runtime.circuit_breaker.trip_on_critical_exception(
            failure, where="paper order probe status confirmation"
        )
        raise failure
    checks.append(
        ProbeCheck(
            "Broker-confirmed order state",
            CheckStatus.PASS if state is not OrderState.UNKNOWN else CheckStatus.FAIL,
            state.value,
        )
    )

    # A working probe order is removed before exit.  A fill is accepted only
    # when the broker reports native child legs, proving protection exists at
    # the broker rather than merely in local intent.
    if state in {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED}:
        runtime.broker.cancel_order(receipt.broker_order_id)
        final_state = lifecycle.refresh_from_broker(order)
        if final_state is not OrderState.CANCELLED:
            raise OrderProbeRefused(
                f"broker did not independently confirm cancellation ({final_state.value})"
            )
        checks.append(ProbeCheck("Unfilled order cancelled", CheckStatus.PASS))
    elif state is OrderState.FILLED:
        raw = runtime.broker.get_order_status(receipt.broker_order_id).raw or {}
        if not raw.get("legs"):
            raise OrderProbeRefused(
                "fill reported without broker-confirmed protective bracket legs"
            )
        checks.append(ProbeCheck("Protective bracket legs", CheckStatus.PASS))
    elif state is OrderState.PARTIALLY_FILLED:
        runtime.broker.cancel_order(receipt.broker_order_id)
        raise OrderProbeRefused(
            "partial fill requires supervised reconciliation; remaining quantity was cancelled"
        )
    elif state in {OrderState.REJECTED, OrderState.UNKNOWN}:
        raise OrderProbeRefused(f"broker order ended in {state.value}")

    final_reconciliation = lifecycle.reconcile(
        journal.open_orders(), _local_positions_from_journal(journal)
    )
    if final_reconciliation.blocks_trading:
        raise OrderProbeRefused(
            f"post-order reconciliation blocks trading: {final_reconciliation.detail}"
        )
    checks.append(
        ProbeCheck(
            "Post-order reconciliation",
            CheckStatus.PASS,
            final_reconciliation.detail,
        )
    )

    return ProbeReport("AEGIS SINGLE PAPER ORDER PROBE", tuple(checks))

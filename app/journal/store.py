"""
Trade Journal (spec §19). Records every considered setup — traded AND
rejected — with full fidelity. This module is the only place strategy or
scanner code writes Candidate/Order rows; it centralizes the "append,
don't mutate" discipline (spec §33) instead of leaving it to each caller.
"""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy.orm import Session

from app.analytics.performance import (
    calculate_holding_duration,
    calculate_mfe_mae,
    calculate_r_multiple,
    calculate_slippage,
)
from app.common.db import (
    Candidate,
    CircuitBreakerEvent,
    Order,
    OrderEvent,
    RiskEvent,
    TradeMode,
    OrderState,
    TradePerformance,
)


def _as_text(value) -> str | None:
    """Coerce a list/tuple/dict to JSON for a TEXT column; pass strings through.

    Returns None only for a genuinely absent value, so an empty list is recorded
    as "[]" rather than becoming indistinguishable from "never populated".
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(sorted(value) if isinstance(value, set) else value,
                          default=str)
    return str(value)


class TradeJournal:
    def __init__(self, session: Session):
        self.session = session

    def record_candidate(self, *, ticker, strategy, strategy_version, setup, catalyst, regime,
                          score, breakdown, entry, stop, targets, reward_risk, position_size,
                          invalidation, major_risks, confidence, sources, mode: TradeMode,
                          data_timestamp: dt.datetime, decision="PENDING", rejection_reason=None) -> Candidate:
        c = Candidate(
            data_timestamp=data_timestamp, ticker=ticker, strategy=strategy,
            strategy_version=strategy_version, setup_json=setup, catalyst_json=catalyst,
            market_regime_json=regime, score=score, score_breakdown_json=breakdown,
            entry=entry, stop=stop, targets_json=targets, reward_risk=reward_risk,
            position_size=position_size, invalidation=invalidation,
            # major_risks is a TEXT column but every caller passes a list
            # (Setup.prohibited_conditions). SQLite refused to bind it, which
            # aborted the transaction and left the whole candidate unjournaled --
            # a rejected setup that vanishes instead of being recorded is exactly
            # the training data the spec says must be kept. Serialised here, at
            # the persistence boundary, so callers can keep passing a list.
            major_risks=_as_text(major_risks),
            confidence=confidence, sources_json=sources, decision=decision,
            rejection_reason=rejection_reason, mode=mode,
        )
        self.session.add(c)
        self.session.commit()
        return c

    def record_data_fault(self, *, ticker: str, mode: TradeMode,
                          detected_at: dt.datetime, detail: str,
                          context: dict) -> Candidate:
        """Record a candidate refused for a DATA-INTEGRITY reason, not a strategy one.

        Written to the same candidates table as every other refusal, because the
        spec requires rejected setups to be retained as training information and
        a data fault that leaves no row is indistinguishable from "no setup
        found". strategy is recorded as "n/a" since the refusal happened before
        any strategy was consulted -- attributing it to a strategy would poison
        that strategy's measured hit rate.
        """
        c = Candidate(
            data_timestamp=detected_at, ticker=ticker, strategy="n/a",
            strategy_version="n/a", setup_json={}, catalyst_json=None,
            market_regime_json=None, score=0.0,
            score_breakdown_json={"not_scored": "refused before scoring"},
            entry=None, stop=None, targets_json=None, reward_risk=None,
            position_size=None, invalidation=None,
            major_risks=_as_text(["data_integrity_fault"]),
            confidence=None, sources_json=None, decision="REJECTED",
            rejection_reason=detail, mode=mode,
        )
        c.setup_json = {"data_fault": context}
        self.session.add(c)
        self.session.commit()
        return c

    def record_rejection(self, candidate: Candidate, reason: str) -> None:
        candidate.decision = "REJECTED"
        candidate.rejection_reason = reason
        self.session.commit()

    def record_risk_event(self, *, candidate_id, decision, rule_triggered, inputs, message) -> RiskEvent:
        e = RiskEvent(candidate_id=candidate_id, decision=decision, rule_triggered=rule_triggered,
                       inputs_json=inputs, message=message)
        self.session.add(e)
        self.session.commit()
        return e

    def open_order(self, *, candidate_id, mode, ticker, side, order_type, qty, intended_entry,
                    stop, targets, strategy) -> Order:
        o = Order(candidate_id=candidate_id, mode=mode, ticker=ticker, side=side,
                   order_type=order_type, qty=qty, intended_entry=intended_entry, stop=stop,
                   targets_json=targets, strategy=strategy, state=OrderState.PROPOSED)
        self.session.add(o)
        self.session.commit()
        self.record_order_event(o.id, None, OrderState.PROPOSED, "created")
        return o

    def record_order_event(self, order_id, from_state, to_state: OrderState, reason,
                            broker_response=None, data_timestamp=None) -> OrderEvent:
        ev = OrderEvent(order_id=order_id, from_state=from_state.value if from_state else None,
                         to_state=to_state.value, reason=reason, broker_response_json=broker_response,
                         data_timestamp=data_timestamp)
        self.session.add(ev)
        self.session.commit()
        return ev

    def update_order_state(self, order: Order, new_state: OrderState, reason: str, **fields) -> None:
        old = order.state
        for k, v in fields.items():
            setattr(order, k, v)
        order.state = new_state
        self.session.commit()
        self.record_order_event(order.id, old, new_state, reason)

    def record_paper_trade_performance(
        self,
        order: Order,
        *,
        strategy_version: str | None = None,
        actual_fill_price: float | None = None,
        exit_price: float | None = None,
        realized_pnl: float | None = None,
        direction: str | None = None,
        highs: list[float] | None = None,
        lows: list[float] | None = None,
        catalyst_summary: str | None = None,
        market_regime: dict | None = None,
        time_of_day: str | None = None,
        rejection_reason: str | None = None,
        exit_reason: str | None = None,
        decision_data_timestamp: dt.datetime | None = None,
        fill_data_timestamp: dt.datetime | None = None,
        exit_data_timestamp: dt.datetime | None = None,
        performance_data_timestamp: dt.datetime | None = None,
    ) -> TradePerformance:
        """Persist deterministic performance facts for one PAPER order.

        No values are estimated: slippage, R, excursions, and duration are
        computed only when their required recorded inputs are supplied.  The
        one-record-per-order database constraint prevents duplicate outcomes.
        """
        if order.mode != TradeMode.PAPER:
            raise ValueError(
                f"Performance records are reserved for PAPER orders; order {order.id} mode is {order.mode.value}."
            )
        if order.performance is not None:
            raise ValueError(f"Performance is already recorded for PAPER order {order.id}.")

        intended_entry = order.intended_entry
        fill_price = actual_fill_price if actual_fill_price is not None else order.fill_price
        if fill_price is not None and intended_entry is not None:
            slippage_absolute, slippage_pct = calculate_slippage(intended_entry, fill_price)
        else:
            slippage_absolute, slippage_pct = None, None

        trade_direction = direction or ("long" if order.side in {"BUY", "COVER"} else "short")
        r_multiple = None
        r_entry = fill_price if fill_price is not None else intended_entry
        if r_entry is not None and order.stop is not None and (exit_price is not None or realized_pnl is not None):
            r_multiple = calculate_r_multiple(
                entry_price=r_entry,
                stop_price=order.stop,
                exit_price=exit_price,
                direction=trade_direction,
                quantity=order.qty,
                realized_pnl=realized_pnl,
            )

        mfe = mae = None
        if r_entry is not None and highs is not None and lows is not None:
            mfe, mae = calculate_mfe_mae(
                entry_price=r_entry, highs=highs, lows=lows, direction=trade_direction
            )

        holding_duration_seconds = None
        if fill_data_timestamp is not None and exit_data_timestamp is not None:
            holding_duration_seconds = calculate_holding_duration(
                fill_data_timestamp, exit_data_timestamp
            )
        if time_of_day is None and fill_data_timestamp is not None:
            time_of_day = fill_data_timestamp.timetz().isoformat()

        record = TradePerformance(
            order_id=order.id,
            candidate_id=order.candidate_id,
            mode=TradeMode.PAPER,
            ticker=order.ticker,
            strategy_name=order.strategy,
            strategy_version=strategy_version,
            intended_entry=intended_entry,
            stop=order.stop,
            targets_json=order.targets_json,
            actual_fill_price=fill_price,
            slippage_absolute=slippage_absolute,
            slippage_pct=slippage_pct,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            r_multiple=r_multiple,
            mfe=mfe,
            mae=mae,
            catalyst_summary=catalyst_summary,
            market_regime_json=market_regime,
            time_of_day=time_of_day,
            holding_duration_seconds=holding_duration_seconds,
            rejection_reason=rejection_reason,
            exit_reason=exit_reason,
            decision_data_timestamp=decision_data_timestamp,
            fill_data_timestamp=fill_data_timestamp,
            exit_data_timestamp=exit_data_timestamp,
            performance_data_timestamp=performance_data_timestamp,
        )
        self.session.add(record)
        self.session.commit()
        return record

    def record_circuit_breaker(self, *, trigger, details, session_date) -> CircuitBreakerEvent:
        e = CircuitBreakerEvent(trigger=trigger, details_json=details, session_date=session_date)
        self.session.add(e)
        self.session.commit()
        return e

    # ---- read helpers for dashboard/analytics ------------------------
    def candidates_today(self, session_date: str) -> list[Candidate]:
        return (
            self.session.query(Candidate)
            .filter(Candidate.data_timestamp.isnot(None))
            .all()
        )

    def open_orders(self) -> list[Order]:
        return self.session.query(Order).filter(Order.state.notin_([
            OrderState.CLOSED, OrderState.REJECTED, OrderState.CANCELLED,
            OrderState.EXPIRED, OrderState.RISK_REJECTED,
        ])).all()

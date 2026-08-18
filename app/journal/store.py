"""
Trade Journal (spec §19). Records every considered setup — traded AND
rejected — with full fidelity. This module is the only place strategy or
scanner code writes Candidate/Order rows; it centralizes the "append,
don't mutate" discipline (spec §33) instead of leaving it to each caller.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.common.db import Candidate, CircuitBreakerEvent, Order, OrderEvent, RiskEvent, TradeMode, OrderState


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
            position_size=position_size, invalidation=invalidation, major_risks=major_risks,
            confidence=confidence, sources_json=sources, decision=decision,
            rejection_reason=rejection_reason, mode=mode,
        )
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

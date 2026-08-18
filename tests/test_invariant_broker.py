"""Broker acknowledgement, state, and reconciliation safety invariants."""
from __future__ import annotations

import pytest

from app.broker.base import BrokerError, BrokerOrderStatus, OrderRequest, Position
from app.common.db import OrderState, TradeMode
from app.common.modes import Mode
from app.execution.engine import ExecutionEngine, SubmissionUncertainError
from app.execution.lifecycle import OrderLifecycleManager
from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
from tests.helpers import grant_for, intent_for
from tests.invariant_support import OneTickerProvider, TestBroker, journal_for


def _approved_order(journal):
    order = journal.open_order(
        candidate_id=None, mode=TradeMode.PAPER, ticker="SAFE", side="BUY",
        order_type="market", qty=1, intended_entry=100, stop=99, targets=[],
        strategy="test",
    )
    return order


def _risk_approved(lifecycle, order):
    lifecycle.mark_risk_approved(order, "test")


def test_rejected_broker_order_is_not_recorded_as_a_fill(tmp_path):
    """A broker rejection must remain rejected, or a refused order could be counted as exposure."""
    status = BrokerOrderStatus("r1", "rejected", 0, None, {})
    broker, journal = TestBroker(submit_status=status, status=status), journal_for(tmp_path)
    lifecycle, order = OrderLifecycleManager(broker, journal), _approved_order(journal)
    _risk_approved(lifecycle, order)
    req, grant = OrderRequest("SAFE", "BUY", 1, "market"), None
    grant = grant_for(req, Mode.PAPER)  # noqa: F821
    receipt = ExecutionEngine(broker).submit(req, grant, intent_for(req, Mode.PAPER))
    lifecycle.mark_submitted(order, receipt)
    assert lifecycle.refresh_from_broker(order) is OrderState.REJECTED
    assert order.state is not OrderState.FILLED


def test_partial_fill_stays_partial_until_broker_reports_full_fill(tmp_path):
    """A partial fill must remain partial, or risk controls could understate residual open exposure."""
    status = BrokerOrderStatus("p1", "partially_filled", 0.5, 100, {})
    broker, journal = TestBroker(submit_status=status, status=status), journal_for(tmp_path)
    lifecycle, order = OrderLifecycleManager(broker, journal), _approved_order(journal)
    _risk_approved(lifecycle, order)
    req = OrderRequest("SAFE", "BUY", 1, "market")
    receipt = ExecutionEngine(broker).submit(req, grant_for(req, Mode.PAPER), intent_for(req, Mode.PAPER))
    lifecycle.mark_submitted(order, receipt)
    assert lifecycle.refresh_from_broker(order) is OrderState.PARTIALLY_FILLED


def test_timeout_after_submission_is_unknown_and_never_retried():
    """An ambiguous submission must halt as UNKNOWN without retrying, or a timeout could double a position."""
    calls = []
    broker = TestBroker(submit_error=BrokerError("transport timeout"))
    engine = ExecutionEngine(broker, on_submission_uncertain=lambda **kw: calls.append(kw))
    req = OrderRequest("SAFE", "BUY", 1, "market")
    with pytest.raises(SubmissionUncertainError):
        engine.submit(req, grant_for(req, Mode.PAPER), intent_for(req, Mode.PAPER))
    assert len(broker.submissions) == 1
    assert len(calls) == 1


def test_duplicate_partial_broker_event_is_idempotent(tmp_path):
    """A duplicated partial-fill event must not advance state twice, or replayed events could corrupt exposure records."""
    status = BrokerOrderStatus("p2", "partially_filled", 0.5, 100, {})
    broker, journal = TestBroker(status=status), journal_for(tmp_path)
    lifecycle, order = OrderLifecycleManager(broker, journal), _approved_order(journal)
    _risk_approved(lifecycle, order)
    req = OrderRequest("SAFE", "BUY", 1, "market")
    lifecycle.mark_submitted(order, ExecutionEngine(broker).submit(
        req, grant_for(req, Mode.PAPER), intent_for(req, Mode.PAPER)))
    assert lifecycle.refresh_from_broker(order) is OrderState.PARTIALLY_FILLED
    assert lifecycle.refresh_from_broker(order) is OrderState.PARTIALLY_FILLED


def test_unknown_broker_status_fails_to_unknown(tmp_path):
    """An unrecognized broker state must become UNKNOWN, or new exchange states could be guessed as safe."""
    status = BrokerOrderStatus("u1", "mystery_state", 0, None, {})
    broker, journal = TestBroker(status=status), journal_for(tmp_path)
    lifecycle, order = OrderLifecycleManager(broker, journal), _approved_order(journal)
    _risk_approved(lifecycle, order)
    req = OrderRequest("SAFE", "BUY", 1, "market")
    lifecycle.mark_submitted(order, ExecutionEngine(broker).submit(
        req, grant_for(req, Mode.PAPER), intent_for(req, Mode.PAPER)))
    assert lifecycle.refresh_from_broker(order) is OrderState.UNKNOWN


def test_submit_receipt_never_marks_filled_without_fresh_broker_query(tmp_path):
    """A submit receipt alone must not mark FILLED, or an acknowledgement could be mistaken for a confirmed trade."""
    optimistic = BrokerOrderStatus("a1", "filled", 1, 100, {})
    broker, journal = TestBroker(submit_status=optimistic), journal_for(tmp_path)
    lifecycle, order = OrderLifecycleManager(broker, journal), _approved_order(journal)
    _risk_approved(lifecycle, order)
    req = OrderRequest("SAFE", "BUY", 1, "market")
    receipt = ExecutionEngine(broker).submit(req, grant_for(req, Mode.PAPER), intent_for(req, Mode.PAPER))
    lifecycle.mark_submitted(order, receipt)
    assert order.state is OrderState.SUBMITTED


def test_disconnect_blocks_pipeline_before_new_entries(tmp_path):
    """A disconnected broker must block entry before submission, or the system could trade blind without confirmation."""
    from tests.test_invariant_market_data import _run
    result, broker, _ = _run(tmp_path, OneTickerProvider(), TestBroker(connected=False))
    assert result.halted
    assert not broker.submissions


def test_unexpected_broker_position_blocks_new_entries(tmp_path):
    """An unexpected broker position must block entries, or sizing could ignore real exposure of unknown provenance."""
    from tests.test_invariant_market_data import _run
    pos = Position("SAFE", 3, 100, 100, 0, "long", __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    result, broker, _ = _run(tmp_path, OneTickerProvider(), TestBroker(positions=[pos]))
    assert result.halted
    assert "reconciliation" in result.halt_reason
    assert not broker.submissions


def test_local_broker_position_disagreement_blocks_new_entries(tmp_path):
    """A local-versus-broker position disagreement must block entries, or risk could be sized from a false book."""
    from tests.test_invariant_market_data import _run
    journal = journal_for(tmp_path)
    order = _approved_order(journal)
    journal.update_order_state(order, OrderState.FILLED, "test recovery fixture")
    # Rebuild the same journal path through the pipeline helper manually.
    from app.catalyst.engine import CatalystEngine, NullNewsProvider
    from app.common.modes import Mode
    from app.execution.authorization import ExecutionAuthorizer
    from app.orchestration.pipeline import run_pipeline
    from app.risk.engine import RiskEngine
    from app.scanner.base import ScanCriteria
    from app.strategy.scoring import OpportunityScorer
    from tests.invariant_support import AlwaysSetup, RISK_CFG, WEIGHTS
    broker = TestBroker()
    result = run_pipeline(
        mode=Mode.PAPER, provider=OneTickerProvider(), criteria=ScanCriteria(), strategies=[AlwaysSetup({})],
        scorer=OpportunityScorer(WEIGHTS), catalyst_engine=CatalystEngine([NullNewsProvider()]),
        risk_engine=RiskEngine(RISK_CFG), risk_cfg=RISK_CFG, broker=broker, journal=journal,
        authorizer=ExecutionAuthorizer(target_mode=Mode.PAPER),
        circuit_breaker=PersistentCircuitBreaker(tmp_path / "breaker.db"),
        config_mode=Mode.PAPER, config_mode_source="default.yaml", min_score_to_consider=0,
    )
    assert result.halted
    assert not broker.submissions

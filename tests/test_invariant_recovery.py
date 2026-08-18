"""Crash-recovery and durable-state safety invariants."""
from __future__ import annotations

import datetime as dt

import pytest
from types import SimpleNamespace

from app.broker.base import BrokerOrderStatus, Position
from app.common.db import OrderState, TradeMode
from app.execution.lifecycle import OrderLifecycleManager
from tests.invariant_support import OneTickerProvider, TestBroker, journal_for


def _open(journal, state, broker_order_id=None):
    order = journal.open_order(
        candidate_id=None, mode=TradeMode.PAPER, ticker="SAFE", side="BUY",
        order_type="market", qty=1, intended_entry=100, stop=99, targets=[],
        strategy="recovery",
    )
    journal.update_order_state(order, state, "crash fixture", broker_order_id=broker_order_id)
    return order


def _run_existing(tmp_path, journal, broker):
    from app.catalyst.engine import CatalystEngine, NullNewsProvider
    from app.common.modes import Mode
    from app.execution.authorization import ExecutionAuthorizer
    from app.orchestration.pipeline import run_pipeline
    from app.risk.engine import RiskEngine
    from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
    from app.scanner.base import ScanCriteria
    from app.strategy.scoring import OpportunityScorer
    from tests.invariant_support import AlwaysSetup, RISK_CFG, WEIGHTS
    return run_pipeline(
        mode=Mode.PAPER, provider=OneTickerProvider(), criteria=ScanCriteria(), strategies=[AlwaysSetup({})],
        scorer=OpportunityScorer(WEIGHTS), catalyst_engine=CatalystEngine([NullNewsProvider()]),
        risk_engine=RiskEngine(RISK_CFG), risk_cfg=RISK_CFG, broker=broker, journal=journal,
        authorizer=ExecutionAuthorizer(target_mode=Mode.PAPER),
        circuit_breaker=PersistentCircuitBreaker(tmp_path / "breaker.db"),
        config_mode=Mode.PAPER, config_mode_source="default.yaml", min_score_to_consider=0,
        session_service=SimpleNamespace(
            current_session=lambda: SimpleNamespace(
                session="REGULAR", reason="", scheduled_close=None, is_unknown=False
            ),
            permits_orders=lambda *_: True,
        ),
    )


def test_crash_before_submission_blocks_new_entries_after_restart(tmp_path):
    """A crash before submission must block recovery until resolved, or a pending local decision could be silently forgotten."""
    first = journal_for(tmp_path)
    _open(first, OrderState.RISK_APPROVED)
    restarted = journal_for(tmp_path)
    broker = TestBroker()
    result = _run_existing(tmp_path, restarted, broker)
    assert result.halted
    assert not broker.submissions


def test_crash_after_submission_blocks_new_entries_after_restart(tmp_path):
    """A crash after submission must block recovery until broker state is resolved, or retry-like new entries could double exposure."""
    first = journal_for(tmp_path)
    _open(first, OrderState.SUBMITTED, "maybe-sent")
    restarted = journal_for(tmp_path)
    broker = TestBroker()
    result = _run_existing(tmp_path, restarted, broker)
    assert result.halted
    assert not broker.submissions


def test_crash_after_partial_fill_preserves_exposure_and_blocks_duplicate_entry(tmp_path):
    """A partial fill must survive restart as exposure, or recovery could open the same position twice."""
    first = journal_for(tmp_path)
    _open(first, OrderState.PARTIALLY_FILLED, "partial-1")
    restarted = journal_for(tmp_path)
    pos = Position("SAFE", 1, 100, 100, 0, "long", dt.datetime.now(dt.timezone.utc))
    broker = TestBroker(positions=[pos])
    result = _run_existing(tmp_path, restarted, broker)
    assert not broker.submissions
    # A partially-filled order absent from the broker's working list means the
    # remainder is gone and the actually-filled quantity is unconfirmed. The
    # earlier expectation here was that the run would continue and merely reject
    # the symbol as a duplicate; that is weaker than PART 12 requires, because
    # continuing implies the exposure is known when it is not.
    assert result.halted
    assert "missing_broker_order" in (result.halt_reason or "")


def test_restart_while_holding_position_keeps_duplicate_entry_blocked(tmp_path):
    """A held position must remain visible after restart, or the system could exceed its one-symbol exposure limit."""
    first = journal_for(tmp_path)
    _open(first, OrderState.FILLED, "filled-1")
    restarted = journal_for(tmp_path)
    pos = Position("SAFE", 1, 100, 100, 0, "long", dt.datetime.now(dt.timezone.utc))
    result = _run_existing(tmp_path, restarted, TestBroker(positions=[pos]))
    assert not result.halted
    assert not result.outcomes[0].rejection_reason is None
    assert result.outcomes[0].rejection_reason == "duplicate_position"


def test_restart_with_open_order_blocks_new_entries(tmp_path):
    """An open order at restart must block new entries until reconciled, or an unknown pending order could create duplicate exposure."""
    first = journal_for(tmp_path)
    _open(first, OrderState.ACKNOWLEDGED, "open-1")
    restarted = journal_for(tmp_path)
    result = _run_existing(tmp_path, restarted, TestBroker())
    assert result.halted

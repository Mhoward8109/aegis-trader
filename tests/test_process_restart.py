"""Real-process crash/restart checks for durable ambiguous order state."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest


WRITER_SCRIPT = r"""
import json
import os
import sys
from sqlalchemy.orm import Session
from app.common.db import OrderState, TradeMode, init_db
from app.journal.store import TradeJournal

db_path, state_name, broker_id = sys.argv[1:4]
journal = TradeJournal(Session(init_db(db_path)))
order = journal.open_order(
    candidate_id=None, mode=TradeMode.PAPER, ticker="SPY", side="BUY",
    order_type="bracket", qty=1, intended_entry=500, stop=495,
    targets=[510], strategy="process-restart-test",
)
order.state = OrderState[state_name]
order.broker_order_id = broker_id or None
journal.session.commit()
print(json.dumps({"order_id": order.id, "state": state_name, "broker_id": broker_id}))
sys.stdout.flush()
os._exit(97)
"""


READER_SCRIPT = r"""
import datetime as dt
import json
import sys
from sqlalchemy.orm import Session
from app.broker.base import BrokerOrderStatus, Position
from app.common.db import init_db
from app.execution.lifecycle import OrderLifecycleManager
from app.journal.store import TradeJournal
from app.orchestration.pipeline import _local_positions
from tests.invariant_support import TestBroker

db_path, broker_case = sys.argv[1:3]
journal = TradeJournal(Session(init_db(db_path)))
open_orders = []
positions = []
if broker_case == "same_order":
    local = journal.open_orders()[0]
    open_orders = [BrokerOrderStatus(local.broker_order_id, "accepted", 0, None, {"symbol": "SPY"})]
elif broker_case == "other_order":
    open_orders = [BrokerOrderStatus("outside-order", "accepted", 0, None, {"symbol": "SPY"})]
elif broker_case == "qty_mismatch":
    positions = [Position("SPY", 2, 500, 500, 0, "long", dt.datetime.now(dt.timezone.utc))]

broker = TestBroker(open_orders=open_orders, positions=positions)
report = OrderLifecycleManager(broker, journal).reconcile(
    journal.open_orders(), _local_positions(journal)
)
print(json.dumps(report.as_record(), sort_keys=True))
"""


SCENARIOS = [
    ("PROPOSED", "", "empty"),
    ("RISK_APPROVED", "", "empty"),
    ("SUBMITTED", "", "empty"),
    ("ACKNOWLEDGED", "", "empty"),
    ("PARTIALLY_FILLED", "", "empty"),
    ("UNKNOWN", "", "empty"),
    ("SUBMITTED", "broker-1", "empty"),
    ("ACKNOWLEDGED", "broker-1", "empty"),
    ("PARTIALLY_FILLED", "broker-1", "empty"),
    ("UNKNOWN", "broker-1", "empty"),
    ("FILLED", "broker-1", "empty"),
    ("EXIT_PENDING", "broker-1", "empty"),
    ("SUBMITTED", "broker-1", "other_order"),
    ("ACKNOWLEDGED", "broker-1", "other_order"),
    ("FILLED", "broker-1", "qty_mismatch"),
]


@pytest.mark.parametrize("state,broker_id,broker_case", SCENARIOS)
def test_genuine_process_restart_blocks_ambiguous_state(
    tmp_path, state, broker_id, broker_case
):
    db_path = tmp_path / "restart-journal.db"
    writer = subprocess.run(
        [sys.executable, "-c", WRITER_SCRIPT, str(db_path), state, broker_id],
        cwd=str(__import__("pathlib").Path(__file__).parents[1]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert writer.returncode == 97, writer.stderr
    handoff = json.loads(writer.stdout.strip())
    assert handoff["state"] == state

    reader = subprocess.run(
        [sys.executable, "-c", READER_SCRIPT, str(db_path), broker_case],
        cwd=str(__import__("pathlib").Path(__file__).parents[1]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert reader.returncode == 0, reader.stderr
    report = json.loads(reader.stdout.strip())
    assert report["blocks_trading"] is True
    assert report["discrepancies"]


def test_writer_uses_flush_without_pipe_fsync_and_controlled_hard_exit():
    assert "sys.stdout.flush()" in WRITER_SCRIPT
    assert "os.fsync" not in WRITER_SCRIPT
    assert "os._exit(97)" in WRITER_SCRIPT

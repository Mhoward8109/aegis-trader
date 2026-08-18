"""Small deterministic collaborators used by adversarial invariant tests."""
from __future__ import annotations

import datetime as dt
import uuid

from app.broker.base import (
    AccountSnapshot, BrokerAdapter, BrokerError, BrokerOrderStatus, OrderRequest,
    Position, Quote,
)
from app.common.db import init_db
from app.execution.authorization import BrokerEnvironment, ExecutionIntent
from app.journal.store import TradeJournal
from app.scanner.base import MarketDataProvider, ScanCriteria, ScanResult
from app.strategy.base import Setup, Strategy
from sqlalchemy.orm import Session


NOW = dt.datetime.now(dt.timezone.utc)


class OneTickerProvider(MarketDataProvider):
    """A fresh, coherent provider whose failure modes can be changed per test."""

    supported_fields = {"price_min", "price_max", "rvol_min", "dollar_volume_min"}

    def __init__(self, *, quote=None, bars=None, quote_error=None, bars_error=None):
        self.quote = quote or Quote("SAFE", 99.9, 100.1, 100.0, NOW, "test")
        if bars is None:
            import pandas as pd
            bars = pd.DataFrame([{
                "timestamp": NOW, "open": 99.5, "high": 100.5, "low": 99.0,
                "close": 100.0, "volume": 1_000_000,
            }])
        self.bars = bars
        self.quote_error = quote_error
        self.bars_error = bars_error

    def scan(self, criteria: ScanCriteria):
        return [ScanResult(
            ticker="SAFE",
            fields={"price": 100.0, "rvol": 2.0, "dollar_volume": 20_000_000,
                    "spread_pct": 0.1},
            unavailable_fields=[], data_timestamp=NOW, source="test",
        )]

    def get_bars(self, ticker, timeframe, start, end):
        if self.bars_error:
            raise self.bars_error
        return self.bars

    def get_quote(self, ticker):
        if self.quote_error:
            raise self.quote_error
        return self.quote


class AlwaysSetup(Strategy):
    """A strategy deliberately guaranteed to reach the gates under test."""

    name = "always_setup"
    version = "test"

    def market_conditions_ok(self, ctx): return True
    def candidate_criteria_met(self, ctx): return True
    def setup_conditions_met(self, ctx): return True
    def confirmation_met(self, ctx): return True
    def entry_trigger(self, ctx): return True

    def build_setup(self, ctx):
        return Setup(
            ticker=ctx.ticker, strategy=self.name, strategy_version=self.version,
            direction="long", entry=100.0, stop=99.0, targets=[],
            invalidation="test", confirmation_notes="test", max_spread_pct=1.0,
            min_liquidity_usd=1_000_000, permitted_sessions=["REGULAR"],
            prohibited_conditions=[], raw_signals={},
        )


class TestBroker(BrokerAdapter):
    """In-memory PAPER adapter with controllable broker responses."""

    environment = BrokerEnvironment.PAPER
    name = "test-paper"
    __test__ = False

    def __init__(self, *, submit_status=None, status=None, positions=None,
                 open_orders=None, connected=True, submit_error=None,
                 status_error=None):
        self.submit_status = submit_status or BrokerOrderStatus(
            "submitted-1", "accepted", 0.0, None, {"symbol": "SAFE"}
        )
        self.status = status or BrokerOrderStatus(
            "submitted-1", "accepted", 0.0, None, {"symbol": "SAFE"}
        )
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.connected = connected
        self.submit_error = submit_error
        self.status_error = status_error
        self.submissions = []

    def is_connected(self): return self.connected

    def get_account(self):
        return AccountSnapshot(100_000, 100_000, 100_000, "USD", NOW)

    def get_buying_power(self): return 100_000
    def get_positions(self): return list(self.positions)
    def get_open_orders(self): return list(self.open_orders)
    def get_quote(self, ticker): return Quote(ticker, 99.9, 100.1, 100.0, NOW, "test")

    def submit_order(self, req: OrderRequest, grant):
        intent = ExecutionIntent(
            mode=grant.mode, ticker=req.ticker, side=req.side, qty=req.qty,
            order_type=req.order_type,
        )
        self.assert_grant_authorizes(grant, intent)
        self.submissions.append(req)
        if self.submit_error:
            raise self.submit_error
        return self.submit_status

    def submit_protective_order(self, req):
        self._assert_risk_reducing(req)
        return BrokerOrderStatus("protective-1", "accepted", 0, None, {})

    def modify_order(self, broker_order_id, **changes): return self.status
    def cancel_order(self, broker_order_id): return None
    def cancel_all_orders(self): return None
    def close_position(self, ticker): return BrokerOrderStatus("close-1", "filled", 1, 100, {})
    def close_all_positions(self): return None

    def get_order_status(self, broker_order_id):
        if self.status_error:
            raise self.status_error
        return self.status

    def get_trade_history(self, since=None): return []


def journal_for(tmp_path):
    """Make a new journal at the test's isolated durable path."""
    return TradeJournal(Session(init_db(str(tmp_path / "journal.db"))))


RISK_CFG = {
    "max_risk_per_trade_pct": 0.5, "max_risk_per_trade_usd": None,
    "max_daily_loss_pct": 2.0, "max_daily_loss_usd": None,
    "max_weekly_loss_pct": 5.0, "max_trades_per_day": 10,
    "max_concurrent_positions": 5, "max_position_pct_of_account": 20.0,
    "max_sector_exposure_pct": 40.0, "max_spread_pct": 1.0,
    "min_liquidity_avg_dollar_vol": 1_000_000, "max_slippage_pct": 1.0,
    "max_consecutive_losses": 3,
}

WEIGHTS = {
    "catalyst_quality": 15, "catalyst_freshness": 10, "relative_volume": 15,
    "liquidity": 10, "spread_quality": 5, "technical_alignment": 15,
    "market_trend": 10, "reward_risk": 10, "data_confidence": 5,
    "historical_strategy_performance": 5,
}

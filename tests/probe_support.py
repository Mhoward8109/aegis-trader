"""Deterministic collaborators for PAPER probe safety tests."""
from __future__ import annotations

import dataclasses
import datetime as dt

import pandas as pd

from app.broker.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerOrderStatus,
    Position,
    Quote,
)
from app.common.modes import Mode
from app.execution.authorization import BrokerEnvironment, ExecutionAuthorizer, ExecutionIntent
from app.marketdata.regime import RegimeSnapshot
from app.marketdata.session import REGULAR, SessionState
from app.paper_runtime import PaperRuntime
from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker


NOW = dt.datetime.now(dt.timezone.utc)


class RecordingBroker(BrokerAdapter):
    environment = BrokerEnvironment.PAPER
    name = "recording-paper"

    def __init__(
        self,
        *,
        now: dt.datetime = NOW,
        account: AccountSnapshot | None = None,
        quote: Quote | None = None,
        positions: list[Position] | None = None,
        open_orders: list[BrokerOrderStatus] | None = None,
        submit_status: BrokerOrderStatus | None = None,
        status_sequence: list[BrokerOrderStatus | BaseException] | None = None,
        read_error: BaseException | None = None,
        submit_error: BaseException | None = None,
    ):
        self.now = now
        self.account = account or AccountSnapshot(
            100_000, 100_000, 100_000, "USD", now
        )
        self.quote = quote or Quote("SPY", 499.9, 500.0, 499.95, now, "test")
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.submit_status = submit_status or BrokerOrderStatus(
            "probe-1", "accepted", 0.0, None, {"symbol": "SPY"}
        )
        self.status_sequence = list(
            status_sequence
            or [BrokerOrderStatus("probe-1", "accepted", 0.0, None, {"symbol": "SPY"})]
        )
        self.read_error = read_error
        self.submit_error = submit_error
        self.mutations: list[tuple[str, object]] = []
        self.submissions = []

    def _read(self):
        if self.read_error:
            raise self.read_error

    def get_account(self):
        self._read()
        return self.account

    def get_buying_power(self):
        self._read()
        return self.account.buying_power

    def get_positions(self):
        self._read()
        return list(self.positions)

    def get_open_orders(self):
        self._read()
        return list(self.open_orders)

    def get_quote(self, ticker):
        self._read()
        return dataclasses.replace(self.quote, ticker=ticker)

    def submit_order(self, req, grant):
        intent = ExecutionIntent(
            mode=grant.mode,
            ticker=req.ticker,
            side=req.side,
            qty=req.qty,
            order_type=req.order_type,
        )
        self.assert_grant_authorizes(grant, intent)
        self.mutations.append(("submit_order", req))
        self.submissions.append(req)
        if self.submit_error:
            raise self.submit_error
        return self.submit_status

    def submit_protective_order(self, req):
        self.mutations.append(("submit_protective_order", req))
        return BrokerOrderStatus("protective", "accepted", 0, None, {})

    def modify_order(self, broker_order_id, **changes):
        self.mutations.append(("modify_order", broker_order_id))
        return self.submit_status

    def cancel_order(self, broker_order_id):
        self.mutations.append(("cancel_order", broker_order_id))
        self.status_sequence = [
            BrokerOrderStatus(broker_order_id, "canceled", 0.0, None, {"symbol": "SPY"})
        ]

    def cancel_all_orders(self):
        self.mutations.append(("cancel_all_orders", None))

    def close_position(self, ticker):
        self.mutations.append(("close_position", ticker))
        return BrokerOrderStatus("close", "filled", 1.0, 500.0, {})

    def close_all_positions(self):
        self.mutations.append(("close_all_positions", None))

    def get_order_status(self, broker_order_id):
        self._read()
        value = self.status_sequence.pop(0) if len(self.status_sequence) > 1 else self.status_sequence[0]
        if isinstance(value, BaseException):
            raise value
        if str(value.status).lower() == "filled" and value.filled_qty > 0:
            self.positions = [
                Position(
                    value.raw.get("symbol", "SPY"),
                    value.filled_qty,
                    value.filled_avg_price or self.quote.last,
                    self.quote.last,
                    0.0,
                    "long",
                    self.now,
                )
            ]
        return value

    def get_trade_history(self, since=None):
        self._read()
        return []


class MarketData:
    def __init__(self, *, now: dt.datetime = NOW, error=None, volume=1_000_000):
        self.now = now
        self.error = error
        self.volume = volume

    def get_bars(self, ticker, timeframe, start, end):
        if self.error:
            raise self.error
        frame = pd.DataFrame(
            [{
                "timestamp": self.now,
                "open": 499.0,
                "high": 501.0,
                "low": 498.0,
                "close": 500.0,
                "volume": self.volume,
            }]
        )
        frame.attrs["data_timestamp"] = self.now
        frame.attrs["source"] = "test"
        return frame


class SessionService:
    def __init__(self, state=None, error=None):
        self.state = state or SessionState(REGULAR, NOW, is_open=True)
        self.error = error

    def current_session(self):
        if self.error:
            raise self.error
        return self.state


class RegimeEngine:
    def __init__(self, *, unknown=False, error=None):
        self.unknown = unknown
        self.error = error

    def build(self):
        if self.error:
            raise self.error
        direction = "unknown" if self.unknown else "up"
        return RegimeSnapshot(
            direction,
            direction,
            direction,
            15.0,
            "normal",
            None,
            "trending",
            "neutral",
            NOW.isoformat(),
        )


class SecResult(list):
    source_url = "https://www.sec.gov/files/company_tickers.json"


class SecProvider:
    def __init__(self, error=None):
        self.error = error

    def research(self, ticker, since):
        if self.error:
            raise self.error
        return SecResult()


def runtime_for(
    tmp_path,
    *,
    broker=None,
    market_data=None,
    session_service=None,
    regime_engine=None,
    sec_provider=None,
    authorizer=None,
    breaker=None,
):
    return PaperRuntime(
        broker=broker or RecordingBroker(),
        market_data=market_data or MarketData(),
        session_service=session_service or SessionService(),
        regime_engine=regime_engine or RegimeEngine(),
        sec_provider=sec_provider if sec_provider is not None else SecProvider(),
        circuit_breaker=breaker or PersistentCircuitBreaker(tmp_path / "breaker.db"),
        authorizer=authorizer or ExecutionAuthorizer(target_mode=Mode.PAPER),
    )

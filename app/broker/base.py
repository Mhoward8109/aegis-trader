"""
Broker abstraction (spec §3). Strategy/execution code depends ONLY on this
interface, never on a concrete broker SDK, so swapping Alpaca <-> IBKR <->
a future broker never touches strategy code.

Every adapter MUST declare which Mode(s) it is allowed to run under. This is
enforced structurally: ShadowBroker literally cannot submit a network order
(the method has no network call in it at all — see shadow_adapter.py),
and AlpacaBroker's constructor refuses to build a live trading client
unless allow_live=True is passed AND the caller already passed
ModeGovernor.assert_execution_allowed(Mode.LIVE) (see cli.py wiring).
"""
from __future__ import annotations

import abc
import dataclasses
from datetime import datetime


@dataclasses.dataclass
class Quote:
    ticker: str
    bid: float
    ask: float
    last: float
    timestamp: datetime
    source: str


@dataclasses.dataclass
class AccountSnapshot:
    """NOTE (per docs/ARCHITECTURE.md broker research, Aug 2026): FINRA
    retired the classic PDT rule effective June 4, 2026 (FINRA Reg. Notice
    26-10), and Alpaca has scheduled the legacy `pattern_day_trader` /
    `daytrade_count` fields for full removal from its API by July 6, 2026
    (already past as of this build). We deliberately do NOT model
    `pattern_day_trader` as a first-class field any more — gating logic
    must be re-derived from real-time buying power / intraday margin
    deficit, not a static day-trade counter. `intraday_buying_power` and
    `margin_deficit` are optional because ShadowBroker (no margin concept)
    and non-margin cash-only adapters may legitimately have neither.
    """
    equity: float
    buying_power: float
    cash: float
    currency: str
    timestamp: datetime
    intraday_buying_power: float | None = None
    margin_deficit: float | None = None


@dataclasses.dataclass
class Position:
    ticker: str
    qty: float
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float
    side: str
    timestamp: datetime


@dataclasses.dataclass
class OrderRequest:
    ticker: str
    side: str                 # BUY | SELL | SHORT | COVER
    qty: float
    order_type: str           # market|limit|stop|stop_limit|bracket|oco|trailing_stop
    limit_price: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None
    trail_percent: float | None = None
    time_in_force: str = "day"
    extended_hours: bool = False
    client_order_id: str | None = None


@dataclasses.dataclass
class BrokerOrderStatus:
    broker_order_id: str
    status: str                # broker's native status string
    filled_qty: float
    filled_avg_price: float | None
    raw: dict


class BrokerError(Exception):
    pass


class BrokerAdapter(abc.ABC):
    """Normalized operations every adapter must implement (spec §3)."""

    name: str = "base"
    supports_live: bool = False   # only real network-order adapters set True

    @abc.abstractmethod
    def get_account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    def get_buying_power(self) -> float: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def get_open_orders(self) -> list[BrokerOrderStatus]: ...

    @abc.abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abc.abstractmethod
    def submit_order(self, req: OrderRequest) -> BrokerOrderStatus: ...

    @abc.abstractmethod
    def modify_order(self, broker_order_id: str, **changes) -> BrokerOrderStatus: ...

    @abc.abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abc.abstractmethod
    def cancel_all_orders(self) -> None: ...

    @abc.abstractmethod
    def close_position(self, ticker: str) -> BrokerOrderStatus: ...

    @abc.abstractmethod
    def close_all_positions(self) -> None: ...

    @abc.abstractmethod
    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus: ...

    @abc.abstractmethod
    def get_trade_history(self, since: datetime | None = None) -> list[dict]: ...

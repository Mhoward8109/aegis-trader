"""
Broker abstraction (spec §3). Strategy/execution code depends ONLY on this
interface, never on a concrete broker SDK, so swapping Alpaca <-> IBKR <->
a future broker never touches strategy code.

MILESTONE 2 CHANGE (PART 2). Every adapter now declares a concrete
`environment: BrokerEnvironment` as a CLASS attribute, and `submit_order()`
requires an `ExecutionGrant` that the adapter independently re-verifies against
its own declared environment before touching a wire.

Milestone 1's claim that "AlpacaBroker's constructor refuses to build a live
trading client unless the caller already passed ModeGovernor" was false:
`allow_live` was an ordinary default-False boolean any caller could flip, and
nothing in the adapter could observe whether authorization had run. See
docs/AUDIT_MILESTONE2.md §3.3. The grant argument replaces that honour system:
an adapter cannot be talked into submitting without evidence, because it needs
an object it cannot construct.

The re-verification inside the adapter is deliberately redundant with the check
in ExecutionEngine. Defence in depth means a caller who bypasses the execution
engine and holds an adapter directly still cannot submit.
"""
from __future__ import annotations

import abc
import dataclasses
from datetime import datetime

from app.execution.authorization import (
    BrokerEnvironment,
    ExecutionGrant,
    ExecutionIntent,
)


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

    #: What this adapter is ACTUALLY wired to. Subclasses must override with a
    #: real value; the base sentinel of None makes "forgot to declare it" a
    #: hard failure at authorization time rather than a silent default.
    environment: BrokerEnvironment | None = None

    def assert_grant_authorizes(self, grant: ExecutionGrant, intent: ExecutionIntent) -> None:
        """Every concrete submit_order() MUST call this first. Verifies the
        grant was issued for this exact order AND for this adapter's declared
        environment, then consumes it so it cannot authorize a second order."""
        if self.environment is None:
            raise BrokerError(
                f"{type(self).__name__} does not declare a BrokerEnvironment. "
                f"Refusing to submit: an adapter whose environment is unknown "
                f"cannot be proven to match the operating mode."
            )
        grant.assert_valid_for(intent, self.environment)
        grant.consume()

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
    def submit_order(self, req: OrderRequest, grant: ExecutionGrant) -> BrokerOrderStatus:
        """Submit an order. `grant` is not optional and has no default: a
        caller who has not been through ExecutionAuthorizer cannot even form a
        valid call to this method."""
        ...

    def submit_protective_order(self, req: OrderRequest) -> BrokerOrderStatus:
        """Submit a RISK-REDUCING order with NO ExecutionGrant.

        Deliberately ungated, and deliberately separate from `submit_order()`.
        The asymmetry is a safety property: attaching a stop, target, or OCO to a
        position that already exists must never be blocked by the entry
        authorization path. A tripped circuit breaker, a stale quote, or an
        expired grant must not be able to leave an open position unprotected.

        Because it is ungated, implementations MUST refuse anything that could
        open or increase exposure. `_assert_risk_reducing()` below enforces that,
        and every implementation must call it first.
        """
        raise BrokerError(
            f"{type(self).__name__} does not implement an ungated protective-order "
            f"path. Broker-side protection cannot be attached through this adapter, "
            f"so positions opened with it cannot be reported as protected."
        )

    @staticmethod
    def _assert_risk_reducing(req: OrderRequest) -> None:
        """Guard for the ungated protective path.

        Without this, `submit_protective_order()` would be an unauthenticated way
        to open a position -- a hole straight through the authorization boundary.
        Only exit-side order types that require a trigger price are permitted.
        """
        side = (req.side or "").upper()
        if side not in ("SELL", "COVER"):
            raise BrokerError(
                f"submit_protective_order() refuses side {side!r}. This path is "
                f"ungated precisely because it can only REDUCE exposure; BUY and "
                f"SHORT must go through submit_order() with an ExecutionGrant."
            )
        if req.order_type not in ("stop", "stop_limit", "limit", "oco", "trailing_stop"):
            raise BrokerError(
                f"submit_protective_order() refuses order_type "
                f"{req.order_type!r}; only protective exit types are permitted "
                f"on the ungated path."
            )
        if req.order_type == "oco" and (req.stop_price is None
                                        or req.take_profit_price is None):
            raise BrokerError(
                "An OCO requires BOTH a stop_price and a take_profit_price; a "
                "one-legged OCO is rejected by the broker and would leave the "
                "position believing it is protected."
            )
        if req.order_type in ("stop", "stop_limit") and req.stop_price is None:
            raise BrokerError(
                f"A {req.order_type} protective order requires a stop_price.")
        if req.order_type == "trailing_stop" and req.trail_percent is None:
            raise BrokerError(
                "A trailing_stop protective order requires trail_percent.")

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

"""
Shadow broker (MODE 1). No network calls to any broker exist in this file —
that is the safety property, not just a comment. It simulates fills
in-memory using the last quote it was given, so strategy/execution code can
be exercised end-to-end with zero possibility of touching real infrastructure.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from app.broker.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerOrderStatus,
    OrderRequest,
    Position,
    Quote,
)


class ShadowBroker(BrokerAdapter):
    name = "shadow"
    supports_live = False

    def __init__(self, starting_equity: float, quote_source):
        """quote_source: callable(ticker) -> Quote, e.g. the market-data
        pipeline's latest cached quote. No HTTP client is constructed here."""
        self._equity = starting_equity
        self._cash = starting_equity
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, BrokerOrderStatus] = {}
        self._trade_history: list[dict] = []
        self._quote_source = quote_source

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            equity=self._equity, buying_power=self._cash, cash=self._cash,
            currency="USD", timestamp=datetime.now(timezone.utc),
        )

    def get_buying_power(self) -> float:
        return self._cash

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_open_orders(self) -> list[BrokerOrderStatus]:
        return [o for o in self._orders.values() if o.status not in ("filled", "cancelled")]

    def get_quote(self, ticker: str) -> Quote:
        return self._quote_source(ticker)

    def submit_order(self, req: OrderRequest) -> BrokerOrderStatus:
        q = self._quote_source(req.ticker)
        fill_price = req.limit_price if req.order_type == "limit" and req.limit_price else q.last
        order_id = f"SHADOW-{uuid.uuid4().hex[:10]}"
        status = BrokerOrderStatus(order_id, "filled", req.qty, fill_price, raw={"shadow": True})
        self._orders[order_id] = status

        notional = req.qty * fill_price
        if req.side in ("BUY",):
            self._cash -= notional
        elif req.side in ("SELL", "COVER"):
            self._cash += notional

        existing = self._positions.get(req.ticker)
        if req.side == "BUY":
            self._positions[req.ticker] = Position(
                req.ticker, (existing.qty + req.qty) if existing else req.qty,
                fill_price, fill_price, 0.0, "long", datetime.now(timezone.utc),
            )
        elif req.side == "SELL" and existing:
            remaining = existing.qty - req.qty
            if remaining <= 0:
                self._positions.pop(req.ticker, None)
            else:
                self._positions[req.ticker] = dataclasses.replace(existing, qty=remaining)

        self._trade_history.append({
            "order_id": order_id, "ticker": req.ticker, "side": req.side,
            "qty": req.qty, "fill_price": fill_price, "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return status

    def modify_order(self, broker_order_id: str, **changes) -> BrokerOrderStatus:
        existing = self._orders[broker_order_id]
        return existing  # shadow orders fill instantly; nothing to modify

    def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id in self._orders:
            self._orders[broker_order_id] = dataclasses.replace(self._orders[broker_order_id], status="cancelled")

    def cancel_all_orders(self) -> None:
        for oid in list(self._orders):
            self.cancel_order(oid)

    def close_position(self, ticker: str) -> BrokerOrderStatus:
        pos = self._positions.get(ticker)
        if not pos:
            raise KeyError(f"No shadow position in {ticker}")
        return self.submit_order(OrderRequest(ticker=ticker, side="SELL", qty=pos.qty, order_type="market"))

    def close_all_positions(self) -> None:
        for ticker in list(self._positions):
            self.close_position(ticker)

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        return self._orders[broker_order_id]

    def get_trade_history(self, since=None) -> list[dict]:
        return list(self._trade_history)

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
    BrokerError,
    BrokerOrderStatus,
    OrderRequest,
    Position,
    Quote,
)
from app.execution.authorization import (
    BrokerEnvironment,
    ExecutionGrant,
    ExecutionIntent,
)


_KNOWN_SIDES = {"BUY", "SELL", "SHORT", "COVER"}


class ShadowBroker(BrokerAdapter):
    name = "shadow"
    environment = BrokerEnvironment.SHADOW

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

    def submit_order(self, req: OrderRequest, grant: ExecutionGrant) -> BrokerOrderStatus:
        # Even though this adapter is incapable of reaching a network, it still
        # enforces the grant. A SHADOW run that skipped authorization would be a
        # rehearsal of an unauthorized run, and would let authorization bugs
        # hide until the day they mattered.
        self.assert_grant_authorizes(grant, ExecutionIntent(
            mode=grant.mode, ticker=req.ticker, side=req.side, qty=req.qty,
            order_type=req.order_type,
        ))
        return self._simulate_fill(req)

    def submit_protective_order(self, req: OrderRequest) -> BrokerOrderStatus:
        """Simulated broker-held protection. Ungated, and constrained to
        risk-reducing orders by the base-class guard so that SHADOW rehearses
        the same asymmetry PAPER uses.

        Unlike an entry, this is NOT filled immediately: a protective order sits
        working until its trigger. Recording it as `accepted` rather than
        `filled` is what lets ProtectiveExitManager.verify_protection() find it
        in `get_open_orders()`, which is precisely the behaviour we want to
        rehearse offline.
        """
        self._assert_risk_reducing(req)
        order_id = f"SHADOW-PROT-{uuid.uuid4().hex[:8]}"
        status = BrokerOrderStatus(
            order_id, "accepted", 0.0, None,
            raw={"shadow": True, "symbol": req.ticker,
                 "side": "sell", "type": req.order_type,
                 "stop_price": req.stop_price,
                 "limit_price": req.take_profit_price,
                 "protective": True},
        )
        self._orders[order_id] = status
        return status

    def _simulate_fill(self, req: OrderRequest) -> BrokerOrderStatus:
        """Internal fill simulation, shared by submit_order() and the
        position-closing helpers. Kept separate so that closing a position does
        not require minting a second ExecutionGrant: an exit is risk-reducing
        and must never be blocked by an entry-authorization failure."""
        q = self._quote_source(req.ticker)
        fill_price = req.limit_price if req.order_type == "limit" and req.limit_price else q.last
        order_id = f"SHADOW-{uuid.uuid4().hex[:10]}"
        status = BrokerOrderStatus(order_id, "filled", req.qty, fill_price, raw={"shadow": True})
        self._orders[order_id] = status

        # Side is normalised and then VALIDATED. Previously these were bare
        # case-sensitive comparisons against "BUY"/"SELL"/"COVER", so any other
        # spelling -- notably lowercase "buy" -- fell through every branch. The
        # order still reported `filled`, but no position was recorded and no cash
        # moved. The damage is not cosmetic: open_positions stayed at 0, so
        # max_concurrent_positions never bound, buying power never decreased, and
        # every SHADOW P&L figure was wrong, all with no error anywhere.
        #
        # An unrecognised side now raises. A simulator that silently ignores an
        # instruction is worse than one that refuses it, because the refusal is
        # visible and the silence is not.
        side = (req.side or "").strip().upper()
        if side not in _KNOWN_SIDES:
            raise BrokerError(
                f"Unrecognised order side {req.side!r} for {req.ticker}. "
                f"Known sides: {sorted(_KNOWN_SIDES)}. Refusing rather than "
                f"reporting a fill that moves no position."
            )

        notional = req.qty * fill_price
        if side in ("BUY", "COVER"):
            self._cash -= notional
        else:  # SELL, SHORT
            self._cash += notional

        existing = self._positions.get(req.ticker)
        if side == "BUY":
            self._positions[req.ticker] = Position(
                req.ticker, (existing.qty + req.qty) if existing else req.qty,
                fill_price, fill_price, 0.0, "long", datetime.now(timezone.utc),
            )
        elif side == "SHORT":
            self._positions[req.ticker] = Position(
                req.ticker, -((abs(existing.qty) + req.qty) if existing else req.qty),
                fill_price, fill_price, 0.0, "short", datetime.now(timezone.utc),
            )
        elif side in ("SELL", "COVER") and existing:
            remaining = abs(existing.qty) - req.qty
            if remaining <= 0:
                self._positions.pop(req.ticker, None)
            else:
                signed = remaining if existing.qty > 0 else -remaining
                self._positions[req.ticker] = dataclasses.replace(existing, qty=signed)

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
        # Exits bypass entry authorization deliberately: reducing risk must
        # always be possible, including while the circuit breaker is tripped.
        return self._simulate_fill(
            OrderRequest(ticker=ticker, side="SELL", qty=pos.qty, order_type="market")
        )

    def close_all_positions(self) -> None:
        for ticker in list(self._positions):
            self.close_position(ticker)

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        return self._orders[broker_order_id]

    def get_trade_history(self, since=None) -> list[dict]:
        return list(self._trade_history)

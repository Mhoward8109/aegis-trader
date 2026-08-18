"""
Alpaca adapter (spec §3).

SAFETY PROPERTY (read before touching this file):
  This adapter's constructor HARD-CODES the paper-trading base URL
  (https://paper-api.alpaca.markets) unless `allow_live=True` is passed
  explicitly by the caller. The caller (app/cli.py) only ever passes
  allow_live=True after ModeGovernor.assert_execution_allowed(Mode.LIVE)
  has already succeeded, which itself requires config/local.yaml to set
  mode: LIVE AND the --i-understand-this-is-live-trading CLI flag.
  There is no other way to make this class talk to the live endpoint.

Credentials: read ONLY from environment variables
(ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY), never from config files or
source code (spec §3). See docs/SETUP.md for how to obtain paper-trading
keys (they are free and separate from any live keys).

Uses the official `alpaca-py` SDK. If alpaca-py is not installed or keys
are not present, constructing this class raises immediately with a clear
message rather than degrading silently — a broker adapter that fails
should fail loud, per spec §11/§28.
"""
from __future__ import annotations

import os
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

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
# LIVE_BASE_URL is intentionally NOT a module constant. It is only
# constructed inline inside __init__ when allow_live=True, so grepping this
# file for "api.alpaca.markets" without "paper-" only turns up something
# behind the explicit allow_live guard below.


class AlpacaBroker(BrokerAdapter):
    name = "alpaca"
    supports_live = True   # capability exists; whether it's REACHABLE is gated below

    def __init__(self, allow_live: bool = False, feed: str = "iex"):
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as e:
            raise BrokerError(
                "alpaca-py is not installed. Run `pip install -r requirements.txt`."
            ) from e

        api_key = os.environ.get("ALPACA_API_KEY_ID")
        api_secret = os.environ.get("ALPACA_API_SECRET_KEY")
        if not api_key or not api_secret:
            raise BrokerError(
                "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set in the "
                "environment. See docs/SETUP.md. Never put these in a config "
                "file or in source code."
            )

        self.allow_live = allow_live
        self.feed = feed
        # alpaca-py's TradingClient takes paper=True/False; we invert allow_live
        # so the SAFE value (paper=True) is the default with no argument needed.
        self._client = TradingClient(api_key, api_secret, paper=not allow_live)
        self._data_client = StockHistoricalDataClient(api_key, api_secret)
        self.base_url_in_use = PAPER_BASE_URL if not allow_live else "https://api.alpaca.markets"
        if allow_live:
            # This branch is only reachable if the caller already forced
            # allow_live=True, which app/cli.py only does after ModeGovernor
            # has approved LIVE. We still log it loudly here.
            import logging
            logging.getLogger("aegis.broker.alpaca").warning(
                "AlpacaBroker constructed with allow_live=True — LIVE ORDERS ARE POSSIBLE."
            )

    # ---- account / positions ----------------------------------------
    def get_account(self) -> AccountSnapshot:
        """Per broker research (docs/ARCHITECTURE.md §3): Alpaca's legacy
        `pattern_day_trader` / `daytrade_count` fields were frozen after
        the June 4, 2026 FINRA PDT retirement and scheduled for full API
        removal by July 6, 2026 — both dates are already past. We read them
        defensively with getattr(...) in case a cached SDK version still
        returns them, but we never gate any logic on `pattern_day_trader`
        (see AccountSnapshot docstring). `intraday_buying_power` is read
        speculatively from whatever field name the current alpaca-py
        exposes for the new Intraday Margin framework; if the installed
        SDK doesn't have it yet, this is None and the risk engine simply
        falls back to `buying_power`.
        """
        a = self._client.get_account()
        return AccountSnapshot(
            equity=float(a.equity), buying_power=float(a.buying_power), cash=float(a.cash),
            currency=a.currency, timestamp=datetime.now(timezone.utc),
            intraday_buying_power=float(getattr(a, "intraday_buying_power", None) or 0) or None,
            margin_deficit=float(getattr(a, "intraday_margin_deficit", None) or 0) or None,
        )

    def get_buying_power(self) -> float:
        return float(self._client.get_account().buying_power)

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._client.get_all_positions():
            out.append(Position(
                ticker=p.symbol, qty=float(p.qty), avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price), unrealized_pnl=float(p.unrealized_pl),
                side=p.side.value if hasattr(p.side, "value") else str(p.side),
                timestamp=datetime.now(timezone.utc),
            ))
        return out

    def get_open_orders(self) -> list[BrokerOrderStatus]:
        orders = self._client.get_orders()
        return [self._to_status(o) for o in orders]

    def get_quote(self, ticker: str) -> Quote:
        from alpaca.data.requests import StockLatestQuoteRequest
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=self.feed)
        q = self._data_client.get_stock_latest_quote(req)[ticker]
        return Quote(
            ticker=ticker, bid=float(q.bid_price), ask=float(q.ask_price),
            last=(float(q.bid_price) + float(q.ask_price)) / 2,
            timestamp=q.timestamp, source=f"alpaca:{self.feed}",
        )

    # ---- orders --------------------------------------------------------
    def submit_order(self, req: OrderRequest) -> BrokerOrderStatus:
        from alpaca.trading.requests import (
            LimitOrderRequest, MarketOrderRequest, StopLimitOrderRequest, StopOrderRequest,
            TrailingStopOrderRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce

        side = OrderSide.BUY if req.side in ("BUY", "COVER") else OrderSide.SELL
        tif = TimeInForce.DAY if req.time_in_force == "day" else TimeInForce.GTC

        kwargs = dict(symbol=req.ticker, qty=req.qty, side=side, time_in_force=tif,
                      extended_hours=req.extended_hours)

        if req.order_type == "market":
            order_req = MarketOrderRequest(**kwargs)
        elif req.order_type == "limit":
            order_req = LimitOrderRequest(limit_price=req.limit_price, **kwargs)
        elif req.order_type == "stop":
            order_req = StopOrderRequest(stop_price=req.stop_price, **kwargs)
        elif req.order_type == "stop_limit":
            order_req = StopLimitOrderRequest(stop_price=req.stop_price, limit_price=req.limit_price, **kwargs)
        elif req.order_type == "trailing_stop":
            order_req = TrailingStopOrderRequest(trail_percent=req.trail_percent, **kwargs)
        else:
            raise BrokerError(f"Unsupported order_type for Alpaca adapter: {req.order_type}")

        result = self._client.submit_order(order_req)
        return self._to_status(result)

    def modify_order(self, broker_order_id: str, **changes) -> BrokerOrderStatus:
        from alpaca.trading.requests import ReplaceOrderRequest
        result = self._client.replace_order_by_id(broker_order_id, ReplaceOrderRequest(**changes))
        return self._to_status(result)

    def cancel_order(self, broker_order_id: str) -> None:
        self._client.cancel_order_by_id(broker_order_id)

    def cancel_all_orders(self) -> None:
        self._client.cancel_orders()

    def close_position(self, ticker: str) -> BrokerOrderStatus:
        result = self._client.close_position(ticker)
        return self._to_status(result)

    def close_all_positions(self) -> None:
        self._client.close_all_positions(cancel_orders=True)

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        return self._to_status(self._client.get_order_by_id(broker_order_id))

    def get_trade_history(self, since=None) -> list[dict]:
        orders = self._client.get_orders()
        return [dict(o) if hasattr(o, "__iter__") else o.__dict__ for o in orders]

    @staticmethod
    def _to_status(o) -> BrokerOrderStatus:
        return BrokerOrderStatus(
            broker_order_id=str(o.id),
            status=str(o.status.value if hasattr(o.status, "value") else o.status),
            filled_qty=float(o.filled_qty or 0),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
            raw={"symbol": getattr(o, "symbol", None)},
        )

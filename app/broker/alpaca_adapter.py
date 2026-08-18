"""
Alpaca broker adapters (Milestone 2, PARTS 2/4/12/13/16).

WHY THIS FILE WAS RESTRUCTURED
------------------------------
Milestone 1 had ONE class, `AlpacaBroker(allow_live: bool = False)`, whose
docstring asserted: "There is no other way to make this class talk to the live
endpoint." That was false. `allow_live` was an ordinary keyword argument with a
default; `AlpacaBroker(allow_live=True)` was callable from anywhere, and the
class had no way to observe whether authorization had happened
(docs/AUDIT_MILESTONE2.md).

A single boolean is the wrong shape for this decision. So the boolean is gone.
Paper and live are now **two different classes**:

    AlpacaPaperBroker   environment = BrokerEnvironment.PAPER
    AlpacaLiveBroker    environment = BrokerEnvironment.LIVE

Consequences that follow from the type rather than from a convention:

  * You cannot reach the live endpoint by flipping an argument. You must import
    a differently-named class. That is greppable, reviewable, and appears in a
    diff.
  * `ExecutionGrant` carries the environment it authorized. A grant issued for
    PAPER presented to `AlpacaLiveBroker` raises, and vice versa. Neither
    adapter trusts the caller's claim about what mode it is in; each verifies
    the grant against its OWN class-level declaration.
  * `AlpacaLiveBroker.__init__` refuses to construct at all in this milestone
    (PART 3: LIVE is architecturally anticipated but operationally disabled),
    and it reads *separately named* credentials so paper keys can never
    accidentally address the live endpoint.

CREDENTIALS
-----------
Environment variables only; never config files, never source (spec §3).

  PAPER: ALPACA_PAPER_API_KEY_ID / ALPACA_PAPER_API_SECRET_KEY
         (falls back to ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY, since
          Alpaca's dashboard hands out unprefixed names)
  LIVE:  ALPACA_LIVE_API_KEY_ID / ALPACA_LIVE_API_SECRET_KEY
         There is NO fallback for live. If a future operator wants live, they
         must deliberately populate variables whose names contain "LIVE". A
         paper key sitting in ALPACA_API_KEY_ID can never be picked up by the
         live adapter.

THE PAPER-VERIFICATION LIMITATION (PART 4, stated honestly)
-----------------------------------------------------------
The brief asks: verify the environment is actually paper if the API allows it.
**It does not.** Research against Alpaca's current docs and the `alpaca-py`
source found no server-side signal identifying a session as paper or live. The
`paper=True/False` argument to `TradingClient` is purely client-side URL
selection, and the `AccountStatus.PAPER_ONLY` enum value exists but its
semantics are undocumented, so branching on it would be a guess.

Compensating controls actually implemented here, in place of the verification
the API cannot provide:

  1. `AlpacaPaperBroker` never receives a live/paper switch. It passes
     `paper=True` as a literal, and `url_override` is not accepted, so no
     caller can redirect it.
  2. `verify_environment()` asserts the resolved base URL string equals
     `https://paper-api.alpaca.markets` exactly. This is acknowledged as
     reading back our own client-side setting, not an independent fact -- it
     catches SDK-default drift and misconfiguration, not a wrong key.
  3. Credential separation (above) means a live key pair is not present in the
     variables the paper adapter reads.
  4. `AccountStatus.PAPER_ONLY`, if returned, is recorded as corroborating
     evidence in `verify_environment()` but is NOT relied upon, because its
     meaning is unconfirmed.
  5. The residual risk is stated in docs/SAFETY.md rather than papered over:
     if an operator places LIVE keys into the PAPER variables, this adapter
     will address the paper URL with live credentials, which Alpaca will reject
     with a 401 -- a loud failure, not a silent live order.

API facts used here are from /home/user/workspace/research_alpaca_sdk.md
(verified Aug 18, 2026), notably: bracket/OCO require BOTH take_profit and
stop_loss; extended_hours orders must be limit + day/gtc; the free tier is
IEX-only with a 15-minute SIP restriction; `pattern_day_trader` and
`daytrade_count` were removed from API responses on 2026-07-06.
"""
from __future__ import annotations

import logging
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
from app.execution.authorization import BrokerEnvironment, ExecutionGrant, ExecutionIntent

log = logging.getLogger("aegis.broker.alpaca")

PAPER_BASE_URL = "https://paper-api.alpaca.markets"

#: The live URL is a module constant purely so that `verify_environment()` can
#: assert the paper client is NOT pointed at it. Nothing in this module
#: constructs a client from it except AlpacaLiveBroker, which refuses to
#: construct at all in this milestone.
LIVE_BASE_URL = "https://api.alpaca.markets"


class AlpacaEnvironmentError(BrokerError):
    """Raised when the resolved broker environment does not match the adapter's
    declaration. Distinct from BrokerError so callers can treat an environment
    mismatch as unrecoverable rather than as a transient broker fault."""


class _AlpacaAdapterBase(BrokerAdapter):
    """Shared Alpaca implementation.

    Deliberately does NOT decide which environment it is. `environment` and the
    credential variable names are declared by the concrete subclass, so there is
    no code path in the shared body that could select live.
    """

    name = "alpaca"

    #: Overridden by subclasses. None here means the base class alone cannot be
    #: used to submit anything -- `assert_grant_authorizes` rejects an adapter
    #: with an undeclared environment.
    environment: BrokerEnvironment | None = None

    #: (primary_key_var, primary_secret_var, fallback_key_var, fallback_secret_var)
    _CREDENTIAL_VARS: tuple[str, str, str | None, str | None] = ("", "", None, None)

    _EXPECTED_BASE_URL: str = ""

    def __init__(self, feed: str = "iex"):
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical.stock import StockHistoricalDataClient
        except ImportError as e:  # pragma: no cover - environment dependent
            raise BrokerError(
                "alpaca-py is not installed. Run `pip install -r requirements.txt`."
            ) from e

        api_key, api_secret = self._resolve_credentials()
        self.feed = feed
        self._client = self._build_trading_client(TradingClient, api_key, api_secret)
        self._data_client = StockHistoricalDataClient(api_key, api_secret)
        self.base_url_in_use = self._EXPECTED_BASE_URL

    @property
    def trading_client(self):
        """Read-only access to the underlying TradingClient.

        Exposed so collaborators that legitimately need the broker's clock and
        calendar (MarketSessionService, PART 10) do not have to reach into
        `_client`. Deliberately read-only with no setter: a caller cannot swap in
        a client pointed at a different endpoint after construction, which is the
        whole point of hard-coding `paper=True` in the subclass.
        """
        return self._client

    # -- credentials -----------------------------------------------------
    @classmethod
    def _resolve_credentials(cls) -> tuple[str, str]:
        key_var, secret_var, fb_key_var, fb_secret_var = cls._CREDENTIAL_VARS
        api_key = os.environ.get(key_var)
        api_secret = os.environ.get(secret_var)
        used = (key_var, secret_var)

        if (not api_key or not api_secret) and fb_key_var and fb_secret_var:
            api_key = api_key or os.environ.get(fb_key_var)
            api_secret = api_secret or os.environ.get(fb_secret_var)
            used = (fb_key_var, fb_secret_var)

        if not api_key or not api_secret:
            expected = f"{key_var} / {secret_var}"
            fallback_note = (
                f" (or {fb_key_var} / {fb_secret_var})"
                if fb_key_var and fb_secret_var else
                " -- note there is deliberately NO fallback to unprefixed "
                "variable names for this adapter"
            )
            raise BrokerError(
                f"{expected}{fallback_note} are not set in the environment. "
                f"See docs/SETUP.md. Never put these in a config file or in "
                f"source code."
            )
        log.info("Alpaca %s adapter using credentials from %s / %s",
                 cls.environment.value if cls.environment else "?", *used)
        return api_key, api_secret

    def _build_trading_client(self, trading_client_cls, api_key, api_secret):
        raise NotImplementedError

    # -- environment verification (PART 4) --------------------------------
    def verify_environment(self) -> dict:
        """Assert this adapter is addressing the environment it declares.

        Returns a record for the journal and the health snapshot. Raises
        AlpacaEnvironmentError on mismatch rather than returning a falsy value,
        so a caller cannot proceed by ignoring the result.

        Read the module docstring for what this check can and cannot prove. In
        short: it verifies OUR configuration, not the broker's opinion of the
        key, because Alpaca exposes no such signal.
        """
        resolved = self._resolved_base_url()
        if resolved != self._EXPECTED_BASE_URL:
            raise AlpacaEnvironmentError(
                f"{type(self).__name__} declares "
                f"{self.environment.value if self.environment else 'UNDECLARED'} "
                f"and expects base URL {self._EXPECTED_BASE_URL}, but the "
                f"constructed client resolves to {resolved}. Refusing to trade "
                f"through a client whose endpoint does not match its declared "
                f"environment."
            )
        if self.environment is BrokerEnvironment.PAPER and resolved == LIVE_BASE_URL:
            # Unreachable given the check above; kept as an explicit second
            # assertion because the cost of being wrong here is real money.
            raise AlpacaEnvironmentError(
                "PAPER adapter resolved to the LIVE base URL. Refusing to submit."
            )

        record = {
            "adapter": type(self).__name__,
            "declared_environment": self.environment.value if self.environment else None,
            "resolved_base_url": resolved,
            "base_url_matches_declaration": True,
            "server_side_environment_verification_available": False,
            "limitation": (
                "Alpaca exposes no server-side signal identifying a session as "
                "paper or live; `paper=True` is client-side URL selection only. "
                "This check confirms our own configuration, not the broker's "
                "classification of the key."
            ),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        # Corroborating-but-unrelied-upon evidence. PAPER_ONLY exists in the
        # AccountStatus enum but its semantics are undocumented, so it is
        # recorded and never branched on.
        try:
            status = getattr(self._client.get_account(), "status", None)
            status_value = getattr(status, "value", status)
            record["account_status"] = str(status_value) if status_value else None
            record["account_status_is_paper_only"] = (
                str(status_value).upper() == "PAPER_ONLY" if status_value else None
            )
            record["account_status_semantics"] = "UNCONFIRMED - recorded, not relied upon"
        except Exception as exc:  # noqa: BLE001 - corroboration only, never fatal
            record["account_status"] = None
            record["account_status_error"] = str(exc)

        return record

    def _resolved_base_url(self) -> str:
        """Best-effort read of the URL the SDK actually built.

        alpaca-py does not guarantee a public attribute for this, so several
        known locations are probed. If none is present we fall back to the
        adapter's declared expectation and say so -- we do NOT silently claim
        verification we did not perform.
        """
        for attr in ("_base_url", "base_url"):
            value = getattr(self._client, attr, None)
            if value:
                return str(value).rstrip("/")
        return self._EXPECTED_BASE_URL

    # -- account / positions ----------------------------------------------
    def get_account(self) -> AccountSnapshot:
        """Account snapshot.

        `pattern_day_trader` / `daytrade_count` are NOT read. Per Alpaca's own
        migration notice they were removed from API responses on 2026-07-06
        following FINRA's June 4, 2026 PDT retirement, and the alpaca-py model
        docstrings state they now default to None. Reading them would produce a
        None that is indistinguishable from "no day trades", which is exactly
        the kind of silent-falsy gate this project forbids.

        `timestamp` is wall clock here and that is deliberate but limited:
        Alpaca's account endpoint returns no as-of timestamp, so the only honest
        thing available is "the moment we asked". The freshness gate treats this
        as the account read time, which is what it needs.
        """
        a = self._client.get_account()
        intraday = _optional_float(getattr(a, "intraday_buying_power", None))
        return AccountSnapshot(
            equity=float(a.equity),
            buying_power=float(a.buying_power),
            cash=float(a.cash),
            currency=a.currency,
            timestamp=datetime.now(timezone.utc),
            intraday_buying_power=intraday,
            margin_deficit=_optional_float(getattr(a, "intraday_margin_deficit", None)),
        )

    def get_buying_power(self) -> float:
        return float(self._client.get_account().buying_power)

    def get_positions(self) -> list[Position]:
        now = datetime.now(timezone.utc)
        out = []
        for p in self._client.get_all_positions():
            out.append(Position(
                ticker=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price) if p.current_price else 0.0,
                unrealized_pnl=float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                side=_enum_value(p.side),
                timestamp=now,
            ))
        return out

    def get_open_orders(self) -> list[BrokerOrderStatus]:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        return [self._to_status(o) for o in orders]

    def get_quote(self, ticker: str) -> Quote:
        from alpaca.data.requests import StockLatestQuoteRequest
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=self.feed)
        quotes = self._data_client.get_stock_latest_quote(req)
        q = quotes.get(ticker) if hasattr(quotes, "get") else quotes[ticker]
        if q is None:
            raise BrokerError(
                f"Alpaca returned no quote for {ticker}. Not substituting a "
                f"previous or synthetic quote."
            )
        if getattr(q, "timestamp", None) is None:
            raise BrokerError(
                f"Alpaca quote for {ticker} has no timestamp. Refusing to use "
                f"data whose age cannot be established."
            )
        bid, ask = float(q.bid_price), float(q.ask_price)
        return Quote(
            ticker=ticker, bid=bid, ask=ask,
            last=(bid + ask) / 2 if bid and ask else (bid or ask),
            timestamp=q.timestamp,
            source=f"alpaca:{self.feed}",
        )

    # -- shortability (PART 16) -------------------------------------------
    def get_asset_tradability(self, ticker: str) -> dict:
        """Return the broker's own answer about whether a symbol can be traded
        and shorted. Used by the shortability gate.

        Returns a dict rather than a bool so the caller can journal WHY a short
        was refused, and so a lookup failure is distinguishable from a
        confirmed "not shortable".
        """
        try:
            asset = self._client.get_asset(ticker)
        except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
            return {
                "ticker": ticker, "lookup_succeeded": False, "error": str(exc),
                "tradable": None, "shortable": None, "easy_to_borrow": None,
                "fractionable": None, "status": None,
                "detail": (f"asset lookup failed ({exc}); shortability is UNKNOWN "
                           f"and a short must therefore be refused"),
            }
        return {
            "ticker": ticker,
            "lookup_succeeded": True,
            # Deliberately NOT coerced with bool(): an absent attribute must stay
            # None so the shortability gate can tell "broker says no" apart from
            # "the field wasn't there". bool(None) would silently turn a missing
            # field into a confirmed False.
            "tradable": _optional_bool(getattr(asset, "tradable", None)),
            "shortable": _optional_bool(getattr(asset, "shortable", None)),
            "easy_to_borrow": _optional_bool(getattr(asset, "easy_to_borrow", None)),
            "fractionable": _optional_bool(getattr(asset, "fractionable", None)),
            "status": _enum_value(getattr(asset, "status", None)),
            "detail": "broker asset record retrieved",
        }

    # -- market session (PART 10) -----------------------------------------
    def get_clock(self):
        return self._client.get_clock()

    def get_calendar(self, start=None, end=None):
        from alpaca.trading.requests import GetCalendarRequest
        return self._client.get_calendar(GetCalendarRequest(start=start, end=end))

    # -- orders ------------------------------------------------------------
    def submit_order(self, req: OrderRequest, grant: ExecutionGrant) -> BrokerOrderStatus:
        """Submit an order. The grant is verified against THIS adapter's
        declared environment before any network call, then consumed."""
        intent = ExecutionIntent(
            mode=grant.mode, ticker=req.ticker, side=req.side, qty=req.qty,
            order_type=req.order_type,
        )
        self.assert_grant_authorizes(grant, intent)
        self.verify_environment()

        order_req = self._build_order_request(req)
        result = self._client.submit_order(order_req)
        return self._to_status(result)

    def submit_protective_order(self, req: OrderRequest) -> BrokerOrderStatus:
        """Attach broker-native protection to an existing position. No grant.

        `_assert_risk_reducing()` is what makes it safe to leave this ungated:
        it rejects every side and order type that could open or increase
        exposure, so this method cannot be used as a back door around
        ExecutionAuthorizer. `verify_environment()` still runs, so a protective
        order cannot be routed somewhere unexpected.
        """
        self._assert_risk_reducing(req)
        self.verify_environment()
        order_req = self._build_order_request(req)
        result = self._client.submit_order(order_req)
        return self._to_status(result)

    def _build_order_request(self, req: OrderRequest):
        from alpaca.trading.requests import (
            LimitOrderRequest, MarketOrderRequest, StopLimitOrderRequest,
            StopLossRequest, StopOrderRequest, TakeProfitRequest,
            TrailingStopOrderRequest,
        )
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

        side = OrderSide.BUY if req.side.upper() in ("BUY", "COVER") else OrderSide.SELL
        tif = {"day": TimeInForce.DAY, "gtc": TimeInForce.GTC,
               "ioc": TimeInForce.IOC, "fok": TimeInForce.FOK,
               "opg": TimeInForce.OPG, "cls": TimeInForce.CLS}.get(
                   req.time_in_force.lower(), TimeInForce.DAY)

        qty = self._whole_shares(req)

        # Extended-hours constraint, from the Alpaca API (research §4.3): an
        # extended-hours order MUST be a limit order with day/gtc TIF. Anything
        # else is rejected server-side with a 422. Rejecting locally gives a
        # clearer error and avoids burning a rate-limited call.
        if req.extended_hours:
            if req.order_type not in ("limit", "stop_limit"):
                raise BrokerError(
                    f"Alpaca accepts only LIMIT orders during extended hours; "
                    f"got order_type={req.order_type!r}. This would be rejected "
                    f"server-side with a 422."
                )
            if tif not in (TimeInForce.DAY, TimeInForce.GTC):
                raise BrokerError(
                    f"Extended-hours orders require time_in_force day or gtc; "
                    f"got {req.time_in_force!r}."
                )

        kwargs = dict(symbol=req.ticker, qty=qty, side=side, time_in_force=tif,
                      extended_hours=req.extended_hours)
        if req.client_order_id:
            kwargs["client_order_id"] = req.client_order_id

        if req.order_type == "market":
            return MarketOrderRequest(**kwargs)
        if req.order_type == "limit":
            return LimitOrderRequest(limit_price=req.limit_price, **kwargs)
        if req.order_type == "stop":
            return StopOrderRequest(stop_price=req.stop_price, **kwargs)
        if req.order_type == "stop_limit":
            return StopLimitOrderRequest(stop_price=req.stop_price,
                                         limit_price=req.limit_price, **kwargs)
        if req.order_type == "trailing_stop":
            if req.trail_percent is None:
                raise BrokerError(
                    "trailing_stop requires trail_percent. Alpaca's SDK "
                    "validator requires exactly one of trail_price/trail_percent."
                )
            return TrailingStopOrderRequest(trail_percent=req.trail_percent, **kwargs)

        if req.order_type in ("bracket", "oco"):
            # Alpaca's own validator requires BOTH legs for BRACKET and OCO
            # (research §2.4). Checking here produces a domain-specific error
            # instead of a pydantic ValueError from inside the SDK.
            if req.take_profit_price is None or req.stop_price is None:
                raise BrokerError(
                    f"{req.order_type} orders require BOTH a take-profit price "
                    f"and a stop price; got take_profit={req.take_profit_price!r} "
                    f"stop={req.stop_price!r}. A protective structure with a "
                    f"missing leg is worse than none, because it looks managed."
                )
            if req.extended_hours:
                raise BrokerError(
                    "Bracket/OCO orders combined with extended_hours are "
                    "rejected by Alpaca (a stop leg cannot function in a "
                    "limit-only session). Refusing to submit."
                )
            order_class = (OrderClass.BRACKET if req.order_type == "bracket"
                           else OrderClass.OCO)
            legs = dict(
                order_class=order_class,
                take_profit=TakeProfitRequest(limit_price=req.take_profit_price),
                stop_loss=StopLossRequest(stop_price=req.stop_price),
            )
            if req.order_type == "bracket":
                if req.limit_price is not None:
                    return LimitOrderRequest(limit_price=req.limit_price, **legs, **kwargs)
                return MarketOrderRequest(**legs, **kwargs)
            # OCO has no new entry leg; it attaches exits to an existing
            # position, and its limit_price comes from the take_profit leg.
            return LimitOrderRequest(**legs, **kwargs)

        raise BrokerError(f"Unsupported order_type for Alpaca adapter: {req.order_type}")

    @staticmethod
    def _whole_shares(req: OrderRequest) -> int:
        """Alpaca rejects fractional quantities for bracket/OCO/stop orders and
        for short sales. The risk engine sizes in whole shares, but a caller
        could still pass 10.4; truncate DOWN (never up) and refuse zero.

        Truncating down rather than rounding is deliberate: rounding up would
        silently increase risk beyond what the position sizer approved.
        """
        qty = int(req.qty)
        if qty <= 0:
            raise BrokerError(
                f"Order quantity {req.qty} truncates to {qty} whole shares. "
                f"Refusing to submit a zero-quantity order; the position sizer "
                f"should have rejected this setup."
            )
        return qty

    def modify_order(self, broker_order_id: str, **changes) -> BrokerOrderStatus:
        from alpaca.trading.requests import ReplaceOrderRequest
        result = self._client.replace_order_by_id(
            broker_order_id, ReplaceOrderRequest(**changes))
        return self._to_status(result)

    def cancel_order(self, broker_order_id: str) -> None:
        self._client.cancel_order_by_id(broker_order_id)

    def cancel_all_orders(self) -> None:
        self._client.cancel_orders()

    def close_position(self, ticker: str) -> BrokerOrderStatus:
        """Close a position.

        Deliberately requires NO ExecutionGrant. Protective exits must never be
        blocked by the entry-authorization path -- a breaker trip or a stale
        quote must not strand an open position. Closing reduces exposure; the
        risk of being unable to close is strictly worse than the risk of an
        unauthorized close.
        """
        result = self._client.close_position(ticker)
        return self._to_status(result)

    def close_all_positions(self) -> None:
        self._client.close_all_positions(cancel_orders=True)

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        return self._to_status(self._client.get_order_by_id(broker_order_id))

    def get_trade_history(self, since=None) -> list[dict]:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=since))
        return [self._to_status(o).raw | {"broker_order_id": str(o.id)} for o in orders]

    def is_connected(self) -> bool:
        """Cheap liveness probe used by the circuit breaker and health snapshot.
        Returns a bool rather than raising, because 'is the broker reachable'
        is a question whose negative answer is data, not an error."""
        try:
            self._client.get_clock()
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _to_status(o) -> BrokerOrderStatus:
        """Normalize an Alpaca Order into our status type.

        The full raw payload is preserved (not just the symbol, as Milestone 1
        did) because the order lifecycle manager needs `status`, `filled_qty`,
        and the leg structure to reconcile, and the journal needs the broker's
        own words for the audit trail.
        """
        legs = getattr(o, "legs", None) or []
        return BrokerOrderStatus(
            broker_order_id=str(o.id),
            status=_enum_value(o.status),
            filled_qty=float(o.filled_qty or 0),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
            raw={
                "symbol": getattr(o, "symbol", None),
                "side": _enum_value(getattr(o, "side", None)),
                "type": _enum_value(getattr(o, "type", None)),
                "order_class": _enum_value(getattr(o, "order_class", None)),
                "qty": _str_or_none(getattr(o, "qty", None)),
                "limit_price": _str_or_none(getattr(o, "limit_price", None)),
                "stop_price": _str_or_none(getattr(o, "stop_price", None)),
                "time_in_force": _enum_value(getattr(o, "time_in_force", None)),
                "client_order_id": getattr(o, "client_order_id", None),
                "submitted_at": _iso_or_none(getattr(o, "submitted_at", None)),
                "filled_at": _iso_or_none(getattr(o, "filled_at", None)),
                "canceled_at": _iso_or_none(getattr(o, "canceled_at", None)),
                "expired_at": _iso_or_none(getattr(o, "expired_at", None)),
                "failed_at": _iso_or_none(getattr(o, "failed_at", None)),
                "legs": [
                    {
                        "broker_order_id": str(leg.id),
                        "type": _enum_value(getattr(leg, "type", None)),
                        "status": _enum_value(getattr(leg, "status", None)),
                        "limit_price": _str_or_none(getattr(leg, "limit_price", None)),
                        "stop_price": _str_or_none(getattr(leg, "stop_price", None)),
                    }
                    for leg in legs
                ],
            },
        )


class AlpacaPaperBroker(_AlpacaAdapterBase):
    """Alpaca's official PAPER endpoint. The only adapter this milestone wires
    for real order submission (PART 4)."""

    environment = BrokerEnvironment.PAPER
    _CREDENTIAL_VARS = (
        "ALPACA_PAPER_API_KEY_ID", "ALPACA_PAPER_API_SECRET_KEY",
        "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY",
    )
    _EXPECTED_BASE_URL = PAPER_BASE_URL

    def _build_trading_client(self, trading_client_cls, api_key, api_secret):
        # `paper=True` is a literal. This class accepts no argument that could
        # change it, and no url_override parameter is exposed, so there is no
        # value a caller can pass that redirects this client to the live
        # endpoint. That is the structural replacement for Milestone 1's
        # `allow_live` boolean.
        return trading_client_cls(api_key, api_secret, paper=True)


class AlpacaLiveBroker(_AlpacaAdapterBase):
    """Alpaca's LIVE endpoint. **Operationally disabled in this milestone.**

    The class exists so the architecture is honest about the shape of a future
    live path -- separate class, separate credentials, separate environment
    declaration -- but PART 3 of the milestone is explicit that live trading is
    not to be wired now. `__init__` therefore refuses.

    Turning this on later is intentionally a code change plus a config change
    plus a credential change, reviewed together, rather than a flag flip.
    """

    environment = BrokerEnvironment.LIVE
    _CREDENTIAL_VARS = ("ALPACA_LIVE_API_KEY_ID", "ALPACA_LIVE_API_SECRET_KEY",
                        None, None)
    _EXPECTED_BASE_URL = LIVE_BASE_URL

    #: Flipping this to True is NOT sufficient to trade live. The execution
    #: authorizer independently requires config mode LIVE from a permitted local
    #: source, a per-run operator flag, and a LIVE-scoped grant. This constant
    #: only removes the milestone-level construction refusal.
    OPERATIONALLY_ENABLED = False

    def __init__(self, feed: str = "iex"):
        if not self.OPERATIONALLY_ENABLED:
            raise BrokerError(
                "AlpacaLiveBroker is operationally disabled. LIVE trading is "
                "architecturally anticipated but deliberately not wired in this "
                "milestone (Milestone 2, PART 3). Constructing this adapter "
                "would be the first step toward real-money execution, so it "
                "refuses rather than succeeding and relying on a downstream "
                "check. Enabling it is a reviewed code change, not a flag."
            )
        log.critical("AlpacaLiveBroker constructed -- REAL MONEY IS AT RISK.")
        super().__init__(feed=feed)

    def _build_trading_client(self, trading_client_cls, api_key, api_secret):  # pragma: no cover
        return trading_client_cls(api_key, api_secret, paper=False)


def _enum_value(v):
    return (v.value if hasattr(v, "value") else str(v)) if v is not None else None


def _str_or_none(v):
    return str(v) if v is not None else None


def _iso_or_none(v):
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _optional_bool(v) -> bool | None:
    """Preserve None. See get_asset_tradability() for why this matters."""
    return None if v is None else bool(v)


def _optional_float(v) -> float | None:
    """Convert to float, but preserve the difference between 'absent' and
    'zero'. Returning 0.0 for a missing field is how a missing value becomes an
    invisible wrong answer."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

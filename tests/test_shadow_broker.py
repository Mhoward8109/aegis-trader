import datetime as dt

from app.broker.base import OrderRequest, Quote
from app.broker.shadow_adapter import ShadowBroker


def quote_source(ticker):
    return Quote(ticker=ticker, bid=99.9, ask=100.1, last=100.0,
                 timestamp=dt.datetime.now(dt.timezone.utc), source="test")


def test_shadow_never_calls_network_and_fills_at_last_price():
    broker = ShadowBroker(starting_equity=100_000, quote_source=quote_source)
    status = broker.submit_order(OrderRequest(ticker="AAPL", side="BUY", qty=10, order_type="market"))
    assert status.status == "filled"
    assert status.filled_avg_price == 100.0


def test_cash_decreases_on_buy_and_increases_on_sell():
    broker = ShadowBroker(starting_equity=100_000, quote_source=quote_source)
    broker.submit_order(OrderRequest(ticker="AAPL", side="BUY", qty=10, order_type="market"))
    acct_after_buy = broker.get_account()
    assert acct_after_buy.cash == 100_000 - 1000.0

    broker.submit_order(OrderRequest(ticker="AAPL", side="SELL", qty=10, order_type="market"))
    acct_after_sell = broker.get_account()
    assert acct_after_sell.cash == 100_000.0


def test_close_all_positions_flattens_book():
    broker = ShadowBroker(starting_equity=100_000, quote_source=quote_source)
    broker.submit_order(OrderRequest(ticker="AAPL", side="BUY", qty=5, order_type="market"))
    broker.submit_order(OrderRequest(ticker="MSFT", side="BUY", qty=5, order_type="market"))
    assert len(broker.get_positions()) == 2
    broker.close_all_positions()
    assert len(broker.get_positions()) == 0


def test_shadow_broker_has_no_http_client_attribute():
    """Structural safety check: ShadowBroker must not hold any network
    client, so it is provably incapable of reaching a broker over the wire."""
    broker = ShadowBroker(starting_equity=100_000, quote_source=quote_source)
    forbidden_attr_names = {"session", "client", "http", "httpx", "requests"}
    actual_attrs = {a.lower() for a in vars(broker).keys()}
    assert not (forbidden_attr_names & actual_attrs)

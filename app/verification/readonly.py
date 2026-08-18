"""Capability boundary used by the connectivity probe.

Only read operations are exposed.  Mutation-shaped attribute access raises
before the wrapped broker is touched, providing a runtime backstop in addition
to the static invariant test.
"""
from __future__ import annotations


MUTATION_METHODS = frozenset(
    {
        "submit_order",
        "submit_protective_order",
        "modify_order",
        "cancel_order",
        "cancel_all_orders",
        "close_position",
        "close_all_positions",
    }
)


class ReadOnlyViolation(RuntimeError):
    pass


class ReadOnlyBroker:
    def __init__(self, broker: object):
        self._broker = broker

    @property
    def wrapped_type(self) -> type:
        return type(self._broker)

    @property
    def environment(self):
        return self._broker.environment

    def get_account(self):
        return self._broker.get_account()

    def get_buying_power(self):
        return self._broker.get_buying_power()

    def get_positions(self):
        return self._broker.get_positions()

    def get_open_orders(self):
        return self._broker.get_open_orders()

    def get_quote(self, ticker: str):
        return self._broker.get_quote(ticker)

    def get_order_status(self, broker_order_id: str):
        return self._broker.get_order_status(broker_order_id)

    def get_trade_history(self, since=None):
        return self._broker.get_trade_history(since)

    def __getattr__(self, name: str):
        if name in MUTATION_METHODS:
            raise ReadOnlyViolation(
                f"connectivity probe has no capability to call broker.{name}()"
            )
        raise AttributeError(name)

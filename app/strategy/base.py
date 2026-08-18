"""
Strategy framework (spec §7). Every strategy is an independent module
implementing this interface, so its performance can be measured separately
(spec §7, §16) and it can be versioned independently (spec §22).

A Strategy NEVER talks to a broker directly and NEVER decides risk sizing —
it only produces a Setup (a hypothesis), which the risk engine may approve
or reject, and only the execution engine (already risk-approved, mode-gated)
ever calls a broker. This separation is what makes spec §34
("Performance Is Not Permission") structurally true rather than a promise.
"""
from __future__ import annotations

import abc
import dataclasses
from datetime import datetime


@dataclasses.dataclass
class MarketContext:
    """Snapshot the strategy needs to evaluate a candidate — assembled by the
    orchestrator from market-data + regime + catalyst modules. Strategies
    never fetch this themselves, which keeps them trivially backtestable
    against recorded contexts."""
    ticker: str
    timestamp: datetime
    bars_intraday: "pd.DataFrame"          # noqa: F821 - typed loosely to avoid hard pandas import here
    bars_prev_day: "pd.DataFrame"
    quote: dict                              # {bid, ask, last}
    indicators: dict
    catalyst: dict | None
    regime: dict
    session: str                              # premarket|regular|postmarket


@dataclasses.dataclass
class Setup:
    ticker: str
    strategy: str
    strategy_version: str
    direction: str                # long | short
    entry: float
    stop: float
    targets: list[float]
    invalidation: str
    confirmation_notes: str
    max_spread_pct: float
    min_liquidity_usd: float
    permitted_sessions: list[str]
    prohibited_conditions: list[str]
    raw_signals: dict


class Strategy(abc.ABC):
    name: str = "base"
    version: str = "0.0.0"

    def __init__(self, params: dict):
        self.params = params

    @abc.abstractmethod
    def market_conditions_ok(self, ctx: MarketContext) -> bool:
        """Broad regime gate (e.g. only trade breakouts in a trending tape)."""

    @abc.abstractmethod
    def candidate_criteria_met(self, ctx: MarketContext) -> bool:
        """Cheap pre-filter before doing the expensive setup evaluation."""

    @abc.abstractmethod
    def setup_conditions_met(self, ctx: MarketContext) -> bool: ...

    @abc.abstractmethod
    def confirmation_met(self, ctx: MarketContext) -> bool: ...

    @abc.abstractmethod
    def entry_trigger(self, ctx: MarketContext) -> bool: ...

    @abc.abstractmethod
    def build_setup(self, ctx: MarketContext) -> Setup | None:
        """Only called after all the gates above return True. Returns None
        if, despite passing gates, no valid entry/stop/target can be
        constructed (e.g. ATR unavailable) — 'NO TRADE' is a valid and
        expected output (spec §35)."""

    def evaluate(self, ctx: MarketContext) -> Setup | None:
        """Orchestrates the full gate sequence. Strategies should not
        override this — override the individual gate methods instead, so
        every strategy is auditable against the same checklist shape."""
        if not self.market_conditions_ok(ctx):
            return None
        if not self.candidate_criteria_met(ctx):
            return None
        if not self.setup_conditions_met(ctx):
            return None
        if not self.confirmation_met(ctx):
            return None
        if not self.entry_trigger(ctx):
            return None
        return self.build_setup(ctx)

"""Fail-closed construction of the real Alpaca PAPER dependency graph.

This module intentionally contains no fallback imports.  A construction error
is a PAPER startup failure, never a reason to substitute an offline component.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from app.common.modes import Mode


class PaperRuntimeError(RuntimeError):
    """A required PAPER dependency could not be constructed."""


@dataclasses.dataclass(frozen=True)
class PaperRuntime:
    broker: object
    market_data: object
    session_service: object
    regime_engine: object
    sec_provider: object
    circuit_breaker: object
    authorizer: object


def paper_credentials_present() -> bool:
    paper = os.getenv("ALPACA_PAPER_API_KEY_ID") and os.getenv(
        "ALPACA_PAPER_API_SECRET_KEY"
    )
    plain = os.getenv("ALPACA_API_KEY_ID") and os.getenv("ALPACA_API_SECRET_KEY")
    return bool(paper or plain)


def build_paper_runtime(
    *,
    symbols: tuple[str, ...],
    breaker_path: str | Path,
    breaker_config: dict | None = None,
    require_sec: bool = True,
) -> PaperRuntime:
    """Construct only production PAPER components, or raise with stage context."""
    if not paper_credentials_present():
        raise PaperRuntimeError("missing Alpaca PAPER credentials")
    if require_sec and not os.getenv("SEC_EDGAR_CONTACT_EMAIL"):
        raise PaperRuntimeError("missing SEC_EDGAR_CONTACT_EMAIL")

    # Imports remain local so tests can replace each production constructor and
    # prove that every failure terminates construction without a substitute.
    from app.broker.alpaca_adapter import AlpacaPaperBroker
    from app.catalyst.sec_edgar import SecEdgarFilingProvider
    from app.execution.authorization import ExecutionAuthorizer
    from app.marketdata.alpaca_provider import AlpacaMarketDataProvider
    from app.marketdata.regime_engine import MarketRegimeEngine
    from app.marketdata.session import MarketSessionService
    from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker

    built: dict[str, object] = {}

    def assert_production_type(name: str, value: object, expected_type: type) -> None:
        mro_names = {cls.__name__.lower() for cls in type(value).__mro__}
        forbidden = ("mock", "shadow", "null", "offline", "synthetic", "stub")
        tainted = sorted(
            class_name
            for class_name in mro_names
            if any(marker in class_name for marker in forbidden)
        )
        if type(value) is not expected_type or tainted:
            raise PaperRuntimeError(
                f"PAPER {name} resolved to {type(value).__name__}, not the exact "
                f"production {expected_type.__name__} type; offline substitutes "
                f"are prohibited (tainted MRO={tainted})"
            )

    def construct(name: str, factory):
        try:
            built[name] = factory()
        except Exception as exc:  # noqa: BLE001 - convert to a fail-closed boundary error
            raise PaperRuntimeError(
                f"PAPER {name} construction failed ({type(exc).__name__}); "
                "no offline substitute was selected"
            ) from exc
        return built[name]

    broker = construct("broker", AlpacaPaperBroker)
    assert_production_type("broker", broker, AlpacaPaperBroker)
    market_data = construct(
        "market data", lambda: AlpacaMarketDataProvider(symbols=symbols)
    )
    assert_production_type("market data", market_data, AlpacaMarketDataProvider)
    session_service = construct(
        "session service",
        lambda: MarketSessionService(client=broker.trading_client),
    )
    assert_production_type("session service", session_service, MarketSessionService)
    regime_engine = construct(
        "regime service", lambda: MarketRegimeEngine(market_data)
    )
    assert_production_type("regime service", regime_engine, MarketRegimeEngine)
    sec_provider = (
        construct("SEC provider", SecEdgarFilingProvider) if require_sec else None
    )
    if sec_provider is not None:
        assert_production_type("SEC provider", sec_provider, SecEdgarFilingProvider)
    circuit_breaker = construct(
        "circuit breaker",
        lambda: PersistentCircuitBreaker(breaker_path, cfg=breaker_config),
    )
    assert_production_type("circuit breaker", circuit_breaker, PersistentCircuitBreaker)
    authorizer = construct(
        "execution authorizer", lambda: ExecutionAuthorizer(target_mode=Mode.PAPER)
    )
    assert_production_type("execution authorizer", authorizer, ExecutionAuthorizer)
    return PaperRuntime(
        broker=broker,
        market_data=market_data,
        session_service=session_service,
        regime_engine=regime_engine,
        sec_provider=sec_provider,
        circuit_breaker=circuit_breaker,
        authorizer=authorizer,
    )

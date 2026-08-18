from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.paper_runtime import PaperRuntimeError, build_paper_runtime, paper_credentials_present


@pytest.mark.parametrize(
    "paper_key,paper_secret,plain_key,plain_secret,expected",
    [
        (None, None, None, None, False),
        ("k", None, None, None, False),
        ("k", "s", None, None, True),
        (None, None, "k", "s", True),
        (None, None, "k", None, False),
        (None, "s", "k", None, False),
    ],
)
def test_paper_credentials_require_a_complete_pair(
    monkeypatch, paper_key, paper_secret, plain_key, plain_secret, expected
):
    values = {
        "ALPACA_PAPER_API_KEY_ID": paper_key,
        "ALPACA_PAPER_API_SECRET_KEY": paper_secret,
        "ALPACA_API_KEY_ID": plain_key,
        "ALPACA_API_SECRET_KEY": plain_secret,
    }
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert paper_credentials_present() is expected


def _install_constructors(monkeypatch, failing_stage=None, tainted_stage=None):
    import app.broker.alpaca_adapter as broker_module
    import app.catalyst.sec_edgar as sec_module
    import app.execution.authorization as auth_module
    import app.marketdata.alpaca_provider as data_module
    import app.marketdata.regime_engine as regime_module
    import app.marketdata.session as session_module
    import app.risk.persistent_circuit_breaker as breaker_module

    classes = {}

    def make(name, init):
        class Production:
            def __init__(self, *args, **kwargs):
                if failing_stage == name:
                    raise RuntimeError(f"forced {name} failure")
                init(self, *args, **kwargs)

        Production.__name__ = f"Production{name.title().replace(' ', '')}"
        if tainted_stage == name:
            Production.__name__ = f"Mock{name.title().replace(' ', '')}"
        classes[name] = Production
        return Production

    Broker = make("broker", lambda self: setattr(self, "trading_client", object()))
    Data = make("market data", lambda self, symbols: setattr(self, "symbols", symbols))
    Session = make("session service", lambda self, client: setattr(self, "client", client))
    Regime = make("regime service", lambda self, source: setattr(self, "source", source))
    Sec = make("SEC provider", lambda self: None)
    Breaker = make("circuit breaker", lambda self, path, cfg=None: setattr(self, "path", path))
    Authorizer = make("execution authorizer", lambda self, target_mode: setattr(self, "mode", target_mode))

    monkeypatch.setattr(broker_module, "AlpacaPaperBroker", Broker)
    monkeypatch.setattr(data_module, "AlpacaMarketDataProvider", Data)
    monkeypatch.setattr(session_module, "MarketSessionService", Session)
    monkeypatch.setattr(regime_module, "MarketRegimeEngine", Regime)
    monkeypatch.setattr(sec_module, "SecEdgarFilingProvider", Sec)
    monkeypatch.setattr(breaker_module, "PersistentCircuitBreaker", Breaker)
    monkeypatch.setattr(auth_module, "ExecutionAuthorizer", Authorizer)
    return classes


def _credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_API_KEY_ID", "present-not-real")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET_KEY", "present-not-real")
    monkeypatch.setenv("SEC_EDGAR_CONTACT_EMAIL", "test@example.invalid")


@pytest.mark.parametrize(
    "stage",
    [
        "broker",
        "market data",
        "session service",
        "regime service",
        "SEC provider",
        "circuit breaker",
        "execution authorizer",
    ],
)
def test_every_paper_dependency_failure_fails_closed(monkeypatch, tmp_path, stage):
    _credentials(monkeypatch)
    _install_constructors(monkeypatch, failing_stage=stage)
    with pytest.raises(PaperRuntimeError, match=f"PAPER {stage} construction failed"):
        build_paper_runtime(symbols=("SPY",), breaker_path=tmp_path / "breaker.db")


@pytest.mark.parametrize(
    "stage",
    [
        "broker",
        "market data",
        "session service",
        "regime service",
        "SEC provider",
        "circuit breaker",
        "execution authorizer",
    ],
)
def test_offline_substitute_mro_is_rejected(monkeypatch, tmp_path, stage):
    _credentials(monkeypatch)
    _install_constructors(monkeypatch, tainted_stage=stage)
    with pytest.raises(PaperRuntimeError, match="offline substitutes are prohibited"):
        build_paper_runtime(symbols=("SPY",), breaker_path=tmp_path / "breaker.db")


def test_complete_production_graph_is_returned_without_substitution(monkeypatch, tmp_path):
    _credentials(monkeypatch)
    classes = _install_constructors(monkeypatch)
    runtime = build_paper_runtime(symbols=("SPY",), breaker_path=tmp_path / "breaker.db")
    assert type(runtime.broker) is classes["broker"]
    assert type(runtime.market_data) is classes["market data"]
    assert type(runtime.session_service) is classes["session service"]
    assert type(runtime.regime_engine) is classes["regime service"]
    assert type(runtime.sec_provider) is classes["SEC provider"]
    assert type(runtime.circuit_breaker) is classes["circuit breaker"]
    assert type(runtime.authorizer) is classes["execution authorizer"]


def test_sec_is_required_by_default(monkeypatch, tmp_path):
    _credentials(monkeypatch)
    monkeypatch.delenv("SEC_EDGAR_CONTACT_EMAIL")
    _install_constructors(monkeypatch)
    with pytest.raises(PaperRuntimeError, match="missing SEC_EDGAR_CONTACT_EMAIL"):
        build_paper_runtime(symbols=("SPY",), breaker_path=tmp_path / "breaker.db")


def test_order_probe_runtime_can_explicitly_omit_sec(monkeypatch, tmp_path):
    _credentials(monkeypatch)
    monkeypatch.delenv("SEC_EDGAR_CONTACT_EMAIL")
    _install_constructors(monkeypatch)
    runtime = build_paper_runtime(
        symbols=("SPY",), breaker_path=tmp_path / "breaker.db", require_sec=False
    )
    assert runtime.sec_provider is None


def test_paper_runtime_has_no_offline_fallback_imports_or_handlers():
    path = Path(__file__).parents[1] / "app" / "paper_runtime.py"
    tree = ast.parse(path.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    source = path.read_text()
    assert not any(name in imported for name in {"MockProvider", "ShadowBroker"})
    assert "except PaperRuntimeError" not in source
    assert "fallback" not in {node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)}

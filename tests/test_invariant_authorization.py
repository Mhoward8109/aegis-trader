"""Authorization-boundary safety invariants."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca_adapter import AlpacaLiveBroker
from app.broker.base import BrokerError, OrderRequest
from app.broker.shadow_adapter import ShadowBroker
from app.catalyst.engine import CatalystEngine, NullNewsProvider
from app.common.modes import Mode
from app.execution.authorization import (
    AuthorizationEvidence, BrokerEnvironment, ExecutionAuthorizer,
    ExecutionNotAuthorizedError,
)
from app.execution.engine import ExecutionEngine
from app.orchestration.pipeline import run_pipeline
from app.risk.engine import RiskEngine
from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
from app.scanner.base import ScanCriteria
from app.strategy.scoring import OpportunityScorer
from tests.helpers import all_conditions_satisfied_evidence, grant_for, intent_for
from tests.invariant_support import AlwaysSetup, OneTickerProvider, RISK_CFG, TestBroker, WEIGHTS, journal_for


def _session_service():
    state = SimpleNamespace(session="REGULAR", reason="", scheduled_close=None, is_unknown=False)
    return SimpleNamespace(current_session=lambda: state, permits_orders=lambda *_: True)


def _live_pipeline(tmp_path, **kwargs):
    broker = TestBroker()
    result = run_pipeline(
        mode=Mode.LIVE, provider=OneTickerProvider(), criteria=ScanCriteria(),
        strategies=[AlwaysSetup({})], scorer=OpportunityScorer(WEIGHTS),
        catalyst_engine=CatalystEngine([NullNewsProvider()]), risk_engine=RiskEngine(RISK_CFG),
        risk_cfg=RISK_CFG, broker=broker, journal=journal_for(tmp_path),
        authorizer=ExecutionAuthorizer(target_mode=Mode.LIVE),
        circuit_breaker=PersistentCircuitBreaker(tmp_path / "breaker.db"),
        config_mode=Mode.LIVE, config_mode_source="default.yaml",
        session_service=_session_service(), min_score_to_consider=0,
        **kwargs,
    )
    return result, broker


def test_live_refuses_config_not_from_permitted_local_source(tmp_path):
    """LIVE requires permitted local configuration provenance, or a versioned default could place real orders."""
    result, broker = _live_pipeline(tmp_path, live_config_from_permitted_source=False,
                                    operator_live_flag_present=True)
    assert not broker.submissions
    assert result.outcomes[0].stage_reached == "not_authorized"
    assert "live_config_from_permitted_source" in result.outcomes[0].detail


def test_live_refuses_without_per_run_operator_authorization(tmp_path):
    """LIVE requires a per-run operator flag, or a stale configuration alone could place real orders."""
    result, broker = _live_pipeline(tmp_path, live_config_from_permitted_source=True,
                                    operator_live_flag_present=False)
    assert not broker.submissions
    assert "operator_per_run_authorization" in result.outcomes[0].detail


def test_direct_pipeline_call_cannot_bypass_live_cli_guard(tmp_path):
    """Calling run_pipeline directly in LIVE still submits no order without CLI-derived evidence, or importers could bypass the CLI."""
    result, broker = _live_pipeline(tmp_path)
    assert result.orders_submitted == 0
    assert not broker.submissions


def test_execution_engine_and_broker_refuse_calls_without_real_grant():
    """Execution and broker submission require a real grant, or arbitrary code could send an unapproved order."""
    req = OrderRequest("SAFE", "BUY", 1, "market")
    broker = ShadowBroker(10_000, lambda ticker: OneTickerProvider().get_quote(ticker))
    with pytest.raises(TypeError):
        broker.submit_order(req)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ExecutionEngine(broker).submit(req, intent_for(req))  # type: ignore[call-arg]


def test_grant_is_rejected_by_different_broker_environment():
    """A grant is environment-bound, or a PAPER authorization could be replayed at a different venue."""
    req = OrderRequest("SAFE", "BUY", 1, "market")
    shadow = ShadowBroker(10_000, lambda ticker: OneTickerProvider().get_quote(ticker))
    paper_grant = grant_for(req, Mode.PAPER)
    with pytest.raises(ExecutionNotAuthorizedError):
        shadow.submit_order(req, paper_grant)
    with pytest.raises(ExecutionNotAuthorizedError):
        TestBroker().submit_order(req, grant_for(req, Mode.SHADOW))


def test_live_broker_refuses_construction_while_operationally_disabled():
    """The live adapter must refuse construction while disabled, or a configuration mistake could instantiate real-money infrastructure."""
    assert AlpacaLiveBroker.OPERATIONALLY_ENABLED is False
    with pytest.raises(BrokerError):
        AlpacaLiveBroker()


def _seal_name_uses(path: Path, seal_name: str) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == seal_name:
            hits.append((node.lineno, "name"))
        elif isinstance(node, ast.ImportFrom) and any(a.name == seal_name for a in node.names):
            hits.append((node.lineno, "import"))
    return hits


def test_no_module_outside_authorization_imports_the_grant_seal():
    """Only authorization may use the grant seal, or a module could silently forge an execution grant."""
    root = Path(__file__).resolve().parents[1]
    seal = "_GRANT_SEAL"
    offenders = {
        str(path.relative_to(root)): _seal_name_uses(path, seal)
        for folder in (root / "app", root / "tests")
        for path in folder.rglob("*.py")
        if path != root / "app/execution/authorization.py"
        and _seal_name_uses(path, seal)
    }
    assert offenders == {}


def test_no_strategy_risk_execution_scanner_or_orchestration_module_uses_reset_seal():
    """Only the explicit operator-reset factory may use the reset seal, or trading code could clear its own halt."""
    root = Path(__file__).resolve().parents[1]
    seal = "_RESET_SEAL"
    protected = ("strategy", "risk", "execution", "scanner", "orchestration")
    offenders = {
        str(path.relative_to(root)): _seal_name_uses(path, seal)
        for area in protected
        for path in (root / "app" / area).rglob("*.py")
        if path != root / "app/risk/persistent_circuit_breaker.py"
        and _seal_name_uses(path, seal)
    }
    assert offenders == {}


def test_each_unset_authorization_evidence_field_fails_closed():
    """Every missing authorization evidence field must fail a check, or an omitted future gate could silently authorize orders."""
    req = OrderRequest("SAFE", "BUY", 1, "market")
    evidence = AuthorizationEvidence()
    decision = ExecutionAuthorizer(target_mode=Mode.SHADOW).evaluate(intent_for(req), evidence)
    failed = {check.name for check in decision.failed}
    required_failures = {
        "config_mode_matches_target", "risk_engine_approved", "data_freshness_passed",
        "broker_connected", "broker_account_state_valid", "circuit_breaker_clear",
        "market_session_permits_orders", "broker_environment_matches_mode",
    }
    assert not decision.authorized
    assert required_failures <= failed

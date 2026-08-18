from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.verification.readonly import MUTATION_METHODS, ReadOnlyBroker, ReadOnlyViolation
from tests.probe_support import RecordingBroker


@pytest.mark.parametrize("method", sorted(MUTATION_METHODS))
def test_readonly_broker_has_no_mutation_capability(method):
    broker = RecordingBroker()
    readonly = ReadOnlyBroker(broker)
    with pytest.raises(ReadOnlyViolation, match=method):
        getattr(readonly, method)
    assert broker.mutations == []


def test_connectivity_source_contains_no_broker_mutation_calls():
    path = Path(__file__).parents[1] / "app" / "verification" / "connectivity.py"
    tree = ast.parse(path.read_text())
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(MUTATION_METHODS)


def test_connectivity_source_cannot_import_strategy_pipeline_or_execution_engine():
    path = Path(__file__).parents[1] / "app" / "verification" / "connectivity.py"
    source = path.read_text()
    assert "app.orchestration.pipeline" not in source
    assert "app.strategy" not in source
    assert "ExecutionEngine" not in source

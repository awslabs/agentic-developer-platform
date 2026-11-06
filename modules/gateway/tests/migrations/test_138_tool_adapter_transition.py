"""Handoff progress is additive security state and cannot be rolled away."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_transition_migration_retains_existing_rows_and_refuses_destructive_downgrade():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/138_tool_adapter_transition.py"
    spec = importlib.util.spec_from_file_location("adapter_transition_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    changes = []
    module.op = SimpleNamespace(add_column=lambda table, column: changes.append((table, column)))
    module.upgrade()
    assert module.down_revision == "137_platform_admin_grants"
    assert len(changes) == 1
    table, column = changes[0]
    assert table == "tool_adapter_activations" and column.name == "transition" and column.nullable
    with pytest.raises(RuntimeError, match="Retain the adapter transition"):
        module.downgrade()

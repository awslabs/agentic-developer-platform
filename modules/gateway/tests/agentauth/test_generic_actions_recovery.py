"""A verified failed workflow reconciles dispatch, never runtime acceptance."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agentauth import actions_routes
from src.orchestration import generic_delivery_recovery, operations_runtime_evidence, operations_workflow_evidence, validation_diagnostics
from tests.orchestration.test_generic_delivery import config, context_for


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure", "success"])
@pytest.mark.parametrize("recovery", [True, False, "delivery_target_lease_changed"])
@pytest.mark.parametrize("transport", ["github-artifact", "s3-oidc-v1"])
async def test_actions_observation_reconciles_failed_generic_workflow(monkeypatch, conclusion, recovery, transport):
    from src.orchestration.review_cycle import CycleBlockedError

    delivery = config()
    context = context_for("payments-api", "ship.yml", delivery, "run-1")
    binding = SimpleNamespace(repo="example/payments", org_id="tenant", installation_id=12, provider_repository_id=123)
    issued = SimpleNamespace(node_id="node-1", workflow_allowlist={"ship.yml"}, plan_version=1, delivery_contract=delivery)
    execution = {
        "invocation_id": {"S": "run-1"},
        "persona": {"S": "operations"},
        "source_revision": {"S": "a" * 40},
        "definition_revision": {"S": "b" * 40},
        "default_branch": {"S": "main"},
    }
    raw = {
        "id": 42,
        "run_attempt": 1,
        "path": context.workflow_path,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": conclusion,
        "display_title": "ADP deployment run-1",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "repository": {"id": 123},
    }
    provider = SimpleNamespace(context=AsyncMock(return_value=(context, 99, "f" * 64)))
    session = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def factory():
        yield session

    monkeypatch.setattr(actions_routes, "get_session_factory", lambda: factory)
    monkeypatch.setattr(actions_routes, "_authority", AsyncMock(return_value=(issued, binding, execution)))
    monkeypatch.setattr(actions_routes, "installation_token", AsyncMock(return_value="test-token"))
    monkeypatch.setattr(actions_routes, "_head_contains_pin", AsyncMock(return_value=True))
    monkeypatch.setattr(actions_routes, "_definition", AsyncMock(return_value={name: "" for name in delivery.inputs}))
    monkeypatch.setattr(actions_routes, "_evidence_transport", AsyncMock(return_value=transport))
    signed_context = AsyncMock(return_value=(context, "f" * 64, "s3://evidence/context?versionId=original"))
    monkeypatch.setattr(actions_routes, "_s3_context", signed_context)
    monkeypatch.setattr(operations_workflow_evidence, "record_correlated_workflow", AsyncMock(return_value="context-id"))
    reconcile, verify, diagnosis = AsyncMock(return_value=recovery), AsyncMock(), AsyncMock()
    if isinstance(recovery, str):
        reconcile.side_effect = CycleBlockedError(recovery)
    monkeypatch.setattr(generic_delivery_recovery, "reconcile_failed_dispatches", reconcile)
    monkeypatch.setattr(operations_runtime_evidence, "verify_operations_runtime", verify)
    monkeypatch.setattr(validation_diagnostics, "record_runtime_validation", diagnosis)

    if conclusion == "success":
        verify.side_effect = CycleBlockedError("delivery_required_checks_missing")
        result = await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=raw, provider=provider)
        assert result["runtime_hold_reason"] == "delivery_required_checks_missing"
        reconcile.assert_not_awaited()
        if transport == "s3-oidc-v1":
            signed_context.assert_awaited_once()
            provider.context.assert_not_awaited()
            assert (
                operations_workflow_evidence.record_correlated_workflow.await_args.kwargs["reference"] == "s3://evidence/context?versionId=original"
            )
        else:
            signed_context.assert_not_awaited()
            provider.context.assert_awaited_once()
        return

    result = await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=raw, provider=provider)
    expected_code = recovery if isinstance(recovery, str) else "delivery_workflow_failed" if recovery else "delivery_dispatch_reconciliation_required"
    assert result["dispatch_reconciled"] is (recovery is True)
    assert result["runtime_evidence_id"] is None
    assert result["runtime_hold_reason"] == expected_code
    reconcile.assert_awaited_once()
    assert reconcile.await_args.kwargs["node_id"] == "node-1"
    provider.context.assert_not_awaited()
    operations_workflow_evidence.record_correlated_workflow.assert_not_awaited()
    assert diagnosis.await_args.kwargs["code"] == expected_code
    verify.assert_not_awaited()

    # An active run cannot release a lock; a failed run needs no success artifact.
    reconcile.reset_mock()
    assert (
        await actions_routes._capture_correlated_run(
            None, None, execution=execution, binding=binding, raw={**raw, "status": "in_progress"}, provider=provider
        )
        is None
    )
    reconcile.assert_not_awaited()
    provider.context.return_value = None
    result = await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=raw, provider=provider)
    assert result["dispatch_reconciled"] is (recovery is True)
    reconcile.assert_awaited_once()
    provider.context.assert_not_awaited()

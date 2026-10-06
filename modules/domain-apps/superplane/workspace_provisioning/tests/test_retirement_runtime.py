"""Refuse authority substitution before ownership reads or provider effects."""

from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.identity import OperationRefused

from workspace_provisioning.artifacts import digest
from workspace_provisioning.retirement_inventory import OwnedGrant
from workspace_provisioning.retirement_runtime import (
    RetirementRuntime,
    verify_retirement_inventory,
)

from .test_retirement_plan import component, inventory


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["allocation", "attempt", "fence", "tenant", "job"])
async def test_runtime_refuses_original_authority_substitution(changed):
    identity = {
        "operation_id": "op",
        "org_id": "org",
        "workspace_id": "ws",
        "attempt_id": "attempt",
        "fence_token": 2,
    }
    call = SimpleNamespace(**identity, job_id="job")
    if changed == "job":
        call.job_id = "other-job"
    if changed == "attempt":
        call.attempt_id = "new-attempt"
    if changed == "fence":
        call.fence_token = 3
    if changed == "tenant":
        call.org_id = "other-org"
    parameters = {"allocation_id": "original", "original_allocation_id": "original"}
    if changed == "allocation":
        parameters["allocation_id"] = "new-allocation"
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=SimpleNamespace(**identity)),
        request=SimpleNamespace(action="teardown", parameters=parameters),
        job_id="job",
    )
    connect = AsyncMock()
    remover = SimpleNamespace(execute=AsyncMock())
    runtime = RetirementRuntime(
        connect=connect,
        context=AsyncMock(return_value=(operation, "binding")),
        registration_store=None,
        removals=remover,
        lifecycle=None,
        verify_inventory=None,
    )
    with pytest.raises(OperationRefused):
        await runtime(call)
    connect.assert_not_called()
    remover.execute.assert_not_called()


@pytest.mark.parametrize(
    "changed", [None, "missing", "namespace", "ownership", "tenant"]
)
def test_retirement_requires_exact_reviewed_inventory(changed):
    owned = inventory(cluster_ownership="adopted", remove_namespace=False)
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(workspace_id=owned.workspace_id, org_id=owned.org_id)
        ),
        request=SimpleNamespace(
            parameters={"retirement_inventory_sha256": digest(asdict(owned))}
        ),
    )
    if changed == "missing":
        operation.request.parameters.clear()
    elif changed == "namespace":
        owned = replace(owned, namespace_uid="replacement")
    elif changed == "ownership":
        owned = replace(owned, remove_namespace=True)
    elif changed == "tenant":
        owned = replace(owned, org_id="another-tenant")
    if changed is None:
        verify_retirement_inventory(operation, owned)
    else:
        with pytest.raises(OperationRefused, match="approved inventory"):
            verify_retirement_inventory(operation, owned)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ClusterRole", "ClusterRoleBinding"])
@pytest.mark.parametrize("source", ["retained-grant", "owned-component"])
async def test_old_adopted_admission_cannot_start_partial_retirement_with_global_rbac(
    monkeypatch, kind, source
):
    import workspace_provisioning.retirement_runtime as module

    owned = inventory(cluster_ownership="adopted", remove_namespace=False)
    if source == "retained-grant":
        owned = replace(
            owned,
            grants=(
                OwnedGrant(
                    {
                        "kind": "kubernetes",
                        "body": {
                            "kind": kind,
                            "metadata": {"name": "generation-owned"},
                        },
                    },
                    {"uid": "original-global-uid"},
                ),
            ),
        )
    else:
        owned = replace(
            owned, components=(component("generation-owned", kind=kind, namespace=""),)
        )
    lease = SimpleNamespace(
        operation_id="old-admitted-teardown",
        org_id=owned.org_id,
        workspace_id=owned.workspace_id,
        attempt_id="attempt",
        fence_token=3,
        holder="worker",
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease),
        job_id="original-job",
        plan_digest="approved-digest",
        request_payload="approved-payload",
        request=SimpleNamespace(
            action="teardown",
            parameters={
                "allocation_id": "original",
                "original_allocation_id": "original",
                "retirement_inventory_sha256": digest(asdict(owned)),
            },
        ),
    )

    @asynccontextmanager
    async def transaction():
        yield

    @asynccontextmanager
    async def connect():
        yield SimpleNamespace(
            transaction=transaction, fetchval=AsyncMock(return_value=False)
        )

    monkeypatch.setattr(module, "lock_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(
        module, "load_bootstrap_retirement_inventory", lambda **kwargs: owned
    )
    lifecycle = SimpleNamespace(status=AsyncMock(), drain=AsyncMock())
    removals = SimpleNamespace(execute=AsyncMock())
    artifact = AsyncMock()
    runtime = RetirementRuntime(
        connect=connect,
        context=AsyncMock(return_value=(operation, "binding")),
        registration_store=None,
        removals=removals,
        lifecycle=lifecycle,
        verify_inventory=AsyncMock(),
        artifact_for=artifact,
    )
    with pytest.raises(OperationRefused, match="independent exact-name"):
        await runtime(SimpleNamespace(**vars(lease), job_id="original-job"))
    artifact.assert_not_called()
    lifecycle.status.assert_not_called()
    lifecycle.drain.assert_not_called()
    removals.execute.assert_not_called()
    runtime.verify_inventory.assert_not_called()

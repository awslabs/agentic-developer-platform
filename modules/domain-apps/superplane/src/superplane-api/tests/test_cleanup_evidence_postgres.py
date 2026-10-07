"""Actual managed preview/compiler over paid fixture facts and split SQL roles.

Authentication/current registration facts are supplied by a fixture; downstream
artifact, admission, revision and SQL checks run unmodified. No cloud is contacted.
"""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import asyncpg
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.routers.retirement import RetirementReviewResponse
from app.services import managed_retirement
from app.services.provisioning import ProvisioningUnavailable
from tests.test_lifecycle_evidence_postgres import split_authorities
from workspace_provisioning.artifacts import read_artifact
from workspace_provisioning.control_registry import register_control_operation
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.tests.managed_retirement_composition_fixture import build
from workspace_provisioning.tests.postgres_bridge import requires_harness_postgres
from workspace_provisioning.tests.conftest import (  # noqa: F401
    binding,
    principal,
    provider_identity,
    observed_cluster,
    expected_target,
    runtime,
    database,
    loop,
    schema_ddl,
    server,
)

pytestmark = requires_harness_postgres


@pytest.mark.parametrize(
    "mutation",
    [None, "approver", "approval_id", "producer", "pointer", "creation_request"],
)
def test_preview_retains_concrete_approval_and_compiled_revision(
    runtime,  # noqa: F811
    server,  # noqa: F811
    tmp_path,
    monkeypatch,
    mutation,
):
    driver = runtime.store.store._loop
    from harness_jobs import SpendEnvelope
    from workspace_provisioning.tests import (
        managed_retirement_composition_fixture as fixture,
    )

    class ExactApproval(fixture._Approves):
        async def approval_for(self, *, principal, request):  # noqa: F811
            context = await super().approval_for(principal=principal, request=request)
            envelope = SpendEnvelope(
                **{
                    key: int(request.parameters[key])
                    for key in (
                        "max_resource_units",
                        "max_runtime_seconds",
                        "max_cost_micros",
                    )
                }
            )
            return replace(
                context,
                record=replace(context.record, envelope=envelope),
                requested_envelope=envelope,
            )

    monkeypatch.setattr(fixture, "_Approves", ExactApproval)

    async def run():
        case = await build(runtime, server, tmp_path)
        h, plan = case.harness, case.plan
        original_request_id = str(uuid4())
        async with h.connect() as connection:
            await connection.execute(
                "UPDATE workspaces SET status='Active',operation_id=$2 WHERE id::text=$1",
                plan.workspace_id,
                original_request_id,
            )
            await register_control_operation(
                connection,
                domain_connection=connection,
                operation_id=case.control.operation_id,
                org_id=plan.org_id,
                workspace_id=plan.workspace_id,
                source_bootstrap_operation_id=case.bootstrap.operation_id,
                request_id=plan.retirement_request_id,
            )
            paid = await connection.fetchrow(
                "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                case.control.operation_id,
            )
            count = await connection.fetchval("SELECT count(*) FROM harness_operations")
            if mutation == "approver":
                await connection.execute(
                    "UPDATE harness_approval_consumption SET approved_by=requester WHERE operation_id=$1",
                    case.control.operation_id,
                )
            elif mutation == "approval_id":
                await connection.execute(
                    "UPDATE harness_approval_consumption SET approval_id='replacement' WHERE operation_id=$1",
                    case.control.operation_id,
                )
            elif mutation == "producer":
                await connection.execute(
                    "UPDATE workspace_lifecycle_artifacts SET producer_fence_token=producer_fence_token+1 WHERE artifact_id=$1",
                    case.access["artifact_id"],
                )
        bootstrap = await read_artifact(
            h.connect,
            artifact_id=plan.bootstrap_artifact_id,
            org_id=plan.org_id,
            workspace_id=plan.workspace_id,
            require_fresh=False,
        )
        workspace = SimpleNamespace(operation_id=original_request_id)
        policy = json.loads(case.policy_file.read_text())["tenants"][plan.org_id]
        facts = AsyncMock(
            return_value=(
                workspace,
                object(),
                case.bootstrap,
                bootstrap,
                case.inventory,
                None,
                policy,
                case.config,
            )
        )
        monkeypatch.setattr("app.services.retirement.retirement_facts", facts)
        monkeypatch.setattr(managed_retirement, "require_runtime", AsyncMock())
        if mutation == "pointer":
            async with h.connect() as connection:
                await connection.execute(
                    "UPDATE workspaces SET provisioning_operation_id='replaced' WHERE id::text=$1",
                    plan.workspace_id,
                )
        if mutation == "creation_request":
            async with h.connect() as connection:
                await connection.execute(
                    "UPDATE workspaces SET operation_id=$2 WHERE id::text=$1",
                    plan.workspace_id,
                    str(uuid4()),
                )
        cloud_events = list(runtime.cloud.events)
        database_name = runtime.store.store._connection._params.database
        engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=lambda: asyncpg.connect(server, database=database_name),
        )
        try:
            async with (
                AsyncSession(engine) as db,
                split_authorities(h, managed=True) as composition,
            ):
                if mutation:
                    with pytest.raises((LifecycleRefused, ProvisioningUnavailable)):
                        await managed_retirement.preview(
                            composition,
                            db,
                            plan.org_id,
                            plan.workspace_id,
                            plan.retirement_request_id,
                        )
                else:
                    _, _, request, review = await managed_retirement.preview(
                        composition,
                        db,
                        plan.org_id,
                        plan.workspace_id,
                        plan.retirement_request_id,
                    )
                    proof = review["cleanup_preparation"]
                    assert proof.preparation_approval_id == paid["approval_id"]
                    assert (
                        proof.retirement_plan_sha256
                        == request.parameters["plan_revision"]
                    )
                    assert proof.retirement_revision_sha256 == review["revision"]
                    assert proof.preparation_revision == paid["plan_digest"]
                    assert proof.artifact_id == case.access["artifact_id"]
                    assert (
                        proof.producer_attempt_id == case.access["producer_attempt_id"]
                    )
                    assert (
                        proof.producer_fence_token
                        == case.access["producer_fence_token"]
                    )
                    assert proof.status == "OBSERVED"
                    response = RetirementReviewResponse(**review).model_dump(
                        mode="json"
                    )
                    assert (
                        response["cleanup_preparation"]["retirement_plan_sha256"]
                        == request.parameters["plan_revision"]
                    )
                    assert (
                        "source_request_payload" not in response["cleanup_preparation"]
                    )
        finally:
            await engine.dispose()
        assert runtime.cloud.events == cloud_events
        async with h.connect() as connection:
            assert (
                await connection.fetchval("SELECT count(*) FROM harness_operations")
                == count
            )

    try:
        driver.run(run())
    finally:
        if hasattr(runtime, "retirement_pool"):
            driver.run(runtime.retirement_pool.close())

"""A distinct approved control admission retains its paid apply allocation."""

import importlib.util
import json
from dataclasses import asdict, replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from harness_jobs import REQUIRED_PERMISSION, OperationFacadeService, OperationStore
from harness_jobs.effects import CallEffect, call_effect
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.identity import ResolvedPrincipal, decode_payload
from superplane_bootstrap.errors import BootstrapRefused

from workspace_provisioning import retirement_runtime
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.retirement_access_artifact import (
    access_metadata,
    access_target,
)
from workspace_provisioning.retirement_access_authority import access_request
from workspace_provisioning.retirement_access_grants import (
    managed_revocation_recipe,
    revoke_managed_access_grant,
)
from workspace_provisioning.retirement_adapters import OwnedResourceRemover
from workspace_provisioning.retirement_managed_access import (
    compile_managed_access_plan,
    require_managed_control_source,
    require_managed_paid_plan,
)
from workspace_provisioning.retirement_plan import REVOKE_GRANT, compose_retirement_plan
from workspace_provisioning.retirement_runtime import RetirementRuntime
from workspace_provisioning.runtime_config import LifecycleRefused

from .postgres_bridge import Harness, requires_harness_postgres
from .test_lifecycle_policy import policy
from .test_retirement_access_grants import Journal
from .test_retirement_execution_postgres import (
    _Approves,
    _Cloud,
    _Ledger,
    _Resolver,
)
from .test_retirement_managed_access import inputs
from .test_retirement_plan import component

pytestmark = requires_harness_postgres


async def _execute_managed_revoke(
    harness, facade, principal, arguments, plan, artifact, paid_operation_id, monkeypatch, case, cloud
):
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        path = (
            Path(__file__).resolve().parents[2]
            / "src/superplane-api/alembic/versions/019_lifecycle_artifacts.py"
        )
        spec = importlib.util.spec_from_file_location("retirement_artifacts", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.upgrade()
    row = {key: value for key, value in artifact.items() if key != "artifact_id"}
    row.update(
        producer_holder="approved-control-worker",
        producer_attempt_id=artifact["source_attempt_id"],
        producer_fence_token=1,
        request_revision=json.loads(artifact["parameters_json"])["plan_revision"],
    )
    artifact["artifact_id"] = digest(row)
    columns = tuple(row)
    async with harness.connect() as connection:
        await connection.execute(output.getvalue())
        await connection.execute(
            "INSERT INTO workspace_lifecycle_artifacts (artifact_id,"
            + ",".join(columns)
            + ") VALUES ($1,"
            + ",".join("$" + str(index) for index in range(2, len(columns) + 2))
            + ")",
            artifact["artifact_id"],
            *row.values(),
        )

    owned = arguments["inventory"]
    deletion = compose_retirement_plan(owned, managed_access=(plan, artifact))
    progress = await facade.open_operation(
        action="teardown",
        workspace_id=plan.workspace_id,
        org_id=plan.org_id,
        permission=REQUIRED_PERMISSION,
        parameters={
            "execution_steps": deletion.encode(),
            "idempotency_key": "reviewed-managed-retirement",
            "allocation_id": plan.original_allocation_id,
            "original_allocation_id": plan.original_allocation_id,
            "retirement_inventory_sha256": digest(asdict(owned)),
            "retirement_access_artifact_id": artifact["artifact_id"],
            "control_allocation_id": plan.allocation_id,
            "retirement_request_id": plan.retirement_request_id,
        },
    )
    lease = await harness.lease(progress.operation_id, holder="retirement-worker")
    async with harness.connect() as connection:
        record = await OperationStore().get(connection, principal, progress.operation_id)
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease, principal=principal),
        request=record.admitted_request(),
        job_id=record.job_id,
        plan_digest=record.plan_digest,
        request_payload=record.request_payload,
    )
    monkeypatch.setattr(
        retirement_runtime,
        "load_bootstrap_retirement_inventory",
        lambda **kwargs: owned,
    )
    provider = RetirementRuntime(
        connect=harness.connect,
        domain_connect=harness.connect,
        context=AsyncMock(return_value=(operation, "approved-binding")),
        registration_store=None,
        removals=OwnedResourceRemover(
            kubernetes=arguments["kubernetes"], eks=arguments["eks"]
        ),
        lifecycle=SimpleNamespace(managed_fence=AsyncMock(return_value=True)),
        verify_inventory=AsyncMock(),
        control_access_for=AsyncMock(return_value=(plan, paid_operation_id)),
        control_verify=AsyncMock(),
    )

    async def authenticate(token):
        assert token == "scoped-worker"
        return ExecutionGrant(
            ResolvedPrincipal(
                org_id=plan.org_id,
                workspace_id=plan.workspace_id,
                subject=lease.holder,
                permissions=frozenset({REQUIRED_PERMISSION}),
            ),
            lease,
        )

    prefix = ExecutionRPCServer(
        connect=harness.connect, provider_call=_Cloud(), authenticate=authenticate
    )
    for step in deletion.steps:
        if step.step_id == "revoke-control-entry":
            break
        await prefix.dispatch(
            {
                "token": "scoped-worker",
                "method": "execute_step",
                "arguments": {"step_id": step.step_id},
            }
        )
    if case == "released":
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_approval_consumption SET reservation_state='released' "
                "WHERE operation_id=$1", artifact["source_operation_id"]
            )
    elif case == "missing":
        async with harness.connect() as connection:
            await connection.execute(
                "DELETE FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
                artifact["artifact_id"],
            )
    before = cloud.mutations
    server = ExecutionRPCServer(
        connect=harness.connect, provider_call=provider, authenticate=authenticate
    )
    request = {
        "token": "scoped-worker",
        "method": "execute_step",
        "arguments": {"step_id": "revoke-control-entry"},
    }
    result = await server.dispatch(request)
    if case == "confirmed":
        assert result[0]["outcome"] == "succeeded"
        assert cloud.mutations == before + 1
        assert arguments["eks"].observe(plan.grants[0]) is None
        assert await server.dispatch(request) == result
        assert cloud.mutations == before + 1
    else:
        assert result[0]["outcome"] != "succeeded"
        assert cloud.mutations == before
        assert arguments["eks"].observe(plan.grants[0]) is not None
    async with harness.connect() as connection:
        assert await connection.fetchval(
            "SELECT sealed_revision FROM harness_allocation_seal WHERE allocation_id=$1",
            plan.original_allocation_id,
        ) == "reviewed-paid-apply"


@pytest.mark.parametrize("admitted_revoke", ["none", "confirmed", "released", "missing"])
def test_managed_control_approval_keeps_paid_source_and_refuses_unsealed(
    runtime, tmp_path_factory, request, monkeypatch, admitted_revoke
):
    arguments = inputs(runtime)
    arguments["inventory"] = replace(
        arguments["inventory"],
        components_complete=True,
        components=(
            component("fixture-controller", namespace=arguments["inventory"].namespace),
        ),
    )
    plan = compile_managed_access_plan(**arguments)
    account_id, region = plan.cluster_arn.split(":")[4], plan.cluster_arn.split(":")[3]
    deployment = policy()
    deployment["runtime"] = arguments["runtime"]
    deployment["permitted_target_accounts"] = [account_id]
    deployment["permitted_regions"] = [region]
    deployment["credential_references"][account_id] = deployment[
        "credential_references"
    ].pop("000000000002")

    async def exercise(harness):
        principal = ResolvedPrincipal(
            org_id=plan.org_id,
            workspace_id=plan.workspace_id,
            subject="user-1",
            permissions=frozenset({REQUIRED_PERMISSION}),
        )
        facade = OperationFacadeService(
            connect=harness.connect,
            resolver=_Resolver(principal),
            approvals=_Approves(),
            ledger=_Ledger(),
        )

        async def admit(parameters, identifier):
            progress = await facade.open_operation(
                action="provision",
                workspace_id=plan.workspace_id,
                org_id=plan.org_id,
                permission=REQUIRED_PERMISSION,
                parameters={**parameters, "idempotency_key": identifier},
            )
            async with harness.connect() as connection:
                return await OperationStore().get(
                    connection, principal, progress.operation_id
                )

        paid = await admit(
            {
                "allocation_id": plan.original_allocation_id,
                "lifecycle_phase": "apply-infrastructure",
            },
            "paid-apply",
        )
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
                paid.operation_id,
            )
            paid = await OperationStore().get(connection, principal, paid.operation_id)

        bootstrap = await admit(
            {
                "allocation_id": "bootstrap-allocation",
                "lifecycle_phase": "bootstrap-workspace",
                "lifecycle_source_operation_id": paid.operation_id,
                "lifecycle_request": json.dumps(
                    {
                        "mode": "existing-account-managed",
                        "region": region,
                        "target_account_id": account_id,
                        "workspace_id": plan.workspace_id,
                    }
                ),
                "lifecycle_inputs": json.dumps({"isolation_mode": "dedicated"}),
                "lifecycle_artifact_id": plan.bootstrap_artifact_id,
                "aws_account_id": account_id,
            },
            "managed-bootstrap",
        )
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
                bootstrap.operation_id,
            )
            bootstrap = await OperationStore().get(
                connection, principal, bootstrap.operation_id
            )
            with pytest.raises(LifecycleRefused, match="sealed"):
                await require_managed_paid_plan(connection, paid, plan)
            await connection.execute(
                "INSERT INTO harness_allocation_seal "
                "(org_id,workspace_id,allocation_id,sealed_revision,operation_id,"
                "attempt_id,executor_id,fence_token) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                paid.org_id,
                paid.workspace_id,
                plan.original_allocation_id,
                "reviewed-paid-apply",
                paid.operation_id,
                paid.attempt_id,
                "original-worker",
                1,
            )
            await require_managed_paid_plan(connection, paid, plan)
            forged_paid = SimpleNamespace(
                state=paid.state,
                org_id=paid.org_id,
                workspace_id=paid.workspace_id,
                operation_id="unapproved-paid-apply",
                plan_digest=paid.plan_digest,
                admitted_request=paid.admitted_request,
            )
            with pytest.raises(LifecycleRefused, match="original paid approval"):
                await require_managed_paid_plan(connection, forged_paid, plan)
        proposed = access_request(plan, bootstrap, deployment, allocation_source=paid)
        control = await admit(dict(proposed.parameters), proposed.idempotency_key)
        stored = decode_payload(control.request_payload)
        assert stored == proposed
        assert (
            stored.parameters["original_allocation_id"] == plan.original_allocation_id
        )
        assert stored.parameters["allocation_id"] == plan.allocation_id
        assert stored.parameters["allocation_id"] != plan.original_allocation_id
        async with harness.connect() as connection:
            approval = await connection.fetchrow(
                "SELECT reservation_state,plan_digest FROM harness_approval_consumption "
                "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                control.operation_id,
                plan.org_id,
                plan.workspace_id,
            )
            await connection.execute(
                "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
                control.operation_id,
            )
            control = await OperationStore().get(
                connection, principal, control.operation_id
            )
            grant = plan.grants[0]
            identity = arguments["eks"].create(grant)
            artifact = {
                "artifact_id": "f" * 64,
                "source_operation_id": control.operation_id,
                "source_job_id": control.job_id,
                "source_attempt_id": control.attempt_id,
                "source_payload_digest": control.plan_digest,
                "source_request_payload": control.request_payload,
                "org_id": plan.org_id,
                "workspace_id": plan.workspace_id,
                "account_id": account_id,
                "target_json": canonical(access_target(plan)),
                "parameters_json": canonical(
                    dict(control.admitted_request().parameters)
                ),
                "artifact_metadata_json": canonical(
                    access_metadata(plan, {"cleaner-entry": identity})
                ),
            }
            assert (
                await require_managed_control_source(
                    connection,
                    plan=plan,
                    access_artifact=artifact,
                    paid_operation_id=paid.operation_id,
                )
                == control.operation_id
            )
            deletion = compose_retirement_plan(
                arguments["inventory"], managed_access=(plan, artifact)
            )
            control_steps = [
                step
                for step in deletion.steps
                if step.step_id == "revoke-control-entry"
            ]
            assert len(control_steps) == 1
            assert control_steps[0].operation_kind == REVOKE_GRANT
            assert (
                call_effect(REVOKE_GRANT, provider=control_steps[0].provider)
                is CallEffect.REMOVES
            )
            assert control_steps[0] in deletion.deletion_steps()
            assert deletion.steps.index(control_steps[0]) < len(deletion.steps) - 1
            assert not deletion.completes_teardown
            with pytest.raises(BootstrapRefused, match="owned inventory"):
                compose_retirement_plan(
                    replace(arguments["inventory"], workspace_id="foreign-workspace"),
                    managed_access=(plan, artifact),
                )
            with pytest.raises(LifecycleRefused, match="artifact"):
                compose_retirement_plan(
                    arguments["inventory"],
                    managed_access=(plan, {**artifact, "workspace_id": "foreign"}),
                )
            if admitted_revoke != "none":
                await _execute_managed_revoke(
                    harness,
                    facade,
                    principal,
                    arguments,
                    plan,
                    artifact,
                    paid.operation_id,
                    monkeypatch,
                    admitted_revoke,
                    runtime.cloud,
                )
                return
            journal = Journal(
                SimpleNamespace(
                    recipe=lambda: managed_revocation_recipe(plan, artifact)
                )
            )
            before = runtime.cloud.mutations
            assert (
                await revoke_managed_access_grant(
                    plan,
                    artifact,
                    journal,
                    eks=arguments["eks"],
                    verify_cluster=AsyncMock(),
                    connect=harness.connect,
                    paid_operation_id=paid.operation_id,
                )
                == identity
            )
            assert runtime.cloud.mutations == before + 1
            assert journal.events == {next(iter(journal.recipe)): identity}
            with pytest.raises(LifecycleRefused, match="original scope"):
                await require_managed_control_source(
                    connection,
                    plan=plan,
                    access_artifact=artifact,
                    paid_operation_id="foreign-apply",
                )
            with pytest.raises(LifecycleRefused, match="admitted producer"):
                await require_managed_control_source(
                    connection,
                    plan=plan,
                    access_artifact={
                        **artifact,
                        "source_payload_digest": digest("changed"),
                    },
                    paid_operation_id=paid.operation_id,
                )
            await connection.execute(
                "UPDATE harness_operations SET state='failed' WHERE operation_id=$1",
                bootstrap.operation_id,
            )
            with pytest.raises(LifecycleRefused, match="admission changed"):
                await require_managed_control_source(
                    connection,
                    plan=plan,
                    access_artifact=artifact,
                    paid_operation_id=paid.operation_id,
                )
            await connection.execute(
                "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
                bootstrap.operation_id,
            )
            assert (
                await require_managed_control_source(
                    connection,
                    plan=plan,
                    access_artifact=artifact,
                    paid_operation_id=paid.operation_id,
                )
                == control.operation_id
            )
            await connection.execute(
                "UPDATE harness_approval_consumption SET reservation_state='released' "
                "WHERE operation_id=$1",
                control.operation_id,
            )
            with pytest.raises(LifecycleRefused, match="no longer retained"):
                await require_managed_control_source(
                    connection,
                    plan=plan,
                    access_artifact=artifact,
                    paid_operation_id=paid.operation_id,
                )
            assert approval["reservation_state"] == "confirmed"
            with pytest.raises(LifecycleRefused, match="no longer retained"):
                await revoke_managed_access_grant(
                    plan,
                    artifact,
                    journal,
                    eks=arguments["eks"],
                    verify_cluster=AsyncMock(),
                    connect=harness.connect,
                    paid_operation_id=paid.operation_id,
                )
            assert runtime.cloud.mutations == before + 1
            assert approval["plan_digest"] == control.plan_digest
            seal = await connection.fetchval(
                "SELECT sealed_revision FROM harness_allocation_seal "
                "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
                paid.org_id,
                paid.workspace_id,
                plan.original_allocation_id,
            )
            assert seal == "reviewed-paid-apply"
            assert not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_allocation_seal WHERE allocation_id=$1)",
                plan.allocation_id,
            )

    with Harness.started(tmp_path_factory, request.node.name) as harness:
        harness.run(exercise(harness))

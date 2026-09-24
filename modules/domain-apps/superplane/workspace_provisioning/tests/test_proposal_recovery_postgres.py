"""Lost proposal replies recover original proof, never current resource absence.

The Terraform process transport is offline; original paid admission, call intent,
immutable proposal publication, recovery claim and terminal receipt use PostgreSQL.
"""

# Imported pytest fixtures intentionally share names with fixture parameters.
# ruff: noqa: F811

import json
from types import SimpleNamespace

import pytest
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from superplane_executor.authority import VerifiedOperation

from workspace_provisioning.artifacts import digest, record_artifact
from workspace_provisioning.authority import validated_request
from workspace_provisioning.terraform import operation_directory, prepare

from .postgres_bridge import requires_harness_postgres
from .test_lifecycle_effects_postgres import harness as harness  # noqa: F401
from .test_lifecycle_recovery_postgres import recovery_case
from .test_lifecycle_terraform import Process

pytestmark = requires_harness_postgres


async def proposed(harness, tmp_path, monkeypatch, *, adopt=False):
    from account_factory.modes import OwnershipMode

    maintained = tmp_path / "maintained"
    maintained.mkdir()
    (maintained / "main.tf").write_text("# offline reviewed Terraform source")
    (maintained / ".terraform.lock.hcl").write_text("# offline provider lock")
    (maintained / "scripts").mkdir()
    for name in ("prepare_workspace_plan.py", "apply_workspace_plan.py"):
        (maintained / "scripts" / name).write_text("# offline process transport")
    monkeypatch.setattr(
        "workspace_provisioning.terraform.workspace_source", lambda: maintained
    )
    state = SimpleNamespace(directory=None, artifact=None, process=None, revoked=False)

    async def prepare_before_crash(operation, context):
        context.state_root = tmp_path / "worker-state"
        config, request, _ = validated_request(operation, context)
        lease = operation.grant.lease
        if adopt:
            from workspace_provisioning.adoption import prepare_adoption
            from .test_lifecycle_adoption import fixture

            fake, _, sdk, _, calls = fixture(
                monkeypatch,
                account=request.target_account_id,
                region=request.region,
                cluster=request.existing_cluster_name,
                org=lease.org_id,
                workspace=lease.workspace_id,
            )
            target, metadata = await prepare_adoption(fake, object(), request, sdk)
            directory, process = None, SimpleNamespace(calls=calls)
        else:
            directory = operation_directory(
                context.state_root,
                lease.org_id,
                lease.workspace_id,
                lease.operation_id,
                create=True,
            )
            operation.max_runtime_seconds = 900
            process = Process(directory)
            target, metadata = prepare(
                operation, context, config, request, request.target_account_id, process
            )
        state.artifact = await record_artifact(
            operation,
            context,
            account_id=request.target_account_id,
            target=target,
            metadata=metadata,
        )
        state.directory, state.process = directory, process

    operation, recovery = await recovery_case(
        harness,
        started=True,
        before_expiry=prepare_before_crash,
        mode=OwnershipMode.BRING_EXISTING_CLUSTER
        if adopt
        else OwnershipMode.EXISTING_ACCOUNT_MANAGED,
    )

    class RecoveryTransport:
        async def resolve_recovery(self, lease):
            if state.revoked:
                raise OperationRefused("actual recovery run was revoked")
            grant = RecoveryGrant(recovery.principal, lease)
            async with harness.connect() as connection, connection.transaction():
                if not await lock_recovery_grant(connection, grant):
                    raise OperationRefused(
                        "original claim no longer belongs to this run"
                    )
                paid = await connection.fetchrow(
                    "SELECT reservation_state,max_resource_units,max_runtime_seconds,max_cost_micros "
                    "FROM harness_approval_consumption WHERE operation_id=$1",
                    lease.operation_id,
                )
            return VerifiedOperation(
                grant,
                operation.job_id,
                operation.plan_digest,
                operation.request_payload,
                **dict(paid),
            )

    recovery.context.authority = RecoveryTransport()
    return operation, recovery, state


@pytest.mark.parametrize("adopt", [False, True])
def test_completed_plan_proposal_recovers_with_retained_budget_and_no_replay(
    harness, tmp_path, monkeypatch, adopt
):
    async def scenario():
        operation, recovery, state = await proposed(
            harness, tmp_path, monkeypatch, adopt=adopt
        )
        process_calls = tuple(state.process.calls)
        (result,) = await recovery.run(limit=1)
        assert result.action == "succeeded"
        assert tuple(state.process.calls) == process_calls
        async with harness.connect() as connection:
            row = await connection.fetchrow(
                "SELECT r.accounting,l.closed_holder,l.closed_attempt_id,l.fence_token "
                "FROM harness_recovery_settlements r JOIN harness_operation_leases l USING(operation_id) "
                "WHERE operation_id=$1",
                operation.grant.lease.operation_id,
            )
            accounting = (
                json.loads(row["accounting"])
                if isinstance(row["accounting"], str)
                else row["accounting"]
            )
            assert accounting["budget"] == "retain"
            assert accounting["inventory_complete"] is False
            assert accounting["release_permitted"] is False
            assert accounting["may_mark_released"] is False
            assert accounting["exposure"] == "unresolved"
            assert (
                accounting["lifecycle_proposal"]["status"] == "awaiting_plan_approval"
            )
            assert (
                accounting["lifecycle_proposal"]["artifact_id"]
                == state.artifact["artifact_id"]
            )
            assert accounting["operation_id"] == operation.grant.lease.operation_id
            assert accounting["job_id"] == operation.job_id
            assert accounting["claim"] == {
                "holder": row["closed_holder"],
                "attempt_id": row["closed_attempt_id"],
                "fence_token": row["fence_token"],
            }
            assert (
                await connection.fetchval("SELECT count(*) FROM harness_operations")
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 1
            )

    harness.run(scenario())


@pytest.mark.parametrize(
    "change", ["saved-plan", "maintained-source", "revoked", "wrong-producer"]
)
def test_unverified_proposal_cannot_close_or_release_original_operation(
    harness, tmp_path, monkeypatch, change
):
    async def scenario():
        operation, recovery, state = await proposed(harness, tmp_path, monkeypatch)
        if change == "saved-plan":
            path = state.directory / "review/workspace.tfplan"
            path.chmod(0o600)
            path.write_text("changed approved plan")
        elif change == "maintained-source":
            (tmp_path / "maintained/main.tf").write_text("changed maintained source")
        elif change == "wrong-producer":
            # Simulate corrupted/restored domain evidence with a self-consistent
            # digest. The original shared execution audit must still disagree.
            async with harness.connect() as connection:
                row = dict(
                    await connection.fetchrow(
                        "SELECT * FROM workspace_lifecycle_artifacts"
                    )
                )
                row["producer_holder"] = "foreign-execution"
                new_digest = digest(
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"artifact_id", "created_at"}
                    }
                )
                await connection.execute(
                    "UPDATE workspace_lifecycle_artifacts SET artifact_id=$1,producer_holder=$2",
                    new_digest,
                    row["producer_holder"],
                )
        else:
            state.revoked = True
        (result,) = await recovery.run(limit=1)
        assert result.action == "deferred"
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_recovery_settlements"
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT closed_at FROM harness_operation_leases WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                is None
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent"
                )
                == 1
            )

    harness.run(scenario())

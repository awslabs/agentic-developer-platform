"""Real paid apply, lost outer reply, and read-only recovery with retained exposure.

Only provider/process transports are doubled. Shared admission, saved plan bytes,
producer audit, immutable artifact, recovery fence and settlement use PostgreSQL.
"""

# Imported pytest fixtures intentionally share names with fixture parameters.
# ruff: noqa: F811

from copy import deepcopy
from builtins import BaseExceptionGroup
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from account_factory.modes import OwnershipMode
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import read_lease, renew as renew_lease
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from superplane_executor.authority import VerifiedOperation

from workspace_provisioning import runtime, terraform
from workspace_provisioning.adoption import prepare_adoption
from workspace_provisioning.artifacts import continuation_parameters
from workspace_provisioning.recovery import LifecycleRecovery
from workspace_provisioning.recovery_observer import observe_result

from .postgres_bridge import requires_harness_postgres
from .test_lifecycle_runtime_postgres import Scenario, harness as harness  # noqa: F401

pytestmark = requires_harness_postgres


class ProcessCrash(BaseException):
    pass


async def interrupted_apply(harness, tmp_path, monkeypatch):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.EXISTING_ACCOUNT_MANAGED
    )
    prepared = await scenario.prepared()
    applying = await scenario.admit(continuation_parameters(prepared))
    request = replace(
        scenario.request,
        mode=OwnershipMode.BRING_EXISTING_CLUSTER,
        existing_cluster_name="adopted",
        vpc_cidr=None,
        availability_zones=(),
        cluster_version=None,
    )
    _, metadata = await prepare_adoption(
        applying, scenario.context, request, scenario.session
    )
    scenario.outputs = metadata["outputs"]
    scenario.outputs["workspace_node_group"] = {
        "value": {
            "name": "nodes",
            "arn": scenario.responses["describe_nodegroup"]["nodegroup"][
                "nodegroupArn"
            ],
            "launch_template_id": "lt-0123456789abcdef0",
            "launch_template_version": "2",
        }
    }
    vpc = scenario.responses["describe_cluster"]["cluster"]["resourcesVpcConfig"]
    vpc["clusterSecurityGroupId"] = "sg-22222222222222222"
    vpc["securityGroupIds"] = ["sg-11111111111111111"]
    scenario.responses["describe_launch_template_versions"]["LaunchTemplateVersions"][
        0
    ]["LaunchTemplateData"].pop("SecurityGroupIds")
    record_artifact = runtime.record_artifact
    published = {}

    async def crash_after_commit(*args, **kwargs):
        published.update(await record_artifact(*args, **kwargs))
        raise ProcessCrash(
            "worker died after immutable result, before outer confirmation"
        )

    monkeypatch.setattr(runtime, "record_artifact", crash_after_commit)
    with pytest.raises(BaseExceptionGroup):
        await runtime.run_lifecycle(applying, scenario.context)
    artifact = await scenario.row(published["artifact_id"])
    lease = applying.grant.lease
    async with harness.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT stage FROM harness_provider_call_intent WHERE operation_id=$1",
                lease.operation_id,
            )
            == "intended"
        )
        await connection.execute(
            "CREATE TABLE workspaces(id text PRIMARY KEY,org_id text,provisioning_operation_id text)"
        )
        await connection.execute(
            "INSERT INTO workspaces VALUES($1,$2,$3)",
            lease.workspace_id,
            lease.org_id,
            lease.operation_id,
        )
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 minute',runtime_deadline=clock_timestamp()-interval '1 minute' WHERE operation_id=$1",
            lease.operation_id,
        )
    principal = replace(
        applying.grant.principal,
        subject="real-recovery-run#1",
        permissions=frozenset({"workspace:recover"}),
    )
    state = SimpleNamespace(
        reads=[],
        revoked=False,
        tamper=False,
        renew=False,
        renewal_started=False,
        extended=False,
    )

    class ProtectedTransport:
        async def resolve_recovery(self, claim):
            if state.revoked:
                raise OperationRefused("recovery run revoked")
            async with harness.connect() as connection, connection.transaction():
                if state.renew and not state.renewal_started:
                    # Give this claim a shorter expiry than its fixed runtime
                    # deadline so the real renewal function can extend it.
                    await connection.execute(
                        "UPDATE harness_operation_leases SET expires_at=clock_timestamp()+interval '30 seconds' WHERE operation_id=$1",
                        claim.operation_id,
                    )
                    state.renewal_started = True
                current = await read_lease(connection, operation_id=claim.operation_id)
                grant = RecoveryGrant(principal, current)
                if not await lock_recovery_grant(connection, grant):
                    raise OperationRefused("recovery claim is stale")
                paid = await connection.fetchrow(
                    "SELECT reservation_state,max_resource_units,max_runtime_seconds,max_cost_micros FROM harness_approval_consumption WHERE operation_id=$1",
                    claim.operation_id,
                )
            return VerifiedOperation(
                grant,
                applying.job_id,
                applying.plan_digest,
                applying.request_payload,
                **dict(paid),
            )

        async def lifecycle(self, claim, key, **binding):
            assert binding == {
                "artifact_id": artifact["artifact_id"],
                "plan_digest": applying.plan_digest,
                "phase": "apply-infrastructure",
            }
            current = await self.resolve_recovery(claim)

            async def read(service, method, **arguments):
                state.reads.append((service, method, deepcopy(arguments)))
                assert method.startswith(("get_", "describe_", "list_"))
                if state.renew:
                    state.renew = False
                    async with harness.connect() as connection:
                        previous = await read_lease(
                            connection, operation_id=claim.operation_id
                        )
                        renewed = await renew_lease(
                            connection, previous, duration=timedelta(seconds=45)
                        )
                        state.extended = renewed.expires_at > previous.expires_at
                return getattr(
                    scenario.session.client(service, region_name="us-east-1"), method
                )(**arguments)

            result = await observe_result(
                current,
                context,
                artifact=artifact,
                provider=SimpleNamespace(aws_read=read),
            )
            await self.resolve_recovery(claim)
            if state.tamper:
                result["source_artifact_id"] = "f" * 64
            return result

    context = SimpleNamespace(
        connect=harness.connect,
        domain_connect=harness.connect,
        authority=ProtectedTransport(),
        state_root=scenario.context.state_root,
        policy=scenario.policy,
        policy_fixture=True,
    )
    pool = SimpleNamespace(acquire=harness.connect)
    recovery = LifecycleRecovery(
        SimpleNamespace(domain_pool=pool, execution_pool=pool),
        principal=principal,
        operation_id=lease.operation_id,
        context=context,
    )
    return scenario, prepared, applying, artifact, recovery, state


@pytest.mark.parametrize("renew", [False, True])
def test_completed_apply_recovers_same_paid_identity_with_fresh_reads_and_retains_budget(
    harness, tmp_path, monkeypatch, renew
):
    async def run():
        scenario, _, applying, artifact, recovery, state = await interrupted_apply(
            harness, tmp_path, monkeypatch
        )
        state.renew = renew
        before = tuple(scenario.process_calls)
        (result,) = await recovery.run(limit=1)
        assert result.action == "succeeded"
        assert state.reads and tuple(scenario.process_calls) == before
        assert state.extended is renew
        assert not scenario.bootstrap_calls and not scenario.creates
        async with harness.connect() as connection:
            row = await connection.fetchrow(
                "SELECT accounting FROM harness_recovery_settlements WHERE operation_id=$1",
                applying.grant.lease.operation_id,
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
            assert (
                accounting["lifecycle_proposal"]["artifact_id"]
                == artifact["artifact_id"]
            )
            assert accounting["lifecycle_proposal"]["phase"] == "bootstrap-workspace"
            assert (
                await connection.fetchval("SELECT count(*) FROM harness_operations")
                == 2
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent"
                )
                == 2
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 2
            )
        assert not scenario.ledger.released

    harness.run(run())


def test_recovery_subject_revoked_after_preparation_cannot_commit_settlement(
    harness, tmp_path, monkeypatch
):
    async def run():
        scenario, _, applying, _, recovery, _ = await interrupted_apply(
            harness, tmp_path, monkeypatch
        )
        prepare = recovery._prepare

        async def revoke_after_prepare(lease, calls):
            result = await prepare(lease, calls)
            async with harness.connect() as connection:
                await connection.execute(
                    "DELETE FROM harness_recovery_claim_bindings WHERE operation_id=$1 AND fence_token=$2",
                    lease.operation_id,
                    lease.fence_token,
                )
            return result

        recovery._prepare = revoke_after_prepare
        before = tuple(scenario.process_calls)
        with pytest.raises(OperationRefused):
            await recovery.run(limit=1)
        assert tuple(scenario.process_calls) == before
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
                    applying.grant.lease.operation_id,
                )
                is None
            )
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    applying.grant.lease.operation_id,
                )
                != "succeeded"
            )
        assert not scenario.ledger.released

    harness.run(run())


@pytest.mark.parametrize(
    "change", ["saved-plan", "provider-replaced", "response-binding", "revoked"]
)
def test_changed_apply_evidence_defers_without_replay_or_release(
    harness, tmp_path, monkeypatch, change
):
    async def run():
        scenario, prepared, applying, _, recovery, state = await interrupted_apply(
            harness, tmp_path, monkeypatch
        )
        before = tuple(scenario.process_calls)
        if change == "saved-plan":
            _, review = terraform.verify_prepared_artifact(prepared, recovery.context)
            path = review / "workspace.tfplan"
            path.chmod(0o600)
            path.write_text("changed approved plan")
        elif change == "provider-replaced":
            scenario.responses["describe_nodegroup"]["nodegroup"]["nodegroupArn"] += (
                "-replacement"
            )
        elif change == "response-binding":
            state.tamper = True
        else:
            state.revoked = True
        (result,) = await recovery.run(limit=1)
        assert result.action == "deferred"
        assert tuple(scenario.process_calls) == before
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
                    applying.grant.lease.operation_id,
                )
                is None
            )
        if change in {"saved-plan", "revoked"}:
            assert not state.reads
        assert not scenario.ledger.released

    harness.run(run())

# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real worker handoff from failed paid apply through separate planning and repair.

AWS reads, Terraform and protected-owner approval are transport doubles. SQL,
admission, lease fencing, artifact selection, inventory sealing and history are real.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from account_factory.modes import OwnershipMode
from harness_jobs import SpendEnvelope
from harness_jobs.execution_rpc import ExecutionGrant
from builtins import BaseExceptionGroup
from harness_jobs.provider_retry import recover_uncredentialed
from harness_jobs.store import _record

from workspace_provisioning import partial_apply_backend, runtime, terraform
from workspace_provisioning.adoption import prepare_adoption
from workspace_provisioning.artifacts import canonical, continuation_parameters, digest
from workspace_provisioning.partial_apply_artifact import repair_artifact
from workspace_provisioning.partial_apply_request import preparation_request
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_runtime_postgres import Scenario, harness as harness
from .test_retirement_execution_postgres import _Approves, _principal
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


class ApprovedEnvelope(_Approves):
    async def approval_for(self, *, principal, request):
        approved = await super().approval_for(principal=principal, request=request)
        envelope = SpendEnvelope(
            **{
                k: int(request.parameters[k])
                for k in (
                    "max_resource_units",
                    "max_cost_micros",
                    "max_runtime_seconds",
                )
            }
        )
        return replace(
            approved,
            record=replace(approved.record, envelope=envelope),
            requested_envelope=envelope,
        )


@pytest.mark.parametrize("lost_reply", [False, True])
@pytest.mark.parametrize("fresh_inspection", [False, True])
def test_original_apply_uses_only_fresh_repair_artifact_and_preserves_inventory(
    harness, tmp_path, monkeypatch, lost_reply, fresh_inspection
):
    scenario = Scenario(
        harness,
        tmp_path,
        monkeypatch,
        OwnershipMode.EXISTING_ACCOUNT_MANAGED,
        isolation="dedicated",
    )
    scenario.facade.approvals = ApprovedEnvelope()
    scenario.parameters["credential_id"] = "fixture-provider"

    async def run():
        prepared = await scenario.prepared()
        applying = await scenario.admit(continuation_parameters(prepared))
        operation_id = applying.grant.lease.operation_id
        scenario.lose_apply_reply = True
        with pytest.raises(LifecycleRefused, match="no unique durable result"):
            await runtime.run_lifecycle(applying, scenario.context)
        assert any(
            "apply_workspace_plan.py" in call[1] for call in scenario.process_calls
        )
        scenario.lose_apply_reply = False
        async with harness.connect() as db:
            # Model a terminated worker: no final status report reached the producer.
            # The intended call and partial cloud facts remain durable.
            await db.execute(
                "UPDATE harness_operations SET state='running' WHERE operation_id=$1",
                operation_id,
            )
            source = _record(
                await db.fetchrow(
                    "SELECT * FROM harness_operations WHERE operation_id=$1",
                    operation_id,
                )
            )
            paid = dict(
                await db.fetchrow(
                    "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                    operation_id,
                )
            )
            original_call = dict(
                await db.fetchrow(
                    "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                    operation_id,
                )
            )
            await db.execute(
                "UPDATE harness_operation_leases SET expires_at=now()-interval '2 hours',runtime_deadline=now()-interval '1 hour' WHERE operation_id=$1",
                operation_id,
            )
            await db.execute(
                "CREATE TABLE workspaces(id text,org_id text,provisioning_operation_id text,status text,cluster_id text,is_default boolean)"
            )
            await db.execute(
                "INSERT INTO workspaces VALUES('ws-1','org-a',$1,'Provisioning',NULL,false)",
                operation_id,
            )
        adopted = replace(
            scenario.request,
            mode=OwnershipMode.BRING_EXISTING_CLUSTER,
            existing_cluster_name="adopted",
            vpc_cidr=None,
            availability_zones=(),
            cluster_version=None,
        )
        _, metadata = await prepare_adoption(
            applying, scenario.context, adopted, scenario.session
        )
        scenario.outputs = metadata["outputs"]
        cluster = scenario.outputs["cluster_name"]["value"]
        snapshot = {
            "serial": 2,
            "lineage": "original",
            "version_id": "original-version",
            "resources": {
                "aws_eks_cluster.workspace": {
                    "type": "aws_eks_cluster",
                    "id": cluster,
                    "tainted": False,
                },
                "aws_eip.nat[0]": {
                    "type": "aws_eip",
                    "id": "eipalloc-0123456789abcdef0",
                    "tainted": True,
                },
            },
        }
        evidence = {
            **{
                k: getattr(source, k)
                for k in ("org_id", "workspace_id", "operation_id", "plan_digest")
            },
            "source_version": source.version,
            "source_fence": applying.grant.lease.fence_token,
            "source_key": original_call["idempotency_key"],
            "original_artifact_id": prepared["artifact_id"],
            "record_id": "sppa1:" + "e" * 64,
            "evidence_kind": "partial-apply-state-v1",
            "apply_started": True,
            "account_id": "000000000002",
            "region": "us-east-1",
            "credential_id": "fixture-provider",
            "snapshot": snapshot,
        }
        if fresh_inspection:
            now = datetime.now(UTC)
            evidence.update(
                version=2,
                inspection_id="1" * 16,
                checked_at=now.isoformat(),
                expires_at=(now + timedelta(hours=1)).isoformat(),
            )
        request = preparation_request(source, evidence)
        planning = await scenario.admit(
            {**request.parameters, "idempotency_key": request.idempotency_key}
        )
        async with harness.connect() as db:
            await db.execute(
                """INSERT INTO workspace_partial_apply_preparations
            (operation_id,org_id,workspace_id,source_operation_id,source_fence,source_version,
             plan_digest,original_allocation_id,owner_evidence_sha256,owner_evidence_json)
            VALUES($1,'org-a','ws-1',$2,$3,$4,$5,$6,$7,$8)""",
                planning.grant.lease.operation_id,
                operation_id,
                evidence["source_fence"],
                source.version,
                planning.plan_digest,
                applying.request.parameters["allocation_id"],
                digest(evidence),
                canonical(evidence),
            )
        reads = []

        def snapshot_read(session, backend, target, *, expected):
            assert session is scenario.session and expected == snapshot
            reads.append(target)
            return snapshot

        monkeypatch.setattr(partial_apply_backend, "read_snapshot", snapshot_read)
        scenario.repair_plan = {
            "format_version": "1.2",
            "terraform_version": "1.9.8",
            "errored": False,
            "complete": True,
            "applyable": True,
            "resource_changes": [
                {
                    "mode": "managed",
                    "address": address,
                    "type": old["type"],
                    "provider_name": "registry.terraform.io/hashicorp/aws",
                    "action_reason": "replace_because_tainted"
                    if old["tainted"]
                    else None,
                    "change": {
                        "actions": ["delete", "create"]
                        if old["tainted"]
                        else ["no-op"],
                        "before": {"id": old["id"]},
                    },
                }
                for address, old in snapshot["resources"].items()
            ]
            + [
                {
                    "mode": "managed",
                    "address": "aws_vpc.workspace[0]",
                    "type": "aws_vpc",
                    "provider_name": "registry.terraform.io/hashicorp/aws",
                    "change": {"actions": ["create"], "before": None},
                }
            ],
        }
        proposal = await runtime.run_lifecycle(planning, scenario.context)
        assert proposal["phase"] == "partial-apply-repair" and len(reads) == 2
        repaired = await scenario.row(proposal["artifact_id"])
        with pytest.raises(LifecycleRefused, match="continuation phase"):
            continuation_parameters(repaired)
        async with harness.connect() as db:
            assert (
                await db.fetchval("SELECT provisioning_operation_id FROM workspaces")
                == operation_id
            )
        _, repair = await repair_artifact(
            harness.connect,
            harness.connect,
            source=source.__dict__,
            artifact_id=repaired["artifact_id"],
            evidence=evidence,
        )
        proof = {
            "evidence_kind": "partial-apply-state-v1",
            "apply_started": True,
            "original_authority_expired": True,
            "issued_sessions_expired": True,
            "owner_evidence_id": evidence["record_id"],
            "owner_evidence": evidence,
            "repair": repair,
        }

        async def authorize(*_):
            return datetime.now(UTC) + timedelta(hours=1)

        async def prove(*_):
            return proof

        async with harness.connect() as db:
            receipt = await recover_uncredentialed(
                db,
                principal=_principal(),
                operation_id=operation_id,
                expected_version=source.version,
                reauthorization_id="fresh-exact-repair-decision",
                authorize=authorize,
                prove_not_started=prove,
                recovery_scope="partial-apply-repair",
            )
        lease = await harness.lease(
            operation_id, holder="repair-worker", attempt="repair-attempt"
        )
        applying.grant = ExecutionGrant(_principal(lease.holder), lease)
        scenario.expected_apply_artifact = terraform.operation_directory(
            scenario.context.state_root,
            "org-a",
            "ws-1",
            planning.grant.lease.operation_id,
        ).name
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
        scenario.responses["describe_launch_template_versions"][
            "LaunchTemplateVersions"
        ][0]["LaunchTemplateData"].pop("SecurityGroupIds")
        scenario.applied_state = {
            "format_version": "1.0",
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "mode": "managed",
                            "type": "aws_eks_cluster",
                            "address": "aws_eks_cluster.workspace",
                            "values": {
                                "id": cluster,
                                "name": cluster,
                                "arn": scenario.outputs["cluster_arn"]["value"],
                            },
                        },
                        {
                            "mode": "managed",
                            "type": "aws_eip",
                            "address": "aws_eip.nat[0]",
                            "values": {"id": "eipalloc-11111111111111111"},
                        },
                        {
                            "mode": "managed",
                            "type": "aws_vpc",
                            "address": "aws_vpc.workspace[0]",
                            "values": {"id": scenario.outputs["vpc_id"]["value"]},
                        },
                    ]
                }
            },
        }

        def addresses(**kwargs):
            if "Filters" in kwargs and kwargs["Filters"][0]["Name"].startswith("tag:"):
                assert {f["Name"]: f["Values"] for f in kwargs["Filters"]} == {
                    "tag:WorkspaceId": ["ws-1"],
                    "tag:OrgId": ["org-a"],
                }
                return {"Addresses": [{"AllocationId": "eipalloc-11111111111111111"}]}
            if "Filters" in kwargs:
                assert kwargs["Filters"] == [
                    {
                        "Name": "allocation-id",
                        "Values": [snapshot["resources"]["aws_eip.nat[0]"]["id"]],
                    }
                ]
                return {"Addresses": []}
            assert kwargs == {"AllocationIds": ["eipalloc-11111111111111111"]}
            return {"Addresses": [{"AllocationId": "eipalloc-11111111111111111"}]}

        monkeypatch.setattr(
            type(scenario.session),
            "describe_addresses",
            lambda _, **kw: addresses(**kw),
            raising=False,
        )
        if lost_reply:
            from .test_applied_recovery_postgres import ProcessCrash

            published = {}
            record = runtime.record_artifact

            async def crash(*args, **kwargs):
                published.update(await record(*args, **kwargs))
                raise ProcessCrash("repair committed before reply")

            monkeypatch.setattr(runtime, "record_artifact", crash)
            with pytest.raises(BaseExceptionGroup):
                await runtime.run_lifecycle(applying, scenario.context)
            monkeypatch.setattr(runtime, "record_artifact", record)
            calls = tuple(scenario.process_calls)
            result = await recover_reply(
                harness, scenario, applying, published["artifact_id"]
            )
            assert tuple(scenario.process_calls) == calls
        else:
            result = await runtime.run_lifecycle(applying, scenario.context)
        assert result["phase"] == "bootstrap-workspace" and len(reads) == 3
        completed = await scenario.row(result["artifact_id"])
        metadata = json.loads(completed["artifact_metadata_json"])
        assert metadata["source_artifact_id"] == prepared["artifact_id"]
        assert metadata["repair_artifact_id"] == repaired["artifact_id"]
        assert metadata["partial_repair_receipt"] == receipt["reauthorization_id"]
        async with harness.connect() as db:
            assert (
                dict(
                    await db.fetchrow(
                        "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                        operation_id,
                    )
                )
                == paid
            )
            assert (
                await db.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent WHERE operation_id=$1",
                    operation_id,
                )
                == 2
            )
            if not lost_reply:
                assert await db.fetchval(
                    "SELECT sealed_revision FROM harness_allocation_seal WHERE allocation_id=$1",
                    applying.request.parameters["allocation_id"],
                )
            else:
                accounting = json.loads(
                    await db.fetchval(
                        "SELECT accounting FROM harness_recovery_settlements WHERE operation_id=$1",
                        operation_id,
                    )
                )
                assert (
                    accounting["budget"] == "retain"
                    and not accounting["release_permitted"]
                )
            assert (
                await db.fetchval(
                    "SELECT attempts FROM harness_operation_leases WHERE operation_id=$1",
                    operation_id,
                )
                == 2
            )
        assert not scenario.bootstrap_calls

    harness.run(run())


async def recover_reply(harness, scenario, applying, artifact_id):
    from types import SimpleNamespace
    from harness_jobs.leases import read_lease
    from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
    from harness_jobs.identity import OperationRefused
    from superplane_executor.authority import VerifiedOperation
    from workspace_provisioning.recovery import LifecycleRecovery
    from workspace_provisioning.recovery_observer import observe_result

    artifact = await scenario.row(artifact_id)
    lease = applying.grant.lease
    async with harness.connect() as db:
        await db.execute(
            "UPDATE harness_operation_leases SET expires_at=now()-interval '1 minute',runtime_deadline=now()-interval '1 minute' WHERE operation_id=$1",
            lease.operation_id,
        )
    principal = replace(
        applying.grant.principal,
        subject="repair-observer#1",
        permissions=frozenset({"workspace:recover"}),
    )

    class ProtectedTransport:
        async def resolve_recovery(self, claim):
            async with harness.connect() as db, db.transaction():
                current = await read_lease(db, operation_id=claim.operation_id)
                grant = RecoveryGrant(principal, current)
                if not await lock_recovery_grant(db, grant):
                    raise OperationRefused("stale recovery claim")
                paid = await db.fetchrow(
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
            assert binding["artifact_id"] == artifact_id
            current = await self.resolve_recovery(claim)

            async def read(service, method, **args):
                assert method.startswith(("get_", "list_", "describe_"))
                return getattr(
                    scenario.session.client(service, region_name="us-east-1"), method
                )(**args)

            return await observe_result(
                current,
                context,
                artifact=artifact,
                provider=SimpleNamespace(aws_read=read),
            )

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
    (settlement,) = await recovery.run(limit=1)
    assert settlement.action == "succeeded"
    return {"artifact_id": artifact_id, "phase": "bootstrap-workspace"}

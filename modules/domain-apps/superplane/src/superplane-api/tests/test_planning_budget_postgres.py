# ruff: noqa: F811 -- shared PostgreSQL fixtures are injected by pytest.
"""Original admission stays immutable while a separate human changes its future cap."""

import json
import uuid
from types import SimpleNamespace
from decimal import Decimal

import pytest
from sqlalchemy import select
from harness_jobs.identity import decode_payload
from workspace_provisioning.authority import validated_request
from workspace_provisioning.artifacts import canonical, continuation_parameters
from workspace_provisioning.planning_budget import approved_budget, amended_review
from workspace_provisioning.runtime_config import LifecycleRefused

from app.models.workspace import Workspace
from app.models.operation_reauthorization import OperationReauthorization
from app.services.planning_budget import PlanningBudgetService
from app.services.operation_approvals import ApprovalDenied
from tests.test_lifecycle_api_postgres import (
    lifecycle as lifecycle,
    ledger as ledger,
    installation_postgres_url as installation_postgres_url,
    pytestmark,  # noqa: F401
)


@pytest.fixture
async def interrupted(lifecycle):
    async with lifecycle.sessions.kw["bind"].begin() as connection:
        await connection.run_sync(OperationReauthorization.__table__.create)
    _, _, workspace = await lifecycle.prepare()
    async with lifecycle.connections.connect() as db:
        await db.execute(
            "UPDATE harness_operations SET state='running' WHERE operation_id=$1",
            workspace.provisioning_operation_id,
        )
        original = dict(
            await db.fetchrow(
                "SELECT * FROM harness_operations WHERE operation_id=$1",
                workspace.provisioning_operation_id,
            )
        )
        await db.execute(
            """INSERT INTO harness_operation_leases
          (operation_id,org_id,workspace_id,fence_token,holder,expires_at,acquired_at,runtime_deadline,attempt_id,attempts,max_attempts)
          VALUES($1,$2,$3,11,'worker#1',now()-interval '1 hour',now()-interval '2 hours',now()-interval '1 hour','worker#1',6,10)""",
            original["operation_id"],
            original["org_id"],
            original["workspace_id"],
        )
        await db.execute(
            """INSERT INTO harness_provider_call_intent
          (idempotency_key,operation_id,org_id,workspace_id,job_id,attempt_id,fence_token,provider,operation_kind,target,stage)
          VALUES('original-step',$1,$2,$3,$4,'worker#1',11,'superplane-lifecycle','prepare-infrastructure','original','intended')""",
            original["operation_id"],
            original["org_id"],
            original["workspace_id"],
            original["job_id"],
        )
    service = PlanningBudgetService(lifecycle.sessions, lifecycle.composition)
    return lifecycle, workspace, original, service


async def read_budget(lifecycle, original):
    return await approved_budget(
        lifecycle.connections.connect,
        lifecycle.connections.connect,
        operation_id=original["operation_id"],
        org_id=original["org_id"],
        workspace_id=original["workspace_id"],
        plan_digest=original["plan_digest"],
        parameters=decode_payload(original["request_payload"]).parameters,
    )


async def review(interrupted, *, daily="1000.00", key=None):
    lifecycle, workspace, original, service = interrupted
    with lifecycle.actor(workspace_id=workspace.id):
        return await service.request(
            str(workspace.id),
            expected_version=original["version"],
            idempotency_key=key or str(uuid.uuid4()),
            daily_usd=Decimal(daily),
        )


async def test_budget_is_separately_decided_and_original_history_and_attempts_survive(
    interrupted,
):
    lifecycle, workspace, original, service = interrupted
    ticket = await review(interrupted)
    assert ticket["budget_amendment"]["daily_micros"] == 1_000_000_000
    assert ticket["reauthorization_scope"] == "planning-budget"
    assert await read_budget(lifecycle, original) is None
    with lifecycle.actor(workspace_id=workspace.id), pytest.raises(ApprovalDenied):
        await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    with lifecycle.actor("approver", workspace.id):
        decided = await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
        replay = await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    assert replay == decided and decided["result"] == "allowed-once"
    async with lifecycle.connections.connect() as db:
        after = dict(
            await db.fetchrow(
                "SELECT * FROM harness_operations WHERE operation_id=$1",
                original["operation_id"],
            )
        )
        lease = await db.fetchrow(
            "SELECT * FROM harness_operation_leases WHERE operation_id=$1",
            original["operation_id"],
        )
    assert (
        after == original
        and lease["attempts"] == 6
        and lease["max_attempts"] == 10
        and lease["fence_token"] == 11
    )
    async with lifecycle.sessions() as session:
        saved = await session.get(Workspace, workspace.id)
        assert saved.budget_max_daily_usd == Decimal("1000.00")
    amendment = await read_budget(lifecycle, original)
    assert amendment["daily_micros"] == 1_000_000_000
    admitted = decode_payload(original["request_payload"])
    op = SimpleNamespace(
        request=admitted,
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id=original["org_id"], workspace_id=original["workspace_id"]
            )
        ),
    )
    context = SimpleNamespace(
        policy_fixture=True, policy=lifecycle.document["tenants"][original["org_id"]]
    )
    _, request, authorization = validated_request(op, context)
    metadata = {
        "next_phase": "apply-infrastructure",
        "daily_budget_amendment": amended_review(
            admitted.parameters, amendment, request, authorization
        ),
    }
    parameters = continuation_parameters(
        {
            "org_id": original["org_id"],
            "workspace_id": original["workspace_id"],
            "source_operation_id": original["operation_id"],
            "source_payload_digest": original["plan_digest"],
            "account_id": "000000000002",
            "artifact_id": "a" * 64,
            "parameters_json": canonical(dict(admitted.parameters)),
            "artifact_metadata_json": canonical(metadata),
        }
    )
    assert (
        parameters["max_cost_micros"]
        == parameters["lifecycle_allocation_max_cost_micros"]
        == "1000000000"
    )
    assert admitted.parameters["lifecycle_allocation_max_cost_micros"] == "3000000"
    assert admitted.parameters["max_cost_micros"] == "0"
    assert parameters["plan_revision"] != admitted.parameters["plan_revision"]
    op.request = SimpleNamespace(parameters=parameters)
    validated_request(op, context)
    with pytest.raises(ApprovalDenied):
        await service.resume(str(workspace.id), ticket["reauthorization_id"])


async def test_review_key_cannot_change_amount_and_approved_key_replays(interrupted):
    lifecycle, workspace, _, service = interrupted
    key = str(uuid.uuid4())
    ticket = await review(interrupted, key=key)
    with pytest.raises(ApprovalDenied):
        await review(interrupted, key=key, daily="2000")
    with lifecycle.actor("approver", workspace.id):
        await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    replay = await review(interrupted, key=key)
    assert (
        replay["reauthorization_id"] == ticket["reauthorization_id"]
        and replay["result"] == "allowed-once"
    )
    with pytest.raises(ApprovalDenied):
        await review(interrupted, daily="2000")


@pytest.mark.parametrize(
    "change", ["expired", "revoked", "source-revoked", "quota-lowered", "wrong-source"]
)
async def test_runtime_refuses_expired_withdrawn_or_misbound_amendment(
    interrupted, change
):
    lifecycle, workspace, original, service = interrupted
    ticket = await review(interrupted)
    with lifecycle.actor("approver", workspace.id):
        await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    async with lifecycle.connections.connect() as db:
        if change == "expired":
            await db.execute(
                "UPDATE operation_reauthorizations SET expires_at=now()-interval '1 second'"
            )
        elif change == "revoked":
            await db.execute("UPDATE operation_reauthorizations SET revoked=true")
        elif change == "source-revoked":
            await db.execute("UPDATE operation_approvals SET revoked=true")
        elif change == "quota-lowered":
            await db.execute("UPDATE workspaces SET budget_max_daily_usd=1")
        else:
            original["plan_digest"] = "b" * 64
    with pytest.raises(LifecycleRefused):
        await read_budget(lifecycle, original)


async def test_quota_drift_refuses_decision_without_rewriting_review(interrupted):
    lifecycle, workspace, _, service = interrupted
    ticket = await review(interrupted)
    async with lifecycle.sessions() as session:
        row = await session.scalar(
            select(Workspace).where(Workspace.id == workspace.id)
        )
        row.budget_max_daily_usd = Decimal("25")
        await session.commit()
    with lifecycle.actor("approver", workspace.id), pytest.raises(ApprovalDenied):
        await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    async with lifecycle.connections.connect() as db:
        row = await db.fetchrow(
            "SELECT * FROM operation_reauthorizations WHERE reauthorization_id=$1",
            ticket["reauthorization_id"],
        )
    assert row["result"] is None
    assert json.loads(row["amendment_json"])["daily_micros"] == 1_000_000_000


async def test_amended_artifact_still_needs_its_own_apply_approval(interrupted):
    from app.models.lifecycle import WorkspaceLifecycleArtifact
    from app.services.provisioning import ProvisioningRefused
    from workspace_provisioning.artifacts import digest

    lifecycle, workspace, original, service = interrupted
    ticket = await review(interrupted)
    with lifecycle.actor("approver", workspace.id):
        await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    amendment = await read_budget(lifecycle, original)
    admitted = decode_payload(original["request_payload"])
    operation = SimpleNamespace(
        request=admitted,
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id=original["org_id"], workspace_id=original["workspace_id"]
            )
        ),
    )
    context = SimpleNamespace(
        policy_fixture=True, policy=lifecycle.document["tenants"][original["org_id"]]
    )
    _, request, authorization = validated_request(operation, context)
    artifact_id = await lifecycle.artifact(workspace)
    async with lifecycle.connections.connect() as db:
        artifact = dict(
            await db.fetchrow(
                "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
                artifact_id,
            )
        )
    artifact.pop("artifact_id")
    artifact.pop("created_at")
    metadata = json.loads(artifact["artifact_metadata_json"])
    metadata["daily_budget_amendment"] = amended_review(
        admitted.parameters, amendment, request, authorization
    )
    artifact["artifact_metadata_json"] = canonical(metadata)
    artifact["artifact_id"] = digest(artifact)
    async with lifecycle.sessions() as session:
        session.add(WorkspaceLifecycleArtifact(**artifact))
        await session.commit()
    request_id = uuid.uuid4()
    preview = await lifecycle.review(workspace.id, artifact["artifact_id"], request_id)
    assert preview["approval_request"]["parameters"]["max_cost_micros"] == "1000000000"
    with pytest.raises(ProvisioningRefused):
        await lifecycle.continue_(
            workspace.id,
            artifact["artifact_id"],
            request_id,
            uuid.UUID(ticket["reauthorization_id"]),
        )
    approval = await lifecycle.approve(preview)
    continued = await lifecycle.continue_(
        workspace.id, artifact["artifact_id"], request_id, approval
    )
    assert continued["workspace_id"] == str(workspace.id)
    assert continued["phase"] == "apply-infrastructure"
    assert continued["provisioning_operation_id"] != original["operation_id"]
    async with lifecycle.connections.connect() as db:
        parent = await db.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            original["operation_id"],
        )
    assert (
        parent["request_payload"] == original["request_payload"]
        and parent["plan_digest"] == original["plan_digest"]
    )


async def confirmed_recovery(interrupted, *, completion=False):
    from app.services.provider_recovery import (
        UnstartedPlanningRecoveryService,
        SavedPlanCompletionService,
    )

    lifecycle, workspace, original, service = interrupted
    ticket = await review(interrupted)
    with lifecycle.actor("approver", workspace.id):
        await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    # This disposable fixture has no immutable trigger: simulate time passing,
    # never renew the original row in production.
    async with lifecycle.connections.connect() as db:
        await db.execute(
            "UPDATE operation_reauthorizations SET expires_at=now()-interval '1 second'"
        )
        before = dict(await db.fetchrow("SELECT * FROM operation_reauthorizations"))
    recovery = (
        SavedPlanCompletionService if completion else UnstartedPlanningRecoveryService
    )(lifecycle.sessions, lifecycle.composition)
    with lifecycle.actor(workspace_id=workspace.id):
        fresh = await recovery.request(
            str(workspace.id),
            expected_version=original["version"],
            idempotency_key=str(uuid.uuid4()),
        )
    assert fresh["planning_budget_confirmation"]["daily_usd"] == "1000"
    assert (
        fresh["planning_budget_confirmation"]["budget_reauthorization_id"]
        == ticket["reauthorization_id"]
    )
    return recovery, fresh, before


async def consume_fixture_recovery(lifecycle, original, fresh):
    async with lifecycle.connections.connect() as db:
        await db.execute(
            """INSERT INTO harness_provider_retries
            (operation_id,org_id,workspace_id,plan_digest,reauthorization_id,
             original_approval_id,requester,approved_by,expected_version,restored_version,
             original_key,replacement_key,source_fence,source_attempt_id,source_intent,proof,recovery_scope)
            SELECT o.operation_id,o.org_id,o.workspace_id,o.plan_digest,$2,c.approval_id,
             c.requester,c.approved_by,o.version,o.version+1,'original-step','retry-step',11,
             'worker#1','{}','{}',$3
            FROM harness_operations o JOIN harness_approval_consumption c USING(operation_id)
            WHERE o.operation_id=$1""",
            original["operation_id"],
            fresh["reauthorization_id"],
            fresh["reauthorization_scope"],
        )
        await db.execute(
            "UPDATE harness_operations SET version=version+1 WHERE operation_id=$1",
            original["operation_id"],
        )


@pytest.mark.parametrize("completion", [False, True])
async def test_expired_budget_needs_fresh_explicit_decision_and_consumed_receipt(
    interrupted,
    completion,
):
    lifecycle, workspace, original, _ = interrupted
    recovery, fresh, before = await confirmed_recovery(
        interrupted, completion=completion
    )
    with pytest.raises(LifecycleRefused):
        await read_budget(lifecycle, original)
    with lifecycle.actor("approver", workspace.id):
        await recovery.decide(
            str(workspace.id), fresh["reauthorization_id"], "allowed-once"
        )
    # A decision alone cannot restore worker budget authority.
    with pytest.raises(LifecycleRefused):
        await read_budget(lifecycle, original)
    await consume_fixture_recovery(lifecycle, original, fresh)
    approved = await read_budget(lifecycle, original)
    assert approved["daily_micros"] == 1_000_000_000
    assert (
        approved["planning_budget_confirmation"]["reauthorization_id"]
        == fresh["reauthorization_id"]
    )
    async with lifecycle.connections.connect() as db:
        after = dict(
            await db.fetchrow(
                "SELECT * FROM operation_reauthorizations WHERE reauthorization_id=$1",
                before["reauthorization_id"],
            )
        )
    assert before == after


@pytest.mark.parametrize(
    "change",
    [
        "revoked",
        "expired",
        "terms",
        "budget-revoked",
        "source-revoked",
        "quota-lowered",
        "version",
    ],
)
async def test_consumed_confirmation_refuses_withdrawal_or_binding_drift(
    interrupted, change
):
    lifecycle, workspace, original, _ = interrupted
    recovery, fresh, before = await confirmed_recovery(interrupted)
    with lifecycle.actor("approver", workspace.id):
        await recovery.decide(
            str(workspace.id), fresh["reauthorization_id"], "allowed-once"
        )
    await consume_fixture_recovery(lifecycle, original, fresh)
    assert await read_budget(lifecycle, original)
    async with lifecycle.connections.connect() as db:
        if change in {"revoked", "expired", "terms"}:
            update = {
                "revoked": "revoked=true",
                "expired": "expires_at=now()-interval '1 second'",
                "terms": "amendment_json='{}'",
            }[change]
            await db.execute(
                "UPDATE operation_reauthorizations SET "
                + update
                + " WHERE reauthorization_id=$1",
                fresh["reauthorization_id"],
            )
        elif change == "budget-revoked":
            await db.execute(
                "UPDATE operation_reauthorizations SET revoked=true WHERE reauthorization_id=$1",
                before["reauthorization_id"],
            )
        elif change == "source-revoked":
            await db.execute("UPDATE operation_approvals SET revoked=true")
        elif change == "quota-lowered":
            await db.execute("UPDATE workspaces SET budget_max_daily_usd=1")
        else:
            await db.execute("UPDATE harness_operations SET version=version-1")
    with pytest.raises(LifecycleRefused):
        await read_budget(lifecycle, original)


async def test_confirmation_requires_current_admin_authority(interrupted, monkeypatch):
    lifecycle, workspace, _, _ = interrupted
    recovery, fresh, _ = await confirmed_recovery(interrupted)
    resolve = recovery.authority.resolve

    async def without_admin(**kwargs):
        if kwargs["permission"] == "workspace:administer":
            return None
        return await resolve(**kwargs)

    monkeypatch.setattr(recovery.authority, "resolve", without_admin)
    with (
        lifecycle.actor("approver", workspace.id),
        pytest.raises(ApprovalDenied, match="administration"),
    ):
        await recovery.decide(
            str(workspace.id), fresh["reauthorization_id"], "allowed-once"
        )
    async with lifecycle.connections.connect() as db:
        assert (
            await db.fetchval(
                "SELECT result FROM operation_reauthorizations WHERE reauthorization_id=$1",
                fresh["reauthorization_id"],
            )
            is None
        )

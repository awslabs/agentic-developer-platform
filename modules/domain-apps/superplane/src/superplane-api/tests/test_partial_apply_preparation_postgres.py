# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real approvals/admissions keep original paid ownership during repair planning."""

import asyncio
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from harness_jobs.identity import decode_payload
from harness_jobs.store import _record
from workspace_provisioning.artifacts import digest
from workspace_provisioning.partial_apply_request import registration_values
from workspace_provisioning.runtime_config import LifecycleRefused

from app.models.lifecycle import WorkspacePartialApplyPreparation
from app.services import partial_apply_preparation as service
from tests.test_lifecycle_api_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    lifecycle as lifecycle,
    pytestmark as pytestmark,
)


@pytest.fixture
async def partial(lifecycle, monkeypatch):
    context = lifecycle
    monkeypatch.setattr(service, "async_session_factory", context.sessions)
    async with context.sessions.kw["bind"].begin() as db:
        await db.run_sync(WorkspacePartialApplyPreparation.__table__.create)
    _, _, workspace = await context.prepare()
    artifact_id = await context.artifact(workspace)
    request_id = uuid.uuid4()
    review = await context.review(workspace.id, artifact_id, request_id)
    approval = await context.approve(review)
    await context.continue_(workspace.id, artifact_id, request_id, approval)
    async with context.connections.connect() as db:
        source_id = await db.fetchval(
            "SELECT provisioning_operation_id FROM workspaces"
        )
        await db.execute(
            "UPDATE harness_operations SET state='running' WHERE operation_id=$1",
            source_id,
        )
        source = _record(
            await db.fetchrow(
                "SELECT * FROM harness_operations WHERE operation_id=$1", source_id
            )
        )
        original = {
            table: dict(
                await db.fetchrow(
                    "SELECT * FROM " + table + " WHERE operation_id=$1", source_id
                )
            )
            for table in ("harness_operations", "harness_approval_consumption")
        }
    params = decode_payload(source.request_payload).parameters
    evidence = {
        **{
            k: getattr(source, k)
            for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
        },
        "source_version": source.version,
        "source_fence": 1,
        "original_artifact_id": artifact_id,
        "evidence_kind": "partial-apply-state-v1",
        "apply_started": True,
        "account_id": params["aws_account_id"],
        "region": json.loads(params["lifecycle_request"])["region"],
        "credential_id": params["credential_id"],
        "record_id": "sppa1:" + "e" * 64,
        "snapshot": {"sha256": "f" * 64, "resources": {"retained": {"id": "cluster"}}},
    }
    calls = []

    async def observe(route, body, **kwargs):
        calls.append((route, body))
        assert route == "/partial-apply-observation"
        assert body["operation_id"] == source.operation_id
        assert body["workspace_id"] == str(workspace.id)
        return {"owner_evidence": copy.deepcopy(evidence)}

    context.composition.dispatcher = SimpleNamespace(
        transport=SimpleNamespace(post=observe)
    )
    return context, workspace, source, evidence, original, calls


async def review_and_approve(partial):
    context, workspace, *_ = partial
    with context.actor(workspace_id=workspace.id):
        async with context.sessions() as db:
            *_, review = await service.preview(
                context.composition, db, context.org_id, workspace.id
            )
    approval = await context.approve(review)
    return review, str(approval)


async def admit(partial, review, approval):
    context, workspace, *_ = partial
    with context.actor(workspace_id=workspace.id):
        async with context.sessions() as db:
            return await service.admit(
                context.composition,
                db,
                context.org_id,
                workspace.id,
                review["request_id"],
                review["revision"],
                approval,
            )


async def test_concurrent_preparation_preserves_original_paid_admission(partial):
    context, workspace, source, evidence, original, _ = partial
    review, approval = await review_and_approve(partial)
    results = await asyncio.gather(
        *(admit(partial, review, approval) for _ in range(2))
    )
    assert results[0] == results[1]
    receipt = results[0]
    assert receipt["workspace_ready"] is False
    assert receipt["source_operation_id"] == source.operation_id
    assert receipt["preparation_operation_id"] != source.operation_id
    from app.adapters.operation_dispatch import OperationDispatcher

    dispatcher = OperationDispatcher(
        context.connections.connect,
        context.composition.dispatcher.transport,
        domain_connect=context.connections.connect,
    )
    async with context.connections.connect() as connection:
        admitted = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            receipt["preparation_operation_id"],
        )
    binding = await dispatcher._domain_registration(admitted)
    assert binding["provisioning_operation_id"] == source.operation_id
    assert await dispatcher._registration(admitted)
    with (
        context.actor("approver", workspace.id),
        pytest.raises(LifecycleRefused, match="original requester"),
    ):
        async with context.sessions() as db:
            await service.admit(
                context.composition,
                db,
                context.org_id,
                workspace.id,
                review["request_id"],
                review["revision"],
                approval,
            )
    async with context.connections.connect() as db:
        for table, before in original.items():
            assert (
                dict(
                    await db.fetchrow(
                        "SELECT * FROM " + table + " WHERE operation_id=$1",
                        source.operation_id,
                    )
                )
                == before
            )
        assert (
            await db.fetchval("SELECT provisioning_operation_id FROM workspaces")
            == source.operation_id
        )
        assert await db.fetchval("SELECT count(*) FROM workspaces") == 1
        assert (
            await db.fetchval(
                "SELECT count(*) FROM workspace_partial_apply_preparations"
            )
            == 1
        )
        registry, actual = await registration_values(
            db,
            db,
            operation_id=receipt["preparation_operation_id"],
            org_id=str(context.org_id),
            workspace_id=str(workspace.id),
        )
        assert actual == evidence and registry["owner_evidence_sha256"] == digest(
            evidence
        )
        paid = await db.fetchrow(
            "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
            receipt["preparation_operation_id"],
        )
        assert paid["max_cost_micros"] == paid["max_resource_units"] == 0


async def test_preparation_requires_exact_approved_review_and_original_requester(
    partial,
):
    context, workspace, *_ = partial
    review, approval = await review_and_approve(partial)
    for changed in (
        {**review, "revision": "0" * 64},
        {**review, "request_id": str(uuid.uuid4())},
    ):
        with pytest.raises(LifecycleRefused):
            await admit(partial, changed, approval)
    with pytest.raises(LifecycleRefused):
        await admit(partial, review, str(uuid.uuid4()))
    with context.actor("approver", workspace.id), pytest.raises(LifecycleRefused):
        async with context.sessions() as db:
            await service.preview(context.composition, db, context.org_id, workspace.id)
    async with context.connections.connect() as db:
        assert (
            await db.fetchval(
                "SELECT count(*) FROM workspace_partial_apply_preparations"
            )
            == 0
        )
        assert await db.fetchval("SELECT count(*) FROM harness_operations") == 2


async def test_admission_survives_registration_reply_loss(partial, monkeypatch):
    context, workspace, *_ = partial
    review, approval = await review_and_approve(partial)
    original = service.registration_values

    async def unavailable(*args, **kwargs):
        raise OSError("lost registration transaction")

    monkeypatch.setattr(service, "registration_values", unavailable)
    with pytest.raises(OSError, match="lost registration"):
        await admit(partial, review, approval)
    monkeypatch.setattr(service, "registration_values", original)
    result = await admit(partial, review, approval)
    assert result["workspace_ready"] is False
    async with context.connections.connect() as db:
        assert await db.fetchval("SELECT count(*) FROM harness_operations") == 3
        assert (
            await db.fetchval(
                "SELECT count(*) FROM workspace_partial_apply_preparations"
            )
            == 1
        )


async def test_exact_repair_decision_accepts_original_distinct_approver(
    partial, monkeypatch
):
    from datetime import datetime, UTC, timedelta
    from unittest.mock import AsyncMock
    from harness_jobs.execution import record_intent
    from harness_jobs.execution_plan import admitted_steps, step_key
    from harness_jobs.leases import acquire
    from app.models.operation_reauthorization import OperationReauthorization
    from app.services import partial_apply_repair
    from app.services.operation_approvals import ApprovalDenied

    context, workspace, source, evidence, original, _ = partial
    async with context.sessions.kw["bind"].begin() as db:
        await db.run_sync(OperationReauthorization.__table__.create)
    async with context.connections.connect() as db:
        lease = await acquire(
            db, operation_id=source.operation_id, holder="worker", attempt_id="worker"
        )
        step = admitted_steps(source)[0]
        await record_intent(
            db,
            lease,
            idempotency_key=step_key(source, step),
            provider=step.provider,
            operation_kind=step.operation_kind,
            target=step.target,
        )
        await db.execute(
            "UPDATE harness_operation_leases SET expires_at=$1,runtime_deadline=$1 WHERE operation_id=$2",
            datetime.now(UTC) - timedelta(hours=1),
            source.operation_id,
        )
    artifact = "d" * 64
    repair = {"artifact_id": artifact, "exact_plan": "bounded-replacement"}
    verifier = AsyncMock(return_value=({}, repair))
    monkeypatch.setattr(partial_apply_repair, "repair_artifact", verifier)
    recovery = partial_apply_repair.PartialApplyRepairService(
        context.sessions, context.composition
    )
    monkeypatch.setattr(
        recovery, "owner_evidence_id", AsyncMock(return_value=evidence["record_id"])
    )
    with context.actor(workspace_id=workspace.id):
        ticket = await recovery.request(
            str(workspace.id),
            expected_version=source.version,
            idempotency_key=str(uuid.uuid4()),
            artifact_id=artifact,
        )
        assert ticket["repair_plan"]["repair"] == repair
        with pytest.raises(ApprovalDenied):
            await recovery.decide(
                str(workspace.id), ticket["reauthorization_id"], "allowed-once"
            )
    with context.actor("approver", workspace.id):
        result = await recovery.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
        assert result["decided_by"] == "approver" and result["result"] == "allowed-once"
    assert verifier.await_count == 2
    async with context.connections.connect() as db:
        assert (
            dict(
                await db.fetchrow(
                    "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                    source.operation_id,
                )
            )
            == original["harness_approval_consumption"]
        )
        assert await db.fetchval("SELECT count(*) FROM harness_provider_retries") == 0


@pytest.mark.parametrize("obstruction", [None, "fresh", "active", "provider-intent"])
async def test_new_inspection_preserves_old_admission_and_requires_unstarted_expired_proof(
    partial, obstruction
):
    from harness_jobs.leases import acquire
    from harness_jobs.execution import record_intent
    from harness_jobs.execution_plan import admitted_steps, step_key

    context, workspace, source, evidence, original, _ = partial
    now = datetime.now(timezone.utc)
    # Model a legitimate admission retained from an earlier clock instant. The
    # protected transport's freshness checks are covered by Gateway tests.
    evidence.update(
        version=1,
        checked_at=(now - timedelta(hours=2)).isoformat(),
        expires_at=(
            now + timedelta(hours=1)
            if obstruction == "fresh"
            else now - timedelta(hours=1)
        ).isoformat(),
    )
    review, approval = await review_and_approve(partial)
    old = await admit(partial, review, approval)
    old_id = old["preparation_operation_id"]
    async with context.connections.connect() as db:
        if obstruction in {"active", "provider-intent"}:
            lease = await acquire(
                db, operation_id=old_id, holder="worker", attempt_id="worker"
            )
            if obstruction == "provider-intent":
                record = _record(
                    await db.fetchrow(
                        "SELECT * FROM harness_operations WHERE operation_id=$1", old_id
                    )
                )
                step = admitted_steps(record)[0]
                await record_intent(
                    db,
                    lease,
                    idempotency_key=step_key(record, step),
                    provider=step.provider,
                    operation_kind=step.operation_kind,
                    target=step.target,
                )
                await db.execute(
                    "UPDATE harness_operation_leases SET expires_at=$1,runtime_deadline=$1 WHERE operation_id=$2",
                    now - timedelta(seconds=1),
                    old_id,
                )
        retained = {
            table: [
                dict(r)
                for r in await db.fetch(
                    "SELECT * FROM " + table + " WHERE operation_id=$1", old_id
                )
            ]
            for table in [
                "harness_operations",
                "harness_approval_consumption",
                "harness_operation_leases",
                "workspace_partial_apply_preparations",
            ]
        }
    evidence.update(
        version=2,
        inspection_id="1" * 16,
        record_id="sppa1:" + "a" * 64,
        checked_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    new_review, new_approval = await review_and_approve(partial)
    assert new_review["request_id"] != review["request_id"] and new_approval != approval
    if obstruction:
        with pytest.raises(
            LifecycleRefused, match="fresh, active or has provider history"
        ):
            await admit(partial, new_review, new_approval)
    else:
        fresh = await admit(partial, new_review, new_approval)
        assert fresh["preparation_operation_id"] != old_id
        assert (
            fresh["source_operation_id"]
            == old["source_operation_id"]
            == source.operation_id
        )
        assert await admit(partial, new_review, new_approval) == fresh
        with pytest.raises(LifecycleRefused):
            await admit(partial, new_review, approval)
    async with context.connections.connect() as db:
        for table, rows in retained.items():
            assert [
                dict(r)
                for r in await db.fetch(
                    "SELECT * FROM " + table + " WHERE operation_id=$1", old_id
                )
            ] == rows
        for table, row in original.items():
            assert (
                dict(
                    await db.fetchrow(
                        "SELECT * FROM " + table + " WHERE operation_id=$1",
                        source.operation_id,
                    )
                )
                == row
            )
        assert (
            await db.fetchval("SELECT provisioning_operation_id FROM workspaces")
            == source.operation_id
        )
        assert await db.fetchval("SELECT count(*) FROM workspaces") == 1

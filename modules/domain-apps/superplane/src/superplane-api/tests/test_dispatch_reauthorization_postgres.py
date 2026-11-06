# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real preview/admission followed by fresh human approval of the same plan."""

import asyncio
import uuid

import pytest
from harness_jobs.outbox import DispatchOutbox
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.operation_reauthorization import OperationReauthorization
from app.services.dispatch_recovery import DispatchRecoveryService
from app.services.operation_approvals import ApprovalDenied
from tests.reauthorization_schema import create_reauthorization_schema
from tests.test_lifecycle_api_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    lifecycle as lifecycle,
    pytestmark,  # noqa: F401
)


@pytest.fixture
async def recovery(lifecycle):  # noqa: F811
    engine = lifecycle.sessions.kw["bind"]
    await create_reauthorization_schema(engine)
    _, _, workspace = await lifecycle.prepare()

    class Refused:
        async def deliver(self, envelope):
            return False

    async with lifecycle.connections.connect() as db:
        await DispatchOutbox(max_attempts=1).drain_once(db, Refused())
        operation = dict(await db.fetchrow("SELECT * FROM harness_operations"))
        await db.execute(
            "UPDATE operation_approvals SET expires_at=now()-interval '1 hour'"
        )
        old = dict(await db.fetchrow("SELECT * FROM operation_approvals"))
    service = DispatchRecoveryService(lifecycle.sessions, lifecycle.composition)
    return lifecycle, service, str(workspace.id), operation, old


async def test_concurrent_review_returns_the_same_request_key_ticket(recovery):
    context, _, workspace_id, operation, _ = recovery
    barrier = asyncio.Barrier(2)

    class ConcurrentSession(AsyncSession):
        async def commit(self):
            await barrier.wait()
            await super().commit()

    sessions = async_sessionmaker(
        context.sessions.kw["bind"], class_=ConcurrentSession, expire_on_commit=False
    )
    service = DispatchRecoveryService(sessions, context.composition)
    request_key = str(uuid.uuid4())
    with context.actor(workspace_id=workspace_id):
        first, second = await asyncio.wait_for(
            asyncio.gather(
                *[
                    service.request(
                        workspace_id,
                        expected_version=operation["version"],
                        idempotency_key=request_key,
                    )
                    for _ in range(2)
                ]
            ),
            timeout=10,
        )
    assert first == second
    async with context.connections.connect() as db:
        assert await db.fetchval("SELECT count(*) FROM operation_reauthorizations") == 1


async def test_fresh_decision_preserves_expired_approval_and_original_plan(recovery):
    context, service, workspace_id, operation, old = recovery
    request_key = str(uuid.uuid4())
    with context.actor(workspace_id=workspace_id):
        ticket = await service.request(
            workspace_id,
            expected_version=operation["version"],
            idempotency_key=request_key,
        )
        repeated = await service.request(
            workspace_id,
            expected_version=operation["version"],
            idempotency_key=request_key,
        )
    assert ticket == repeated
    assert ticket["original_approval_id"] == old["approval_id"]
    assert ticket["plan_digest"] == old["plan_digest"]
    assert ticket["result"] == "pending" and ticket["can_decide"] is False
    with context.actor(workspace_id=workspace_id), pytest.raises(ApprovalDenied):
        await service.decide(workspace_id, ticket["reauthorization_id"], "allowed-once")
    with context.actor("approver", workspace_id):
        decided = await service.decide(
            workspace_id, ticket["reauthorization_id"], "allowed-once"
        )
        with pytest.raises(ApprovalDenied, match="immutable"):
            await service.decide(workspace_id, ticket["reauthorization_id"], "rejected")
    assert decided["result"] == "allowed-once" and decided["decided_by"] == "approver"
    async with context.connections.connect() as db:
        assert dict(await db.fetchrow("SELECT * FROM operation_approvals")) == old
        assert dict(await db.fetchrow("SELECT * FROM harness_operations")) == operation
        assert await db.fetchval("SELECT count(*) FROM operation_reauthorizations") == 1


@pytest.mark.parametrize(
    "mutation", ["version", "foreign-requester", "revoked-original", "revoked-approver"]
)
async def test_reauthorization_review_refuses_stale_or_withdrawn_authority(
    recovery, mutation
):
    context, service, workspace_id, operation, _ = recovery
    subject, version = "requester", operation["version"]
    if mutation == "version":
        version += 1
    elif mutation == "foreign-requester":
        subject = "approver"
    elif mutation == "revoked-original":
        async with context.connections.connect() as db:
            await db.execute("UPDATE operation_approvals SET revoked=true")
    else:
        async with context.sessions() as session:
            await session.execute(
                text(
                    "UPDATE workspace_grants SET revoked_at=now() WHERE principal='approver'"
                )
            )
            await session.commit()
    with context.actor(subject, workspace_id), pytest.raises(ApprovalDenied):
        await service.request(
            workspace_id, expected_version=version, idempotency_key=str(uuid.uuid4())
        )


async def test_streamlined_original_requester_can_make_real_new_decision(
    lifecycle, monkeypatch
):  # noqa: F811
    # This test uses the real streamlined issue/decision path, rather than editing
    # a strict approval's stored policy to manufacture self-approval.
    monkeypatch.setattr(settings, "superplane_require_distinct_approver", False)
    from app.services.operation_approvals import ApprovalService

    original_approve = lifecycle.approve

    async def requester_approve(review):
        from harness_jobs.identity import OperationRequest

        values = review["approval_request"]
        with lifecycle.actor(workspace_id=values["workspace_id"]):
            approvals = ApprovalService(lifecycle.sessions)
            ticket = await approvals.issue(
                workspace_id=values["workspace_id"],
                request=OperationRequest(
                    values["action"],
                    values["idempotency_key"],
                    values["parameters"],
                ),
            )
            await approvals.decide(ticket["approval_id"], "allowed-once")
            return uuid.UUID(ticket["approval_id"])

    lifecycle.approve = requester_approve
    try:
        _, _, workspace = await lifecycle.prepare()
    finally:
        lifecycle.approve = original_approve
    async with lifecycle.sessions.kw["bind"].begin() as db:
        await db.run_sync(OperationReauthorization.__table__.create)
    async with lifecycle.connections.connect() as db:
        await db.execute("UPDATE harness_operations SET state='unknown',version=2")
    service = DispatchRecoveryService(lifecycle.sessions, lifecycle.composition)
    with lifecycle.actor(workspace_id=workspace.id):
        ticket = await service.request(
            str(workspace.id), expected_version=2, idempotency_key=str(uuid.uuid4())
        )
        assert ticket["can_decide"] is True
        decided = await service.decide(
            str(workspace.id), ticket["reauthorization_id"], "allowed-once"
        )
    assert decided["decided_by"] == "requester"

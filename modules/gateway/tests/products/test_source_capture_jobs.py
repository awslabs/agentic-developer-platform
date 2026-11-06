"""Repository preparation survives lost HTTP responses and fences stale workers."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.models.base import Base as L5Base
from l5_services.models.preparation import AdmissionPreparation
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.models import ProductOperation, ProductSourceBinding
from src.products.source_capture_jobs import claim_capture, current_capture, prepare_capture, run_capture
from tests.products.test_l5_snapshots import snapshot_fixture


@pytest.fixture
async def preparation(test_client, db_session, test_engine, app, source_member, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    grant, body, _, _, _, _ = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    source = body.source_ref.model_copy(update={"record_type": "repository_commit", "record_revision": "a" * 40})
    body = body.model_copy(update={"source_ref": source, "max_bytes": 1024**3, "max_files": 100000})
    verified = {"scope": {"tenant_id": grant.tenant_id}, "trigger": {"source_refs": [source.model_dump()]}}
    record = AdmissionPreparation(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        service_id="security",
        actor_id=source_member.user_id,
        operation="submit",
        idempotency_key="assessment-one",
        request_hash="a" * 64,
        verified=verified,
        deadline_at=body.retain_until,
    )
    db_session.add(record)
    await db_session.commit()
    client = GatewayAdmissionSourceClient(
        db_session,
        source_member,
        grant.id,
        verified,
        "submit",
        "assessment-one",
        service_id="security",
        run_input={"capability": "code_review", "sources": [source.model_dump()]},
    )
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == grant.project_id))
    return SimpleNamespace(client=client, body=body, binding=binding, record=record, sessions=async_sessionmaker(test_engine, expire_on_commit=False))


async def enqueue(selected):
    with pytest.raises(HTTPException) as caught:
        await prepare_capture(selected.client, selected.body, selected.binding, selected.record, "snapshot-one")
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "source_capture_preparing"
    assert caught.value.headers["Retry-After"] == "5"
    return caught.value.detail["preparation_id"]


@pytest.mark.asyncio
async def test_request_replay_retains_one_preparation_without_capture(db_session, preparation):
    identifier = await enqueue(preparation)
    assert await enqueue(preparation) == identifier
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "captureL5Source")) == 1
    job = await db_session.get(ProductOperation, (preparation.client.tenant_id, identifier))
    assert job.request["source_request"] == preparation.body.model_dump(mode="json")
    assert job.request["subject"]["is_admin"] is False
    assert not any(name in job.request["subject"] for name in ("token", "expires_at", "attributed_user_id"))
    assert job.request["verified"] == preparation.client.verified
    assert job.expires_at.replace(tzinfo=UTC) == preparation.record.deadline_at


@pytest.mark.asyncio
async def test_conflicting_repeat_does_not_replace_original(preparation):
    await enqueue(preparation)
    preparation.client.run_input = {**preparation.client.run_input, "question": "changed"}
    with pytest.raises(HTTPException) as caught:
        await prepare_capture(preparation.client, preparation.body, preparation.binding, preparation.record, "snapshot-one")
    assert caught.value.detail["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_expired_worker_lease_recovered_without_deadline_extension(preparation):
    await enqueue(preparation)
    first = await claim_capture(preparation.sessions)
    assert first is not None and await claim_capture(preparation.sessions) is None
    async with preparation.sessions() as db:
        job = await current_capture(db, first)
        original_deadline = job.expires_at
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    second = await claim_capture(preparation.sessions)
    assert second[:2] == first[:2] and second[2] != first[2]
    async with preparation.sessions() as db:
        with pytest.raises(HTTPException) as stale:
            await current_capture(db, first)
        assert stale.value.detail["code"] == "source_capture_lease_changed"
        current = await current_capture(db, second)
        assert current.expires_at == original_deadline and current.attempt_count == 2


@pytest.mark.asyncio
async def test_capture_finishes_original_submission_after_client_disappears(preparation, monkeypatch):
    identifier = await enqueue(preparation)
    identity = await claim_capture(preparation.sessions)

    async def pin(client, product, project, request, *, idempotency_key):
        await client.capture_fence(client.db)
        assert idempotency_key == "snapshot-one" and request == preparation.body.model_dump(mode="json")
        return {"snapshot_id": "immutable-snapshot"}

    monkeypatch.setattr(GatewayAdmissionSourceClient, "pin_source", pin)
    continuation = AsyncMock()
    await run_capture(preparation.sessions, identity, continue_submission=continuation)
    continuation.assert_awaited_once()
    continued = continuation.call_args.args[1]
    assert continued.request["admission_key"] == "assessment-one"
    assert continued.request["run_input"] == preparation.client.run_input
    async with preparation.sessions() as db:
        completed = await db.get(ProductOperation, (preparation.client.tenant_id, identifier))
        assert completed.state == "succeeded" and completed.lease_owner is None
    assert await claim_capture(preparation.sessions) is None


@pytest.mark.asyncio
async def test_authority_failure_is_terminal_and_never_continues(preparation, monkeypatch):
    identifier = await enqueue(preparation)
    identity = await claim_capture(preparation.sessions)
    monkeypatch.setattr(GatewayAdmissionSourceClient, "pin_source", AsyncMock(side_effect=HTTPException(404, detail={"code": "not_found"})))
    continuation = AsyncMock()
    await run_capture(preparation.sessions, identity, continue_submission=continuation)
    continuation.assert_not_awaited()
    async with preparation.sessions() as db:
        job = await db.get(ProductOperation, (preparation.client.tenant_id, identifier))
        assert job.state == "failed" and job.receipt["code"] == "not_found"
    assert await claim_capture(preparation.sessions) is None


@pytest.mark.asyncio
async def test_stale_capture_cannot_continue_or_commit(preparation, monkeypatch):
    identifier = await enqueue(preparation)
    identity = await claim_capture(preparation.sessions)

    async def pin(client, *_args, **_kwargs):
        await client.db.execute(update(ProductOperation).where(ProductOperation.id == identifier).values(lease_owner="new-owner"))
        await client.db.commit()
        return {"snapshot_id": "stale-snapshot"}

    monkeypatch.setattr(GatewayAdmissionSourceClient, "pin_source", pin)
    continuation = AsyncMock()
    await run_capture(preparation.sessions, identity, continue_submission=continuation)
    continuation.assert_not_awaited()
    async with preparation.sessions() as db:
        job = await db.get(ProductOperation, (preparation.client.tenant_id, identifier))
        assert job.state == "running" and job.lease_owner == "new-owner"


@pytest.mark.asyncio
async def test_expiration_and_retry_budget_are_terminal(preparation):
    identifier = await enqueue(preparation)
    async with preparation.sessions() as db:
        job = await db.get(ProductOperation, (preparation.client.tenant_id, identifier))
        job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await claim_capture(preparation.sessions) is None
    async with preparation.sessions() as db:
        job = await db.get(ProductOperation, (preparation.client.tenant_id, identifier))
        assert job.state == "failed" and job.attempt_count == 0

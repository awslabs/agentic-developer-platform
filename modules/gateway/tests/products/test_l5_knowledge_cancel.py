"""Never-started publication can be cancelled without provider/source authority."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from l5_services.devops.knowledge_work import current_context, resolve_admitted
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.workers.bindings import execute_setup_work
from sqlalchemy import func, select

from src.shared.models.audit import AuditLog
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_effects import knowledge_worker as knowledge_worker
from tests.products.test_l5_knowledge_resume import blocked
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["knowledge_fixture", "knowledge_worker", "claimed_knowledge", "consent_fixture", "l5_owner"]


async def cancel(client, row, *, key="knowledge_cancel_001", reason="Stop original publication", etag=None):
    return await client.post(
        row.fixture["path"] + "/binding-operations/" + row.intent.operation_id + "/cancel",
        json={"reason": reason},
        headers={"Idempotency-Key": key, "If-Match": etag or f'"{row.operation.revision}"'},
    )


async def test_cancel_expired_consent_preserves_original_work_and_exact_replay(test_client, db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    await blocked(db_session, row)
    row.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    row.grant.revoked_at = datetime.now(UTC)
    row.fixture["source_binding"].status = "revoked"
    await db_session.commit()
    original = row.intent.intent_hash, row.intent.authorization, row.work.work_id, row.work.generation, row.work.fence
    etag = f'"{row.operation.revision}"'
    cancelled = await cancel(test_client, row, etag=etag)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled" and "No native cleanup" in cancelled.json()["next_action"]
    assert cancelled.json()["readback_ref"]["knowledge_cancellation"]["native_write_started"] is False
    assert cancelled.json()["readback_ref"]["knowledge_cancellation"]["native_cleanup_performed"] is False
    assert (await cancel(test_client, row, etag=etag)).json() == cancelled.json()
    assert (await cancel(test_client, row, etag=etag, reason="Changed reason")).status_code == 409
    assert (await cancel(test_client, row, etag=cancelled.headers["etag"])).status_code == 409
    await db_session.refresh(row.work)
    await db_session.refresh(row.intent)
    assert (row.intent.intent_hash, row.intent.authorization, row.work.work_id, row.work.generation, row.work.fence) == original
    assert row.work.completed_at is not None and not native.calls
    assert row.fixture["capture"].await_count == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event_type == "l5_knowledge_cancelled")) == 1
    callback = AsyncMock()
    assert not await execute_setup_work(
        worker.sessions,
        {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation},
        callback,
        knowledge_execute=callback,
    )
    callback.assert_not_awaited()


@pytest.mark.parametrize("state", ["active_lease", "started", "unknown", "ready", "foreign_owner", "stale_etag"])
async def test_cancel_cannot_hide_existing_effects_or_bypass_owner_and_revision(test_client, db_session, knowledge_worker, state):
    row, worker, native = knowledge_worker
    if state in {"started", "unknown", "ready"}:
        if state == "ready":
            await worker.execute(row.intent, row.claim)
        elif state == "unknown":
            native.lose_ack = True
            with pytest.raises(TimeoutError):
                await worker.execute(row.intent, row.claim)
        else:
            await planned(worker, row)
            source = row.intent.authorization["knowledge_admission"]["publications"][0]["source_ref"]
            await worker.effects.start(row.intent, row.claim, source)
    if state != "active_lease":
        await blocked(db_session, row)
    if state == "foreign_owner":
        row.fixture["configurations"]["devops"][0].owner_id = "other-owner"
        await db_session.commit()
    calls = list(native.calls)
    await db_session.refresh(row.operation)
    denied = await cancel(test_client, row, etag='"999"' if state == "stale_etag" else None)
    assert denied.status_code == (404 if state == "foreign_owner" else 412 if state == "stale_etag" else 409), denied.text
    await db_session.refresh(row.operation)
    assert row.operation.state != "cancelled" and native.calls == calls


async def planned(worker, row):
    async with worker.sessions() as session:
        context = await current_context(session, row.intent, row.claim)
    asset = (await resolve_admitted(context, row.claim, worker.sources, AsyncMock()))[0]
    plan = worker.driver.prepare("native-space", asset, row.intent.operation_id, None)
    await worker.effects.plan(row.intent, row.claim, asset.source_ref, plan)


async def test_cancel_plan_retains_ledger_and_releases_only_unstarted_reservation(test_client, db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    await planned(worker, row)
    event = await db_session.scalar(select(KnowledgeEffect))
    original_hash = event.event_hash
    await blocked(db_session, row)
    response = await cancel(test_client, row)
    assert response.status_code == 200, response.text
    await db_session.refresh(event)
    ownership = await db_session.scalar(select(KnowledgeOwnership))
    assert event.phase == "planned" and event.event_hash == original_hash
    assert ownership.state == "cancelled" and ownership.operation_id == row.intent.operation_id and ownership.receipt is None
    assert "create" not in native.calls and "update" not in native.calls

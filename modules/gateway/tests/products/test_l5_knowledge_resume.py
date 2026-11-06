"""Explicit native consent resumes the original blocked work without repinning."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from l5_services.models.knowledge_effect import KnowledgeEffect
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.workers.bindings import execute_setup_work
from sqlalchemy import func, select

from src.shared.models.audit import AuditLog
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_effects import knowledge_worker as knowledge_worker
from tests.products.test_l5_knowledge_native_consent import native_consent
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["knowledge_fixture", "claimed_knowledge", "consent_fixture", "l5_owner", "knowledge_worker"]


async def blocked(db, row):
    row.work.lease_owner, row.work.lease_until = None, None
    row.work.blocked_at = datetime.now(UTC)
    row.operation.state = "blocked"
    row.operation.gaps = ["knowledge_worker_unqualified"]
    await db.commit()


async def reconcile(client, row, *, reason="Resume original explicit publication", key="knowledge_resume_001"):
    return await client.post(
        row.fixture["path"] + "/binding-operations/" + row.intent.operation_id + "/reconcile",
        json={"reason": reason},
        headers={"Idempotency-Key": key},
    )


async def test_consent_and_resume_execute_original_work_and_preserve_admission(test_client, db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    await blocked(db_session, row)
    original = row.intent.intent_hash, row.work.work_id, row.work.generation, row.work.fence
    no_consent = await reconcile(test_client, row)
    assert no_consent.status_code == 403 and no_consent.json()["detail"]["code"] == "knowledge_native_consent_required"
    await native_consent(test_client, row)
    resumed = await reconcile(test_client, row)
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["operation_id"] == row.intent.operation_id and resumed.json()["state"] == "accepted"
    again = await reconcile(test_client, row)
    assert again.status_code == 202 and again.json() == resumed.json()
    changed = await reconcile(test_client, row, reason="Changed reason with reused key")
    assert changed.status_code == 409
    await db_session.refresh(row.work)
    assert (row.intent.intent_hash, row.work.work_id, row.work.generation, row.work.fence) == original
    assert row.fixture["capture"].await_count == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event_type == "l5_knowledge_resumed")) == 1

    async def execute(intent, claim):
        assert claim.work_id == original[1] and claim.generation == original[2] and claim.fence == original[3] + 1
        return await worker.execute(intent, claim)

    generic = AsyncMock(side_effect=AssertionError("generic setup credentials must not run"))
    assert await execute_setup_work(
        worker.sessions, {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation}, generic, knowledge_execute=execute
    )
    generic.assert_not_awaited()
    assert native.calls.count("create") == 1
    await db_session.refresh(row.operation)
    assert row.operation.state == "waiting" and row.operation.gaps == ["knowledge_native_use_unqualified"]


@pytest.mark.parametrize("change", ["lease", "source", "binding", "expired_receipt"])
async def test_resume_rejects_current_lease_or_changed_original_selection(test_client, db_session, knowledge_worker, change):
    from src.products.models import ProductSourceSnapshot

    row, _, native = knowledge_worker
    await blocked(db_session, row)
    await native_consent(test_client, row)
    if change == "lease":
        row.work.lease_owner = "already-running"
        row.work.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    elif change == "source":
        row.fixture["source_binding"].status = "revoked"
    elif change == "binding":
        row.fixture["binding"].generation += 1
    else:
        snapshot = await db_session.scalar(select(ProductSourceSnapshot))
        snapshot.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    denied = await reconcile(test_client, row)
    assert denied.status_code in {403, 409}, denied.text
    await db_session.refresh(row.work)
    assert row.work.blocked_at is not None
    assert not native.calls and row.fixture["capture"].await_count == 1


async def test_failed_dispatch_retains_uncertainty_and_explicit_resume_only_reads_back(test_client, db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    await blocked(db_session, row)
    await native_consent(test_client, row)
    assert (await reconcile(test_client, row)).status_code == 202
    envelope = {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation}
    generic = AsyncMock(side_effect=AssertionError("generic setup credentials must not run"))
    native.lose_ack = True
    assert not await execute_setup_work(worker.sessions, envelope, generic, knowledge_execute=worker.execute)
    await db_session.refresh(row.operation)
    await db_session.refresh(row.work)
    assert row.operation.state == "blocked" and row.operation.gaps == ["knowledge_native_outcome_unknown"]
    assert row.work.blocked_at is not None and row.work.lease_owner is None and row.work.lease_until is None
    first = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    hashes = [event.event_hash for event in first]
    assert [event.phase for event in first] == ["planned", "started", "outcome_unknown"]
    assert not await execute_setup_work(worker.sessions, envelope, generic, knowledge_execute=worker.execute)
    native.lose_ack = False
    assert (await reconcile(test_client, row, key="knowledge_resume_002")).status_code == 202
    assert await execute_setup_work(worker.sessions, envelope, generic, knowledge_execute=worker.execute)
    assert native.calls.count("create") == 1 and "update" not in native.calls
    current = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    assert [event.event_hash for event in current[:3]] == hashes and current[-1].phase == "ready"
    assert current[-1].fence == first[0].fence + 1 and current[-1].lease_owner != first[0].lease_owner
    generic.assert_not_awaited()


@pytest.mark.parametrize("code", [None, ["unsafe"], "native secret must not escape"])
async def test_dispatch_sanitizes_unstructured_errors(test_client, db_session, knowledge_worker, code):
    from fastapi import HTTPException

    row, worker, _ = knowledge_worker
    await blocked(db_session, row)
    await native_consent(test_client, row)
    assert (await reconcile(test_client, row)).status_code == 202
    callback = AsyncMock(side_effect=HTTPException(403, detail={"code": code}))
    assert not await execute_setup_work(
        worker.sessions,
        {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation},
        AsyncMock(),
        knowledge_execute=callback,
    )
    await db_session.refresh(row.operation)
    assert row.operation.state == "blocked" and row.operation.gaps == ["knowledge_publication_failed"]


@pytest.mark.parametrize("repair", [False, True])
async def test_explicit_resume_supplements_exhausted_dispatch_budget_without_reset(test_client, db_session, knowledge_worker, repair):
    from l5_services.dispatcher import MAX_DISPATCH_ATTEMPTS, dispatch_work, repair_owed_work

    row, worker, _ = knowledge_worker
    await blocked(db_session, row)
    row.work.attempts = MAX_DISPATCH_ATTEMPTS
    row.work.last_error = "dispatch_attempt_limit"
    row.work.last_enqueued_at = datetime.now(UTC)
    row.work.redeliver_after = datetime.now(UTC) + timedelta(minutes=15)
    await db_session.commit()
    original = row.work.work_id, row.work.generation, row.work.fence
    await native_consent(test_client, row)
    response = await reconcile(test_client, row)
    assert response.status_code == 202, response.text
    assert (await reconcile(test_client, row)).status_code == 202
    await db_session.refresh(row.work)
    assert row.work.attempts == MAX_DISPATCH_ATTEMPTS and row.work.dispatch_attempt_limit == 2 * MAX_DISPATCH_ATTEMPTS
    assert (row.work.work_id, row.work.generation, row.work.fence) == original
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.event_type == "l5_knowledge_resumed"))
    assert audit.details["dispatch_supplement"] == MAX_DISPATCH_ATTEMPTS
    assert audit.details["lifetime_dispatch_attempts"] == MAX_DISPATCH_ATTEMPTS
    publish = AsyncMock()
    if repair:
        assert await repair_owed_work(worker.sessions, publish, work_model=SetupWork, service_id="devops") == 1
    else:
        assert await dispatch_work(worker.sessions, row.intent.tenant_id, row.work.work_id, publish, work_model=SetupWork, service_id="devops")
    publish.assert_awaited_once()
    await db_session.refresh(row.work)
    assert row.work.attempts == MAX_DISPATCH_ATTEMPTS + 1 and row.work.blocked_at is None
    row.work.attempts = row.work.dispatch_attempt_limit
    row.work.redeliver_after = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await repair_owed_work(worker.sessions, publish, work_model=SetupWork, service_id="devops") == 0
    await db_session.refresh(row.work)
    assert row.work.blocked_at is not None and row.work.attempts == row.work.dispatch_attempt_limit == 2 * MAX_DISPATCH_ATTEMPTS
    assert publish.await_count == 1


@pytest.mark.parametrize("change", ["service", "action"])
async def test_dispatch_supplement_cannot_expand_other_service_or_action(test_client, db_session, knowledge_worker, change):
    from l5_services.dispatcher import MAX_DISPATCH_ATTEMPTS, dispatch_work

    row, worker, _ = knowledge_worker
    await blocked(db_session, row)
    row.work.blocked_at = None
    row.work.attempts, row.work.dispatch_attempt_limit = MAX_DISPATCH_ATTEMPTS, 2 * MAX_DISPATCH_ATTEMPTS
    if change == "service":
        row.intent.service_id = "security"
    else:
        row.intent.request = {**row.intent.request, "action": "check"}
    await db_session.commit()
    publish = AsyncMock()
    assert not await dispatch_work(worker.sessions, row.intent.tenant_id, row.work.work_id, publish, work_model=SetupWork)
    publish.assert_not_awaited()

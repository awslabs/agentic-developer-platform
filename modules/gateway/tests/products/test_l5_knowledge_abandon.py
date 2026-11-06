"""Abandonment closes local work without erasing or cleaning native effects."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.workers.bindings import execute_setup_work
from l5_services.workers.execution import AuthorityRevoked
from sqlalchemy import func, select

from src.products.knowledge_evidence_model import KnowledgeUseEvidence
from src.products.models import ProductSourceSnapshot
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_cancel import planned
from tests.products.test_l5_knowledge_effects import knowledge_worker as knowledge_worker
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["knowledge_fixture", "knowledge_worker", "claimed_knowledge", "consent_fixture", "l5_owner"]
AUTH = {"Authorization": "Bearer separate-human-session"}


@pytest.fixture(autouse=True)
def logout_boundary(monkeypatch):
    # Shared session, membership and interactive-credential checks remain real;
    # only the external logout/session storage boundary is replaced.
    monkeypatch.setattr("src.orchestration.l5_observation_routes.require_native_launch_session", AsyncMock())


async def abandon(client, row, *, key="knowledge_abandon_01", etag=None, **body):
    return await client.post(
        row.fixture["path"] + "/binding-operations/" + row.intent.operation_id + "/abandon",
        json={"reason": "Owner stops uncertain publication", "native_effects_may_remain": True, **body},
        headers={**AUTH, "Idempotency-Key": key, "If-Match": etag or f'"{row.operation.revision}"'},
    )


async def effect(db, knowledge_worker, phase):
    row, worker, native = knowledge_worker
    if phase == "ready":
        await worker.execute(row.intent, row.claim)
    elif phase == "unknown":
        native.lose_ack = True
        with pytest.raises(TimeoutError):
            await worker.execute(row.intent, row.claim)
    else:
        await planned(worker, row)
        if phase == "started":
            await worker.effects.start(row.intent, row.claim, row.fixture["source_ref"])
    await db.refresh(row.operation)
    await db.refresh(row.work)
    row.work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    return row, worker, native


@pytest.mark.parametrize("phase", ["started", "unknown", "ready"])
async def test_expired_authority_can_abandon_with_original_history_and_reservations(test_client, db_session, knowledge_worker, monkeypatch, phase):
    row, worker, native = await effect(db_session, knowledge_worker, phase)
    row.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    row.grant.revoked_at = datetime.now(UTC)
    row.fixture["source_binding"].status = "revoked"
    snapshot = await db_session.scalar(select(ProductSourceSnapshot))
    snapshot.expires_at = snapshot.retain_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    original = deepcopy((row.intent.intent_hash, row.intent.authorization, row.work.work_id, row.work.generation, row.work.fence, row.work.attempts))
    previous_completed = row.work.completed_at
    original_effects = list(await db_session.scalars(select(KnowledgeEffect.event_hash).order_by(KnowledgeEffect.sequence)))
    owned = await db_session.scalar(select(KnowledgeOwnership))
    original_receipt = deepcopy(owned.receipt)
    calls = list(native.calls)
    private_bytes = AsyncMock(side_effect=AssertionError("abandonment must not read private bytes"))
    monkeypatch.setattr("src.knowledge_evidence_store.artifact", private_bytes)
    etag = f'"{row.operation.revision}"'
    result = await abandon(test_client, row, etag=etag)
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "abandoned" and "No native cleanup" in result.json()["next_action"]
    acknowledgement = result.json()["readback_ref"]["knowledge_abandonment"]
    assert acknowledgement["native_effects_may_remain"] is True
    assert all(
        acknowledgement[field] is False
        for field in (
            "native_cleanup_performed",
            "native_authority_granted",
            "native_call_performed",
            "native_ownership_released",
            "release_check_completed",
        )
    )
    assert (await abandon(test_client, row, etag=etag)).json() == result.json()
    assert (await abandon(test_client, row, etag=etag, reason="Changed reason")).status_code == 409
    assert (await abandon(test_client, row, key="different_abandon_01", etag=result.headers["etag"])).status_code == 409
    await db_session.refresh(row.work)
    await db_session.refresh(row.intent)
    await db_session.refresh(owned)
    assert (row.intent.intent_hash, row.intent.authorization, row.work.work_id, row.work.generation, row.work.fence, row.work.attempts) == original
    assert row.work.completed_at is not None
    if previous_completed:
        assert row.work.completed_at == previous_completed
    assert owned.state == "abandoned" and owned.receipt == original_receipt
    assert list(await db_session.scalars(select(KnowledgeEffect.event_hash).order_by(KnowledgeEffect.sequence))) == original_effects
    assert native.calls == calls and row.fixture["capture"].await_count == 1
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence).where(KnowledgeUseEvidence.kind == "abandoned")) == 1
    history = await test_client.get(row.fixture["path"] + "/binding-operations/" + row.intent.operation_id + "/knowledge-evidence", headers=AUTH)
    assert history.status_code == 200 and history.json()["items"][0]["kind"] == "abandoned"
    private_bytes.assert_not_awaited()
    callback = AsyncMock()
    assert not await execute_setup_work(
        worker.sessions,
        {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation},
        callback,
        knowledge_execute=callback,
    )
    callback.assert_not_awaited()
    with pytest.raises(AuthorityRevoked):
        await worker.effects.unknown(row.intent, row.claim, row.fixture["source_ref"])
    assert native.calls == calls


@pytest.mark.parametrize(
    "fault", ["lease", "planned", "foreign_owner", "ownership", "stale_etag", "succeeded", "cancelled", "invalid_ack", "blank_reason"]
)
async def test_abandonment_refuses_unsafe_or_inconsistent_requests(test_client, db_session, knowledge_worker, fault):
    row, worker, native = await effect(db_session, knowledge_worker, "planned" if fault == "planned" else "started")
    if fault == "lease":
        row.work.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    elif fault == "foreign_owner":
        row.fixture["configurations"]["devops"][0].owner_id = "foreign-owner"
    elif fault == "ownership":
        owned = await db_session.scalar(select(KnowledgeOwnership))
        owned.space_id = "another-space"
    elif fault in {"succeeded", "cancelled"}:
        row.operation.state = fault
    await db_session.commit()
    calls = list(native.calls)
    changes = {"native_effects_may_remain": False} if fault == "invalid_ack" else {"reason": " "} if fault == "blank_reason" else {}
    response = await abandon(test_client, row, etag='"999"' if fault == "stale_etag" else None, **changes)
    expected = 404 if fault == "foreign_owner" else 412 if fault == "stale_etag" else 422 if changes else 409
    assert response.status_code == expected, response.text
    await db_session.refresh(row.operation)
    assert row.operation.state != "abandoned"
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence)) == 0
    assert native.calls == calls


@pytest.mark.parametrize("fault", ["service", "refresh", "expired", "fixture", "logout"])
async def test_abandonment_requires_real_current_interactive_human(test_client, app, db_session, knowledge_worker, l5_owner, monkeypatch, fault):
    from src.auth.dependencies import get_current_user

    row, _, _ = await effect(db_session, knowledge_worker, "started")
    if fault in {"fixture", "logout"}:
        path = (
            "src.auth.security_fixture_credential.require_interactive_credential"
            if fault == "fixture"
            else "src.orchestration.l5_observation_routes.require_native_launch_session"
        )
        monkeypatch.setattr(path, AsyncMock(side_effect=HTTPException(403, detail={"code": "session_refused"})))
    else:
        fields = {
            "service": {"account_type": "service", "auth_source": "iam"},
            "refresh": {"token_use": "refresh"},
            "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        }[fault]
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update=fields)
    response = await abandon(test_client, row)
    assert response.status_code in {401, 403, 404}, response.text
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence)) == 0


async def test_corrupt_effect_history_cannot_be_closed(test_client, db_session, knowledge_worker, monkeypatch):
    row, _, _ = await effect(db_session, knowledge_worker, "started")
    # Simulate a bad retained digest at validation, without disabling append-only
    # database protection merely to construct corruption.
    monkeypatch.setattr("src.l5_knowledge_abandon.KnowledgeEffects._body", lambda event: {"changed": True})
    result = await abandon(test_client, row)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "knowledge_abandon_effect_changed"
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence)) == 0


@pytest.mark.parametrize("revocation", ["logout", "automation_designation"])
async def test_human_revocation_while_waiting_cannot_commit_abandonment(test_client, app, db_session, knowledge_worker, monkeypatch, revocation):
    from types import SimpleNamespace

    import jwt

    from src import l5_knowledge_abandon
    from src.auth import native_launch_revocation

    class Redis:
        def __init__(self):
            self.values = {}

        async def get(self, key):
            return self.values.get(key)

        async def set(self, key, value, **kwargs):
            self.values[key] = value
            return True

    row, _, _ = await effect(db_session, knowledge_worker, "started")
    redis = Redis()
    monkeypatch.setattr(native_launch_revocation, "_client", lambda: redis)
    monkeypatch.setattr(
        "src.orchestration.l5_observation_routes.require_native_launch_session", native_launch_revocation.require_native_launch_session
    )
    token = jwt.encode({"exp": (datetime.now(UTC) + timedelta(hours=2)).timestamp()}, "independent-local-test-secret")
    monkeypatch.setitem(AUTH, "Authorization", "Bearer " + token)
    state = SimpleNamespace(designated=False)
    runtime = SimpleNamespace(configured=True, store=SimpleNamespace(designation=lambda subject: {} if state.designated else None))
    monkeypatch.setattr(app.state, "native_test_runtime", runtime, raising=False)
    original = l5_knowledge_abandon.retained_effects

    async def revoke_after_wait(*args):
        result = await original(*args)
        if revocation == "logout":
            await native_launch_revocation.revoke_native_launch(AUTH["Authorization"], datetime.now(UTC) + timedelta(minutes=30))
        else:
            state.designated = True
        return result

    monkeypatch.setattr(l5_knowledge_abandon, "retained_effects", revoke_after_wait)
    result = await abandon(test_client, row)
    assert result.status_code in {401, 403}, result.text
    await db_session.refresh(row.operation)
    assert row.operation.state != "abandoned"
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence)) == 0


async def test_fresh_admission_cannot_take_over_abandoned_native_reservation(test_client, db_session, knowledge_worker):
    from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork

    from tests.products.test_l5_devops_knowledge_admission import submit

    row, worker, native = await effect(db_session, knowledge_worker, "unknown")
    closed = await abandon(test_client, row)
    assert closed.status_code == 200, closed.text
    fixture = row.fixture
    await db_session.refresh(fixture["binding"])
    binding = fixture["binding"]
    params = {"binding_id": binding.binding_id, "for_control": "true", "control_action": "devops_knowledge_publication"}
    preview = await test_client.get(fixture["path"] + "/delegation-scope", params=params)
    assert preview.status_code == 200, preview.text
    revoked = await test_client.delete(
        fixture["path"] + "/delegations/" + row.grant.id,
        headers={"If-Match": f'"{row.grant.revision}"'},
    )
    assert revoked.status_code == 200, revoked.text
    renewed = await test_client.post(
        fixture["path"] + "/delegations",
        json={
            **params,
            "for_control": True,
            "scope_digest": preview.json()["scope_digest"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "new_explicit_admission_consent"},
    )
    assert renewed.status_code == 200, renewed.text
    fresh = await submit(
        test_client,
        fixture,
        body={
            "action": "sync_knowledge",
            "configuration_revision": 1,
            "binding_id": binding.binding_id,
            "binding_revision": binding.revision,
            "binding_generation": binding.generation,
        },
        key="explicit_new_publication",
        etag=f'"{binding.revision}"',
    )
    assert fresh.status_code == 202, fresh.text
    new_intent = await db_session.get(SetupIntent, (row.intent.tenant_id, fresh.json()["operation_id"]))
    work = await db_session.scalar(select(SetupWork).where(SetupWork.operation_id == new_intent.operation_id))
    work.blocked_at = None
    operation = await db_session.get(BindingOperation, (row.intent.tenant_id, new_intent.operation_id))
    operation.state = "accepted"
    await db_session.commit()
    worker.sources.read_source = AsyncMock(return_value=new_intent.authorization["knowledge_admission"]["source_snapshots"][0])
    native.lose_ack = False
    calls = list(native.calls)
    assert not await execute_setup_work(
        worker.sessions,
        {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation},
        AsyncMock(),
        knowledge_execute=worker.execute,
    )
    assert native.calls == calls
    operation = await db_session.get(BindingOperation, (row.intent.tenant_id, new_intent.operation_id), populate_existing=True)
    assert operation.gaps == ["knowledge_owned_asset_busy"]
    owned = await db_session.scalar(select(KnowledgeOwnership))
    assert owned.operation_id == row.intent.operation_id and owned.state == "abandoned"

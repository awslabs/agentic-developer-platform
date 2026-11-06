"""Retained knowledge snapshots require the exact live shared-Binding claim."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.devops.knowledge_work import current_context, resolve_admitted
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from l5_services.workers.bindings import SetupClaim
from l5_services.workers.execution import AuthorityRevoked
from sqlalchemy import select

from src.internal.l5_credential_routes import ConnectionRef, CredentialSessionRequest, get_l5_work_authorizer
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_devops_knowledge_admission import consent, submit
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["consent_fixture", "knowledge_fixture", "l5_owner"]


@pytest.fixture
async def claimed_knowledge(test_client, db_session, knowledge_fixture):
    fixture = knowledge_fixture
    consent_view = await consent(test_client, fixture)
    response = await submit(test_client, fixture)
    assert response.status_code == 202, response.text
    await db_session.refresh(fixture["binding"])
    grant = await db_session.get(CredentialServiceDelegation, consent_view["delegation_id"])
    intent = await db_session.get(SetupIntent, (grant.tenant_id, response.json()["operation_id"]))
    operation = await db_session.get(BindingOperation, (grant.tenant_id, intent.operation_id))
    work = await db_session.scalar(select(SetupWork).where(SetupWork.operation_id == intent.operation_id))
    work.blocked_at = None
    work.lease_owner = "current-worker"
    work.fence = 1
    work.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    operation.state = "running"
    await db_session.commit()
    claim = SetupClaim(grant.tenant_id, intent.operation_id, work.work_id, work.generation, work.lease_owner, work.fence, work.lease_until)
    body = CredentialSessionRequest(
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        operation_id=intent.operation_id,
        attempt_id=work.work_id,
        lease_owner=claim.owner,
        lease_fence=claim.fence,
        connection_ref=ConnectionRef.model_validate(fixture["source_ref"]),
    )
    return SimpleNamespace(fixture=fixture, grant=grant, intent=intent, operation=operation, work=work, claim=claim, body=body)


async def test_exact_setup_source_permit_retains_original_snapshot_and_no_native_policy(db_session, claimed_knowledge):
    row = claimed_knowledge
    permit = await get_l5_work_authorizer(db_session)(row.grant, row.body)
    assert permit.admitted_source_snapshot_ids == frozenset(
        item["snapshot_id"] for item in row.intent.authorization["knowledge_admission"]["source_snapshots"]
    )
    assert permit.lease_owner == row.claim.owner and permit.lease_fence == row.claim.fence
    assert permit.session_policy is None and permit.setup_verified is None
    context = await current_context(db_session, row.intent, row.claim)
    snapshot = context.admission["source_snapshots"][0]

    async def stream(*args):
        yield b"original"

    sources = SimpleNamespace(read_source=AsyncMock(return_value=snapshot), stream_source=stream)
    checked = AsyncMock()
    assets = await resolve_admitted(context, row.claim, sources, checked)
    assert len(assets) == 1 and assets[0].content == "original"
    assert sources.read_source.await_count == 2 and checked.await_count == 2
    for call in sources.read_source.await_args_list:
        assert call.args[-1]["lease_owner"] == row.claim.owner
        assert call.args[-1]["operation_id"] == row.intent.operation_id
        assert call.args[2] == snapshot["snapshot_id"]


@pytest.mark.parametrize(
    "change",
    ["owner", "fence", "expired", "blocked", "done", "generation", "config", "space", "source", "grant", "credential", "snapshot_source", "intent"],
)
async def test_stale_or_foreign_knowledge_claim_cannot_read_sources(db_session, claimed_knowledge, change):
    row = claimed_knowledge
    if change == "owner":
        row.work.lease_owner = "replacement"
    elif change == "fence":
        row.work.fence += 1
    elif change == "expired":
        row.work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "blocked":
        row.work.blocked_at = datetime.now(UTC)
    elif change == "done":
        row.work.completed_at = datetime.now(UTC)
    elif change == "generation":
        row.fixture["binding"].generation += 1
    elif change == "config":
        row.fixture["configurations"]["devops"][0].current_revision += 1
    elif change == "space":
        binding = row.fixture["binding"]
        binding.native_resource_ref = {**binding.native_resource_ref, "record_id": "other-space"}
    elif change == "source":
        row.fixture["source_binding"].status = "revoked"
    elif change == "grant":
        row.grant.revoked_at = datetime.now(UTC)
    elif change == "credential":
        row.body.connection_ref = ConnectionRef.model_validate(row.grant.sources[0])
    elif change == "snapshot_source":
        row.body.connection_ref = row.body.connection_ref.model_copy(update={"record_revision": "r2"})
    else:
        row.intent.intent_hash = "a" * 64
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await get_l5_work_authorizer(db_session)(row.grant, row.body)
    assert error.value.status_code in {403, 409}


async def test_source_resolution_stops_if_hash_or_receipt_changes(db_session, claimed_knowledge):
    row = claimed_knowledge
    context = await current_context(db_session, row.intent, row.claim)
    snapshot = context.admission["source_snapshots"][0]

    async def wrong_stream(*args):
        yield b"changed!"

    sources = SimpleNamespace(read_source=AsyncMock(return_value=snapshot), stream_source=wrong_stream)
    with pytest.raises(AuthorityRevoked, match="knowledge_source_content_changed"):
        await resolve_admitted(context, row.claim, sources, AsyncMock())
    sources.read_source = AsyncMock(return_value={**snapshot, "snapshot_id": "other"})
    with pytest.raises(AuthorityRevoked, match="knowledge_source_receipt_changed"):
        await resolve_admitted(context, row.claim, sources, AsyncMock())


@pytest.mark.parametrize(
    "visibility", ["owner", "team", "organization", "other_owner", "other_tenant", "expired", "revoked_binding", "unlinked_binding"]
)
async def test_document_credential_visibility_requires_current_selected_source(db_session, claimed_knowledge, l5_owner, visibility):
    from src.shared.models.vault import UserCredential

    row = claimed_knowledge
    credential = await db_session.get(UserCredential, row.fixture["source_ref"]["source_connection_id"])
    if visibility == "team":
        credential.user_id, credential.team_id = None, l5_owner.team_id
    elif visibility == "organization":
        credential.user_id, credential.team_id = None, None
    elif visibility == "other_owner":
        credential.user_id = "other-owner"
    elif visibility == "other_tenant":
        credential.org_id = "other-tenant"
    elif visibility == "expired":
        credential.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif visibility == "revoked_binding":
        row.fixture["source_binding"].status = "revoked"
    elif visibility == "unlinked_binding":
        row.fixture["source_binding"].status = "unlinked"
        row.fixture["source_binding"].unlinked_at = datetime.now(UTC)
    await db_session.commit()
    if visibility in {"owner", "team", "organization"}:
        assert await get_l5_work_authorizer(db_session)(row.grant, row.body)
    else:
        with pytest.raises(HTTPException) as error:
            await get_l5_work_authorizer(db_session)(row.grant, row.body)
        assert error.value.status_code == 403


async def test_snapshot_endpoint_uses_knowledge_claim_and_exact_admitted_receipt(db_session, claimed_knowledge, l5_owner, monkeypatch):
    from starlette.requests import Request

    from src.internal.l5_snapshot_routes import authorized_snapshot
    from src.products.source_snapshots import SourceReadRequest

    row = claimed_knowledge
    actor = l5_owner.model_copy(update={"account_type": "service", "auth_source": "iam", "agent_registry_id": row.grant.service_id})
    monkeypatch.setattr("src.internal.l5_snapshot_routes.service_actor", lambda _: actor)
    snapshot = row.intent.authorization["knowledge_admission"]["source_snapshots"][0]
    body = SourceReadRequest.model_validate(row.body.model_dump(exclude={"connection_ref"}))
    response = await authorized_snapshot(
        row.grant.product_id,
        row.grant.project_id,
        snapshot["snapshot_id"],
        body,
        Request({"type": "http"}),
        db_session,
        get_l5_work_authorizer(db_session),
    )
    assert response.model_dump(mode="json") == snapshot
    row.work.fence += 1
    await db_session.commit()
    with pytest.raises(HTTPException):
        await authorized_snapshot(
            row.grant.product_id,
            row.grant.project_id,
            snapshot["snapshot_id"],
            body,
            Request({"type": "http"}),
            db_session,
            get_l5_work_authorizer(db_session),
        )

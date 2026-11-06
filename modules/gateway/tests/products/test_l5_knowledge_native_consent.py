"""Source admission never substitutes for explicit native publication consent."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.auth.l5_knowledge_native_consent import NATIVE_ACTIONS
from src.internal.l5_credential_routes import ConnectionRef, get_l5_work_authorizer
from src.internal.l5_knowledge_work import native_active_grant
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["knowledge_fixture", "claimed_knowledge", "consent_fixture", "l5_owner"]


async def native_consent(client, row):
    path = row.fixture["path"] + "/binding-operations/" + row.intent.operation_id + "/knowledge-native-consent"
    preview = await client.get(path)
    assert preview.status_code == 200, preview.text
    body = {"scope_digest": preview.json()["scope_digest"], "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat()}
    headers = {"If-Match": preview.headers["etag"], "Idempotency-Key": "knowledge_native_001"}
    response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return path, body, headers, response.json()


async def test_explicit_native_consent_pins_original_operation_without_unblocking(test_client, db_session, claimed_knowledge):
    row = claimed_knowledge
    before = (row.work.fence, row.work.lease_owner, row.operation.state)
    path, body, headers, result = await native_consent(test_client, row)
    grant = await db_session.get(CredentialServiceDelegation, result["delegation_id"])
    assert grant.parent_delegation_id == row.grant.id
    assert grant.operations == ["knowledge:asset-write"] and "credential:session" not in grant.operations
    assert result["work_dispatched"] is False
    assert result["scope"]["native_actions"] == NATIVE_ACTIONS
    assert "DeleteAsset" not in NATIVE_ACTIONS and "CreateBacklogTask" not in NATIVE_ACTIONS
    replay = await test_client.post(path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == result
    conflict = await test_client.post(path, json={**body, "expires_at": (datetime.now(UTC) + timedelta(minutes=25)).isoformat()}, headers=headers)
    assert conflict.status_code == 409
    await db_session.refresh(row.work)
    await db_session.refresh(row.operation)
    assert (row.work.fence, row.work.lease_owner, row.operation.state) == before


async def test_native_permit_is_one_space_five_asset_actions_and_current_claim(test_client, db_session, claimed_knowledge):
    row = claimed_knowledge
    _, _, _, result = await native_consent(test_client, row)
    grant = await db_session.get(CredentialServiceDelegation, result["delegation_id"])
    request = row.body.model_copy(
        update={"delegation_id": grant.id, "delegation_revision": grant.revision, "connection_ref": ConnectionRef.model_validate(grant.sources[0])}
    )
    permit = await get_l5_work_authorizer(db_session)(grant, request)
    policy = json.loads(permit.session_policy)
    assert len(policy["Statement"]) == 1
    statement = policy["Statement"][0]
    assert statement["Action"] == ["aidevops:" + name for name in NATIVE_ACTIONS]
    assert statement["Resource"].endswith("/native-space") and "*" not in statement["Resource"]
    assert permit.admitted_source_snapshot_ids == frozenset()
    assert native_active_grant(grant, request, permit) == permit.expires_at
    row.work.fence += 1
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(grant, request)


@pytest.mark.parametrize("change", ["source_revoked", "source_grant_revoked", "native_revoked", "other_operation", "other_space"])
async def test_native_consent_cannot_survive_changed_source_or_operation(test_client, db_session, claimed_knowledge, change):
    row = claimed_knowledge
    path, _, _, result = await native_consent(test_client, row)
    grant = await db_session.get(CredentialServiceDelegation, result["delegation_id"])
    request = row.body.model_copy(
        update={"delegation_id": grant.id, "delegation_revision": grant.revision, "connection_ref": ConnectionRef.model_validate(grant.sources[0])}
    )
    if change == "source_revoked":
        row.fixture["source_binding"].status = "revoked"
    elif change == "source_grant_revoked":
        row.grant.revoked_at = datetime.now(UTC)
    elif change == "native_revoked":
        response = await test_client.delete(path + "/" + grant.id, headers={"If-Match": '"1"'})
        assert response.status_code == 200, response.text
    elif change == "other_operation":
        request.operation_id = "other-operation"
    else:
        grant.execution_scope = {**grant.execution_scope, "native_space_ref": {**grant.execution_scope["native_space_ref"], "record_id": "other"}}
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(grant, request)


async def test_source_only_grant_cannot_get_native_permit(db_session, claimed_knowledge):
    row = claimed_knowledge
    request = row.body.model_copy(update={"connection_ref": ConnectionRef.model_validate(row.grant.sources[0])})
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(row.grant, request)
    assert len((await db_session.scalars(select(CredentialServiceDelegation))).all()) == 1


async def test_native_consent_renews_without_repinning_original_sources(test_client, db_session, claimed_knowledge):
    row = claimed_knowledge
    path, body, headers, original = await native_consent(test_client, row)
    grant = await db_session.get(CredentialServiceDelegation, original["delegation_id"])
    grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    renewed = await test_client.post(path, json=body, headers={**headers, "Idempotency-Key": "knowledge_native_002"})
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["delegation_id"] != original["delegation_id"]
    assert renewed.json()["scope"] == original["scope"] and not renewed.json()["work_dispatched"]
    assert row.fixture["capture"].await_count == 1
    row.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    denied = await test_client.get(path)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "knowledge_source_consent_required"

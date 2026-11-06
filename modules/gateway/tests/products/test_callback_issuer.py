"""Exercise the configured callback issuer and its real reconciliation producer."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from src.product_onboarding.callback_auth import PRODUCER, mint_callback_token, signing_key
from src.products.models import ProductOperation
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership
from tests.products.membership import revoke_platform_admin
from tests.products.test_handoff_regressions import connection_fixture
from tests.products.test_i12 import assign_callback_team, callback_fixture, recovery_path


@pytest.mark.parametrize(
    "changed_claim",
    [
        "iss",
        "aud",
        "sub",
        "scope",
        "operation_id",
        "tenant_id",
        "product_id",
        "project_id",
        "session_id",
        "actor_id",
        "profile_revision",
        "generation",
        "exp",
        "iat",
    ],
)
async def test_issuer_rejects_changed_authority(test_client, db_session, source_member, changed_claim, monkeypatch):
    operation_id, path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    token = mint_callback_token(operation)
    claims = jwt.decode(token, options={"verify_signature": False})
    if changed_claim in ("profile_revision", "generation"):
        claims[changed_claim] += 1
    elif changed_claim == "exp":
        claims[changed_claim] = claims["iat"] - 1
    elif changed_claim == "iat":
        claims[changed_claim] -= 3600
    else:
        claims[changed_claim] = "other-authority"
    altered = jwt.encode(claims, signing_key(), algorithm="HS256")
    rejected = await test_client.post(path, json=callback, headers={"Authorization": f"Bearer {altered}"})
    assert rejected.status_code == 403, rejected.text
    db_session.expire_all()
    unchanged = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert unchanged.state == "waiting_external" and unchanged.callback_sequence == 0 and unchanged.receipt is None
    accepted = await test_client.post(path, json=callback, headers={"Authorization": f"Bearer {token}"})
    assert accepted.status_code == 200, accepted.text


async def test_real_admin_effect_emits_replayable_callback(
    test_client,
    db_session,
    source_member,
    app,
    admin_token_context,
    monkeypatch,
):
    operation_id, callback_path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.request["callback_producer"] == PRODUCER
    issued = []

    def record_issuance(operation):
        token = mint_callback_token(operation)
        issued.append(token)
        return token

    monkeypatch.setattr("src.product_onboarding.recovery.mint_callback_token", record_issuance)
    path = f"{recovery_path(callback)}/{operation_id}/reconcile"
    headers = {"Idempotency-Key": "issuer_reconcile_key_123", "If-Profile-Revision": "1"}
    pending = await test_client.post(path, headers=headers)
    assert pending.status_code == 202 and pending.json()["state"] == "waiting_external"
    assert issued == []
    await assign_callback_team(test_client, app, source_member, admin_token_context)
    completed = await test_client.post(path, headers=headers)
    assert completed.status_code == 202 and completed.json()["state"] == "succeeded", completed.text
    assert len(issued) == 1
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    membership_id = await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback"))
    assert operation.provider_operation_id == membership_id and operation.callback_sequence == 1
    receipt = operation.receipt["peer_receipt"]
    assert receipt["status"] == "succeeded" and receipt["provider_operation_id"] == membership_id
    replay = await test_client.post(
        callback_path,
        json={"sequence": 1, "binding_generation": 1, "receipt": receipt},
        headers={"Authorization": f"Bearer {issued[0]}"},
    )
    assert replay.status_code == 200 and replay.json()["status"] == "duplicate", replay.text
    assert (await test_client.post(path, headers=headers)).status_code == 200
    assert len(issued) == 1
    assert await db_session.scalar(select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == "team-callback")) == 1
    assert "token" not in completed.text


@pytest.mark.parametrize("target", ["oauth_connection", "aws_connection", "github_connection"])
async def test_connection_readback_emits_scoped_receipt(test_client, db_session, source_member, target):
    operation_id, path, _ = await connection_fixture(test_client, db_session, source_member, target)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    operation.provider_operation_id = "zzz-intended-effect"
    await db_session.commit()
    result = await test_client.post(
        f"{path}/reconcile",
        headers={"Idempotency-Key": "connection_issuer_key_123", "If-Profile-Revision": "1"},
    )
    assert result.status_code == 202 and result.json()["state"] == "succeeded", result.text
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.callback_sequence == 1
    assert operation.receipt["peer_receipt"]["provider_operation_id"] == "zzz-intended-effect"


async def test_cancel_fences_previously_issued_token(test_client, db_session, source_member, monkeypatch):
    operation_id, path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    token = mint_callback_token(operation)
    cancelled = await test_client.post(
        f"{recovery_path(callback)}/{operation_id}/cancel",
        headers={"Idempotency-Key": "cancel_issuer_key_123", "If-Profile-Revision": "1"},
    )
    assert cancelled.status_code == 200
    rejected = await test_client.post(
        path,
        json={**callback, "binding_generation": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rejected.status_code == 403
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "cancelled" and operation.callback_sequence == 0 and operation.receipt is None
    with pytest.raises(HTTPException) as error:
        mint_callback_token(operation)
    assert error.value.status_code == 410


async def test_revoked_initiator_cannot_apply_issued_token(test_client, db_session, source_member, monkeypatch):
    operation_id, path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    token = mint_callback_token(operation)
    await revoke_platform_admin(db_session, source_member)
    db_session.add(TenantMembership(user_id=source_member.user_id, tenant_id=source_member.org_id, revoked_at=datetime.now(UTC)))
    await db_session.commit()
    rejected = await test_client.post(path, json=callback, headers={"Authorization": f"Bearer {token}"})
    assert rejected.status_code == 404, rejected.text
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "waiting_external" and operation.callback_sequence == 0 and operation.receipt is None


async def test_missing_signer_leaves_native_effect_recoverable(
    test_client,
    db_session,
    source_member,
    app,
    admin_token_context,
    monkeypatch,
):
    operation_id, _, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    await assign_callback_team(test_client, app, source_member, admin_token_context)
    path = f"{recovery_path(callback)}/{operation_id}/reconcile"
    headers = {"Idempotency-Key": "missing_signer_key_123", "If-Profile-Revision": "1"}
    with monkeypatch.context() as missing:
        missing.setenv("BG_TOKEN_SECRET_KEY", "")
        result = await test_client.post(path, headers=headers)
    assert result.status_code == 503, result.text
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "waiting_external" and operation.callback_sequence == 0
    assert (await test_client.post(path, headers=headers)).json()["state"] == "succeeded"
    assert await db_session.scalar(select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == "team-callback")) == 1


async def test_legacy_operation_can_issue_with_deadline_bound_expiry(test_client, db_session, source_member, monkeypatch):
    operation_id, _, _ = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    operation.request = {key: value for key, value in operation.request.items() if key != "callback_producer"}
    operation.expires_at = datetime.now(UTC) + timedelta(seconds=20)
    await db_session.commit()
    token = mint_callback_token(operation)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == PRODUCER
    assert claims["exp"] == int(operation.expires_at.timestamp())
    assert claims["operation_id"] == operation_id and claims["generation"] == 1

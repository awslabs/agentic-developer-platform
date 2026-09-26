"""Exact decision receipts, stale refusal and durable retry boundaries."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.admin.onboarding import cli_contract as api
from src.admin.onboarding import handler
from src.shared.models.onboarding import TenantAccessRequest


def row():
    return TenantAccessRequest(
        id="request",
        cognito_sub="human",
        provider="github",
        provider_user_id="42",
        proposed_tenant_id="tenant",
        target_login="alice",
        status="pending",
        created_at=datetime.now(UTC),
    )


def decision(target, **changes):
    return api.Decision(
        **{
            "operation_id": str(uuid4()),
            "expected_revision": api.revision(target),
            "expected_role": "member",
            "expected_scope": "join_existing",
            "decision_note": "Reviewed disposable request",
            **changes,
        }
    )


@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_exact_decision_replays_without_second_effect(monkeypatch, action):
    target = row()
    db = AsyncMock()
    db.get.return_value = target
    authorize = AsyncMock()
    monkeypatch.setattr(handler, "_authorize_decision", authorize)

    async def mutate(*args):
        target.status = "approved" if action == "approve" else "denied"
        return {"status": target.status, "tenant_id": "tenant"}

    mutation = AsyncMock(side_effect=mutate)
    monkeypatch.setattr(handler, "approve_access_request" if action == "approve" else "deny_access_request", mutation)
    body = decision(target)
    first = await api.decide("request", body, Mock(), db, action)
    second = await api.decide("request", body, Mock(), db, action)
    assert first == second
    assert first["operation_id"] == str(body.operation_id)
    assert mutation.await_count == 1
    assert authorize.await_count == 2


async def test_different_retry_and_unconfirmed_receipt_refused(monkeypatch):
    target = row()
    db = AsyncMock()
    db.get.return_value = target
    monkeypatch.setattr(handler, "_authorize_decision", AsyncMock())
    body = decision(target)
    target.decision_receipt = {"operation_id": str(body.operation_id), "fingerprint": "other", "result": None}
    with pytest.raises(HTTPException) as exc:
        await api.decide("request", body, Mock(), db, "approve")
    assert exc.value.status_code == 409
    db.commit.assert_not_awaited()


async def test_stale_review_refuses_before_effect(monkeypatch):
    target = row()
    body = decision(target)
    target.motivation = "changed"
    db = AsyncMock()
    db.get.return_value = target
    monkeypatch.setattr(handler, "_authorize_decision", AsyncMock())
    mutate = AsyncMock()
    monkeypatch.setattr(handler, "approve_access_request", mutate)
    with pytest.raises(HTTPException) as exc:
        await api.decide("request", body, Mock(), db, "approve")
    assert exc.value.status_code == 409
    mutate.assert_not_awaited()


async def test_foreign_request_refused_before_receipt_disclosure(monkeypatch):
    target = row()
    db = AsyncMock()
    db.get.return_value = target
    monkeypatch.setattr(handler, "_authorize_decision", AsyncMock(side_effect=HTTPException(403, "Denied")))
    with pytest.raises(HTTPException) as exc:
        await api.decide("request", decision(target), Mock(), db, "approve")
    assert exc.value.status_code == 403
    assert target.decision_receipt is None


def test_unknown_fields_cannot_change_granted_role():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        decision(row(), granted_role="platform_admin")


# Reuse the real SQL tenant/membership fixtures, not hand-written HTTP responses.
from tests.admin.test_onboarding_access_request_scope import (  # noqa: E402
    OWN_ORG,
    _client,
    _context,
    db_engine,  # noqa: F401
    seeded,  # noqa: F401
)


async def test_http_denial_receipt_replay_and_foreign_scope(seeded, monkeypatch):  # noqa: F811
    monkeypatch.setattr(handler, "_determine_role_for_matched_user", AsyncMock(return_value="member"))
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        review = await client.get("/admin/access-requests/req-own-b/review")
        assert review.status_code == 200, review.text
        reviewed = review.json()
        body = {
            "operation_id": str(uuid4()),
            "expected_revision": reviewed["revision"],
            "expected_role": reviewed["proposed_role"],
            "expected_scope": reviewed["requested_scope"],
            "decision_note": "Disposable denial",
        }
        first = await client.post("/admin/access-requests/req-own-b/deny/revision", json=body)
        assert first.status_code == 200, first.text
        replay = await client.post("/admin/access-requests/req-own-b/deny/revision", json=body)
        assert replay.status_code == 200, replay.text
        assert first.json() == replay.json()
        conflict = await client.post("/admin/access-requests/req-own-b/approve/revision", json=body)
        assert conflict.status_code == 409
        foreign = await client.get("/admin/access-requests/req-other-b/review")
        assert foreign.status_code == 403


async def test_explicit_request_deduplicates_without_granting_membership(seeded, monkeypatch):  # noqa: F811
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.shared.models.onboarding import TenantMembership

    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_WRITE", "true")
    monkeypatch.setattr(handler, "_extract_github_identity", lambda *args: ("fixture", "901"))
    monkeypatch.setattr(handler, "_proven_link_conflict", AsyncMock(return_value=None))
    async with _client(seeded, _context("fixture-sub", OWN_ORG)) as client:
        first = await client.post("/access/request", json={"target_tenant": OWN_ORG, "motivation": "fixture"})
        assert first.status_code == 200, first.text
        second = await client.post("/access/request", json={"target_tenant": OWN_ORG, "motivation": "fixture"})
        assert second.json()["request_id"] == first.json()["request_id"]
        assert first.json()["status"] == "pending"
        status = await client.get("/access/status?target_tenant=" + OWN_ORG)
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "pending"
        assert status.json()["spend_eligibility"] == "not_evaluated"
    async with async_sessionmaker(seeded)() as db:
        # Only the three seeded tenant admins/members exist after a join request.
        assert len(list((await db.execute(select(TenantMembership))).scalars())) == 3


async def test_http_revoke_reads_actual_gateway_family_without_cognito_claim(seeded):  # noqa: F811
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.shared.models.organization import User
    from src.shared.models.token import Token

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as db:
        user = await db.scalar(select(User).where(User.cognito_sub == "sub-plain-member"))
        user_id = user.id
        db.add(
            Token(
                id="fixture-token",
                token_hash="b" * 64,
                entity_type="user",
                entity_id=user_id,
                org_id=OWN_ORG,
                team_id=user.team_id,
                department_id="",
                is_admin=False,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await db.commit()
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        path = "/auth/admin/revoke-user-tokens/" + user_id
        before = await client.get(path + "/review", params={"org": OWN_ORG})
        assert before.status_code == 200, before.text
        body = {"org": OWN_ORG, "reason": "Disposable session test", "expected_revision": before.json()["revision"]}
        result = await client.post(path + "/revision", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["tokens_revoked"] == 1
        assert result.json()["active_gateway_tokens"] == 0
        assert result.json()["cognito_sessions_revoked"] is False
        stale = await client.post(path + "/revision", json=body)
        assert stale.status_code == 409
    async with factory() as db:
        assert (await db.get(Token, "fixture-token")).revoked_at is not None

"""Recovery is available only to the protected launch owner's current human session."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from anyio import from_thread
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from src.activity import exit_recovery_routes as routes
from src.agentauth import human_control
from src.agentauth.bootstrap import BootstrapRefusedError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from tests.agentauth.test_bootstrap_exit_recovery import INVOCATION, OPERATION, TENANT, read_event
from tests.agentauth.test_bootstrap_exit_recovery import client as client_fixture
from tests.agentauth.test_bootstrap_exit_recovery import recovery as recovery_fixture

client, recovery = client_fixture, recovery_fixture


async def request(ctx, monkeypatch, *, changes=None, member=True, method="POST", suffix="", extra=None):
    app = FastAPI()
    app.include_router(routes.router)
    current = TokenContext(
        user_id="human",
        org_id=TENANT,
        team_id="team",
        department_id="dept",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    current = current.model_copy(update=changes or {})
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: Mock()
    app.dependency_overrides[routes.get_exit_recovery] = lambda: ctx.service

    @asynccontextmanager
    async def fresh_db():
        yield Mock()

    monkeypatch.setattr(routes, "get_session_factory", lambda: fresh_db)

    async def canonical(_db, user_id, **kwargs):
        return user_id

    monkeypatch.setattr(human_control, "resolve_canonical_user_id", canonical)
    monkeypatch.setattr(routes, "resolve_canonical_user_id", canonical)
    membership = AsyncMock(
        side_effect=[None, BootstrapRefusedError("membership revoked")]
        if member == "revoked-after-observation"
        else None
        if member
        else BootstrapRefusedError("membership unavailable")
    )
    monkeypatch.setattr(human_control, "require_live_human_membership", membership)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        return await http.request(
            method,
            f"/activity/invocations/{INVOCATION}/reconcile-exit" + suffix,
            json={**ctx.request.model_dump(), **(extra or {})} if method == "POST" else None,
        )


async def test_owner_can_preview_recover_and_read_same_receipt(recovery, monkeypatch):
    ctx = recovery
    response = await request(ctx, monkeypatch, suffix="/preview")
    assert response.status_code == 200 and response.json()["status"] == "verified"
    assert read_event(ctx.store.client)["status"] == {"S": "webhook_received"}
    response = await request(ctx, monkeypatch)
    assert response.status_code == 200 and response.json()["status"] == "recovered"
    assert response.headers["cache-control"] == "no-store"
    readback = await request(ctx, monkeypatch, method="GET", suffix="/" + OPERATION)
    assert readback.status_code == 200 and readback.json() == response.json()


@pytest.mark.parametrize(
    "changes,member",
    [
        ({"user_id": "other"}, True),
        ({"org_id": "other"}, True),
        ({"account_type": "service"}, True),
        ({"auth_source": "iam"}, True),
        ({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, True),
        ({}, False),
    ],
)
async def test_nonowner_or_nonhuman_or_expired_session_never_reads_provider_or_writes(recovery, monkeypatch, changes, member):
    ctx = recovery
    response = await request(ctx, monkeypatch, changes=changes, member=member)
    assert response.status_code == 409
    ctx.logs.filter_log_events.assert_not_called()
    assert read_event(ctx.store.client)["status"] == {"S": "webhook_received"}


@pytest.mark.parametrize("field", ["audit_document", "log_group", "tenant_id", "generation", "source", "restart"])
async def test_caller_cannot_supply_evidence_authority_or_replay_instructions(recovery, monkeypatch, field):
    response = await request(recovery, monkeypatch, extra={field: "caller-chosen"})
    assert response.status_code == 422
    recovery.logs.filter_log_events.assert_not_called()


async def test_membership_revocation_during_provider_read_prevents_commit(recovery, monkeypatch):
    response = await request(recovery, monkeypatch, member="revoked-after-observation")
    assert response.status_code == 409
    recovery.logs.filter_log_events.assert_called_once()
    assert read_event(recovery.store.client)["status"] == {"S": "webhook_received"}


@pytest.mark.parametrize("change", ["revoked", "shadow", None])
async def test_precommit_authorization_reads_current_sql_rows_despite_retained_orm_objects(
    recovery, monkeypatch, db_session, db_session_factory, change
):
    """A separate SQLite session commits while the request retains its initial ORM objects."""
    user = User(id="human", org_id=TENANT, team_id="team", email="owner@example.test", is_shadow=False)
    membership = TenantMembership(user_id="human", tenant_id=TENANT, is_active=True)
    db_session.add_all([user, membership])
    await db_session.commit()
    current = TokenContext(
        user_id="human",
        org_id=TENANT,
        team_id="team",
        department_id="dept",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[routes.get_exit_recovery] = lambda: recovery.service
    monkeypatch.setattr(routes, "get_session_factory", lambda: db_session_factory)

    async def change_authorization():
        async with db_session_factory() as other:
            assert other is not db_session
            if change == "revoked":
                await other.execute(update(TenantMembership).where(TenantMembership.id == membership.id).values(revoked_at=datetime.now(UTC)))
            elif change == "shadow":
                await other.execute(update(User).where(User.id == user.id).values(is_shadow=True))
            await other.commit()
        # Strong references keep SQLAlchemy's identity-map entries alive. The
        # original request session still holds the old authorization attributes.
        assert membership.revoked_at is None
        assert user.is_shadow is False

    def provider_read(**kwargs):
        from_thread.run(change_authorization)
        return recovery.logs.filter_log_events.return_value

    recovery.logs.filter_log_events.side_effect = provider_read
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(f"/activity/invocations/{INVOCATION}/reconcile-exit", json=recovery.request.model_dump())
    recovery.logs.filter_log_events.assert_called_once()
    if change is None:
        assert response.status_code == 200, response.text
        assert read_event(recovery.store.client)["status"] == {"S": "failed"}
    else:
        assert response.status_code == 409, response.text
        assert read_event(recovery.store.client)["status"] == {"S": "webhook_received"}
        assert recovery.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}") == recovery.raw
        assert recovery.service.receipt(tenant=TENANT, invocation=INVOCATION, attempt=1) is None

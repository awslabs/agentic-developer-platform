# ruff: noqa: F811 -- pytest fixture imports are intentionally injected by argument name.
"""Mounted audit outcomes survive request teardown and provider/sink failures."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.admin import audit_operation
from src.shared.models.audit import AuditLog
from src.shared.models.organization import Organization
from tests.admin.test_admin_audit import (
    _build_admin_app,
    _client,
    _platform_admin_ctx,
    _regular_user_ctx,
    _seed_org_and_user,
)
from tests.admin.test_admin_audit import (
    engine as engine,
)
from tests.admin.test_admin_audit import (
    session as session,
)
from tests.admin.test_admin_audit import (
    session_factory as session_factory,
)


async def rows(factory):
    async with factory() as fresh:
        return (await fresh.execute(select(AuditLog))).scalars().all()


async def test_success_and_correlation_survive_request_teardown(session, session_factory):
    seed = await _seed_org_and_user(session)
    actor = _platform_admin_ctx(seed["org_id"])
    app = _build_admin_app(session, actor)
    async with _client(app) as client:
        response = await client.put(
            f"/admin/organizations/{seed['org_id']}", json={"name": "Persisted change"}, headers={"X-Correlation-ID": "forged-correlation"}
        )
    assert response.status_code == 200
    await session.close()
    receipts = await rows(session_factory)
    assert len(receipts) == 2
    success = next(row for row in receipts if row.event_type == "admin_update_organization")
    assert success.actor_id == actor.user_id
    assert success.details["correlation_id"] == response.headers["X-Admin-Operation-Id"]
    assert success.details["correlation_id"] != "forged-correlation"
    assert success.details["target_tenant"] == seed["org_id"]
    async with session_factory() as fresh:
        assert (await fresh.get(Organization, seed["org_id"])).name == "Persisted change"


async def test_route_permission_denial_is_durable_and_target_unchanged(session, session_factory):
    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _regular_user_ctx(seed["org_id"]))
    async with _client(app) as client:
        response = await client.delete(f"/admin/organizations/{seed['org_id']}")
    assert response.status_code == 403
    await session.close()
    receipts = await rows(session_factory)
    denied = [row for row in receipts if row.event_type == "admin_operation_refused"]
    assert len(denied) == 1
    assert denied[0].details["outcome"] == "denied"
    async with session_factory() as fresh:
        assert await fresh.get(Organization, seed["org_id"]) is not None


async def test_router_dependency_denial_is_durable(session, session_factory):
    from src.admin.identity.router import router

    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _regular_user_ctx(seed["org_id"]))
    app.include_router(router)
    async with _client(app) as client:
        response = await client.delete(f"/api/admin/identity/organizations/{seed['org_id']}")
    assert response.status_code == 403
    await session.close()
    assert sum(row.event_type == "admin_operation_refused" for row in await rows(session_factory)) == 1
    async with session_factory() as fresh:
        assert await fresh.get(Organization, seed["org_id"]) is not None


async def test_admission_sink_failure_prevents_business_mutation(session, session_factory, monkeypatch):
    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))
    monkeypatch.setattr(audit_operation, "persist", AsyncMock(side_effect=RuntimeError("synthetic sink failure")))
    async with _client(app) as client:
        response = await client.put(f"/admin/organizations/{seed['org_id']}", json={"name": "Must not change"})
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "admin_audit_unavailable"
    await session.close()
    async with session_factory() as fresh:
        assert (await fresh.get(Organization, seed["org_id"])).name != "Must not change"


async def test_terminal_sink_failure_exposes_pending_operation(session, session_factory, monkeypatch):
    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))
    original = audit_operation.persist

    async def fail_terminal(operation, **kwargs):
        if kwargs["outcome"] == "success":
            raise RuntimeError("synthetic terminal failure")
        await original(operation, **kwargs)

    monkeypatch.setattr(audit_operation, "persist", fail_terminal)
    async with _client(app) as client:
        response = await client.put(f"/admin/organizations/{seed['org_id']}", json={"name": "Committed before terminal failure"})
        assert response.status_code == 503
        unresolved = await client.get("/admin/audit-events", params={"unresolved_only": True})
    assert unresolved.status_code == 200
    assert unresolved.json()["total"] == 1
    assert unresolved.json()["events"][0]["details"]["outcome"] == "pending"
    await session.close()
    assert [row.event_type for row in await rows(session_factory)] == ["admin_operation_started"]
    async with session_factory() as fresh:
        assert (await fresh.get(Organization, seed["org_id"])).name == "Committed before terminal failure"


@pytest.mark.parametrize("provider_fails", [False, True])
async def test_external_operation_has_preexisting_intent_and_honest_outcome(session, session_factory, monkeypatch, provider_fails):
    from src.admin.connections import routes

    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))
    app.include_router(routes.router)
    calls = []

    async def rotate():
        persisted = await rows(session_factory)
        assert len(persisted) == 1 and persisted[0].event_type == "admin_operation_started"
        calls.append("provider effect")
        if provider_fails:
            raise RuntimeError("synthetic partial provider effect")
        return {"rotated": True, "app_id": "42", "message": "fixture"}

    monkeypatch.setattr(routes, "rotate_app_key", rotate)
    async with _client(app) as client:
        response = await client.post("/admin/connections/github/app/rotate-key")
    assert response.status_code == (500 if provider_fails else 200)
    assert calls == ["provider effect"]
    await session.close()
    receipts = await rows(session_factory)
    outcomes = [row.details["outcome"] for row in receipts]
    assert outcomes.count("pending") == 1
    assert outcomes.count("reconciliation_required" if provider_fails else "success") == 1


async def test_identity_target_org_is_not_platform_admin_home_org(session, session_factory, monkeypatch):
    from src.admin.identity.identity_index_writer import IdentityIndexWriter
    from src.admin.identity.router import router
    from src.shared.models.vault import UserIdentity

    monkeypatch.setattr(IdentityIndexWriter, "delete_user_identity", AsyncMock(return_value=None))
    seed = await _seed_org_and_user(session)
    identity = UserIdentity(
        id="identity-fixture",
        org_id=seed["org_id"],
        user_id=seed["user_id"],
        team_id=seed["team_id"],
        provider="github",
        provider_user_id="42",
        verification_method="admin_manual",
    )
    session.add(identity)
    await session.commit()
    actor = _platform_admin_ctx("operator-home")
    app = _build_admin_app(session, actor)
    app.include_router(router)
    async with _client(app) as client:
        response = await client.delete(f"/api/admin/identity/users/{seed['user_id']}/identities/identity-fixture")
    assert response.status_code == 204
    await session.close()
    success = next(row for row in await rows(session_factory) if row.event_type == "admin_identity_delete_identity")
    assert success.org_id == seed["org_id"]
    assert success.details["target_tenant"] == seed["org_id"]
    assert success.actor_id == actor.user_id


@pytest.mark.parametrize("kind", ["install", "register"])
@pytest.mark.parametrize("outcome", ["success", "replay", "partial"])
async def test_callback_receipts_survive_redirect_without_jwt(session, session_factory, monkeypatch, kind, outcome):
    import json
    from types import SimpleNamespace

    from src.admin.connections import routes
    from src.auth.dependencies import get_current_user
    from src.auth.magic_link import NonceAlreadyConsumedError

    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))
    app.include_router(routes.router)

    async def no_jwt():
        raise AssertionError("Callback must use nonce authority, not browser JWT")

    app.dependency_overrides[get_current_user] = no_jwt
    calls = []

    async def provider(**kwargs):
        assert len(await rows(session_factory)) == 1
        if outcome == "replay":
            raise NonceAlreadyConsumedError("synthetic replay")
        audit_operation.callback_actor(SimpleNamespace(cognito_sub="nonce-actor", id="user"), seed["org_id"])
        audit_operation.mark_admin_effects()
        calls.append("effect")
        if outcome == "partial":
            raise RuntimeError("synthetic partial provider failure")
        return {"success": True} if kind == "install" else "https://example.test/complete"

    monkeypatch.setattr(routes, "install_callback" if kind == "install" else "register_app_callback", provider)
    url = "/admin/connections/github/install-callback" if kind == "install" else "/admin/connections/github/app/register-callback"
    async with _client(app) as client:
        response = await client.get(url, params={"installation_id": 42, "state": "private-nonce-fixture", "code": "private-code-fixture"})
    assert response.status_code in (302, 303, 307)
    await session.close()
    receipts = await rows(session_factory)
    assert len(receipts) == 2
    terminal = next(r for r in receipts if r.details["outcome"] != "pending")
    assert terminal.details["outcome"] == {"success": "success", "replay": "denied", "partial": "reconciliation_required"}[outcome]
    assert terminal.actor_id == (None if outcome == "replay" else "nonce-actor")
    assert terminal.details["operation_id"] == response.headers["X-Admin-Operation-Id"]
    assert calls == ([] if outcome == "replay" else ["effect"])
    serialized = json.dumps([r.details for r in receipts])
    assert "private-nonce-fixture" not in serialized and "private-code-fixture" not in serialized


async def test_existing_posture_writer_has_exactly_one_durable_event(session, session_factory):
    from tests.admin.persona_models.test_operator_pmm07_posture_api import call_mutation

    response = await call_mutation(session, registered=True, revision=1)
    assert response.status_code == 200
    await session.close()
    receipts = await rows(session_factory)
    assert len(receipts) == 1
    assert receipts[0].actor_id == "canonical-admin"
    assert not receipts[0].event_type.startswith("admin_")


async def test_missing_terminal_instrumentation_fails_closed(session, session_factory, monkeypatch):
    from src.admin import routes

    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))
    monkeypatch.setattr(routes, "write_admin_audit", AsyncMock())
    async with _client(app) as client:
        response = await client.put(f"/admin/organizations/{seed['org_id']}", json={"name": "Effect without terminal"})
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "admin_audit_incomplete"
    await session.close()
    assert [r.details["outcome"] for r in await rows(session_factory)] == ["pending"]


async def test_uncommitted_sql_is_rolled_back_on_failure(session, session_factory, monkeypatch):
    from src.admin.service import AdminService

    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))

    async def fail(self, org_id, request):
        org = await self.db.get(Organization, org_id)
        org.name = "Must roll back"
        await self.db.flush()
        raise RuntimeError("synthetic SQL failure")

    monkeypatch.setattr(AdminService, "update_organization", fail)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.put(f"/admin/organizations/{seed['org_id']}", json={"name": "Must roll back"})
    assert response.status_code == 500
    await session.close()
    async with session_factory() as fresh:
        assert (await fresh.get(Organization, seed["org_id"])).name != "Must roll back"
    assert [r.details["outcome"] for r in await rows(session_factory)] == ["pending", "reconciliation_required"]


async def test_provider_refusal_after_effect_is_still_unresolved(session, session_factory, monkeypatch):
    from fastapi import HTTPException

    from src.admin.connections import routes

    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))
    app.include_router(routes.router)
    provider = AsyncMock(side_effect=HTTPException(403, "Provider refused after possible effect"))
    monkeypatch.setattr(routes, "rotate_app_key", provider)
    async with _client(app) as client:
        response = await client.post("/admin/connections/github/app/rotate-key")
        unresolved = await client.get("/admin/audit-events", params={"unresolved_only": True})
    assert response.status_code == 403
    assert unresolved.json()["total"] == 1
    provider.assert_awaited_once()
    assert [r.details["outcome"] for r in await rows(session_factory)] == ["pending", "reconciliation_required"]


async def test_idempotent_tenant_link_returns_success_with_one_receipt_each(session, session_factory):
    from src.admin.tenants.routes import router

    seed = await _seed_org_and_user(session)
    session.add(Organization(id="child-org", name="Child", github_org_id="42"))
    await session.commit()
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))
    app.include_router(router)
    operation_ids = []
    async with _client(app) as client:
        for _ in range(2):
            response = await client.post(f"/admin/tenants/{seed['org_id']}/orgs", json={"github_org_id": "42"})
            assert response.status_code == 200
            operation_ids.append(response.headers["X-Admin-Operation-Id"])
    assert len(set(operation_ids)) == 2
    await session.close()
    receipts = await rows(session_factory)
    assert len(receipts) == 4
    assert sum(r.event_type == "admin_tenant_link_org" for r in receipts) == 2
    async with session_factory() as fresh:
        assert (await fresh.get(Organization, "child-org")).parent_tenant_id == seed["org_id"]


@pytest.mark.parametrize("case", ["higher-role", "cross-tenant"])
async def test_scoped_admin_denial_preserves_authoritative_target(session, session_factory, case):
    from src.shared.models.organization import User

    seed = await _seed_org_and_user(session)
    session.add(Organization(id="other-org", name="Other tenant"))
    await session.commit()
    app = _build_admin_app(session, _regular_user_ctx(seed["org_id"]))
    async with _client(app) as client:
        if case == "higher-role":
            response = await client.put(f"/admin/organizations/{seed['org_id']}/users/admin-pg-001", json={"role": "member"})
        else:
            response = await client.put("/admin/organizations/other-org", json={"name": "Unauthorized rename"})
    assert response.status_code == 403
    await session.close()
    receipts = await rows(session_factory)
    assert [r.details["outcome"] for r in receipts] == ["pending", "denied"]
    async with session_factory() as fresh:
        assert (await fresh.get(Organization, "other-org")).name == "Other tenant"
        assert (await fresh.get(User, "admin-pg-001")).role == "platform_admin"


async def test_actor_substitution_cannot_produce_success_receipt(session, session_factory, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from src.admin import audit, routes

    seed = await _seed_org_and_user(session)
    app = _build_admin_app(session, _platform_admin_ctx(seed["org_id"]))

    async def forged_writer(db, **kwargs):
        kwargs["actor"] = _regular_user_ctx(seed["org_id"])
        await audit.write_admin_audit(db, **kwargs)

    monkeypatch.setattr(routes, "write_admin_audit", forged_writer)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.put(f"/admin/organizations/{seed['org_id']}", json={"name": "Effect before forged writer"})
    assert response.status_code == 500
    await session.close()
    receipts = await rows(session_factory)
    assert [r.details["outcome"] for r in receipts] == ["pending", "reconciliation_required"]
    assert all(r.actor_id == "admin-sub-001" for r in receipts)


@pytest.mark.parametrize("operation", ["switch", "disconnect", "disconnect-pending"])
async def test_connection_receipt_uses_resolved_tenant(session, session_factory, monkeypatch, operation):
    from types import SimpleNamespace

    from src.admin.connections import routes
    from src.auth import workspaces
    from src.shared.identity import workspaces as identity_workspaces

    seed = await _seed_org_and_user(session)
    actor = _platform_admin_ctx(seed["org_id"])
    app = _build_admin_app(session, actor)
    app.include_router(routes.router)
    target = "resolved-target-tenant"
    if operation == "switch":
        monkeypatch.setattr(workspaces, "get_workspace_claims", lambda: object())
        monkeypatch.setattr(workspaces, "select_workspace", AsyncMock(return_value=SimpleNamespace(org_id=target)))
    else:
        monkeypatch.setattr(routes, "resolve_effective_org_id", AsyncMock(return_value=target))
        monkeypatch.setattr(identity_workspaces, "workspace_user", AsyncMock(return_value=SimpleNamespace(id="resolved-user")))
        monkeypatch.setattr(
            routes,
            "delete_connection",
            AsyncMock(
                return_value={"deleted": True, "installation_id": 42, "residual": ["provider_uninstall"] if operation == "disconnect-pending" else []}
            ),
        )
    async with _client(app) as client:
        if operation == "switch":
            response = await client.post("/admin/connections/switch-tenant", json={"tenant_id": "caller-input"})
        else:
            response = await client.delete("/admin/connections/github/42")
    assert response.status_code == 200
    await session.close()
    expected = "reconciliation_required" if operation == "disconnect-pending" else "success"
    success = next(row for row in await rows(session_factory) if row.details["outcome"] == expected)
    assert success.org_id == target
    assert success.details["target_tenant"] == target
    assert success.actor_id == actor.user_id
    if operation == "switch":
        assert success.details["target_id"] == target
    else:
        assert routes.delete_connection.await_args.kwargs["caller_org_id"] == target

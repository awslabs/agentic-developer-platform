"""#9640: an owner session minted for a verified Dev Box host run authenticates on the public API.

The box CLI sends the session as a Bearer token to the same routes a laptop login uses
(``/v1/products``, ``/orchestration/gates/...``). These tests mint a real session through
the crew route on the full application and then use it through the real
``get_current_user`` dependency: member and admin standing, current-authority re-decision,
revoked session, removed membership, forgery, and the legacy token that must not take the
owner-session path.
"""

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.dependencies import get_current_user
from src.auth.token_manager import TokenManager
from src.crew.model_token import mint_model_token
from src.products.models import Product, ProductProjectBinding, Project
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from tests.crew.test_host_tools import active, host_context, registry  # noqa: F401

SECRET = "test-secret-key-do-not-use-in-production"
OWNER_SUB = "sub-owner-a"


async def probe(context: TokenContext = Depends(get_current_user)):
    """Test-only view of what the real dependency produced."""
    return {
        "user_id": context.user_id,
        "is_admin": context.is_admin,
        "delegation": context.delegation,
        "cognito_username": context.cognito_username,
        "team_id": context.team_id,
    }


@pytest.fixture
async def owner(request, test_client, app, db_session, test_engine, active, monkeypatch):  # noqa: F811
    """``native``: users.cognito_sub carries the login. ``linked``: the login lives only in
    the account's verified UserIdentity placement link (cognito_sub is null), as for
    linked workspace accounts (reviewer R1)."""
    shape = getattr(request, "param", "native")
    for tenant, owner_id in (("tenant-a", "owner-a"), ("tenant-b", "owner-b")):
        db_session.add(Organization(id=tenant, name=tenant))
        db_session.add(Department(id=f"{tenant}-department", org_id=tenant, name="Engineering"))
        db_session.add(Team(id=f"{tenant}-team", org_id=tenant, department_id=f"{tenant}-department", name="Engineering"))
        db_session.add(
            User(
                id=owner_id,
                org_id=tenant,
                team_id=f"{tenant}-team",
                email=f"{owner_id}@example.test",
                cognito_sub=None if shape == "linked" else f"sub-{owner_id}",
                cognito_username=owner_id,
            )
        )
        if shape == "linked":
            # The login's canonical row lives in the person's home org; the tenant's
            # member row is reached through the verified placement link.
            db_session.add(Organization(id=f"home-{owner_id}", name=f"home-{owner_id}"))
            db_session.add(Department(id=f"home-{owner_id}-department", org_id=f"home-{owner_id}", name="Home"))
            db_session.add(Team(id=f"home-{owner_id}-team", org_id=f"home-{owner_id}", department_id=f"home-{owner_id}-department", name="Home"))
            db_session.add(
                User(
                    id=f"canonical-{owner_id}",
                    org_id=f"home-{owner_id}",
                    team_id=f"home-{owner_id}-team",
                    email=f"{owner_id}@example.test",
                    cognito_sub=f"sub-{owner_id}",
                    cognito_username=owner_id,
                )
            )
            db_session.add(
                UserIdentity(
                    user_id=owner_id,
                    org_id=tenant,
                    team_id=f"{tenant}-team",
                    provider="cognito",
                    provider_user_id=f"sub-{owner_id}",
                    verification_method="org_placement",
                    verified_at=datetime.now(UTC),
                )
            )
        db_session.add(TeamMembership(org_id=tenant, user_id=owner_id, team_id=f"{tenant}-team", is_primary=True))
        db_session.add(TenantMembership(tenant_id=tenant, user_id=owner_id, role="member", is_active=True))
        db_session.add(
            Product(
                tenant_id=tenant,
                id=f"{tenant}-product",
                name=f"{tenant} product",
                purpose="Example",
                intended_users=["Owner"],
                owner_user_id=owner_id,
                team_id=f"{tenant}-team",
                starting_point="idea",
                created_by=owner_id,
            )
        )
        db_session.add(Project(tenant_id=tenant, id=f"{tenant}-project", name="Base", owner_user_id=owner_id))
        db_session.add(
            ProductProjectBinding(
                tenant_id=tenant,
                product_id=f"{tenant}-product",
                project_id=f"{tenant}-project",
                is_base=True,
                provenance={},
                valid_from=datetime.now(UTC),
            )
        )
    await db_session.commit()

    identity = host_context()

    async def context(request: Request):
        request.state.token_context = identity
        return identity

    async def run_id(request: Request):
        return request.headers.get("x-agent-runid")

    app.dependency_overrides[get_token_context] = context
    app.dependency_overrides[set_agent_run_id_from_header] = run_id
    app.add_api_route("/v1/owner-session-probe", probe, methods=["GET"])
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setenv("ADP_PUBLIC_API_URL", "https://app.example.test/api")
    cognito_admin = Mock(return_value=False)
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", cognito_admin)
    model_token = await mint_model_token(active.owner, active.target, now=active.now)

    async def mint():
        response = await test_client.post(
            "/crew/orchestration/owner-session",
            headers={"X-Agent-RunId": active.run_id, "X-Adp-Crew-Model-Token": model_token},
        )
        assert response.status_code == 201, response.text
        return response.json()

    return type("Owner", (), {"client": test_client, "mint": staticmethod(mint), "cognito_admin": cognito_admin, "db": db_session})


def bearer(session):
    return {"Authorization": f"Bearer {session['access_token']}"}


async def test_member_session_authenticates_products_and_gates_as_the_owner(owner):
    session = await owner.mint()
    assert session["user_id"] == OWNER_SUB and session["is_admin"] is False

    products = await owner.client.get("/v1/products", headers=bearer(session))
    assert products.status_code == 200, products.text
    assert {item["id"] for item in products.json()["items"]} == {"tenant-a-product"}

    seen = await owner.client.get("/v1/owner-session-probe", headers=bearer(session))
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert (body["user_id"], body["cognito_username"], body["team_id"], body["is_admin"]) == (OWNER_SUB, "owner-a", "tenant-a-team", False)
    assert body["delegation"] == session["delegation"]
    assert body["delegation"].startswith("dev-box:box-a:")
    # The middleware that pre-populates request.state for the model paths resolves the
    # same identity and attribution through the same trusted path.
    from src.auth.middleware import validate_cognito_jwt

    pre = await validate_cognito_jwt(f"Bearer {session['access_token']}")
    assert (pre.user_id, pre.is_admin, pre.delegation) == (OWNER_SUB, False, session["delegation"])

    # The gate route accepts the session as the owner: an unknown gate is 404, not 401.
    gate = await owner.client.post(f"/orchestration/gates/{uuid4()}/approve", headers=bearer(session), json={"reason": "ok"})
    assert gate.status_code == 404, gate.text


async def test_admin_session_carries_current_platform_authority(owner):
    owner.cognito_admin.return_value = True
    session = await owner.mint()
    assert session["is_admin"] is True

    seen = await owner.client.get("/v1/owner-session-probe", headers=bearer(session))
    assert seen.status_code == 200 and seen.json()["is_admin"] is True, seen.text
    products = await owner.client.get("/v1/products", headers=bearer(session))
    assert products.status_code == 200, products.text

    # Authority is re-decided on every request: IdP offboarding takes effect on the live session.
    owner.cognito_admin.return_value = False
    demoted = await owner.client.get("/v1/owner-session-probe", headers=bearer(session))
    assert demoted.status_code == 200 and demoted.json()["is_admin"] is False, demoted.text


async def test_revoked_session_and_removed_membership_are_refused(owner):
    session = await owner.mint()
    assert (await owner.client.get("/v1/products", headers=bearer(session))).status_code == 200

    await owner.db.execute(update(TenantMembership).where(TenantMembership.user_id == "owner-a").values(revoked_at=datetime.now(UTC)))
    await owner.db.commit()
    removed = await owner.client.get("/v1/products", headers=bearer(session))
    assert removed.status_code == 403, removed.text
    assert removed.json()["detail"]["error"] == "workspace_membership_denied"

    await owner.db.execute(update(TenantMembership).where(TenantMembership.user_id == "owner-a").values(revoked_at=None))
    await owner.db.commit()
    assert await TokenManager(SECRET).revoke_token(session["access_token"], owner.db)
    revoked = await owner.client.get("/v1/products", headers=bearer(session))
    assert revoked.status_code == 401, revoked.text
    assert revoked.json()["detail"]["error"] == "owner_session_invalid"


async def test_forged_and_legacy_tokens_do_not_take_the_owner_session_path(owner):
    import time

    import jwt

    claims = {
        "sub": OWNER_SUB,
        "org_id": "tenant-a",
        "team_id": "tenant-a-team",
        "department_id": "",
        "account_type": "human",
        "is_admin": True,
        "exp": int(time.time()) + 600,
        "iat": int(time.time()),
        "jti": "forged",
        "delegation": "dev-box:box-a:run",
    }
    forged = jwt.encode(claims, "not-the-gateway-secret", algorithm="HS256")
    denied = await owner.client.get("/v1/products", headers={"Authorization": f"Bearer {forged}"})
    assert denied.status_code == 401 and denied.json()["detail"]["error"] == "owner_session_invalid", denied.text

    # Correct secret but never stored: the revocable token table is the authority.
    unstored = jwt.encode(claims, SECRET, algorithm="HS256")
    denied = await owner.client.get("/v1/products", headers={"Authorization": f"Bearer {unstored}"})
    assert denied.status_code == 401 and denied.json()["detail"]["error"] == "owner_session_invalid", denied.text

    # A gateway token without a dev-box delegation is an ordinary legacy token: it never
    # reaches the owner-session path and the public route does not accept it.
    legacy = jwt.encode({k: v for k, v in claims.items() if k != "delegation"}, SECRET, algorithm="HS256")
    denied = await owner.client.get("/v1/products", headers={"Authorization": f"Bearer {legacy}"})
    assert denied.status_code in (401, 503), denied.text
    assert denied.json()["detail"]["error"] != "owner_session_invalid"


@pytest.mark.parametrize("owner", ["linked"], indirect=True)
async def test_linked_workspace_owner_keeps_the_verified_login_subject_and_admin_standing(owner):
    owner.cognito_admin.return_value = True
    session = await owner.mint()
    # The signed subject is the login in the placement link, never the local users.id,
    # and Cognito standing was asked for that login.
    assert session["user_id"] == OWNER_SUB and session["is_admin"] is True
    assert owner.cognito_admin.call_args.args[0] == OWNER_SUB

    products = await owner.client.get("/v1/products", headers=bearer(session))
    assert products.status_code == 200, products.text
    assert {item["id"] for item in products.json()["items"]} >= {"tenant-a-product"}
    seen = await owner.client.get("/v1/owner-session-probe", headers=bearer(session))
    assert seen.status_code == 200, seen.text
    assert (seen.json()["user_id"], seen.json()["team_id"], seen.json()["is_admin"]) == (OWNER_SUB, "tenant-a-team", True)
    assert owner.cognito_admin.call_args.args[0] == OWNER_SUB


async def test_request_logs_name_the_owner_and_the_delegation(owner, caplog, monkeypatch):
    import logging

    from src.shared.middleware import logging_middleware

    session = await owner.mint()
    # The principal reaches the log line through the request context the middleware sets
    # from the same state; capture what it was given.
    attributed = []
    real_set_request_context = logging_middleware.set_request_context

    def record_context(**fields):
        attributed.append(fields)
        return real_set_request_context(**fields)

    monkeypatch.setattr(logging_middleware, "set_request_context", record_context)
    caplog.set_level(logging.INFO, logger="src.shared.middleware.logging_middleware")
    caplog.clear()
    assert (await owner.client.get("/v1/products", headers=bearer(session))).status_code == 200
    gate = await owner.client.post(f"/orchestration/gates/{uuid4()}/approve", headers=bearer(session), json={"reason": "ok"})
    assert gate.status_code == 404, gate.text

    ended = {r.path: r for r in caplog.records if getattr(r, "event", "") == "request_end"}
    assert "/v1/products" in ended and any(path.startswith("/orchestration/gates/") for path in ended), sorted(ended)
    for record in ended.values():
        assert record.delegation == session["delegation"], (record.path, record.__dict__.get("delegation"))
    principals = [fields for fields in attributed if "user_id" in fields]
    assert len(principals) == 2 and all(fields["user_id"] == OWNER_SUB and fields["org_id"] == "tenant-a" for fields in principals), attributed

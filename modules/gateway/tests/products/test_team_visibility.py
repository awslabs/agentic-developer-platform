"""Product browsing and creation use current team assignments, not owner labels."""

import pytest
from sqlalchemy import delete, func, select

from src.admin.platform_authority import apply_platform_admin_authority, set_platform_admin_grant
from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductOperation, ProductProjectBinding, Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, TeamMembership, User
from tests.products.membership import add_team_member
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.fixture
async def assigned_product(test_client, db_session, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    result = created.json()
    product = await db_session.get(Product, (source_member.org_id, result["product_id"]))
    return product, result["project_id"]


async def assert_browsing(client, product, project_id, *, visible):
    path = f"/v1/products/{product.id}/projects/{project_id}"
    listing = await client.get("/v1/products")
    assert listing.status_code == 200, listing.text
    assert (product.id in {row["id"] for row in listing.json()["items"]}) is visible
    discovery = await client.get("/v1/projects")
    assert discovery.status_code == 200, discovery.text
    assert (project_id in {row["id"] for row in discovery.json()["items"]}) is visible
    for suffix in ("", "/people", "/changes", "/revisions/1"):
        response = await client.get(path + suffix)
        assert response.status_code == (200 if visible else 404), (suffix, response.text)
        if not visible:
            assert product.name not in response.text


async def test_current_member_can_browse_but_cannot_archive_or_bind_as_owner(test_client, app, db_session, source_member, assigned_product):
    product, project_id = assigned_product
    teammate = source_member.model_copy(update={"user_id": "current-teammate", "team_id": "stale-other-team"})
    db_session.add(User(id=teammate.user_id, org_id=teammate.org_id, team_id=teammate.team_id, email="teammate@example.test"))
    await add_team_member(db_session, teammate, team_id=product.team_id)
    db_session.add(Project(id="teammate-project", tenant_id=teammate.org_id, owner_user_id=teammate.user_id, name="Teammate project"))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: teammate
    await assert_browsing(test_client, product, project_id, visible=True)
    headers = {"Idempotency-Key": "team_visibility_mutation", "If-Profile-Revision": "1"}
    archive = await test_client.post(
        f"/v1/products/{product.id}/projects/{project_id}/lifecycle", json={"action": "archive", "reason": "Permission check"}, headers=headers
    )
    assert archive.status_code == 403, archive.text
    bind = await test_client.post(f"/v1/products/{product.id}/projects", json={"project_id": "teammate-project"}, headers=headers)
    assert bind.status_code == 404, bind.text


@pytest.mark.parametrize("actor_kind", ["owner", "stale_pointer", "org_admin"])
async def test_owner_pointer_and_org_admin_do_not_replace_team_membership(test_client, app, db_session, source_member, assigned_product, actor_kind):
    product, project_id = assigned_product
    if actor_kind == "owner":
        actor = source_member
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == actor.user_id))
    else:
        actor = source_member.model_copy(update={"user_id": actor_kind})
        db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=product.team_id, email=f"{actor_kind}@example.test"))
        await db_session.flush()
        if actor_kind == "org_admin":
            db_session.add(TenantMembership(user_id=actor.user_id, tenant_id=actor.org_id, role="org_admin"))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    await assert_browsing(test_client, product, project_id, visible=False)
    if actor_kind == "owner":
        replay = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
        assert replay.status_code == 404, replay.text
        create_scope = await test_client.post(
            f"/v1/products/{product.id}/projects",
            json={"mode": "create", "name": "Hidden scope"},
            headers={"Idempotency-Key": "removed_owner_scope", "If-Profile-Revision": "1"},
        )
        assert create_scope.status_code == 404, create_scope.text


async def test_removing_membership_revokes_already_browsed_product_with_stale_user_pointer(test_client, db_session, source_member, assigned_product):
    product, project_id = assigned_product
    await assert_browsing(test_client, product, project_id, visible=True)
    await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == source_member.user_id))
    await db_session.commit()
    assert (await db_session.get(User, source_member.user_id)).team_id == product.team_id
    await assert_browsing(test_client, product, project_id, visible=False)


@pytest.mark.parametrize("teamless", [False, True])
async def test_verified_platform_admin_can_browse_selected_tenant_without_local_membership(
    test_client, app, db_session, source_member, assigned_product, monkeypatch, teamless
):
    product, project_id = assigned_product
    if teamless:
        product.team_id = None
    db_session.add(Organization(id="admin-home", name="Administrator home"))
    db_session.add(User(id="global-admin", org_id="admin-home", team_id="admin-team", cognito_sub="global-sub", email="admin@example.test"))
    await set_platform_admin_grant(db_session, "global-sub", enabled=True)
    await db_session.commit()
    candidate = source_member.model_copy(update={"user_id": "global-sub", "is_admin": True, "auth_source": "jwt"})
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lambda subject, username: subject == "global-sub")

    async def verified_admin():
        return await apply_platform_admin_authority(candidate, db_session)

    app.dependency_overrides[get_current_user] = verified_admin
    await assert_browsing(test_client, product, project_id, visible=True)
    # Platform authority changes visibility, never the selected tenant boundary.
    candidate = candidate.model_copy(update={"org_id": "admin-home"})
    await assert_browsing(test_client, product, project_id, visible=False)


@pytest.mark.parametrize("authority", ["current_member", "token_claim_only", "revoked_platform_grant"])
async def test_teamless_product_is_not_visible_without_current_platform_authority(
    test_client, app, db_session, source_member, assigned_product, monkeypatch, authority
):
    product, project_id = assigned_product
    product.team_id = None
    await db_session.commit()
    candidate = source_member.model_copy(update={"is_admin": authority != "current_member", "auth_source": "jwt"})
    if authority == "revoked_platform_grant":
        await set_platform_admin_grant(db_session, candidate.user_id, enabled=False)
        await db_session.commit()
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lambda subject, username: authority == "revoked_platform_grant")

    async def verified_actor():
        return await apply_platform_admin_authority(candidate, db_session)

    app.dependency_overrides[get_current_user] = verified_actor
    await assert_browsing(test_client, product, project_id, visible=False)


@pytest.mark.parametrize("assignment", ["primary", "sole", "missing", "ambiguous"])
async def test_creation_assigns_authoritative_team_or_fails_atomically(test_client, db_session, source_member, assignment):
    await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == source_member.user_id))
    if assignment in {"sole", "primary", "ambiguous"}:
        await add_team_member(db_session, source_member, team_id="actual-team", primary=assignment == "primary")
    if assignment in {"primary", "ambiguous"}:
        await add_team_member(db_session, source_member, team_id="second-team", primary=False)
    await db_session.commit()
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    if assignment in {"missing", "ambiguous"}:
        assert created.status_code == 409, created.text
        assert created.json()["detail"]["code"] == "team_assignment_required"
        for model in (Product, Project, ProductOperation, ProductChange, ProductProjectBinding):
            assert await db_session.scalar(select(func.count()).select_from(model)) == 0
    else:
        assert created.status_code == 201, created.text
        assert created.json()["product"]["team_id"] == "actual-team"
        product = await db_session.get(Product, (source_member.org_id, created.json()["product_id"]))
        assert product.team_id == "actual-team"
        await assert_browsing(test_client, product, created.json()["project_id"], visible=True)

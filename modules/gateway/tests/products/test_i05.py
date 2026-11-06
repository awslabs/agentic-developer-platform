"""Dated Product binding behavior with independent registry readback."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductOperation, ProductProjectBinding, Project
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member, select_creation_team

REQUEST = {
    "name": "Planner",
    "purpose": "Plan releases",
    "intended_users": ["Maintainers"],
    "starting_point": "idea",
    "project": {"mode": "create"},
}


@pytest.fixture
async def owner(app, db_session, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Fixture workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="owner@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context


def headers(revision, key):
    return {"Idempotency-Key": key, "If-Profile-Revision": str(revision)}


@pytest.mark.asyncio
async def bind_close_history(test_client, db_session, app, owner, monkeypatch):
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: created_at)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_binding_fixture_01"})
    assert created.status_code == 201, created.text
    product_id = created.json()["product_id"]
    existing_id = created.json()["project_id"]
    new_id = "run-owned-project"
    configuration = {"basic": {"summary": "Keep configuration"}}
    db_session.add(Project(id=new_id, tenant_id=owner.org_id, name="Second scope", owner_user_id=owner.user_id, configuration=configuration))
    await db_session.commit()
    bound_at = datetime(2026, 1, 2, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: bound_at)
    path = f"/v1/products/{product_id}/projects"
    bound = await test_client.post(path, json={"project_id": new_id}, headers=headers(1, "bind_owned_project_01"))
    assert bound.status_code == 200, bound.text
    assert bound.json()["configuration"] == configuration and bound.json()["revision"] == 2
    binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.project_id == new_id))
    assert binding.valid_from.replace(tzinfo=UTC) == bound_at
    assert (await db_session.get(Project, (owner.org_id, new_id))).configuration == configuration
    assert (await test_client.get("/v1/products")).json()["items"][0]["project_ids"] == [existing_id, new_id]
    closed_at = datetime(2026, 1, 3, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: closed_at)
    closed = await test_client.delete(f"{path}/{new_id}", headers=headers(2, "close_owned_project_01"))
    assert closed.status_code == 200, closed.text
    assert closed.json()["project_id"] == existing_id and closed.json()["revision"] == 3
    await db_session.refresh(binding)
    assert binding.valid_to.replace(tzinfo=UTC) == closed_at
    assert (await db_session.get(Project, (owner.org_id, new_id))).configuration == configuration
    history = (await test_client.get(f"{path}/{new_id}/project-bindings")).json()["items"]
    assert [entry["revision"] for entry in history] == [2, 3]
    assert history[0]["relationship_versions"][0]["relationship_id"] == binding.id
    assert history[0]["relationship_versions"][0]["mapping_version"] == "profile-registry-1"
    assert history[0]["relationship_versions"][0]["valid_from"] == "2026-01-02T00:00:00Z"
    assert history[1]["relationship_versions"][0]["valid_to"] == "2026-01-03T00:00:00Z"
    changes = (
        await db_session.scalars(
            select(ProductChange)
            .where(
                ProductChange.product_id == product_id,
            )
            .order_by(ProductChange.revision)
        )
    ).all()
    assert len(changes) == 3
    assert [change.recorded_at.replace(tzinfo=UTC) for change in changes] == [created_at, bound_at, closed_at]
    assert changes[1].relationship_versions[0] == history[0]["relationship_versions"][0]
    assert (await test_client.get(f"{path}/{new_id}")).status_code == 404
    assert (await test_client.get("/v1/products")).json()["items"][0]["project_ids"] == [existing_id]
    foreign = owner.model_copy(update={"org_id": "other-fixture-tenant", "user_id": "other-fixture-owner"})
    db_session.add_all(
        [
            Organization(id=foreign.org_id, name="Other fixture workspace"),
            User(id=foreign.user_id, org_id=foreign.org_id, team_id="other-fixture-team", email="other-owner@example.test", user_kind="human"),
        ]
    )
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.get(f"{path}/{new_id}/project-bindings")).status_code == 404
    assert (await test_client.get("/v1/products")).json()["items"] == []


@pytest.mark.asyncio
async def test_authorized_project_discovery_and_edit(test_client, db_session, app, owner):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_project_discovery_01"})
    assert created.status_code == 201, created.text
    product_id = created.json()["product_id"]
    project_id = created.json()["project_id"]
    foreign_id = "another-member-project"
    db_session.add(Project(id=foreign_id, tenant_id=owner.org_id, name="Other scope", owner_user_id="another-member"))
    await db_session.commit()
    projects = await test_client.get("/v1/projects")
    assert projects.status_code == 200, projects.text
    assert [item["id"] for item in projects.json()["items"]] == [project_id]
    path = f"/v1/products/{product_id}/projects/{project_id}/configuration"
    updated = await test_client.patch(
        path, json={"configuration": {"governance": {"criticality": "low"}}}, headers=headers(1, "update_project_configuration_01")
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["configuration"] == {"governance": {"criticality": "low"}}
    assert (await db_session.get(Project, (owner.org_id, project_id))).configuration == updated.json()["configuration"]
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")).json()["revision"] == 2
    stranger = owner.model_copy(update={"user_id": "another-member"})
    db_session.add(User(id=stranger.user_id, org_id=owner.org_id, team_id="another-team", email="other@example.test", user_kind="human"))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: stranger
    assert [item["id"] for item in (await test_client.get("/v1/projects")).json()["items"]] == [foreign_id]
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}/project-bindings")).status_code == 404
    assert (await test_client.patch(path, json={"name": "No"}, headers=headers(2, "unauthorized_project_edit_01"))).status_code == 404


@pytest.mark.asyncio
async def binding_constraints(test_client, db_session, owner, monkeypatch):
    fixed_at = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: fixed_at)
    first = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_first_manager_01"})
    second = await test_client.post(
        "/v1/products", json={**REQUEST, "name": "Second manager"}, headers={"Idempotency-Key": "create_second_manager_01"}
    )
    assert first.status_code == second.status_code == 201
    first_id, first_project = first.json()["product_id"], first.json()["project_id"]
    second_id, second_project = second.json()["product_id"], second.json()["project_id"]
    other_tenant = "other-constraints-tenant"
    db_session.add_all(
        [
            Organization(id=other_tenant, name="Other constraints workspace"),
            User(
                id="other-constraints-owner",
                org_id=other_tenant,
                team_id="other-constraints-team",
                email="constraints-owner@example.test",
                user_kind="human",
            ),
            Project(
                id="other-constraints-project",
                tenant_id=other_tenant,
                name="Foreign",
                owner_user_id="other-constraints-owner",
                configuration={"source": "fixture-revision-1"},
            ),
        ]
    )
    await db_session.commit()
    before = [await db_session.scalar(select(func.count()).select_from(model)) for model in (ProductProjectBinding, ProductOperation, ProductChange)]
    wrong_scope = await test_client.post(
        f"/v1/products/{second_id}/projects", json={"project_id": "other-constraints-project"}, headers=headers(1, "foreign_tenant_binding_01")
    )
    assert wrong_scope.status_code == 404
    foreign = await test_client.post(
        f"/v1/products/{second_id}/projects", json={"project_id": first_project}, headers=headers(1, "bind_already_managed_01")
    )
    assert foreign.status_code == 409 and foreign.json()["detail"]["code"] == "binding_conflict"
    unchanged = await db_session.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.tenant_id == owner.org_id,
            ProductProjectBinding.project_id == first_project,
            ProductProjectBinding.valid_to.is_(None),
        )
    )
    assert unchanged.product_id == first_id
    assert (await db_session.get(Product, (owner.org_id, second_id))).revision == 1
    assert (await test_client.get(f"/v1/products/{second_id}/projects/{first_project}")).status_code == 404
    last = await test_client.delete(f"/v1/products/{first_id}/projects/{first_project}", headers=headers(1, "remove_last_scope_01"))
    assert last.status_code == 409 and last.json()["detail"]["code"] == "last_project"
    assert (await db_session.get(Product, (owner.org_id, first_id))).revision == 1
    assert unchanged.valid_to is None
    assert (await test_client.get(f"/v1/products/{first_id}/projects/{first_project}")).status_code == 200
    assert (await test_client.get(f"/v1/products/{second_id}/projects/{second_project}")).status_code == 200
    assert not (await db_session.scalars(select(ProductChange).where(ProductChange.revision > 1))).all()
    assert [
        await db_session.scalar(select(func.count()).select_from(model)) for model in (ProductProjectBinding, ProductOperation, ProductChange)
    ] == before
    bindings = (
        await db_session.scalars(
            select(ProductProjectBinding).where(
                ProductProjectBinding.tenant_id == owner.org_id,
            )
        )
    ).all()
    assert {binding.project_id for binding in bindings} == {first_project, second_project}
    assert all(binding.valid_from.replace(tzinfo=UTC) == fixed_at and binding.valid_to is None for binding in bindings)


@pytest.mark.asyncio
async def shared_team_permissions(test_client, db_session, app, owner, monkeypatch):
    from src.admin.team_memberships import remove_membership
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Team, TeamMembership

    fixed_at = datetime(2026, 3, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: fixed_at)
    await select_creation_team(db_session, owner, "initial-owner-team")
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_shared_team_01"})
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    second_id = "second-team-project"
    editor = owner.model_copy(update={"user_id": "editor-member-8248", "team_id": "editor-primary"})
    db_session.add_all(
        [
            Project(id=second_id, tenant_id=owner.org_id, name="Second", owner_user_id=owner.user_id),
            User(id=editor.user_id, org_id=owner.org_id, team_id=editor.team_id, email="editor@example.test", user_kind="human"),
            Team(id=editor.team_id, org_id=owner.org_id, department_id="test", name="Editor's primary"),
            Team(id="owner-only-team", org_id=owner.org_id, department_id="test", name="Owner-only team"),
            TenantMembership(id="owner-admin-8248", tenant_id=owner.org_id, user_id=owner.user_id, role="org_admin"),
            TenantMembership(id="editor-member-8248", tenant_id=owner.org_id, user_id=editor.user_id, role="member"),
        ]
    )
    await db_session.commit()
    db_session.add_all(
        [
            TeamMembership(user_id=editor.user_id, org_id=owner.org_id, team_id=editor.team_id, is_primary=True),
            TeamMembership(user_id=editor.user_id, org_id=owner.org_id, team_id=owner.team_id, is_primary=False),
        ]
    )
    await db_session.commit()
    await add_team_member(db_session, owner, team_id="owner-only-team", primary=False)
    await db_session.commit()
    bound = await test_client.post(f"/v1/products/{product_id}/projects", json={"project_id": second_id}, headers=headers(1, "bind_shared_scope_01"))
    assert bound.status_code == 200, bound.text
    first_path = f"/v1/products/{product_id}/projects/{first_id}"
    second_path = f"/v1/products/{product_id}/projects/{second_id}"
    mapped = await test_client.patch(first_path, json={"team_id": owner.team_id}, headers=headers(2, "map_shared_team_01"))
    assert mapped.status_code == 200, mapped.text
    app.dependency_overrides[get_current_user] = lambda: editor
    assert {item["id"] for item in (await test_client.get("/v1/projects")).json()["items"]} == {first_id, second_id}
    assert (await test_client.get(f"{second_path}/project-bindings")).status_code == 200
    for revision, path in ((3, first_path), (4, second_path)):
        assert (await test_client.get(path)).status_code == 200
        changed = await test_client.patch(
            f"{path}/configuration", json={"name": f"Edited at {revision}"}, headers=headers(revision, f"editor_project_{revision}_8248")
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["project"]["name"] == f"Edited at {revision}"
        retry = await test_client.patch(
            f"{path}/configuration",
            json={"name": f"Edited at {revision}"},
            headers=headers(revision, f"editor_project_{revision}_8248"),
        )
        assert retry.status_code == 200 and retry.json() == changed.json()
    stale = await test_client.patch(f"{second_path}/configuration", json={"name": "Stale update"}, headers=headers(4, "stale_editor_update_01"))
    assert stale.status_code == 412
    scoped_feed = await test_client.get(f"{second_path}/changes?limit=1")
    assert scoped_feed.status_code == 200 and scoped_feed.json()["next_cursor"]
    scoped_cursor = scoped_feed.json()["next_cursor"]
    assert (await test_client.get(f"{first_path}/changes?limit=1&cursor={scoped_cursor}")).status_code == 400
    with monkeypatch.context() as readback_clock:
        readback_clock.setattr("src.products.routes.utcnow", lambda: fixed_at + timedelta(days=1))
        assert (await test_client.get(f"{second_path}/revisions/5")).json()["project_id"] == second_id
        assert (await test_client.get(f"{first_path}/revisions/5")).status_code == 404
    denied = await test_client.patch(first_path, json={"team_id": "owner-only-team"}, headers=headers(5, "editor_team_denied_01"))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "permission_denied"
    assert (await db_session.get(Product, (owner.org_id, product_id))).team_id == owner.team_id
    app.dependency_overrides[get_current_user] = lambda: owner
    moved = await test_client.patch(first_path, json={"team_id": "owner-only-team"}, headers=headers(5, "owner_team_move_01"))
    assert moved.status_code == 200, moved.text
    app.dependency_overrides[get_current_user] = lambda: editor
    for path in (first_path, second_path):
        assert (await test_client.get(path)).status_code == 404
        assert (
            await test_client.patch(f"{path}/configuration", json={"name": "Denied"}, headers=headers(6, f"denied_{path.rsplit('/', 1)[-1]}_01"))
        ).status_code == 404
    assert (await test_client.get("/v1/projects")).json()["items"] == []
    app.dependency_overrides[get_current_user] = lambda: owner
    restored = await test_client.patch(first_path, json={"team_id": owner.team_id}, headers=headers(6, "owner_team_restore_01"))
    assert restored.status_code == 200, restored.text
    app.dependency_overrides[get_current_user] = lambda: editor
    assert (await test_client.get(second_path)).status_code == 200
    await remove_membership(db_session, user_id=editor.user_id, team_id=owner.team_id, org_id=owner.org_id)
    await db_session.commit()
    assert (
        await db_session.scalars(
            select(TeamMembership).where(
                TeamMembership.user_id == editor.user_id,
                TeamMembership.team_id == owner.team_id,
            )
        )
    ).all() == []
    before_denial = [await db_session.scalar(select(func.count()).select_from(model)) for model in (ProductOperation, ProductChange)]
    for path in (first_path, second_path):
        assert (await test_client.get(path)).status_code == 404
        assert (await test_client.get(f"{path}/project-bindings")).status_code == 404
        assert (await test_client.get(f"{path}/changes")).status_code == 404
        assert (await test_client.get(f"{path}/revisions/4")).status_code == 404
        assert (
            await test_client.patch(
                f"{path}/configuration", json={"name": "Still denied"}, headers=headers(7, f"revoked_{path.rsplit('/', 1)[-1]}_01")
            )
        ).status_code == 404
    assert (await test_client.get("/v1/projects")).json()["items"] == []
    assert (await test_client.get(f"{second_path}/changes?limit=1&cursor={scoped_cursor}")).status_code == 404
    assert (
        [await db_session.scalar(select(func.count()).select_from(model)) for model in (ProductOperation, ProductChange)] == before_denial == [7, 7]
    )
    product = await db_session.get(Product, (owner.org_id, product_id))
    await db_session.refresh(product)
    assert product.revision == 7 and product.team_id == owner.team_id
    assert (await db_session.get(Project, (owner.org_id, first_id))).name == "Edited at 3"
    assert (await db_session.get(Project, (owner.org_id, second_id))).name == "Edited at 4"


@pytest.mark.asyncio
async def test_discovery_uses_current_product_team_for_both_scopes(test_client, db_session, app, owner):
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Team

    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "discovery_owner_fixture_01"})
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    new_owner = owner.model_copy(update={"user_id": "replacement-owner", "team_id": "replacement-team"})
    db_session.add_all(
        [
            Project(id="second-owned-scope", tenant_id=owner.org_id, name="Second", owner_user_id=owner.user_id),
            Project(id="unbound-owned-scope", tenant_id=owner.org_id, name="Unbound", owner_user_id=owner.user_id),
            Team(id=new_owner.team_id, org_id=owner.org_id, department_id="test", name="Replacement team"),
            User(id=new_owner.user_id, org_id=owner.org_id, team_id=new_owner.team_id, email="replacement@example.test", user_kind="human"),
            TenantMembership(tenant_id=owner.org_id, user_id=owner.user_id, role="org_admin"),
            TenantMembership(tenant_id=owner.org_id, user_id=new_owner.user_id, role="member"),
        ]
    )
    await db_session.commit()
    await add_team_member(db_session, new_owner)
    await db_session.commit()
    path = f"/v1/products/{product_id}/projects"
    bound = await test_client.post(path, json={"project_id": "second-owned-scope"}, headers=headers(1, "discovery_bind_second_01"))
    assert bound.status_code == 200, bound.text
    reassigned = await test_client.patch(
        f"{path}/{first_id}",
        json={"owner_user_id": new_owner.user_id, "team_id": new_owner.team_id},
        headers=headers(2, "discovery_reassign_product_01"),
    )
    assert reassigned.status_code == 200, reassigned.text
    app.dependency_overrides[get_current_user] = lambda: new_owner
    for revision, project_id in enumerate((first_id, "second-owned-scope"), start=3):
        renamed = await test_client.patch(
            f"{path}/{project_id}/configuration",
            json={"name": f"Private scope {revision}"},
            headers=headers(revision, f"discovery_private_rename_{revision}"),
        )
        assert renamed.status_code == 200, renamed.text
    assert {item["id"] for item in (await test_client.get("/v1/projects")).json()["items"]} == {first_id, "second-owned-scope"}
    app.dependency_overrides[get_current_user] = lambda: owner
    for project_id in (first_id, "second-owned-scope"):
        assert (await test_client.get(f"{path}/{project_id}")).status_code == 404
        assert (await db_session.get(Project, (owner.org_id, project_id))).owner_user_id == owner.user_id
    assert (await test_client.get("/v1/products")).json()["items"] == []
    discovered = await test_client.get("/v1/projects")
    assert discovered.status_code == 200
    assert [item["id"] for item in discovered.json()["items"]] == ["unbound-owned-scope"]
    assert discovered.json()["items"][0]["product_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configuration",
    [
        {"basic": "not-an-object", "governance": {"criticality": "not-a-valid-level"}, "unknown_field": True},
        {"unknown_field": True},
        {"basic": {"summary": "Unknown nested field"}},
        {"basic": None},
        {"basic": {"repositories": ["not-a-record"]}},
        {"basic": {"user_mappings": [{"provider": "github"}]}},
        {"advanced": {"environments": {}}},
        {"financials": {"allocations": [{}]}},
        {"financials": {"release_target_date": "2026-02-30"}},
        {"governance": {"criticality": "urgent"}},
        {"governance": {"data_classification": "secret"}},
        {"governance": {"access_review_date": 20260101}},
        {"governance": {"cost_center": " "}},
        {"selected_capabilities": [{"capability": "Delivery", "ownership_mode": "owner"}]},
        {"selected_capabilities": [{"capability": "Delivery", "ownership_mode": "managed"}] * 2},
        {"selected_capabilities": [{"capability": str(index), "ownership_mode": "managed"} for index in range(51)]},
        {"applicability": [{"check_id": "review", "state": "unknown"}]},
    ],
)
async def test_invalid_project_configuration_is_atomic(test_client, db_session, owner, configuration):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "invalid_configuration_fixture"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    path = f"/v1/products/{product_id}/projects/{project_id}"
    before = (await test_client.get(path)).json()
    counts_before = [await db_session.scalar(select(func.count()).select_from(model)) for model in (ProductOperation, ProductChange)]
    rejected = await test_client.patch(
        f"{path}/configuration", json={"name": "Must not change", "configuration": configuration}, headers=headers(1, "invalid_configuration_patch")
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "validation_error"
    assert (await test_client.get(path)).json() == before
    project = await db_session.get(Project, (owner.org_id, project_id))
    await db_session.refresh(project)
    assert project.configuration == before["configuration"] and project.name == before["project"]["name"]
    assert [await db_session.scalar(select(func.count()).select_from(model)) for model in (ProductOperation, ProductChange)] == counts_before


@pytest.mark.asyncio
async def test_typed_project_configuration_roundtrip_and_replacement(test_client, db_session, owner):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "typed_configuration_fixture"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    path = f"/v1/products/{product_id}/projects/{project_id}"
    creation = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 1))
    target = {
        "tenant_id": owner.org_id,
        "project_id": project_id,
        "source_connection_id": "adp.profile",
        "record_type": "product",
        "record_id": product_id,
        "record_revision": creation.id,
    }
    reference = {
        "tenant_id": owner.org_id,
        "project_id": project_id,
        "source_connection_id": "fixture-source",
        "record_type": "document",
        "record_id": "fixture-record",
        "record_revision": "1",
    }
    configuration = {
        "basic": {
            "repositories": [reference],
            "user_mappings": [{"provider": "github", "provider_user_id": "fixture-member", "user_id": owner.user_id}],
        },
        "advanced": {"environments": [reference], "agent_policy_ref": reference, "skills": []},
        "financials": {
            "allocations": [
                {
                    "id": "fixture-allocation",
                    "name": "Test budget",
                    "target_ref": target,
                    "amount": {"amount": "2500.00", "currency": "USD"},
                    "category": "cloud",
                    "period_start": "2026-01-01",
                    "period_end": "2026-12-31",
                    "approval_status": "proposed",
                    "owner_ref": {"kind": "user", "id": owner.user_id},
                    "parent_allocation_ref": reference,
                }
            ],
            "release_target_date": "2026-06-30",
        },
        "selected_capabilities": [{"capability": "Delivery", "ownership_mode": "integrated", "configuration_ref": reference}],
        "governance": {"criticality": "low", "data_classification": "internal", "access_review_date": "2026-02-28"},
        "applicability": [{"check_id": "release-readiness", "state": "later", "reason": "No release scheduled"}],
    }
    for revision, replacement in enumerate((configuration, {"governance": {"criticality": "medium"}}, {}), start=1):
        request_headers = headers(revision, f"typed_configuration_update_{revision}")
        updated = await test_client.patch(f"{path}/configuration", json={"configuration": replacement}, headers=request_headers)
        assert updated.status_code == 200, updated.text
        assert updated.json()["configuration"] == replacement
        assert updated.json()["revision"] == revision + 1
        assert (await test_client.get(path)).json()["configuration"] == replacement
        replay = await test_client.patch(f"{path}/configuration", json={"configuration": replacement}, headers=request_headers)
        assert replay.status_code == 200 and replay.json() == updated.json()
        project = await db_session.get(Project, (owner.org_id, project_id))
        await db_session.refresh(project)
        assert project.configuration == replacement
    assert [await db_session.scalar(select(func.count()).select_from(model)) for model in (ProductOperation, ProductChange)] == [4, 4]


@pytest.mark.asyncio
async def test_new_scope_create_and_unlink_keep_configuration(test_client, db_session, owner):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_scope_host_001"})
    assert created.status_code == 201, created.text
    product_id = created.json()["product_id"]
    first_id = created.json()["project_id"]
    path = f"/v1/products/{product_id}/projects"
    request = {"mode": "create", "name": "Second scope"}
    new = await test_client.post(path, json=request, headers=headers(1, "add_new_scope_001"))
    assert new.status_code == 201, new.text
    second_id = new.json()["project_id"]
    assert second_id != first_id
    assert new.json()["product"]["name"] == created.json()["product"]["name"]
    assert {item["id"] for item in (await test_client.get("/v1/projects")).json()["items"]} == {first_id, second_id}
    replay = await test_client.post(path, json=request, headers=headers(1, "add_new_scope_001"))
    assert replay.status_code == 201 and replay.json()["project_id"] == second_id
    assert (await test_client.post(path, json={"mode": "create", "name": "Other"}, headers=headers(1, "add_new_scope_001"))).status_code == 409
    assert (await test_client.post(path, json={"mode": "create", "name": "Third"}, headers=headers(1, "add_stale_scope_001"))).status_code == 412
    assert (await test_client.post(path, json={"mode": "create"}, headers=headers(2, "add_missing_name_001"))).status_code == 422
    updated = await test_client.patch(
        f"{path}/{second_id}/configuration",
        json={"configuration": {"governance": {"criticality": "low"}}},
        headers=headers(2, "configure_second_scope_001"),
    )
    assert updated.status_code == 200, updated.text
    unlinked = await test_client.delete(f"{path}/{second_id}", headers=headers(3, "unlink_second_scope_001"))
    assert unlinked.status_code == 200 and unlinked.json()["project_id"] == first_id
    assert (await test_client.delete(f"{path}/{first_id}", headers=headers(4, "unlink_last_scope_001"))).status_code == 409
    assert (await test_client.get(f"{path}/{second_id}")).status_code == 404
    assert (await test_client.get(f"{path}/{first_id}")).json()["product"]["name"] == created.json()["product"]["name"]
    saved = await db_session.get(Project, (owner.org_id, second_id))
    assert saved is not None and saved.configuration == {"governance": {"criticality": "low"}}
    binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.project_id == second_id))
    assert binding.valid_to is not None


@pytest.mark.asyncio
async def test_archived_owned_project_is_not_discoverable_after_fixture_retirement(test_client, db_session, owner):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "archive_owned_scope_12345"})
    assert created.status_code == 201, created.text
    project_id = created.json()["project_id"]
    orphan = Project(id="retired-owned-second", tenant_id=owner.org_id, name="Owned second", owner_user_id=owner.user_id)
    db_session.add(orphan)
    await db_session.commit()
    assert {item["id"] for item in (await test_client.get("/v1/projects")).json()["items"]} == {project_id, orphan.id}
    first = await db_session.get(Project, (owner.org_id, project_id))
    for project in (first, orphan):
        project.archived_at = datetime.now(UTC)
        project.state = "archived"
    await db_session.commit()
    response = await test_client.get("/v1/projects")
    assert response.status_code == 200 and response.json()["items"] == []

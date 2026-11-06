"""Delivery-scope behavior through the gateway's authenticated Product routes."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductProjectBinding, Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from tests.products.membership import add_team_member, select_creation_team
from tests.products.test_i04 import GatewayClient, cli


@pytest.fixture
async def scope_owner(app, db_session, valid_token_context):
    owner = valid_token_context
    db_session.add_all(
        [
            Organization(id=owner.org_id, name="Fixture workspace"),
            User(id=owner.user_id, org_id=owner.org_id, team_id=owner.team_id, email="scope-owner@example.test", user_kind="human"),
        ]
    )
    await add_team_member(db_session, owner)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: owner
    return owner


@pytest.mark.asyncio
async def scope_admin_states(test_client, db_session, app, scope_owner):
    owner = scope_owner
    await select_creation_team(db_session, owner, "initial-owner-team")
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Planner",
            "purpose": "Organize releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "create_admin_scope_fixture_01"},
    )
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    editor = owner.model_copy(update={"user_id": "scope-editor", "team_id": "editor-primary"})
    db_session.add_all(
        [
            User(id=editor.user_id, org_id=owner.org_id, team_id=editor.team_id, email="scope-editor@example.test", user_kind="human"),
            Team(id=editor.team_id, org_id=owner.org_id, department_id="test", name="Editor's team"),
            TenantMembership(id="scope-owner-admin", tenant_id=owner.org_id, user_id=owner.user_id, role="org_admin"),
            TenantMembership(id="scope-editor-member", tenant_id=owner.org_id, user_id=editor.user_id, role="member"),
            Project(id="editor-unbound", tenant_id=owner.org_id, name="Editor Project", owner_user_id=editor.user_id),
            Project(id="private-a", tenant_id=owner.org_id, name="Restricted One", owner_user_id="private-owner"),
            Project(id="private-b", tenant_id=owner.org_id, name="Restricted Two", owner_user_id="private-owner"),
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
    mapped = await test_client.patch(
        f"/v1/products/{product_id}/projects/{first_id}",
        json={"team_id": owner.team_id},
        headers={"Idempotency-Key": "map_admin_scope_fixture_01", "If-Profile-Revision": "1"},
    )
    assert mapped.status_code == 200, mapped.text
    app.dependency_overrides[get_current_user] = lambda: editor
    visible = (await test_client.get("/v1/products")).json()
    assert set(visible) == {"items", "next_cursor"} and len(visible["items"]) == 1
    assert visible["next_cursor"] is None and visible["items"][0]["project_ids"] == [first_id]
    cursor = None
    pages = []
    while True:
        response = await test_client.get("/v1/projects", params={"limit": 1, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        page = response.json()
        assert set(page) == {"items", "next_cursor"}
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(pages) < 3
    assert len(pages) == 2
    assert pages[0]["next_cursor"] == pages[0]["items"][0]["id"] and pages[1]["next_cursor"] is None
    assert {item["id"] for page in pages for item in page["items"]} == {first_id, "editor-unbound"}
    assert all("Restricted" not in str(page) and "private-" not in str(page) for page in pages)
    denied = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "select", "project_id": "private-a"},
        headers={"Idempotency-Key": "deny_private_scope_bind_01", "If-Profile-Revision": "2"},
    )
    assert denied.status_code == 404 and "private-a" not in denied.text
    denied_create = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Private new scope"},
        headers={"Idempotency-Key": "deny_editor_scope_create_01", "If-Profile-Revision": "2"},
    )
    assert denied_create.status_code == 404 and "Private new scope" not in denied_create.text
    bindings = (
        await db_session.scalars(
            select(ProductProjectBinding).where(
                ProductProjectBinding.product_id == product_id,
                ProductProjectBinding.valid_to.is_(None),
            )
        )
    ).all()
    assert len(bindings) == 1 and bindings[0].project_id == first_id
    assert (await test_client.get("/v1/products")).json()["items"][0]["project_ids"] == [first_id]
    edited = await test_client.patch(
        f"/v1/products/{product_id}/projects/{first_id}",
        json={"purpose": "Editor can still work"},
        headers={"Idempotency-Key": "editor_common_scope_edit_01", "If-Profile-Revision": "2"},
    )
    assert edited.status_code == 200 and edited.json()["product"]["purpose"] == "Editor can still work"
    product = await db_session.get(Product, (owner.org_id, product_id))
    assert product.team_id == owner.team_id and product.owner_user_id == owner.user_id


@pytest.mark.asyncio
async def scope_switch(test_client, db_session, scope_owner, monkeypatch):
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: observed_at)
    owner = scope_owner
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Planner",
            "purpose": "Coordinate releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "create_scope_switch_001"},
    )
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    before = await test_client.get(f"/v1/products/{product_id}/projects/{first_id}")
    assert before.status_code == 200 and before.json()["configuration"] == {}
    assert (await test_client.get("/v1/products")).json()["items"][0]["project_ids"] == [first_id]
    second = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Second scope"},
        headers={"Idempotency-Key": "create_second_scope_switch_001", "If-Profile-Revision": "1"},
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["project_id"]
    assert second_id != first_id
    before_second_configuration = (await test_client.get(f"/v1/products/{product_id}/projects/{first_id}")).json()
    assert before_second_configuration["project_id"] == first_id
    assert before_second_configuration["configuration"] == {}
    second_configuration = {
        "basic": {
            "repositories": [
                {
                    "tenant_id": owner.org_id,
                    "project_id": second_id,
                    "source_connection_id": "fixture-connection-two",
                    "record_type": "repository",
                    "record_id": "fixture-repository-two",
                    "record_revision": "source-etag:2",
                }
            ]
        },
        "financials": {"release_target_date": "2026-12-01"},
        "governance": {"criticality": "low"},
    }
    configured = await test_client.patch(
        f"/v1/products/{product_id}/projects/{second_id}/configuration",
        json={"configuration": second_configuration},
        headers={"Idempotency-Key": "configure_second_switch_001", "If-Profile-Revision": "2"},
    )
    assert configured.status_code == 200, configured.text
    first_read = (await test_client.get(f"/v1/products/{product_id}/projects/{first_id}")).json()
    second_read = (await test_client.get(f"/v1/products/{product_id}/projects/{second_id}")).json()
    assert {first_read["project_id"], second_read["project_id"]} == {first_id, second_id}
    assert first_read["product_id"] == second_read["product_id"] == product_id
    assert first_read["tenant_id"] == second_read["tenant_id"] == owner.org_id
    assert first_read["project"]["id"] == first_id
    assert second_read["project"]["id"] == second_id
    assert first_read["product"] == second_read["product"]
    assert first_read["product"]["name"] == "Planner"
    assert first_read["product"]["owner_ref"] == {"kind": "user", "id": owner.user_id}
    assert first_read["configuration"] == before_second_configuration["configuration"] == {}
    assert first_read["progress"] == before_second_configuration["progress"]
    assert [check["id"] for check in first_read["progress"]["checks"]] == ["information", "project_scope"]
    assert first_read["project"]["name"] == "Planner"
    assert second_read["configuration"] == second_configuration
    assert second_read["configuration"]["basic"]["repositories"][0]["project_id"] == second_id
    assert [check["id"] for check in second_read["progress"]["checks"]] == [
        "information",
        "project_scope",
        "repository",
        "financials",
    ]
    assert first_read["configuration"] != second_read["configuration"]
    assert first_read["progress"] != second_read["progress"]
    assert second_read["project"]["name"] == "Second scope"
    assert first_read["revision"] == second_read["revision"] == 3
    listed = (await test_client.get("/v1/products")).json()["items"]
    assert len(listed) == 1 and set(listed[0]["project_ids"]) == {first_id, second_id}
    first_project = await db_session.get(Project, (owner.org_id, first_id))
    second_project = await db_session.get(Project, (owner.org_id, second_id))
    product = await db_session.get(Product, (owner.org_id, product_id))
    binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.project_id == second_id))
    changes = (await db_session.scalars(select(ProductChange).where(ProductChange.product_id == product_id))).all()
    assert first_project.configuration == {} and second_project.configuration == second_read["configuration"]
    assert product.name == "Planner" and product.owner_user_id == owner.user_id
    assert binding.valid_from.replace(tzinfo=UTC) == observed_at and binding.valid_to is None
    assert {change.revision for change in changes} == {1, 2, 3}
    db_session.add_all(
        [
            TenantMembership(id="scope-switch-owner-admin", tenant_id=owner.org_id, user_id=owner.user_id, role="org_admin"),
        ]
    )
    await db_session.commit()
    mapped = await test_client.patch(
        f"/v1/products/{product_id}/projects/{first_id}",
        json={"team_id": owner.team_id},
        headers={"Idempotency-Key": "map_shared_team_scope_switch", "If-Profile-Revision": "3"},
    )
    assert mapped.status_code == 200, mapped.text
    first_after = (await test_client.get(f"/v1/products/{product_id}/projects/{first_id}")).json()
    second_after = (await test_client.get(f"/v1/products/{product_id}/projects/{second_id}")).json()
    await db_session.refresh(product)
    assert product.team_id == owner.team_id
    assert first_after["product"] == second_after["product"] == mapped.json()["product"]
    assert first_after["configuration"] == {} and second_after["configuration"] == second_read["configuration"]
    assert first_after["project_id"] == first_id and second_after["project_id"] == second_id


@pytest.mark.asyncio
async def scope_drafts_and_links(test_client, db_session, scope_owner, tmp_path, monkeypatch):
    observed_at = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: observed_at)
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Planner",
            "purpose": "Coordinate releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "create_draft_link_product_01"},
    )
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    existing_id = "existing-delivery-project"
    configuration = {"governance": {"criticality": "low"}}
    db_session.add(
        Project(
            id=existing_id,
            tenant_id=scope_owner.org_id,
            name="Existing scope",
            owner_user_id=scope_owner.user_id,
            configuration=configuration,
        )
    )
    await db_session.commit()
    client = GatewayClient(test_client, asyncio.get_running_loop())

    async def invoke(*arguments):
        parsed = cli.parser().parse_args(arguments)
        return (await asyncio.to_thread(cli.execute, parsed, client))["detail"]

    authorized = await invoke("project", "list")
    assert {item["id"] for item in authorized["items"]} == {first_id, existing_id}
    request = tmp_path / "project-selection.json"
    request.write_text(json.dumps({"mode": "select", "project_id": existing_id}), encoding="utf-8")
    arguments = (
        "project",
        "bind",
        "--product",
        product_id,
        "--input",
        str(request),
        "--revision",
        "1",
        "--idempotency-key",
        "link_existing_delivery_01",
    )
    linked = await invoke(*arguments)
    replay = await invoke(*arguments)
    assert linked["project_id"] == replay["project_id"] == existing_id
    assert linked["revision"] == replay["revision"] == 2
    assert linked["configuration"] == configuration
    scoped = (await test_client.get(f"/v1/products/{product_id}/projects/{existing_id}")).json()
    assert scoped["project_id"] == existing_id and scoped["configuration"] == configuration
    first = (await test_client.get(f"/v1/products/{product_id}/projects/{first_id}")).json()
    assert first["configuration"] == {} and first["product"] == scoped["product"]
    history = await invoke("project", "bindings", "--product", product_id, "--project", existing_id)
    observed = (await test_client.get(f"/v1/products/{product_id}/projects/{existing_id}/project-bindings")).json()
    assert history == observed
    version = history["items"][0]["relationship_versions"][0]
    binding_id = version["relationship_id"]
    assert version["to_ref"]["record_id"] == version["to_ref"]["project_id"] == existing_id
    assert version["from_ref"]["record_id"] == product_id
    binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.project_id == existing_id))
    assert binding.id == binding_id and binding.valid_from.replace(tzinfo=UTC) == observed_at
    assert binding.valid_to is None
    monkeypatch.setattr("src.products.routes.utcnow", lambda: datetime(2026, 2, 2, tzinfo=UTC))
    remaining = await invoke(
        "project",
        "unbind",
        "--product",
        product_id,
        "--project",
        existing_id,
        "--yes",
        "--revision",
        "2",
        "--idempotency-key",
        "unlink_existing_delivery_01",
    )
    assert remaining["project_id"] == first_id and remaining["revision"] == 3
    history_after = await invoke("project", "bindings", "--product", product_id, "--project", existing_id)
    assert history_after["items"][-1]["relationship_versions"][0]["relationship_id"] == binding_id
    assert history_after["items"][-1]["relationship_versions"][0]["valid_to"]
    await db_session.refresh(binding)
    assert binding.valid_to is not None and binding.valid_to > binding.valid_from
    preserved = await db_session.get(Project, (scope_owner.org_id, existing_id))
    assert preserved.configuration == configuration
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{existing_id}")).status_code == 404
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{first_id}")).json()["product"]["name"] == "Planner"
    assert (await test_client.get("/v1/products")).json()["items"][0]["project_ids"] == [first_id]
    assert version["valid_from"] == "2026-02-01T00:00:00Z" and "valid_to" not in version
    assert [item["revision"] for item in history_after["items"]] == [2, 3]
    closed = history_after["items"][1]["relationship_versions"][0]
    assert (closed["relationship_id"], closed["relationship_revision"], closed["supersedes_revision"]) == (binding_id, "2", "1")
    assert (closed["valid_from"], closed["valid_to"]) == ("2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z")
    stale_rebind = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "select", "project_id": existing_id},
        headers={"Idempotency-Key": "stale_rebind_existing_01", "If-Profile-Revision": "2"},
    )
    assert stale_rebind.status_code == 412
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{existing_id}/project-bindings")).json() == history_after
    assert (await db_session.get(Project, (scope_owner.org_id, existing_id))).configuration == configuration
    monkeypatch.setattr("src.products.routes.utcnow", lambda: datetime(2026, 2, 3, tzinfo=UTC))
    rebound = await invoke(
        "project",
        "bind",
        "--product",
        product_id,
        "--input",
        str(request),
        "--revision",
        "3",
        "--idempotency-key",
        "rebind_existing_delivery_01",
    )
    assert rebound["revision"] == 4 and rebound["project_id"] == existing_id
    assert rebound["configuration"] == configuration and rebound["product"] == scoped["product"]
    renewed = await invoke("project", "bindings", "--product", product_id, "--project", existing_id)
    assert renewed == (await test_client.get(f"/v1/products/{product_id}/projects/{existing_id}/project-bindings")).json()
    assert renewed["items"][:2] == history_after["items"]
    assert [item["revision"] for item in renewed["items"]] == [2, 3, 4]
    latest = renewed["items"][2]["relationship_versions"][0]
    assert latest["relationship_id"] != binding_id and latest["relationship_revision"] == "1"
    assert latest["valid_from"] == "2026-02-03T00:00:00Z" and "valid_to" not in latest
    saved = await db_session.get(Project, (scope_owner.org_id, existing_id))
    assert saved is preserved and saved.configuration == configuration


@pytest.mark.asyncio
async def scope_denials(test_client, db_session, app, scope_owner):
    owner = scope_owner
    await select_creation_team(db_session, owner, "initial-owner-team")
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Shared Planner",
            "purpose": "Plan releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "scope_denials_owned_create_01"},
    )
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    second = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Second scope"},
        headers={"Idempotency-Key": "scope_denials_second_create_01", "If-Profile-Revision": "1"},
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["project_id"]
    editor = owner.model_copy(update={"user_id": "scope-denials-editor", "team_id": "editor-primary"})
    db_session.add_all(
        [
            User(id=editor.user_id, org_id=owner.org_id, team_id=editor.team_id, email="scope-denials-editor@example.test", user_kind="human"),
            Team(id=editor.team_id, org_id=owner.org_id, department_id="test", name="Editor's team"),
            TenantMembership(id="scope-denials-owner-role", tenant_id=owner.org_id, user_id=owner.user_id, role="org_admin"),
            TenantMembership(id="scope-denials-editor-role", tenant_id=owner.org_id, user_id=editor.user_id, role="member"),
            Project(id="scope-denials-private", tenant_id=owner.org_id, name="Foreign private scope", owner_user_id="other-owner"),
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
    mapped = await test_client.patch(
        f"/v1/products/{product_id}/projects/{first_id}",
        json={"team_id": owner.team_id},
        headers={"Idempotency-Key": "scope_denials_share_team_01", "If-Profile-Revision": "2"},
    )
    assert mapped.status_code == 200, mapped.text
    baseline = {
        project_id: (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")).json() for project_id in (first_id, second_id)
    }
    baseline_changes = (
        await db_session.scalars(select(ProductChange.revision).where(ProductChange.product_id == product_id).order_by(ProductChange.revision))
    ).all()
    app.dependency_overrides[get_current_user] = lambda: editor
    denied_admin = await test_client.patch(
        f"/v1/products/{product_id}/projects/{first_id}",
        json={"team_id": editor.team_id},
        headers={"Idempotency-Key": "scope_denials_admin_transfer_01", "If-Profile-Revision": "3"},
    )
    assert denied_admin.status_code == 403
    denied = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "select", "project_id": "scope-denials-private"},
        headers={"Idempotency-Key": "scope_denials_foreign_bind_01", "If-Profile-Revision": "3"},
    )
    assert denied.status_code == 404 and "scope-denials-private" not in denied.text
    app.dependency_overrides[get_current_user] = lambda: owner
    for project_id, original in baseline.items():
        response = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
        assert response.status_code == 200 and response.json() == original
    product = await db_session.get(Product, (owner.org_id, product_id))
    assert (product.revision, product.owner_user_id, product.team_id) == (3, owner.user_id, owner.team_id)
    assert (
        await db_session.scalars(select(ProductChange.revision).where(ProductChange.product_id == product_id).order_by(ProductChange.revision))
    ).all() == baseline_changes
    private_project = await db_session.get(Project, (owner.org_id, "scope-denials-private"))
    assert private_project.owner_user_id == "other-owner"
    assert set(
        (
            await db_session.scalars(
                select(ProductProjectBinding.project_id).where(
                    ProductProjectBinding.product_id == product_id, ProductProjectBinding.valid_to.is_(None)
                )
            )
        ).all()
    ) == {first_id, second_id}
    app.dependency_overrides[get_current_user] = lambda: editor
    approved = await test_client.patch(
        f"/v1/products/{product_id}/projects/{second_id}",
        json={"purpose": "Editor update"},
        headers={"Idempotency-Key": "scope_denials_editor_update_01", "If-Profile-Revision": "3"},
    )
    assert approved.status_code == 200 and approved.json()["product"]["purpose"] == "Editor update"
    app.dependency_overrides[get_current_user] = lambda: owner
    approved_baseline = {
        project_id: (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")).json() for project_id in (first_id, second_id)
    }
    assert all(profile["product"]["purpose"] == "Editor update" for profile in approved_baseline.values())
    app.dependency_overrides[get_current_user] = lambda: editor
    membership = await db_session.get(TenantMembership, "scope-denials-editor-role")
    membership.revoked_at = datetime(2026, 2, 1, tzinfo=UTC)
    await db_session.commit()
    for project_id in (first_id, second_id):
        denied_read = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
        assert denied_read.status_code == 404 and "Editor update" not in denied_read.text
        for history_route in ("changes", "project-bindings"):
            denied_history = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}/{history_route}")
            assert denied_history.status_code in (403, 404) and "Editor update" not in denied_history.text
            assert first_id not in denied_history.text and second_id not in denied_history.text
        denied_update = await test_client.patch(
            f"/v1/products/{product_id}/projects/{project_id}",
            json={"purpose": "Revoked write"},
            headers={"Idempotency-Key": f"scope_denials_revoked_{project_id}", "If-Profile-Revision": "4"},
        )
        assert denied_update.status_code == 404 and "Editor update" not in denied_update.text
    assert (await test_client.get("/v1/products")).status_code == 403
    assert (await test_client.get("/v1/projects")).status_code == 403
    app.dependency_overrides[get_current_user] = lambda: owner
    listed = await test_client.get("/v1/products")
    assert listed.status_code == 200 and set(listed.json()["items"][0]["project_ids"]) == {first_id, second_id}
    for project_id, original in approved_baseline.items():
        response = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
        assert response.status_code == 200 and response.json() == original
    await db_session.refresh(product)
    assert (product.revision, product.owner_user_id, product.team_id) == (4, owner.user_id, owner.team_id)
    assert (
        await db_session.scalars(select(ProductChange.revision).where(ProductChange.product_id == product_id).order_by(ProductChange.revision))
    ).all() == [*baseline_changes, 4]

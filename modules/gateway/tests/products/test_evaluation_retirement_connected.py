# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Exercise the registered owning-service route and real IAM authorization chain."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from src.products.models import Product, ProductChange
from src.shared.models.organization import Organization, Team, User
from tests.products.test_evaluation_retirement import HEADERS, PATH
from tests.products.test_evaluation_retirement import evaluator as evaluator
from tests.products.test_evaluation_retirement import owned as owned


async def connect_owned_fixture(db, manifest):
    tenant = await db.get(Organization, manifest["tenant_id"])
    tenant.name = "Connected customer workspace"
    owner = await db.get(User, manifest["actor_id"])
    owner.role = "member"
    owner.cognito_username = "eval-regression-owner"
    owner.cognito_sub = "owned-subject"
    owner.team_id = "evaluation-team"
    db.add_all(
        [
            Team(id="evaluation-team", org_id=manifest["tenant_id"], department_id="evaluation-department", name="Evaluation team"),
            User(
                id="evaluation-editor",
                org_id=manifest["tenant_id"],
                team_id="evaluation-team",
                email="editor@example.test",
                role="member",
                user_kind="human",
                cognito_username="eval-regression-editor",
                cognito_sub="editor-subject",
            ),
        ]
    )
    await db.commit()
    manifest.update(scope_fixture_mode="connected_scope", fixture_editor_id="evaluation-editor")


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("owned", ["-v11_idea", "-v11_existing_assets", "-v11_running_product", "-v11_resume", "-v11_release"], indirect=True)
async def test_workspace_retirement_preserves_real_edits_and_session(test_client, db_session, owned, connected):
    manifest, initial, other = owned
    if connected:
        await connect_owned_fixture(db_session, manifest)
    path = f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
    suffix = initial["product"]["name"].split("-v11_", 1)[1]
    revision = initial["revision"]
    if suffix in ("existing_assets", "running_product"):
        updated = await test_client.patch(
            path,
            json={"starting_point": suffix},
            headers={
                "Idempotency-Key": "workspace_starting_point_123",
                "If-Profile-Revision": str(revision),
            },
        )
        assert updated.status_code == 200, updated.text
        revision = updated.json()["revision"]
    started = await test_client.post(
        path + "/onboarding-sessions",
        json={},
        headers={
            "Idempotency-Key": "workspace_start_session_123",
            "If-Profile-Revision": str(revision),
        },
    )
    assert started.status_code == 201, started.text
    for width in (390, 1440):
        updated = await test_client.patch(
            path,
            json={"purpose": f"Workspace edit at {width}px"},
            headers={
                "Idempotency-Key": f"workspace_edit_{width}_123",
                "If-Profile-Revision": str(revision),
            },
        )
        assert updated.status_code == 200, updated.text
        revision = updated.json()["revision"]
    history = {
        c.id: deepcopy(c.snapshot) for c in await db_session.scalars(select(ProductChange).where(ProductChange.product_id == manifest["product_id"]))
    }
    await db_session.rollback()
    retired = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert retired.status_code == 200, retired.text
    assert retired.json()["fixture_removed"] is True
    assert (await test_client.post(PATH, json=manifest, headers=HEADERS)).json() == retired.json()
    for target in (path, path + "/changes", path + "/onboarding-sessions/" + started.json()["id"]):
        assert (await test_client.get(target)).status_code == 404
    assert (await test_client.get(f"/v1/products/{other['product_id']}/projects/{other['project_id']}")).json() == other
    for key, snapshot in history.items():
        assert (await db_session.get(ProductChange, (manifest["tenant_id"], key))).snapshot == snapshot
    if connected:
        assert (await db_session.get(User, "evaluation-editor")).team_id == "evaluation-team"
        assert (await db_session.get(Organization, manifest["tenant_id"])).name == "Connected customer workspace"


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", ["-v11_resume"], indirect=True)
@pytest.mark.parametrize("fault", ["missing_editor", "admin_editor", "other_team", "wrong_owner", "other_scope", "wrong_mode"])
async def test_single_project_connected_retirement_refuses_unowned_identity(test_client, db_session, owned, fault):
    manifest, _, other = owned
    await connect_owned_fixture(db_session, manifest)
    if fault == "missing_editor":
        manifest.pop("fixture_editor_id")
    elif fault == "admin_editor":
        (await db_session.get(User, "evaluation-editor")).role = "org_admin"
    elif fault == "other_team":
        (await db_session.get(User, "evaluation-editor")).team_id = "another-team"
    elif fault == "wrong_owner":
        (await db_session.get(User, manifest["actor_id"])).cognito_username = "customer-owner"
    elif fault == "other_scope":
        manifest["scope"] = {"editor_id": "evaluation-editor", "project_id": other["project_id"]}
    else:
        manifest.pop("scope_fixture_mode")
    await db_session.commit()
    response = await test_client.post(PATH, json=manifest, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert (await db_session.get(Product, (manifest["tenant_id"], manifest["product_id"]))).archived_at is None

"""Saved onboarding API uses scoped readback, optimistic revisions and receipts."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductOnboardingSession, ProductOperation, Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.fixture
async def member(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Test workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="member@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context


def mutation_headers(revision, key):
    return {"If-Profile-Revision": str(revision), "Idempotency-Key": key}


@pytest.mark.asyncio
async def test_onboarding_shared_edits_preserve_scoped_history(test_client, db_session, member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, first_project = created.json()["product_id"], created.json()["project_id"]
    second_project = "onboarding-second-project"
    unrelated_project = "onboarding-unrelated-project"
    db_session.add_all(
        [
            Project(id=project_id, tenant_id=member.org_id, name=project_id, owner_user_id=member.user_id)
            for project_id in (second_project, unrelated_project)
        ]
    )
    await db_session.commit()
    bound = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"project_id": second_project},
        headers=mutation_headers(1, "onboarding_history_bind_01"),
    )
    assert bound.status_code == 200, bound.text
    first_path = f"/v1/products/{product_id}/projects/{first_project}"
    second_path = f"/v1/products/{product_id}/projects/{second_project}"
    unrelated_path = f"/v1/products/{product_id}/projects/{unrelated_project}"
    started = await test_client.post(f"{first_path}/onboarding-sessions", json={}, headers=mutation_headers(2, "onboarding_history_start_01"))
    assert started.status_code == 201, started.text
    session_path = f"{first_path}/onboarding-sessions/{started.json()['id']}"
    edited = await test_client.put(
        f"{session_path}/steps/information",
        json={"action": "save", "product": {"purpose": "Shared onboarding edit"}},
        headers=mutation_headers(2, "onboarding_history_edit_01"),
    )
    assert edited.status_code == 200, edited.text
    for path, project_id, other_project in ((second_path, second_project, first_project), (first_path, first_project, second_project)):
        historical = await test_client.get(f"{path}/revisions/3")
        assert historical.status_code == 200, historical.text
        assert historical.json()["project_id"] == project_id
        assert historical.json()["product"]["purpose"] == "Shared onboarding edit"
        assert other_project not in historical.text
        feed = await test_client.get(f"{path}/changes")
        assert feed.status_code == 200, feed.text
        shared_edit = next(change for change in feed.json()["items"] if change["revision"] == 3)
        assert shared_edit["changed_paths"] == ["/product/purpose"]
        assert other_project not in feed.text
    assert (await test_client.get(f"{unrelated_path}/revisions/3")).status_code == 404
    assert (await test_client.get(f"{unrelated_path}/changes")).json()["items"] == []
    deferred = await test_client.put(
        f"{session_path}/steps/financials",
        json={"action": "later"},
        headers=mutation_headers(3, "onboarding_history_later_01"),
    )
    assert deferred.status_code == 200, deferred.text
    local_revision = await test_client.get(f"{first_path}/revisions/4")
    assert local_revision.status_code == 200, local_revision.text
    assert local_revision.json()["configuration"]["applicability"][0]["state"] == "later"
    local_feed = (await test_client.get(f"{first_path}/changes")).json()["items"]
    assert local_feed[-1]["changed_paths"] == ["/project/configuration"]
    assert (await test_client.get(f"{second_path}/revisions/4")).status_code == 404
    assert [change["revision"] for change in (await test_client.get(f"{second_path}/changes")).json()["items"]] == [2, 3]
    for path in (first_path, second_path):
        historical = await test_client.get(f"{path}/revisions/3")
        assert historical.status_code == 200 and historical.json()["configuration"] == {}
    db_session.expire_all()
    shared_change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 3))
    assert shared_change.project_id is None
    assert set(shared_change.snapshot["scope_snapshots"]) == {first_project, second_project}
    local_change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 4))
    assert local_change.project_id == first_project
    assert "scope_snapshots" not in local_change.snapshot


@pytest.mark.asyncio
async def test_start_read_update_step_and_replay(test_client, db_session, member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    first = await test_client.post(root, json={"mode": "direct"}, headers=mutation_headers(1, "start_session_key_123"))
    assert first.status_code == 201, first.text
    session_id = first.json()["id"]
    assert first.headers["Profile-Revision"] == "1" and first.headers["Cache-Control"] == "no-store"
    assert first.json()["steps"]["financials"]["status"] == "not_started"
    assert (await test_client.get(f"{root}/{session_id}")).json() == first.json()
    replay = await test_client.post(root, json={"mode": "direct"}, headers=mutation_headers(1, "start_session_key_123"))
    assert replay.status_code == 200 and replay.json() == first.json()
    resumed = await test_client.post(root, json={"mode": "direct"}, headers=mutation_headers(1, "second_start_key_123"))
    assert resumed.status_code == 200 and resumed.json()["id"] == session_id
    assert (await test_client.post(root, json={"mode": "guided"}, headers=mutation_headers(1, "start_session_key_123"))).status_code == 409
    path = f"{root}/{session_id}/steps/information"
    assert (await test_client.put(path, json={"action": "save", "product": {"purpose": "Updated idea"}})).status_code == 428
    change = await test_client.put(
        path, json={"action": "save", "product": {"purpose": "Updated idea"}}, headers=mutation_headers(1, "update_info_key_123")
    )
    assert change.status_code == 200, change.text
    assert change.json()["revision"] == 2 and change.headers["Profile-Revision"] == "2"
    assert change.json()["steps"]["information"]["status"] == "complete"
    assert (await test_client.get(f"{root}/{session_id}")).json() == change.json()
    same = await test_client.put(
        path, json={"action": "save", "product": {"purpose": "Updated idea"}}, headers=mutation_headers(1, "update_info_key_123")
    )
    assert same.status_code == 200 and same.json() == change.json()
    stale = await test_client.put(
        path, json={"action": "save", "product": {"purpose": "Old version"}}, headers=mutation_headers(1, "new_update_key_123")
    )
    assert stale.status_code == 412 and stale.json()["detail"]["code"] == "stale_revision"
    mismatched = await test_client.put(path, json={"action": "later"}, headers=mutation_headers(1, "update_info_key_123"))
    assert mismatched.status_code == 409
    db_session.expire_all()
    assert (await db_session.get(Product, (member.org_id, product_id))).purpose == "Updated idea"
    assert (await db_session.scalar(select(ProductOnboardingSession))).last_product_revision == 2
    assert len((await db_session.scalars(select(ProductChange))).all()) == 2
    assert len((await db_session.scalars(select(ProductOperation))).all()) == 4
    basic = await test_client.put(
        f"{root}/{session_id}/steps/basic",
        json={"action": "save", "configuration": {"basic": {}}},
        headers=mutation_headers(2, "save_basic_key_123"),
    )
    assert basic.status_code == 200, basic.text
    db_session.expire_all()
    assert (await db_session.get(Project, (member.org_id, project_id))).configuration == {"basic": {}}
    assert basic.json()["revision"] == 3
    unchanged = await test_client.put(
        f"{root}/{session_id}/steps/basic",
        json={"action": "save", "configuration": {"basic": {}}},
        headers=mutation_headers(3, "save_basic_again_123"),
    )
    assert unchanged.status_code == 200 and unchanged.json()["revision"] == 3
    db_session.expire_all()
    assert len((await db_session.scalars(select(ProductChange))).all()) == 3


@pytest.mark.asyncio
async def test_session_scope_and_invalid_mutations(test_client, db_session, app, member, valid_token_context):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    session = await test_client.post(root, json={}, headers=mutation_headers(1, "create_scope_key_123"))
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    path = f"{root}/{session_id}"
    assert (await test_client.get(f"{root}/not-a-session")).status_code == 404
    second = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Other product"},
        headers={"Idempotency-Key": "second_product_key_123"},
    )
    assert second.status_code == 201, second.text
    wrong_product_path = f"/v1/products/{second.json()['product_id']}/projects/{second.json()['project_id']}/onboarding-sessions/{session_id}"
    assert (await test_client.get(wrong_product_path)).status_code == 404
    assert (await test_client.get(f"/v1/products/{product_id}/projects/wrong/onboarding-sessions/{session_id}")).status_code == 404
    assert (
        await test_client.put(f"{path}/steps/basic", json={"action": "confirm"}, headers=mutation_headers(1, "invalid_confirm_123"))
    ).status_code == 422
    assert (
        await test_client.put(
            f"{path}/steps/basic", json={"action": "save", "product": {"name": "Bad"}}, headers=mutation_headers(1, "invalid_section_123")
        )
    ).status_code == 422
    assert (await test_client.post(root, json={"tenant_id": "other"}, headers=mutation_headers(1, "invalid_tenant_123"))).status_code == 422
    foreign = valid_token_context.model_copy(update={"org_id": "other-workspace", "user_id": "foreign-user"})
    db_session.add(Organization(id=foreign.org_id, name="Foreign workspace"))
    db_session.add(User(id=foreign.user_id, org_id=foreign.org_id, team_id="other-team", email="foreign@example.test", user_kind="human"))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.get(path)).status_code == 404
    assert (await test_client.post(root, json={}, headers=mutation_headers(1, "foreign_start_key_123"))).status_code == 404
    assert (
        await test_client.put(f"{path}/steps/basic", json={"action": "later"}, headers=mutation_headers(1, "foreign_step_key_123"))
    ).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: member
    assert (await test_client.get(path)).status_code == 200
    db_session.add(
        TenantMembership(id="revoked-onboard", user_id=member.user_id, tenant_id=member.org_id, role="member", revoked_at=datetime.now(UTC))
    )
    await db_session.commit()
    assert (await test_client.get(path)).status_code == 404

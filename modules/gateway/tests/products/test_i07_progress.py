"""Profile-1 check denominator is derived from persisted scope and section choices."""

import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.product_onboarding.progress import profile_progress
from src.products.models import Product, ProductOnboardingSession, Project
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


def request_headers(revision, key):
    return {"If-Profile-Revision": str(revision), "Idempotency-Key": key}


@pytest.mark.asyncio
async def test_minimal_idea_and_later_optional_na_readback(test_client, db_session, member, monkeypatch):
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Onboarding progress must not call a provider"))
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    profile_path = f"/v1/products/{product_id}/projects/{project_id}"
    session_root = profile_path + "/onboarding-sessions"
    initial_profile = (await test_client.get(profile_path)).json()
    initial_progress = initial_profile["progress"]
    assert (initial_progress["completed"], initial_progress["applicable"], initial_progress["percentage"], initial_progress["maturity"]) == (
        2,
        2,
        100,
        "saved",
    )
    assert [check["id"] for check in initial_progress["checks"]] == ["information", "project_scope"]
    assert all(check["required"] and check["status"] == "complete" for check in initial_progress["checks"])
    started = await test_client.post(session_root, json={}, headers=request_headers(1, "start_progress_key_123"))
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    assert started.json()["progress"] == initial_progress
    assert started.json()["steps"]["information"]["status"] == "complete"
    assert all(started.json()["steps"][section]["status"] == "not_started" for section in ("basic", "advanced", "financials"))
    step_path = f"{session_root}/{session_id}/steps/financials"
    selected = await test_client.put(
        step_path,
        json={"action": "save", "configuration": {"financials": {}}},
        headers=request_headers(1, "select_financial_key_123"),
    )
    assert selected.status_code == 200 and selected.json()["progress"]["applicable"] == 3
    assert selected.json()["steps"]["financials"]["status"] == "in_progress"
    later = await test_client.put(step_path, json={"action": "later"}, headers=request_headers(2, "later_progress_key_123"))
    assert later.status_code == 200, later.text
    assert (later.json()["progress"]["completed"], later.json()["progress"]["applicable"], later.json()["progress"]["percentage"]) == (2, 3, 66)
    assert later.json()["progress"]["checks"][-1]["status"] == "later"
    assert len(later.json()["progress"]["next_steps"]) == 1
    assert (await test_client.get(profile_path)).json()["progress"] == later.json()["progress"]
    not_applicable = await test_client.put(
        step_path, json={"action": "not_applicable", "reason": "No financial plan yet"}, headers=request_headers(3, "exclude_financial_key_123")
    )
    assert not_applicable.status_code == 200, not_applicable.text
    assert not_applicable.json()["progress"]["checks"][-1] == {
        "id": "financials",
        "status": "not_applicable",
        "required": False,
        "reason": "No financial plan yet",
    }
    assert (
        not_applicable.json()["progress"]["completed"],
        not_applicable.json()["progress"]["applicable"],
        not_applicable.json()["progress"]["percentage"],
    ) == (2, 2, 100)
    assert not_applicable.json()["steps"]["financials"]["reason"] == "No financial plan yet"
    read = (await test_client.get(f"{session_root}/{session_id}")).json()
    assert read == not_applicable.json()
    assert (await test_client.get(profile_path)).json()["progress"] == read["progress"]
    db_session.expire_all()
    saved = (await db_session.scalars(select(ProductOnboardingSession))).one()
    assert saved.steps["financials"]["status"] == "not_applicable"
    assert (await db_session.get(Project, (member.org_id, project_id))).configuration["applicability"] == [
        {"check_id": "financials", "state": "not_applicable", "reason": "No financial plan yet"},
    ]
    assert (await db_session.get(Product, (member.org_id, product_id))).revision == 4


@pytest.mark.asyncio
async def test_required_check_cannot_be_excluded_or_removed_by_configuration(test_client, db_session, member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    path = f"/v1/products/{product_id}/projects/{project_id}"
    started = await test_client.post(path + "/onboarding-sessions", json={}, headers=request_headers(1, "start_required_key_123"))
    assert started.status_code == 201
    session_id = started.json()["id"]
    step = f"{path}/onboarding-sessions/{session_id}/steps/information"
    invalid = await test_client.put(
        step, json={"action": "not_applicable", "reason": "Not needed"}, headers=request_headers(1, "exclude_required_key_123")
    )
    assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "required_check"
    for check_id in ("information", "project_scope"):
        bad_config = await test_client.patch(
            path + "/configuration",
            json={"configuration": {"applicability": [{"check_id": check_id, "state": "not_applicable", "reason": "Incorrect"}]}},
            headers=request_headers(1, "invalid_config_" + check_id + "_123"),
        )
        assert bad_config.status_code == 422 and bad_config.json()["detail"]["code"] == "required_check"
    missing_reason = await test_client.put(
        f"{path}/onboarding-sessions/{session_id}/steps/financials",
        json={"action": "not_applicable"},
        headers=request_headers(1, "missing_reason_key_123"),
    )
    assert missing_reason.status_code == 422
    db_session.expire_all()
    assert (await db_session.get(Product, (member.org_id, product_id))).revision == 1
    assert (await db_session.get(Project, (member.org_id, project_id))).configuration == {}
    assert (await db_session.get(ProductOnboardingSession, (member.org_id, session_id))).steps["information"]["status"] == "complete"


def test_running_product_required_selected_checks_cannot_be_na():
    product = Product(
        tenant_id="fixture-org",
        id="product",
        name="Planner",
        purpose="Test",
        intended_users=["Maintainers"],
        owner_user_id="owner",
        starting_point="running_product",
        created_by="owner",
    )
    project = Project(tenant_id="fixture-org", id="scope", name="Scope", owner_user_id="owner", configuration={})
    progress = profile_progress(product, project)
    assert progress["applicable"] == 6 and progress["completed"] == 2
    assert {check["id"] for check in progress["checks"] if check["required"]} == {
        "information",
        "project_scope",
        "repository",
        "environment",
        "pipeline",
        "operations",
    }
    assert progress["maturity"] == "saved"

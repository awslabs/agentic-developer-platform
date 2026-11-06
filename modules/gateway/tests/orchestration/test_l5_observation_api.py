"""The mounted collector requires current direct human authority."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.orchestration import l5_observation_routes as routes
from src.orchestration.l5_observation_contract import OBSERVED, PREPARED
from src.orchestration.models import OrchestrationDecision
from src.shared.schemas.auth import TokenContext
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_policy_admission import APPROVER, ORG_A, TEAM_A


@pytest.fixture
def observation_actor(app, monkeypatch, test_client):
    actor = TokenContext(
        user_id=APPROVER,
        org_id=ORG_A,
        team_id=TEAM_A,
        department_id="department",
        account_type="human",
        is_admin=True,
        token_use="access",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    test_client.headers["Authorization"] = "Bearer observation-interactive-fixture"
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setattr(routes, "require_native_launch_session", AsyncMock())
    return actor


@pytest.mark.parametrize(
    "mutation,status",
    [
        ({"account_type": "service"}, 403),
        ({"auth_source": "iam"}, 403),
        ({"token_use": "id"}, 401),
        ({"expires_at": None}, 401),
        ({"expires_at": datetime(2020, 1, 1, tzinfo=UTC)}, 403),
        ({"is_admin": False}, 403),
    ],
)
async def test_prepare_rejects_nonhuman_expired_and_nonadmin(app, test_client, scope_case, observation_actor, mutation, status):  # noqa: F811
    c = scope_case
    actor = observation_actor.model_copy(update=mutation)
    app.dependency_overrides[get_current_user] = lambda: actor
    response = await test_client.post(f"/orchestration/flows/{c.flow.id}/l5-observations/prepare", json=c.request.model_dump(mode="json"))
    assert response.status_code == status, response.text
    assert not list(await c.db.scalars(select(OrchestrationDecision.id).where(OrchestrationDecision.kind.in_([PREPARED, OBSERVED]))))


async def test_prepare_observe_and_read_are_mounted_and_partial(test_client, scope_case, observation_actor):  # noqa: F811
    c = scope_case
    path = f"/orchestration/flows/{c.flow.id}/l5-observations"
    assert c.provider_run["status"] == "in_progress" and c.provider_run["conclusion"] is None
    assert not list(await c.db.scalars(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == OBSERVED)))
    response = await test_client.post(path + "/prepare", json=c.request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    challenge = response.json()
    assert challenge["snapshot"]["release"]["dispatch_action_id"] == c.action.id
    observed = await test_client.post(
        path + "/" + challenge["challenge_id"] + "/observe",
        json={"snapshot_sha256": challenge["snapshot_sha256"], "observation": "displayed", "displayed_configuration_revision": 1},
    )
    assert observed.status_code == 200, observed.text
    receipt = observed.json()
    assert receipt["native_qualified"] is receipt["release_check_completed"] is False
    assert receipt["observation_receipt"]["actor_id"] == APPROVER
    assert (await test_client.get(path + "/" + challenge["challenge_id"])).json() == receipt


async def test_prepare_rejects_another_provider_run_without_writing_evidence(test_client, scope_case, observation_actor):  # noqa: F811
    c = scope_case
    c.provider_run["id"] = 73
    response = await test_client.post(f"/orchestration/flows/{c.flow.id}/l5-observations/prepare", json=c.request.model_dump(mode="json"))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "l5_observation_workflow_changed"
    assert not list(await c.db.scalars(select(OrchestrationDecision.id).where(OrchestrationDecision.kind.in_([PREPARED, OBSERVED]))))


async def test_logged_out_session_and_stale_scope_are_actionable(test_client, scope_case, observation_actor, monkeypatch):  # noqa: F811
    c = scope_case
    path = f"/orchestration/flows/{c.flow.id}/l5-observations/prepare"
    monkeypatch.setattr(routes, "require_native_launch_session", AsyncMock(side_effect=HTTPException(401, detail={"code": "session_required"})))
    assert (await test_client.post(path, json=c.request.model_dump(mode="json"))).status_code == 401
    monkeypatch.setattr(routes, "require_native_launch_session", AsyncMock())
    payload = c.request.model_dump(mode="json")
    payload["expected_attempt"] += 1
    response = await test_client.post(path, json=payload)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "l5_observation_plan_changed"
    assert "do not repeat native work" in response.json()["detail"]["next_action"]

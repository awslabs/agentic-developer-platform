"""A separate Product owner explicitly endorses the unchanged release scope."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from src import l5_observation_launch as launches
from src.admin.membership_revocation import revoke_membership
from src.agentauth.human_control import HumanControlSession
from src.auth.dependencies import get_current_user
from src.l5_integration import GatewayConfigurationBackend
from src.orchestration import l5_observation_routes as routes
from src.orchestration import l5_observations as observations
from src.orchestration.l5_observation_contract import ENDORSED
from src.orchestration.models import OrchestrationDecision
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User
from src.shared.schemas.auth import TokenContext
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_policy_admission import APPROVER, ORG_A, TEAM_A


@pytest.fixture
async def cross_owner_case(scope_case, app, test_client, monkeypatch):  # noqa: F811
    c = scope_case
    c.product_owner_id = "actual-product-owner"
    c.db.add(User(id=c.product_owner_id, org_id=ORG_A, team_id=TEAM_A, email="owner@example.test", user_kind="human"))
    await c.db.flush()
    c.db.add_all(
        [
            TenantMembership(user_id=c.product_owner_id, tenant_id=ORG_A, role="member"),
            TeamMembership(org_id=ORG_A, user_id=c.product_owner_id, team_id=TEAM_A, role="member"),
        ]
    )
    c.product.owner_user_id = c.config.owner_id = c.product_owner_id
    await c.db.commit()
    c.request = c.request.model_copy(update={"observer_id": c.product_owner_id})
    c.preparer = HumanControlSession(user_id=APPROVER, tenant_id=ORG_A, expires_at=c.now + timedelta(hours=1))
    c.owner_actor = TokenContext(
        user_id=c.product_owner_id,
        org_id=ORG_A,
        team_id=TEAM_A,
        department_id="department",
        account_type="human",
        is_admin=False,
        token_use="access",
        expires_at=c.now + timedelta(hours=1),
    )
    c.challenge = await observations.prepare(c.db, flow_id=c.flow.id, human=c.preparer, request=c.request)
    await c.db.commit()
    c.path = f"/orchestration/flows/{c.flow.id}/l5-observations/{c.challenge['challenge_id']}"
    app.dependency_overrides[get_current_user] = lambda: c.owner_actor
    # Authentication is stubbed, but the interactive guard still fingerprints
    # the session credential to exclude credentials used by release fixtures.
    test_client.headers["Authorization"] = "Bearer offline-product-owner-session"
    monkeypatch.setattr(routes, "require_native_launch_session", AsyncMock())
    return c


async def test_ordinary_product_owner_endorses_and_observes_without_ownership_transfer(test_client, cross_owner_case):
    c = cross_owner_case
    assert c.challenge["owner_endorsement_required"] is True
    assert (await test_client.get(c.path)).status_code == 200
    observation = {"snapshot_sha256": c.challenge["snapshot_sha256"], "observation": "displayed", "displayed_configuration_revision": 1}
    refused = await test_client.post(c.path + "/observe", json=observation)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "l5_observation_owner_endorsement_required"
    endorsed = await test_client.post(c.path + "/endorse", json={"snapshot_sha256": c.challenge["snapshot_sha256"]})
    assert endorsed.status_code == 200, endorsed.text
    receipt = endorsed.json()["owner_endorsement"]
    assert receipt["actor_id"] == c.product_owner_id
    repeated = await test_client.post(c.path + "/endorse", json={"snapshot_sha256": c.challenge["snapshot_sha256"]})
    assert repeated.json()["owner_endorsement"] == receipt
    recorded = await test_client.post(c.path + "/observe", json=observation)
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["release_check_completed"] is recorded.json()["native_qualified"] is False
    assert c.product.owner_user_id == c.config.owner_id == c.product_owner_id
    assert c.plan.plan_document["execution_policy"]["principal_id"] == APPROVER
    assert c.node.attempts == 1 and c.node.state == "running"


@pytest.mark.parametrize("invalid", ["wrong_owner", "revoked_owner", "stale_snapshot"])
async def test_endorsement_requires_exact_current_owner_and_snapshot(app, test_client, cross_owner_case, invalid):
    c = cross_owner_case
    body = {"snapshot_sha256": c.challenge["snapshot_sha256"]}
    if invalid == "wrong_owner":
        actor = c.owner_actor.model_copy(update={"user_id": APPROVER, "is_admin": True})
        app.dependency_overrides[get_current_user] = lambda: actor
    elif invalid == "revoked_owner":
        await revoke_membership(c.db, org_id=ORG_A, user_id=c.product_owner_id)
        await c.db.commit()
    else:
        body["snapshot_sha256"] = "0" * 64
    response = await test_client.post(c.path + "/endorse", json=body)
    assert response.status_code == (409 if invalid == "stale_snapshot" else 403), response.text
    assert not list(await c.db.scalars(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == ENDORSED)))


async def test_unendorsed_cross_owner_challenge_cannot_launch(test_client, cross_owner_case, db_session_factory, monkeypatch):
    c = cross_owner_case
    challenge = await observations.prepare(
        c.db,
        flow_id=c.flow.id,
        human=c.preparer,
        request=c.request.model_copy(update={"request_key": "native_observation_0002", "subject": "native_ui"}),
    )
    await c.db.commit()
    monkeypatch.setattr(launches, "get_session_factory", lambda: db_session_factory)
    backend = GatewayConfigurationBackend(c.db, c.owner_actor)
    provider = AsyncMock()
    monkeypatch.setattr(backend, "_launch_browser", provider)
    request = Request({"type": "http", "headers": [(b"x-adp-observation-id", challenge["challenge_id"].encode())]})
    with pytest.raises(HTTPException) as denied:
        await backend.launch_browser(c.request.product_id, c.request.project_id, c.request.binding_id, 1, request)
    assert denied.value.status_code == 403 and denied.value.detail["code"] == "l5_observation_owner_endorsement_required"
    provider.assert_not_awaited()


async def test_product_owner_can_review_negative_scope_while_foreign_observer_is_redacted(app, test_client, cross_owner_case):
    c = cross_owner_case
    observer_id = "negative-observer"
    c.db.add(User(id=observer_id, org_id=ORG_A, team_id=TEAM_A, email="negative@example.test", user_kind="human"))
    await c.db.flush()
    c.db.add(TenantMembership(user_id=observer_id, tenant_id=ORG_A, role="member"))
    await c.db.commit()
    challenge = await observations.prepare(
        c.db,
        flow_id=c.flow.id,
        human=c.preparer,
        request=c.request.model_copy(update={"request_key": "negative_observation_0003", "subject": "native_denial", "observer_id": observer_id}),
    )
    await c.db.commit()
    path = f"/orchestration/flows/{c.flow.id}/l5-observations/{challenge['challenge_id']}"
    review = await test_client.get(path)
    assert review.status_code == 200 and review.json()["snapshot"]["selection"]["owner_id"] == c.product_owner_id
    endorsed = await test_client.post(path + "/endorse", json={"snapshot_sha256": challenge["snapshot_sha256"]})
    assert endorsed.status_code == 200 and endorsed.json()["owner_endorsement"]["actor_id"] == c.product_owner_id
    assert endorsed.json()["observation_receipt"] is None
    actor = c.owner_actor.model_copy(update={"user_id": observer_id})
    app.dependency_overrides[get_current_user] = lambda: actor
    foreign = await test_client.get(path)
    assert foreign.status_code == 200 and foreign.json()["snapshot"] is None
    assert foreign.json()["owner_endorsement"] is None
    refused = await test_client.post(path + "/endorse", json={"snapshot_sha256": challenge["snapshot_sha256"]})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "l5_observation_product_owner_required"

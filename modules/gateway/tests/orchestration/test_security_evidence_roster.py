"""Original Security roster lookup is historical, scoped and metadata-only."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from src.admin.config import Permission
from src.admin.membership_revocation import revoke_membership
from src.auth.dependencies import get_current_user
from src.orchestration import routes
from src.orchestration import security_evidence_roster as roster
from src.orchestration.accepted_node import contract_from_plan
from src.orchestration.l5_observations import append
from src.orchestration.models import OrchestrationAction, OrchestrationDecision
from src.products.security_evidence_store import event_id
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_policy_admission import APPROVER, ORG_A, TEAM_A, _make_flow
from tests.orchestration.test_security_evidence_release import security_release  # noqa: F401


@pytest.fixture
async def roster_case(security_release):  # noqa: F811
    c = security_release
    release = contract_from_plan(c.flow, c.node, c.plan).node.operational_release_binding
    c.action.detail = {
        **c.action.detail,
        "contract_digest": release.delivery_contract.digest,
        "private_token": "NEVER-DISCLOSE",
        "unrelated_tenant": {"source_body": "NEVER-DISCLOSE"},
    }
    c.roster = {
        "fixture_sha256": "a" * 64,
        "fixture_expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "cases": [
            {
                "tenant_id": f"fixture-tenant-{i}",
                "product_id": f"fixture-product-{i}",
                "project_id": f"fixture-project-{i}",
                "binding_id": f"fixture-binding-{i}",
                "configuration_id": f"fixture-config-{i}",
            }
            for i in (1, 2)
        ],
    }
    c.actor = TokenContext(
        user_id=APPROVER,
        org_id=ORG_A,
        team_id=TEAM_A,
        department_id="",
        account_type="human",
        token_use="access",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await c.db.commit()
    return c


async def retain(c, *, action=None, body=None, **changes):
    row = append(
        c.db,
        identity=SimpleNamespace(org_id=ORG_A, flow_id=c.flow.id, node_id=c.node.id),
        kind=roster.KIND,
        actor_id="system:security-release",
        actor_kind="service",
        data=c.roster if body is None else body,
        row_id=event_id((action or c.action).id, "security_fixture_roster"),
    )
    for key, value in changes.items():
        setattr(row, key, value)
    await c.db.commit()
    return row


async def request(c, monkeypatch, *, flow_id=None, params=None, actor=None, permission=True):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: actor or c.actor
    app.dependency_overrides[get_db] = lambda: c.db
    check = AsyncMock() if permission else AsyncMock(side_effect=HTTPException(403, "forbidden"))
    monkeypatch.setattr(roster.AccessControl, "check_permission", check)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.invalid") as client:
        result = await client.get(f"/orchestration/flows/{flow_id or c.flow.id}/security-evidence-rosters", params=params)
    return result, check


async def test_reads_expired_roster_from_superseded_paused_dispatch_without_live_authority(roster_case, monkeypatch):
    c = roster_case
    row = await retain(c)
    c.plan.superseded_at = datetime.now(UTC)
    c.flow.execution_paused = True
    c.flow.archived_at = datetime.now(UTC)
    await c.db.commit()
    before = list(await c.db.scalars(select(OrchestrationDecision.id)))
    response, check = await request(c, monkeypatch)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "recorded" and body["execution_authorized"] is False
    assert body["items"][0]["roster_id"] == row.id
    assert body["items"][0]["cases"] == c.roster["cases"]
    assert body["items"][0]["dispatch_action_id"] == c.action.id
    assert (body["items"][0]["run_id"], body["items"][0]["run_attempt"]) == (72, 1)
    assert body["items"][0]["accepted_plan_version"] == c.execution.accepted_plan_version
    assert "NEVER-DISCLOSE" not in response.text and "private_token" not in response.text
    assert response.headers["cache-control"] == "no-store"
    check.assert_awaited_once_with(c.actor, Permission.PLAN_APPROVE, target_org_id=ORG_A)
    assert before == list(await c.db.scalars(select(OrchestrationDecision.id)))


async def test_empty_retained_history_is_not_inferred_from_missing_product_configuration(roster_case, monkeypatch):
    c = roster_case
    response, _ = await request(c, monkeypatch, params={"node_id": c.node.id})
    assert response.status_code == 200
    assert response.json()["state"] == "no_roster_recorded" and response.json()["items"] == []
    assert "does not prove that no historical grant existed" in response.json()["limitation"]


@pytest.mark.parametrize("operator", [False, True])
async def test_unrelated_live_member_needs_existing_platform_operator_authority(roster_case, monkeypatch, operator):
    c = roster_case
    await retain(c)
    c.db.add(User(id="unrelated-member", org_id=ORG_A, team_id=TEAM_A, email="unrelated@example.test", user_kind="human"))
    await c.db.flush()
    c.db.add(TenantMembership(user_id="unrelated-member", tenant_id=ORG_A, role="member"))
    await c.db.commit()
    actor = c.actor.model_copy(update={"user_id": "unrelated-member", "is_admin": operator})
    response, _ = await request(c, monkeypatch, actor=actor)
    assert response.status_code == (200 if operator else 404)
    if not operator:
        assert "fixture-tenant" not in response.text


@pytest.mark.parametrize("change", ["foreign-flow", "foreign-node", "service", "expired", "membership", "permission"])
async def test_unauthorized_reads_do_not_disclose_roster_presence_or_case_identifiers(roster_case, monkeypatch, change):
    c = roster_case
    await retain(c)
    actor, params, flow_id = c.actor, None, None
    if change == "foreign-flow":
        flow_id = "unknown-or-foreign"
    elif change == "foreign-node":
        params = {"node_id": "unknown-or-foreign"}
    elif change == "service":
        actor = actor.model_copy(update={"account_type": "service"})
    elif change == "expired":
        actor = actor.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    elif change == "membership":
        await revoke_membership(c.db, org_id=ORG_A, user_id=APPROVER)
        await c.db.commit()
    response, _ = await request(c, monkeypatch, actor=actor, flow_id=flow_id, params=params, permission=change != "permission")
    assert response.status_code in {403, 404}
    assert "fixture-tenant" not in response.text and "fixture_sha256" not in response.text


@pytest.mark.parametrize("fault", ["orphan", "unexpected-fields", "same-tenant", "wrong-contract", "bad-plan-hash", "bad-source", "wrong-actor"])
async def test_unverifiable_retained_roster_is_unavailable_never_a_false_empty_history(roster_case, monkeypatch, fault):
    c = roster_case
    body = copy.deepcopy(c.roster)
    changes = {}
    if fault == "unexpected-fields":
        body["secret"] = "NEVER-DISCLOSE"
    elif fault == "same-tenant":
        body["cases"][1]["tenant_id"] = body["cases"][0]["tenant_id"]
    elif fault == "wrong-contract":
        c.action.detail = {**c.action.detail, "contract_digest": "b" * 64}
    elif fault == "bad-plan-hash":
        c.plan.plan_hash = "f" * 64
    elif fault == "bad-source":
        c.action.detail = {**c.action.detail, "source_revision": "HEAD"}
    elif fault == "wrong-actor":
        changes["actor_id"] = "untrusted"
    elif fault == "orphan":
        changes["id"] = "unbound-roster"
    await retain(c, body=body, **changes)
    response, _ = await request(c, monkeypatch)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "security_evidence_roster_unavailable"
    assert "NEVER-DISCLOSE" not in response.text and "fixture-tenant" not in response.text


async def test_pagination_retains_each_original_dispatch_and_late_empty_page_is_not_no_roster(roster_case, monkeypatch):
    c = roster_case
    first = await retain(c)
    action = OrchestrationAction(
        org_id=ORG_A,
        execution_id=c.execution.id,
        operation_key="second-dispatch",
        kind="delivery_workflow_dispatch",
        status="failed",
        attempt=2,
        detail=copy.deepcopy(c.action.detail),
    )
    c.db.add(action)
    await c.db.commit()
    second = await retain(c, action=action, body={**c.roster, "fixture_sha256": "b" * 64})
    response, _ = await request(c, monkeypatch, params={"limit": 1})
    assert response.status_code == 200 and response.json()["next_offset"] == 1
    assert response.json()["items"][0]["roster_id"] == first.id
    response, _ = await request(c, monkeypatch, params={"limit": 1, "offset": 1})
    assert response.json()["next_offset"] is None and response.json()["items"][0]["roster_id"] == second.id
    response, _ = await request(c, monkeypatch, params={"offset": 2})
    assert response.json()["state"] == "recorded" and response.json()["items"] == []


async def test_storage_failure_is_explicitly_unavailable_without_raw_database_error(roster_case, monkeypatch):
    from sqlalchemy.exc import OperationalError

    c = roster_case
    monkeypatch.setattr(roster, "read_rosters", AsyncMock(side_effect=OperationalError("private query", {}, Exception("NEVER-DISCLOSE"))))
    response, _ = await request(c, monkeypatch)
    assert response.status_code == 503 and "NEVER-DISCLOSE" not in response.text


def test_roster_lookup_is_only_a_read_on_the_operator_plane():
    endpoints = [route for route in routes.router.routes if route.path.endswith("/security-evidence-rosters")]
    assert len(endpoints) == 1 and endpoints[0].methods == {"GET"}
    assert endpoints[0].path.startswith("/orchestration/flows/")


async def test_actual_foreign_flow_and_roster_are_indistinguishable_from_unknown(roster_case, monkeypatch):
    c = roster_case
    foreign = await _make_flow(c.db, org_id="foreign-org", slug="private-foreign-flow")
    append(
        c.db,
        identity=SimpleNamespace(org_id="foreign-org", flow_id=foreign.id, node_id=None),
        kind=roster.KIND,
        actor_id="system:security-release",
        actor_kind="service",
        data=c.roster,
    )
    await c.db.commit()
    existing, _ = await request(c, monkeypatch, flow_id=foreign.id)
    missing, _ = await request(c, monkeypatch, flow_id="missing-flow")
    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json() == {"detail": {"code": "not_found"}}


async def test_dispatch_history_bound_does_not_turn_into_empty_history(roster_case, monkeypatch):
    c = roster_case
    await retain(c)
    monkeypatch.setattr(roster, "MAX_DISPATCHES", 0)
    response, _ = await request(c, monkeypatch)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "security_evidence_roster_unavailable"

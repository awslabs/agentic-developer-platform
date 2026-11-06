"""Product teammates share task details without gaining private activity rights."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.orchestration.story_run_insights import current_story_run_insights
from src.shared.models.organization import TeamMembership, User
from tests.orchestration import test_execution_read as base
from tests.orchestration.test_product_flows import seed_product
from tests.orchestration.test_run_report_read import seed, terminal
from tests.orchestration.test_story_run_insights import BOARD
from tests.products.membership import add_team_member

session = base.session
app_with_router = base.app_with_router


async def prepare(session, monkeypatch, *, persona="operations", owner=False):
    actor = base._token_context(base.ORG_A)
    user = User(id=actor.user_id, org_id=actor.org_id, team_id="stale-team", email="member@example.test")
    session.add(user)
    product = await seed_product(session)
    await add_team_member(session, actor, team_id=product.team_id)
    flow, node, report = await seed(session)
    flow.product_id = product.id
    report.persona = persona
    report.dispatch_metadata = {"actor": {"user_id": actor.user_id if owner else "another-person"}}
    report.task_board_snapshot = BOARD
    report.terminal_receipt = terminal(report, "failed")
    report.operations_report_receipt = {
        "run_id": report.run_id,
        "attempt": report.attempt,
        "recorded": True,
        "report": "Teammate investigation: required checks are pending. Authorization: Bearer secret-value",
    }
    await session.commit()
    # The ordinary caller cannot read everybody's private agent activity.
    monkeypatch.setattr("src.admin.access_control.AccessControl.check_permission", AsyncMock(side_effect=AccessDeniedError(message="denied")))
    return SimpleNamespace(actor=actor, user=user, product=product, flow=flow, node=node, report=report)


async def insight(session, case):
    return (await current_story_run_insights(session, current_user=case.actor, flow_id=case.flow.id))[case.node.id]


@pytest.mark.parametrize("persona", ["developer", "agent-codex-developer", "operations", "agent-codex-operations", "reviewer"])
async def test_current_teammate_reads_other_owners_board_and_redacted_report(session, monkeypatch, persona):
    case = await prepare(session, monkeypatch, persona=persona)
    result = await insight(session, case)
    assert result.board_access == "available"
    assert result.task_board.text == BOARD["text"]
    assert "Teammate investigation" in result.failure.report_text
    assert "secret-value" not in result.model_dump_json()


@pytest.mark.parametrize("owner", [False, True])
async def test_removing_membership_revokes_details_even_for_run_owner(session, monkeypatch, owner):
    case = await prepare(session, monkeypatch, owner=owner)
    assert (await insight(session, case)).board_access == "available"
    case.user.team_id = case.product.team_id  # A stale pointer does not restore access.
    await session.execute(delete(TeamMembership).where(TeamMembership.user_id == case.actor.user_id))
    await session.commit()
    result = await insight(session, case)
    assert result.board_access == "restricted"
    assert result.task_board is None and result.failure.report_text is None


@pytest.mark.parametrize("boundary", ["different_team", "teamless", "missing_product", "foreign_product", "service", "shadow"])
async def test_product_membership_does_not_cross_other_boundaries(session, monkeypatch, boundary):
    case = await prepare(session, monkeypatch, owner=True)
    if boundary == "different_team":
        case.product.team_id = "other-team"
    elif boundary == "teamless":
        case.product.team_id = None
    elif boundary == "missing_product":
        case.flow.product_id = "missing"
    elif boundary == "foreign_product":
        foreign = await seed_product(session, "foreign-product", org_id=base.ORG_B)
        case.flow.product_id = foreign.id
    elif boundary == "service":
        case.actor.account_type = "service"
    else:
        case.user.is_shadow = True
    await session.commit()
    result = await insight(session, case)
    assert result.board_access == "restricted"
    assert result.task_board is None and result.failure.report_text is None


async def test_membership_resolves_signed_login_to_current_local_account(session, monkeypatch):
    case = await prepare(session, monkeypatch)
    case.user.cognito_sub = "signed-login-subject"
    case.actor.user_id = case.user.cognito_sub
    await session.commit()
    assert (await insight(session, case)).board_access == "available"


async def test_administrator_access_stays_tenant_scoped(session, monkeypatch):
    case = await prepare(session, monkeypatch)
    case.product.team_id = None
    await session.commit()
    monkeypatch.setattr("src.admin.access_control.AccessControl.check_permission", AsyncMock())
    assert (await insight(session, case)).board_access == "available"
    case.actor.org_id = base.ORG_B
    assert await current_story_run_insights(session, current_user=case.actor, flow_id=case.flow.id) == {}


async def test_product_membership_does_not_grant_standalone_run_access(session, monkeypatch):
    case = await prepare(session, monkeypatch)
    case.flow.product_id = None
    await session.commit()
    assert (await insight(session, case)).board_access == "restricted"
    case.report.dispatch_metadata = {"actor": {"user_id": case.actor.user_id}}
    await session.commit()
    assert (await insight(session, case)).board_access == "available"


@pytest.mark.parametrize("surface", ["graph", "activity"])
async def test_graph_and_refresh_recheck_membership_before_returning_task_details(session, app_with_router, monkeypatch, surface):
    case = await prepare(session, monkeypatch)
    # Avoid external activity and cost providers; authorization and report reads
    # use the real database and the same route the product UI calls.
    monkeypatch.setattr("src.orchestration.routes.load_bounded_story_execution", AsyncMock(return_value={}))
    monkeypatch.setattr("src.orchestration.routes.load_story_execution", AsyncMock(return_value={}))
    client = base.client_for(app_with_router)
    app_with_router.dependency_overrides[get_current_user] = lambda: case.actor
    url = f"/orchestration/flows/{case.flow.id}"
    params = {}
    if surface == "activity":
        url += "/activity"
        params["node_ids"] = case.node.id

    def read():
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        rows = body["items"] if surface == "activity" else body["nodes"]
        result = next(row for row in rows if row.get("node_id", row.get("id")) == case.node.id)["run_insight"]
        return response, result

    _, before = read()
    assert before["task_board"]["text"] == BOARD["text"]
    assert "Teammate investigation" in before["failure"]["report_text"]
    await session.execute(delete(TeamMembership).where(TeamMembership.user_id == case.actor.user_id))
    await session.commit()
    response, after = read()
    assert after["board_access"] == "restricted"
    assert after["task_board"] is None and after["failure"]["report_text"] is None
    assert BOARD["text"] not in response.text and "Teammate investigation" not in response.text

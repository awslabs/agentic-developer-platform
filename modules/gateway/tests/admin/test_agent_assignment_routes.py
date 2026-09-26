"""Selected-organization writes authorize before touching provider metadata."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.admin.agent_schemas import AgentCreateRequest, AgentUpdateRequest
from src.admin.routes import _validate_agent_assignment, create_agent, get_agent_credentials, update_agent
from src.shared.models.organization import Department, Organization, Team


@pytest.mark.asyncio
async def test_assignment_rejects_other_org_and_wrong_department(db_session):
    db_session.add_all([Organization(id="assign-org", name="Org"), Organization(id="other-org", name="Other")])
    await db_session.flush()
    db_session.add_all(
        [
            Department(id="assign-dept", org_id="assign-org", name="Dept"),
            Department(id="other-dept", org_id="other-org", name="Other"),
            Department(id="second-dept", org_id="assign-org", name="Second"),
            Team(id="assign-team", org_id="assign-org", department_id="assign-dept", name="Team"),
            Team(id="other-team", org_id="other-org", department_id="other-dept", name="Other"),
        ]
    )
    await db_session.flush()
    await _validate_agent_assignment(db_session, "assign-org", "assign-dept", "assign-team")
    for dept, team in [("other-dept", "assign-team"), ("assign-dept", "other-team"), ("missing", "assign-team"), ("second-dept", "assign-team")]:
        with pytest.raises(HTTPException) as exc:
            await _validate_agent_assignment(db_session, "assign-org", dept, team)
        assert exc.value.status_code == 422
    await _validate_agent_assignment(db_session, "assign-org", None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["credentials", "update"])
async def test_selected_org_authorized_before_lookup(operation):
    service = AsyncMock()
    access = SimpleNamespace(db=AsyncMock(), check_permission=AsyncMock(side_effect=HTTPException(403)))
    actor = SimpleNamespace(org_id="home")
    with pytest.raises(HTTPException):
        if operation == "credentials":
            await get_agent_credentials("client", service, access, actor, org_id="selected")
        else:
            await update_agent("client", AgentUpdateRequest(team_id="team"), service, access, actor, org_id="selected")
    assert access.check_permission.call_args.kwargs["target_org_id"] == "selected"
    service.get_agent.assert_not_called()
    service.update_agent.assert_not_called()
    service.get_agent_credentials.assert_not_called()


@pytest.mark.asyncio
async def test_update_and_credentials_use_selected_org():
    service = AsyncMock()
    service.get_agent.return_value = SimpleNamespace(org_id="selected", department_id="dept", team_id="team")
    access = SimpleNamespace(db=AsyncMock(), check_permission=AsyncMock())
    actor = SimpleNamespace(org_id="home")
    request = AgentUpdateRequest(department_id="new-dept", team_id="new-team")
    with (
        patch("src.admin.routes._validate_agent_assignment", new_callable=AsyncMock) as validate,
        patch("src.admin.routes.write_admin_audit", new_callable=AsyncMock),
        patch("src.admin.routes.mark_admin_effects"),
    ):
        await update_agent("client", request, service, access, actor, org_id="selected")
        validate.assert_awaited_once_with(access.db, "selected", "new-dept", "new-team")
        service.update_agent.assert_awaited_once_with("client", "selected", request)
    await get_agent_credentials("client", service, access, actor, org_id="selected")
    service.get_agent_credentials.assert_awaited_once_with("client", "selected")


@pytest.mark.asyncio
async def test_invalid_creation_assignment_never_creates_identity():
    service = AsyncMock()
    access = SimpleNamespace(db=AsyncMock(), check_permission=AsyncMock())
    with patch("src.admin.routes._validate_agent_assignment", new_callable=AsyncMock, side_effect=HTTPException(422)):
        with pytest.raises(HTTPException):
            await create_agent(AgentCreateRequest(org_id="org", name="worker", department_id="other"), service, access, SimpleNamespace(org_id="org"))
    service.create_agent.assert_not_called()

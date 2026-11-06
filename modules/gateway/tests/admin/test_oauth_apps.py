"""Only human platform administrators can register provider apps; secrets stay private."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import audit_operation
from src.admin.connections.oauth_apps import router
from src.admin.connections.routes import _app_lifecycle_lock
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity.oauth_setup import get_oauth_setup
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def api(monkeypatch):
    persist = AsyncMock()
    monkeypatch.setattr(audit_operation, "persist", persist)
    db = AsyncMock(spec=AsyncSession)
    actor = TokenContext(
        user_id="operator",
        org_id="org",
        team_id="team",
        department_id="dept",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    setup = MagicMock()
    setup.register.return_value = {"provider": "atlassian", "service": "jira", "status": "registered"}
    setup.status.return_value = setup.register.return_value
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    app.dependency_overrides[_app_lifecycle_lock] = lambda: None
    return TestClient(app, raise_server_exceptions=False), actor, setup, persist


@pytest.mark.parametrize("admin,kind", [(False, "human"), (True, "service")])
def test_only_human_platform_admins_can_read_or_write_app_setup(api, admin, kind):
    client, actor, setup, _ = api
    actor.is_admin, actor.account_type = admin, kind
    assert client.get("/admin/connections/oauth/atlassian/app").status_code == 403
    assert client.post("/admin/connections/oauth/atlassian/app", json={"client_id": "id", "client_secret": "secret"}).status_code == 403
    assert client.post("/admin/connections/oauth/slack/app/automate", json={"configuration_token": "private-token"}).status_code == 403
    setup.status.assert_not_called()
    setup.register.assert_not_called()
    setup.automate_slack.assert_not_called()


def test_slack_automation_is_audited_without_configuration_token(api):
    client, _, setup, persist = api
    setup.automate_slack.return_value = {"provider": "slack", "status": "registered", "callback_configured": True}
    response = client.post("/admin/connections/oauth/slack/app/automate", json={"configuration_token": "private-token"})
    assert response.status_code == 200 and response.json()["callback_configured"] is True
    assert setup.automate_slack.call_args.args == ("private-token",)
    assert "private-token" not in response.text and "private-token" not in str(persist.call_args_list)
    assert persist.await_count == 2


def test_slack_automation_validation_never_echoes_token(api):
    client, _, setup, _ = api
    response = client.post("/admin/connections/oauth/slack/app/automate", json={"configuration_token": {"private-token": "private-token"}})
    assert response.status_code == 422 and "private-token" not in response.text
    setup.automate_slack.assert_not_called()


def test_registration_is_audited_without_credentials(api):
    client, _, setup, persist = api
    response = client.post("/admin/connections/oauth/atlassian/app", json={"client_id": "id", "client_secret": "private-secret"})
    assert response.status_code == 200, response.text
    assert setup.register.call_args.args == ("atlassian", "id", "private-secret")
    assert "private-secret" not in response.text
    assert persist.await_count == 2
    assert "private-secret" not in str(persist.call_args_list)
    assert persist.call_args.kwargs["target_type"] == "oauth_app"


@pytest.mark.parametrize(
    "body",
    [
        {"client_id": "", "client_secret": "private-secret"},
        {"client_id": "id", "client_secret": {"private-secret": "private-secret"}},
        {"client_id": "id", "client_secret": "private-secret", "unexpected": "private-secret"},
    ],
)
def test_invalid_registration_never_echoes_secrets(api, body):
    client, _, setup, _ = api
    response = client.post("/admin/connections/oauth/atlassian/app", json=body)
    assert response.status_code == 422, response.text
    assert "private-secret" not in response.text
    setup.register.assert_not_called()


def test_provider_errors_do_not_escape_or_trigger_automatic_retry(api):
    client, _, setup, _ = api
    setup.register.side_effect = RuntimeError("private-secret")
    response = client.post("/admin/connections/oauth/atlassian/app", json={"client_id": "id", "client_secret": "private-secret"})
    assert response.status_code == 503
    assert "private-secret" not in response.text
    assert "Refresh" in response.text
    setup.register.assert_called_once()

"""Saved Identity consent can retry observation, never authorize another owner."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.auth import atlassian_mcp_routes as routes
from src.auth.middleware import get_current_user_context
from src.shared.identity.oauth_setup import get_oauth_setup
from src.shared.models.audit import AuditLog
from src.shared.models.vault import OAuthConnectionAttempt, UserCredential
from tests.auth.test_connections_routes import api, caller, session  # noqa: F401

PATH = "/auth/connections/atlassian-mcp"


@pytest.fixture
async def consented(api, session, monkeypatch):  # noqa: F811
    client, app, identity = api
    setup = SimpleNamespace(
        settings=SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", atlassian_metadata_sha256="a" * 64),
        atlassian_mcp_status=lambda **kwargs: {"status": "consent_required"},
    )
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(routes, "_consent_plan", AsyncMock(return_value={"metadata_sha256": "a" * 64, "return_url": "https://example.test/return"}))
    monkeypatch.setattr(routes, "read_tenant_policy_observation", AsyncMock(return_value="allowed"))
    observe = AsyncMock(side_effect=HTTPException(503, detail={"error": "adapter_unavailable"}))
    monkeypatch.setattr(routes, "observe_connection", observe)
    started = client.post(PATH + "/start", json={}).json()
    callback = {"state": started["state"], "session_uri": started["session_uri"]}
    assert client.post(PATH + "/complete", json=callback).status_code == 503
    attempt = await session.get(OAuthConnectionAttempt, started["attempt_id"])
    assert attempt.identity_completed_at is not None
    attempt.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await session.commit()
    observe.reset_mock()
    return client, app, identity, attempt, callback, observe


@pytest.mark.asyncio
async def test_completed_expired_consent_resumes_with_fresh_observation(consented, session):  # noqa: F811
    client, app, identity, attempt, callback, observe = consented
    completion_time = attempt.identity_completed_at
    assert client.get(PATH + "/attempts/" + attempt.state_digest).json() == {"status": "observing"}
    assert client.get(PATH + "/status").json()["pending_observation"] == {"attempt_id": attempt.state_digest}
    assert client.post(PATH + "/complete", json=callback).status_code == 403
    observe.side_effect = None
    observe.return_value = SimpleNamespace(
        attempt_id=attempt.state_digest,
        config_revision="a" * 64,
        purpose="connection_observe",
        provider_user_id="fixture-subject",
        sites=(SimpleNamespace(site_id="fixture-site", scopes=("read:jira:agent-interface",)),),
        granted_scopes=("read:me", "read:account", "read:jira:agent-interface"),
    )
    response = client.post(PATH + "/resume", json={"attempt_id": attempt.state_digest})
    assert response.status_code == 201, response.text
    observe.assert_awaited_once()
    assert identity.complete_resource_token_auth.call_count == 1
    assert identity.get_resource_oauth2_token.call_count == 1
    await session.refresh(attempt)
    assert attempt.identity_completed_at.replace(tzinfo=UTC) == completion_time.replace(tzinfo=UTC)
    assert attempt.consumed_at is not None
    assert client.post(PATH + "/resume", json={"attempt_id": attempt.state_digest}).status_code == 403
    assert len((await session.scalars(select(UserCredential))).all()) == 1
    assert len((await session.scalars(select(AuditLog).where(AuditLog.event_type == "oauth_connection_observation_resumed"))).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["owner", "service", "uncompleted", "superseded", "consumed", "config", "snapshot", "scopes", "writes"])
async def test_resume_refuses_changed_or_unconsented_attempts(consented, session, fault):  # noqa: F811
    client, app, identity, attempt, callback, observe = consented
    original_expiry = attempt.expires_at
    if fault == "owner":
        app.dependency_overrides[get_current_user_context] = lambda: caller(user_id="user-bob")
    elif fault == "service":
        app.dependency_overrides[get_current_user_context] = lambda: caller(account_type="service")
    elif fault == "uncompleted":
        attempt.identity_completed_at = None
    elif fault == "superseded":
        attempt.superseded_at = datetime.now(UTC)
    elif fault == "consumed":
        attempt.consumed_at = datetime.now(UTC)
    elif fault == "config":
        attempt.connection_snapshot = {**attempt.connection_snapshot, "config_revision": "b" * 64}
    elif fault == "snapshot":
        attempt.connection_snapshot = {**attempt.connection_snapshot, "binding_revision": "changed"}
    elif fault == "scopes":
        attempt.requested_scopes = ["arbitrary"]
    else:
        attempt.requested_scopes = list(routes.WRITE_SCOPES)
    await session.commit()
    response = client.post(PATH + "/resume", json={"attempt_id": attempt.state_digest})
    assert response.status_code in (403, 409), response.text
    observe.assert_not_awaited()
    await session.refresh(attempt)
    assert attempt.expires_at.replace(tzinfo=UTC) == original_expiry.replace(tzinfo=UTC)
    assert (await session.scalars(select(UserCredential))).all() == []


@pytest.mark.asyncio
async def test_failed_resume_keeps_saved_consent_but_creates_no_connection(consented, session):  # noqa: F811
    client, app, identity, attempt, callback, observe = consented
    response = client.post(PATH + "/resume", json={"attempt_id": attempt.state_digest})
    assert response.status_code == 503
    await session.refresh(attempt)
    assert attempt.identity_completed_at is not None and attempt.consumed_at is None
    assert (await session.scalars(select(UserCredential))).all() == []
    assert identity.complete_resource_token_auth.call_count == 1

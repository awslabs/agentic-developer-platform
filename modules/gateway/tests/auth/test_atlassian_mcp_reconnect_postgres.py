"""Actual PostgreSQL row locks fence consent that finishes after newer consent starts."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth import atlassian_mcp_routes
from src.auth.middleware import get_current_user_context
from src.shared.database import get_db
from src.shared.identity.oauth_setup import get_oauth_setup
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential
from tests.auth.test_connections_routes import caller
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url, upgrade  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_completed_attempt", [False, True, "resume_expired"])
async def test_older_observation_cannot_publish_after_new_attempt_starts(pg_url, monkeypatch, retry_completed_attempt, record_property):  # noqa: F811
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            db.add(
                Organization(
                    id="org-acme",
                    name="Acme Corp",
                    aws_accounts=[],
                    role_mappings={},
                    settings={},
                    github_installation_ids=[],
                    cognito_client_ids=[],
                )
            )
            db.add(Department(id="dept-eng", org_id="org-acme", name="Engineering"))
            db.add(Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Engineering"))
            db.add(User(id="user-alice", org_id="org-acme", team_id="team-eng", email="alice@example.test"))
            await db.commit()

        app = FastAPI()
        app.include_router(atlassian_mcp_routes.router)

        async def database():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user_context] = lambda: caller()
        setup = SimpleNamespace(
            settings=SimpleNamespace(
                oauth_workload_name="adp-tools-dev-oauth-user",
                atlassian_identity_compatibility_verified=True,
            ),
            secret_store=object(),
            atlassian_mcp_status=lambda **kwargs: {"status": "ready"},
        )
        app.dependency_overrides[get_oauth_setup] = lambda: setup
        identity = MagicMock()
        identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "workload-sentinel"}
        identity.get_resource_oauth2_token.return_value = {
            "sessionStatus": "IN_PROGRESS",
            "authorizationUrl": "https://example.test/authorize",
            "sessionUri": "opaque-session",
        }
        app.dependency_overrides[atlassian_mcp_routes.get_identity_client] = lambda: identity
        monkeypatch.setattr(
            atlassian_mcp_routes,
            "read_tenant_policy_observation",
            lambda *args, **kwargs: asyncio.sleep(0, result="allowed"),
        )
        monkeypatch.setattr(
            atlassian_mcp_routes,
            "atlassian_mcp_config",
            lambda settings, secret_store: {
                "return_url": "https://example.test/api/auth/connections/atlassian-mcp/return",
                "metadata_sha256": "a" * 64,
            },
        )
        entered = asyncio.Event()
        resume = asyncio.Event()
        observation_calls = 0

        async def observed(*, attempt, user_id, tenant_id):
            nonlocal observation_calls
            observation_calls += 1
            assert user_id == "user-alice" and tenant_id == "org-acme"
            if retry_completed_attempt and observation_calls == 1:
                raise HTTPException(503, detail={"error": "adapter_unavailable"})
            async with sessions() as admission:
                owner = await admission.scalar(
                    select(User).where(User.id == user_id, User.org_id == tenant_id).with_for_update(read=True, nowait=True)
                )
                completed = await admission.scalar(
                    select(OAuthConnectionAttempt).where(OAuthConnectionAttempt.state_digest == attempt.state_digest).with_for_update(nowait=True)
                )
                assert owner is not None and completed.identity_completed_at is not None
            if not entered.is_set():
                entered.set()
                await resume.wait()
            return SimpleNamespace(
                attempt_id=attempt.state_digest,
                config_revision="a" * 64,
                purpose="connection_observe",
                provider_user_id="subject-alice",
                sites=(SimpleNamespace(site_id="site-1", scopes=("read:jira:agent-interface",)),),
                granted_scopes=atlassian_mcp_routes.SCOPES,
            )

        monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observed)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.test") as client:
            first = await client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"})
            assert first.status_code == 200, first.text
            callback = {"state": first.json()["state"], "session_uri": first.json()["session_uri"]}
            if retry_completed_attempt:
                unavailable = await client.post("/auth/connections/atlassian-mcp/complete", json=callback)
                assert unavailable.status_code == 503, unavailable.text
                assert identity.complete_resource_token_auth.call_count == 1
            if retry_completed_attempt == "resume_expired":
                async with sessions() as db:
                    older = await db.get(OAuthConnectionAttempt, first.json()["attempt_id"])
                    older.expires_at = datetime.now(UTC) - timedelta(hours=1)
                    await db.commit()
                pending = asyncio.create_task(client.post("/auth/connections/atlassian-mcp/resume", json={"attempt_id": first.json()["attempt_id"]}))
            else:
                pending = asyncio.create_task(client.post("/auth/connections/atlassian-mcp/complete", json=callback))
            observing = asyncio.create_task(entered.wait())
            try:
                await asyncio.wait({pending, observing}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                if pending.done():
                    await pending
                assert entered.is_set(), "Observation did not reach admission"
                second = await asyncio.wait_for(
                    client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"}),
                    5,
                )
                assert second.status_code == 200, second.text
                async with sessions() as db:
                    older = await db.get(OAuthConnectionAttempt, first.json()["attempt_id"])
                    assert older.identity_completed_at is not None
                    assert older.superseded_at is not None and older.consumed_at is None
            finally:
                resume.set()
                observing.cancel()
            denied = await asyncio.wait_for(pending, 5)
            assert denied.status_code == 409, denied.text
            assert denied.json()["detail"]["error"] == "connection_changed"
            current = await client.post(
                "/auth/connections/atlassian-mcp/complete",
                json={
                    "state": second.json()["state"],
                    "session_uri": second.json()["session_uri"],
                },
            )
            assert current.status_code == 201, current.text
            assert identity.complete_resource_token_auth.call_count == 2
            async with sessions() as db:
                binding = await db.get(OAuthConnectionBinding, current.json()["id"])
                assert binding.user_id == "user-alice"
                assert binding.revision == current.json()["revision"]
                credentials = (await db.scalars(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).all()
                assert len(credentials) == 1 and credentials[0].user_id == "user-alice"
                assert "workload-sentinel" not in str(binding.__dict__) + str(credentials)
                record_property(
                    "identity_observation",
                    json.dumps(
                        {
                            "retried": retry_completed_attempt,
                            "old_attempt": first.json()["attempt_id"],
                            "new_attempt": second.json()["attempt_id"],
                            "stale_status": denied.status_code,
                            "current_status": current.status_code,
                            "identity_completions": identity.complete_resource_token_auth.call_count,
                            "owner": binding.user_id,
                            "token_persisted": "workload-sentinel" in str(binding.__dict__) + str(credentials),
                        }
                    ),
                )
    finally:
        await engine.dispose()

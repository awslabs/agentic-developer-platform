"""EXT02-t4: a delegated read intersects linked identity with live repository rights."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from src.activity import external_scope
from src.activity.routes import get_activity_service
from src.activity.service import WorkActivityPage
from src.agentauth import chat_data_routes
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential, UserIdentity
from tests.agentauth.chat_activity_fixtures import HEADERS, workload_runtime

WINDOW = {"run_id": "run-a", "from": "2026-10-01T00:00:00Z", "to": "2026-10-04T00:00:00Z", "timezone": "UTC"}
INSTANCE = "https://gitlab.example.invalid"
TIME = "2026-10-02T11:00:00Z"


@pytest.fixture
async def seeded(db_session):
    org = Organization(
        id="tenant-a",
        name="A",
        aws_accounts=[],
        cognito_client_ids=[],
        github_installation_ids=["900"],
        settings={
            "gitlab_cli_v1": {
                "provider_id": hashlib.sha256(INSTANCE.encode()).hexdigest()[:24],
                "projects": {
                    "granted": {"owner": "owner", "active": True, "instance": INSTANCE, "repo": "org/granted", "project_id": 71},
                    "denied": {"owner": "owner", "active": True, "instance": INSTANCE, "repo": "org/denied", "project_id": 72},
                },
            },
        },
    )
    db_session.add_all(
        [
            org,
            Department(id="dept-a", org_id="tenant-a", name="D"),
            Team(id="team-a", org_id="tenant-a", department_id="dept-a", name="T"),
            User(id="owner", org_id="tenant-a", team_id="team-a", name="Owner", email="owner@example.invalid", role="member"),
        ]
    )
    db_session.add_all(
        [
            UserIdentity(
                org_id="tenant-a",
                team_id="team-a",
                user_id="owner",
                provider="github",
                provider_user_id="17",
                provider_username="old-name",
                verification_method="oauth",
                verified_at=datetime.now(UTC),
            ),
            UserIdentity(
                org_id="tenant-a",
                team_id="team-a",
                user_id="owner",
                provider="gitlab",
                provider_user_id=INSTANCE + "#42",
                provider_username="changed-name",
                verification_method="credential_verified",
                verified_at=datetime.now(UTC),
            ),
            UserCredential(
                org_id="tenant-a",
                user_id="owner",
                service="gitlab",
                credential_type="api_key",
                label="own-provider",
                secret_arn="synthetic-secret-reference",
            ),
        ]
    )
    await db_session.commit()
    return db_session


@pytest.mark.asyncio
async def test_delegated_read_uses_current_rights_and_keeps_partial_authorized_events(seeded, db_session_factory, monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setenv("ADP_EXTERNAL_ACTIVITY_ENABLED", "true")
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    monkeypatch.setattr(external_scope, "get_session_factory", lambda: db_session_factory)
    ownership = AsyncMock(return_value=True)
    monkeypatch.setattr(external_scope, "verify_installation_ownership", ownership)
    mint = AsyncMock(side_effect=lambda *args, **kwargs: ("scoped" if kwargs.get("repositories") else "enumerate", "2026-10-06T00:00:00Z"))
    monkeypatch.setattr(external_scope, "mint_installation_token_with_expiry", mint)
    monkeypatch.setattr(external_scope, "resolve_tenant_app_credentials", AsyncMock(return_value=("app-id", "private-key")))
    monkeypatch.setattr(external_scope, "SecretsManagerHelper", lambda: SimpleNamespace(get_secret=lambda arn: "synthetic-user-token"))
    gitlab_provider = {"id": hashlib.sha256(INSTANCE.encode()).hexdigest()[:24], "url": INSTANCE}
    monkeypatch.setattr(external_scope, "gitlab_providers", AsyncMock(return_value=[gitlab_provider]))
    monkeypatch.setattr(external_scope, "approved_project", lambda caller, instance, project_id, repo: True)
    original_client = httpx.AsyncClient
    requests = []
    granted = True

    def handler(request):
        requests.append((request.url.path, request.headers.get("authorization") or request.headers.get("private-token")))
        path = request.url.path
        if path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [{"full_name": "org/granted"}, {"full_name": "org/denied"}], "total_count": 2})
        if path == "/user/17":
            return httpx.Response(200, json={"id": 17, "login": "new-name", "type": "User"})
        if path.endswith("/collaborators/new-name/permission"):
            return httpx.Response(200, json={"user": {"id": 17}, "permission": "read"}) if granted and "granted" in path else httpx.Response(404)
        if path == "/api/v4/user":
            return httpx.Response(200, json={"id": 42, "email": "owner@example.invalid"})
        if path == "/api/v4/user/emails":
            return httpx.Response(200, json=[{"email": "alias@example.invalid", "confirmed_at": TIME}])
        if path.startswith("/api/v4/projects/") and path.endswith(("/71", "/72")):
            if path.endswith("/71"):
                return httpx.Response(
                    200,
                    json={
                        "id": 71,
                        "path_with_namespace": "org/granted",
                        "permissions": {"project_access": {"access_level": 10}},
                    },
                )
            return httpx.Response(404)
        if path.endswith("/commits") and path.startswith("/repos/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "sha-one",
                        "author": {"id": 17},
                        "html_url": "https://github.com/org/granted/commit/sha-one",
                        "commit": {"committer": {"date": TIME}},
                    }
                ],
            )
        if path.endswith("/repository/commits"):
            return httpx.Response(200, json=[{"id": "gitlab-sha", "author_email": "alias@example.invalid", "committed_date": TIME}])
        if path.endswith("/events"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 401,
                        "author": {"id": 42},
                        "target_type": "Note",
                        "target_iid": 601,
                        "action_name": "commented on",
                        "created_at": TIME,
                        "note": {"id": 601, "noteable_type": "Issue", "noteable_iid": 4, "system": False},
                    }
                ],
            )
        if path.endswith(("/pulls", "/issues/comments", "/merge_requests")):
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected provider endpoint {path}")

    monkeypatch.setattr(external_scope.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=[], direct_cursor=None, descendant_cursor=None)
    capabilities = MagicMock()
    capabilities.verify_run.return_value = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    services = workload_runtime(capabilities)
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[chat_data_routes.runtime] = lambda: services
    app.dependency_overrides[get_activity_service] = lambda: service
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.invalid") as client:
        first = await client.post("/v1/chat/data/activity/work", headers=HEADERS, json=WINDOW)
        assert first.status_code == 200, first.text
        assert [event["source_id"] for event in first.json()["external_events"]] == [
            "github:org/granted:commit:sha-one",
            "gitlab:org/granted:commit:gitlab-sha",
            "gitlab:org/granted:comment:401",
        ]
        assert first.json()["external_events"][-1]["source_url"] == f"{INSTANCE}/org/granted/-/issues/4#note_601"
        assert first.json()["status"] == "partial"
        assert {entry["reason"] for entry in first.json()["coverage"]} >= {"repository_not_authorized"}
        forged = await client.post(
            "/v1/chat/data/activity/work",
            headers=HEADERS,
            json={**WINDOW, "user_id": "intruder"},
        )
        assert forged.status_code == 422
        granted = False
        revoked = await client.post("/v1/chat/data/activity/work", headers=HEADERS, json=WINDOW)
        assert revoked.status_code == 200
        assert [event["provider"] for event in revoked.json()["external_events"]] == ["gitlab", "gitlab"]
        org = await seeded.get(Organization, "tenant-a")
        org.github_installation_ids = []
        org.settings = {}
        await seeded.commit()
        disconnected = await client.post("/v1/chat/data/activity/work", headers=HEADERS, json=WINDOW)
        assert disconnected.status_code == 200
        assert disconnected.json()["external_events"] == []
        assert {(item["source"], item["reason"]) for item in disconnected.json()["coverage"]} >= {
            ("github", "disconnected"),
            ("gitlab", "disconnected"),
        }
    ownership.assert_awaited()
    assert mint.await_count >= 2
    assert all("denied/commits" not in path and "denied/repository/commits" not in path for path, _ in requests)
    assert not any(path.startswith("/api/v4/projects/72/") for path, _ in requests)
    assert "synthetic-user-token" not in str(first.json())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,expected_events,expected_coverage,expected_status",
    [
        ("github_rate_limit", ["github", "gitlab"], {("github", "partial", "rate_limited"), ("gitlab", "available", "queried")}, "partial"),
        ("github_primary_limit", ["github", "gitlab"], {("github", "partial", "rate_limited"), ("gitlab", "available", "queried")}, "partial"),
        ("github_secondary_limit", ["github", "gitlab"], {("github", "partial", "rate_limited"), ("gitlab", "available", "queried")}, "partial"),
        ("github_forbidden", ["gitlab"], {("github", "unavailable", "provider_failure"), ("gitlab", "available", "queried")}, "partial"),
        ("github_outage", ["gitlab"], {("github", "unavailable", "provider_failure"), ("gitlab", "available", "queried")}, "partial"),
        ("gitlab_missing_history", ["github"], {("github", "available", "queried"), ("gitlab", "partial", "history_incomplete")}, "partial"),
        ("gitlab_outage", ["github"], {("github", "available", "queried"), ("gitlab", "partial", "provider_unavailable")}, "partial"),
        ("both_partial", [], {("github", "partial", "rate_limited"), ("gitlab", "partial", "history_incomplete")}, "partial"),
        ("both_outage", [], {("github", "unavailable", "provider_failure"), ("gitlab", "unavailable", "provider_failure")}, "unavailable"),
    ],
)
async def test_delegated_provider_coverage_survives_outage_and_missing_history(
    seeded,
    db_session_factory,
    monkeypatch,
    scenario,
    expected_events,
    expected_coverage,
    expected_status,
):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setenv("ADP_EXTERNAL_ACTIVITY_ENABLED", "true")
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    org = await seeded.get(Organization, "tenant-a")
    gitlab = org.settings["gitlab_cli_v1"]
    org.settings = {**org.settings, "gitlab_cli_v1": {**gitlab, "projects": {"granted": gitlab["projects"]["granted"]}}}
    await seeded.commit()
    monkeypatch.setattr(external_scope, "get_session_factory", lambda: db_session_factory)
    monkeypatch.setattr(external_scope, "verify_installation_ownership", AsyncMock(return_value=True))
    monkeypatch.setattr(
        external_scope,
        "mint_installation_token_with_expiry",
        AsyncMock(side_effect=lambda *args, **kwargs: ("scoped" if kwargs.get("repositories") else "enumerate", "2026-10-06T00:00:00Z")),
    )
    monkeypatch.setattr(external_scope, "resolve_tenant_app_credentials", AsyncMock(return_value=("app-id", "private-key")))
    monkeypatch.setattr(external_scope, "SecretsManagerHelper", lambda: SimpleNamespace(get_secret=lambda arn: "synthetic-user-token"))
    monkeypatch.setattr(
        external_scope,
        "gitlab_providers",
        AsyncMock(
            return_value=[
                {
                    "id": hashlib.sha256(INSTANCE.encode()).hexdigest()[:24],
                    "url": INSTANCE,
                }
            ]
        ),
    )
    monkeypatch.setattr(external_scope, "approved_project", lambda caller, instance, project_id, repo: True)

    def handler(request):
        path = request.url.path
        if path == "/installation/repositories":
            if scenario in {"github_outage", "both_outage"}:
                return httpx.Response(503)
            return httpx.Response(200, json={"repositories": [{"full_name": "org/granted"}], "total_count": 1})
        if path == "/user/17":
            return httpx.Response(200, json={"id": 17, "login": "owner", "type": "User"})
        if path == "/repos/org/granted/collaborators/owner/permission":
            return httpx.Response(200, json={"user": {"id": 17}, "permission": "read"})
        if path == "/api/v4/user":
            return httpx.Response(503) if scenario == "both_outage" else httpx.Response(200, json={"id": 42, "email": "alias@example.invalid"})
        if path == "/api/v4/user/emails":
            return httpx.Response(200, json=[])
        if path == "/api/v4/projects/71":
            return httpx.Response(
                200,
                json={
                    "id": 71,
                    "path_with_namespace": "org/granted",
                    "permissions": {"project_access": {"access_level": 10}},
                },
            )
        if path == "/repos/org/granted/commits":
            if scenario == "both_partial":
                return httpx.Response(429)
            failures = {
                "github_rate_limit": (429, {}),
                "github_primary_limit": (403, {"X-RateLimit-Remaining": "0"}),
                "github_secondary_limit": (403, {"X-RateLimit-Remaining": "4999", "Retry-After": "60"}),
                "github_forbidden": (403, {}),
            }
            if scenario in failures and request.url.params["page"] == "2":
                status, headers = failures[scenario]
                return httpx.Response(status, headers=headers)
            headers = {"Link": '<https://api.github.com/repos/org/granted/commits?page=2>; rel="next"'} if scenario in failures else {}
            return httpx.Response(
                200,
                headers=headers,
                json=[
                    {
                        "sha": "sha-one",
                        "author": {"id": 17},
                        "html_url": "https://github.com/org/granted/commit/sha-one",
                        "commit": {"committer": {"date": TIME}},
                    }
                ],
            )
        if path == "/api/v4/projects/71/repository/commits":
            if scenario == "gitlab_outage":
                return httpx.Response(503)
            email = "unknown@example.invalid" if scenario in {"gitlab_missing_history", "both_partial"} else "alias@example.invalid"
            return httpx.Response(200, json=[{"id": "sha-two", "author_email": email, "committed_date": TIME}])
        if path.endswith(("/pulls", "/issues/comments", "/merge_requests", "/events")):
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected provider endpoint {path}")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(external_scope.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=[], direct_cursor=None, descendant_cursor=None)
    capabilities = MagicMock()
    capabilities.verify_run.return_value = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    services = workload_runtime(capabilities)
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[chat_data_routes.runtime] = lambda: services
    app.dependency_overrides[get_activity_service] = lambda: service
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.invalid") as client:
        response = await client.post("/v1/chat/data/activity/work", headers=HEADERS, json=WINDOW)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [event["provider"] for event in payload["external_events"]] == expected_events
    if scenario in {"github_rate_limit", "github_primary_limit", "github_secondary_limit"}:
        assert [event["source_id"] for event in payload["external_events"]] == [
            "github:org/granted:commit:sha-one",
            "gitlab:org/granted:commit:sha-two",
        ]
        assert {(item["status"], item["reason"]) for item in payload["coverage"] if item["source"] == "github"} == {("partial", "rate_limited")}
    if scenario == "github_forbidden":
        assert {(item["status"], item["reason"]) for item in payload["coverage"] if item["source"] == "github"} == {
            ("unavailable", "provider_failure")
        }
    assert {(item["source"], item["status"], item["reason"]) for item in payload["coverage"]} >= expected_coverage
    assert all(len({item["status"] for item in payload["coverage"] if item["source"] == provider}) == 1 for provider in ("github", "gitlab"))
    assert payload["status"] == expected_status
    assert "synthetic-user-token" not in response.text

"""Native source bytes stay behind scoped, bounded broker reads."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from src.auth.vault_routes import get_secrets_manager
from src.internal.connector_source_broker import jira_auth, jira_boards, provider_page
from src.products.models import ConnectorCollectionGrant, ConnectorModuleAdmission, ProductSourceBinding
from src.products.verification import observe_binding
from src.shared.models.vault import ChannelTenantMap, UserCredential
from tests.products.test_collection_jobs import admit
from tests.products.test_collection_jobs import two_sources as shared_sources

two_sources = shared_sources


@pytest.mark.asyncio
async def test_jira_fixed_scope_bytes_and_incomplete_page_denial():
    credential = UserCredential(id="credential", org_id="tenant", provider="jira", service="jira", credential_type="basic_auth", secret_arn="fixture")
    secrets = Mock(
        get_secret=Mock(return_value=json.dumps({"url": "https://fixture.atlassian.net", "email": "fixture@example.test", "token": "fixture-only"}))
    )
    binding = SimpleNamespace(provider="jira", native_id="91", namespace="https://fixture.atlassian.net")
    payload = b'{ "startAt":0,"total":1,"issues":[{"id":"7"}] }'
    seen = []

    def transport(request):
        seen.append(request)
        assert str(request.url).startswith("https://fixture.atlassian.net/rest/agile/1.0/board/91/issue?")
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        assert base64.b64decode(result["payload"]) == payload
        assert result["complete"] is True and result["private"] is False
        payload = b'{"startAt":0,"total":200,"issues":[{"id":"7"}]}'
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        binding.namespace = "https://other.atlassian.net"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
    assert len(seen) == 2
    for endpoint in (
        "http://fixture.atlassian.net",
        "https://127.0.0.1",
        "https://fixture.atlassian.net.evil.test",
        "https://fixture.atlassian.net/path",
    ):
        secrets.get_secret.return_value = json.dumps({"url": endpoint, "email": "fixture@example.test", "token": "fixture-only"})
        with pytest.raises(HTTPException) as failure:
            await jira_auth(credential, secrets)
        assert "fixture-only" not in str(failure.value.detail)


@pytest.mark.asyncio
async def test_sonar_collection_preserves_native_bytes_and_refuses_foreign_or_truncated_pages():
    credential = UserCredential(
        id="credential", org_id="tenant", provider="sonarqube", service="sonarqube", credential_type="api_key", secret_arn="fixture"
    )
    secrets = Mock(
        current_version_id=Mock(return_value="version-1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://sonar.example.test", "token": "fixture-only"}), "version-1")),
    )
    binding = SimpleNamespace(
        provider="sonarqube", native_id="group:service", credential_id="credential", tenant_id="tenant", observed={"credential_version": "version-1"}
    )
    payload = b'{ "paging":{"pageIndex":1,"pageSize":100,"total":1}, "issues":[{"key":"i-1","project":"group:service"}] }'
    seen = []

    def transport(request):
        seen.append(request)
        assert request.url.path == "/api/issues/search"
        assert request.url.params["componentKeys"] == "group:service"
        assert request.headers["Authorization"] == "Bearer fixture-only"
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        assert base64.b64decode(result["payload"]) == payload
        assert result["complete"] and not result["private"]
        credential.org_id = "other-tenant"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        credential.org_id = "tenant"
        secrets.current_version_id.return_value = "version-2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        secrets.current_version_id.return_value = "version-1"
        for invalid in (
            {"paging": {"pageIndex": 1, "pageSize": 100, "total": 501}, "issues": []},
            {"paging": {"pageIndex": 1, "pageSize": 100, "total": 1}, "issues": [{"key": "i-1", "project": "foreign"}]},
        ):
            payload = json.dumps(invalid).encode()
            with pytest.raises(HTTPException):
                await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_argocd_selected_application_and_sync_history_fail_closed():
    credential = UserCredential(id="credential", org_id="tenant", provider="argocd", service="argocd", credential_type="bearer", secret_arn="fixture")
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://argo.example.test", "token": "fixture-only"}), "v1")),
    )
    binding = SimpleNamespace(
        provider="argocd",
        native_id="checkout",
        namespace="team/cluster",
        credential_id="credential",
        tenant_id="tenant",
        observed={"credential_version": "v1"},
    )
    application = {
        "metadata": {"name": "checkout", "namespace": "cluster"},
        "spec": {"project": "team"},
        "status": {"history": [{"revision": "native-commit"}], "operationState": None},
    }
    payload = json.dumps(application).encode()
    seen = []

    def transport(request):
        seen.append(request)
        assert request.url.path == "/api/v1/applications/checkout"
        assert request.url.params["project"] == "team"
        assert request.url.params["appNamespace"] == "cluster"
        assert request.url.params["refresh"] == "false"
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "sync_operation", 1, 100, client=client)
        assert base64.b64decode(result["payload"]) == payload
        assert result["complete"] and not result["private"]
        application["metadata"]["namespace"] = "foreign"
        payload = json.dumps(application).encode()
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "application", 1, 100, client=client)
        application["metadata"]["namespace"] = "cluster"
        application["status"]["history"] = [{}] * 101
        payload = json.dumps(application).encode()
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "sync_operation", 1, 100, client=client)
        credential.org_id = "foreign"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "application", 1, 100, client=client)
        credential.org_id = "tenant"
        secrets.current_version_id.return_value = "v2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "application", 1, 100, client=client)
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_slack_collection_keeps_selected_channel_and_refuses_unread_history():
    credential = UserCredential(
        id="credential",
        org_id="tenant",
        provider="slack",
        service="slack",
        credential_type="oauth_token",
        secret_arn="fixture",
        scopes={"oauth_scopes": ["channels:read", "groups:read"]},
    )
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://slack.com", "token": "fixture-only"}), "v1")),
    )
    binding = SimpleNamespace(
        provider="slack",
        native_id="C123",
        namespace="T123",
        credential_id="credential",
        tenant_id="tenant",
        observed={"credential_version": "v1"},
    )
    identity = {"ok": True, "channel": {"id": "C123", "is_channel": True, "context_team_id": "T123"}}
    history = {"ok": True, "messages": [{"ts": "123.456", "text": "native text"}], "has_more": False, "response_metadata": {"next_cursor": ""}}
    native = json.dumps(history).encode()
    seen = []

    def transport(request):
        seen.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer fixture-only"
        assert request.url.params["channel"] == "C123"
        if request.url.path.endswith("conversations.info"):
            return httpx.Response(200, json=identity)
        assert request.url.path == "/api/conversations.history" and request.url.params["limit"] == "100"
        return httpx.Response(200, content=native)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "channel_message", 1, 100, client=client)
        assert base64.b64decode(result["payload"]) == native
        assert result["complete"] and not result["private"]
        history["response_metadata"]["next_cursor"] = "more"
        native = json.dumps(history).encode()
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "channel_message", 1, 100, client=client)
        identity["channel"]["context_team_id"] = "foreign"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "channel_message", 1, 100, client=client)
        identity["channel"]["context_team_id"] = "T123"
        credential.org_id = "foreign"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "channel", 1, 100, client=client)
        credential.org_id = "tenant"
        secrets.current_version_id.return_value = "v2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "channel", 1, 100, client=client)
    assert seen.count("/api/conversations.history") == 2
    assert seen.count("/api/conversations.info") == 3


@pytest.mark.asyncio
async def test_pagerduty_incidents_keep_selected_service_and_complete_pages():
    credential = UserCredential(
        id="credential", org_id="tenant", provider="pagerduty", service="pagerduty", credential_type="api_key", secret_arn="fixture"
    )
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://api.pagerduty.com", "token": "fixture-only"}), "v1")),
    )
    binding = SimpleNamespace(
        provider="pagerduty",
        native_id="SERVICE1",
        namespace="/",
        credential_id="credential",
        tenant_id="tenant",
        observed={"credential_version": "v1"},
    )
    service = {"service": {"id": "SERVICE1"}}
    incidents = {"incidents": [{"id": "INCIDENT1", "service": {"id": "SERVICE1"}}], "total": 1, "offset": 0, "more": False}
    raw = json.dumps(incidents).encode()
    paths = []

    def transport(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Token token=fixture-only"
        if request.url.path.endswith("/services/SERVICE1"):
            return httpx.Response(200, json=service)
        assert request.url.path == "/incidents"
        assert request.url.params["service_ids[]"] == "SERVICE1"
        assert request.url.params["total"] == "true"
        return httpx.Response(200, content=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "incident", 1, 100, client=client)
        assert base64.b64decode(result["payload"]) == raw
        assert result["complete"] and not result["private"]
        incidents["more"] = True
        raw = json.dumps(incidents).encode()
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "incident", 1, 100, client=client)
        incidents["more"] = False
        incidents["incidents"][0]["service"]["id"] = "FOREIGN"
        raw = json.dumps(incidents).encode()
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "incident", 1, 100, client=client)
        service["service"]["id"] = "FOREIGN"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "incident", 1, 100, client=client)
        credential.org_id = "foreign"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "service", 1, 100, client=client)
        credential.org_id = "tenant"
        secrets.current_version_id.return_value = "v2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "service", 1, 100, client=client)
    assert paths.count("/incidents") == 3


@pytest.mark.asyncio
async def test_jenkins_build_page_requires_exact_job_and_complete_bound():
    credential = UserCredential(
        id="credential", org_id="tenant", provider="jenkins", service="jenkins", credential_type="basic_auth", secret_arn="fixture"
    )
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(
            return_value=(json.dumps({"url": "https://jenkins.example.test/jenkins", "username": "reader", "token": "fixture-only"}), "v1")
        ),
    )
    binding = SimpleNamespace(
        provider="jenkins",
        native_id="team/api",
        namespace="team",
        credential_id="credential",
        tenant_id="tenant",
        observed={"credential_version": "v1"},
    )
    identity = {"fullName": "team/api", "name": "api", "_class": "hudson.model.FreeStyleProject"}
    build_page = {**identity, "allBuilds": [{"number": 7, "timestamp": 1700000000000, "result": "SUCCESS"}]}
    raw = json.dumps(build_page).encode()
    seen = []

    def transport(request):
        seen.append(request)
        assert request.url.path == "/jenkins/job/team/job/api/api/json"
        assert request.headers["Authorization"].startswith("Basic ")
        if request.url.params["tree"] == "fullName,name,_class":
            return httpx.Response(200, json=identity)
        assert "allBuilds[" in request.url.params["tree"]
        return httpx.Response(200, content=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "build", 1, 100, client=client)
        assert base64.b64decode(result["payload"]) == raw
        assert result["complete"] and not result["private"]
        identity["fullName"] = "team/private-sibling"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "build", 1, 100, client=client)
        identity["fullName"] = "team/api"
        build_page["fullName"] = "team/private-sibling"
        raw = json.dumps(build_page).encode()
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "build", 1, 100, client=client)
        build_page["fullName"] = "team/api"
        build_page["allBuilds"].append({"number": 8, "timestamp": 1700000000001})
        raw = json.dumps(build_page).encode()
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "build", 5, 2, client=client)
        credential.org_id = "foreign"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "job", 1, 100, client=client)
        credential.org_id = "tenant"
        secrets.current_version_id.return_value = "v2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "job", 1, 100, client=client)
    assert len(seen) == 7


@pytest.mark.asyncio
async def test_gitlab_collection_rejects_foreign_projects_truncation_and_rotation():
    credential = UserCredential(
        id="credential", org_id="tenant", provider="gitlab", service="gitlab", credential_type="api_key", secret_arn="fixture"
    )
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://gitlab.example.test", "token": "fixture-only"}), "v1")),
    )
    binding = SimpleNamespace(
        provider="gitlab",
        tenant_id="tenant",
        credential_id="credential",
        native_id="901",
        namespace="77",
        observed={"credential_version": "v1"},
    )
    project = {"id": 901, "namespace": {"id": 77, "kind": "group"}, "visibility": "private"}
    pipelines = [{"id": 12, "project_id": 901, "status": "failed", "sha": ""}]
    headers = {"X-Page": "1", "X-Per-Page": "100", "X-Next-Page": ""}
    calls = []

    def transport(request):
        calls.append(request.url.path)
        assert request.headers["PRIVATE-TOKEN"] == "fixture-only"
        if request.url.path == "/api/v4/projects/901":
            return httpx.Response(200, json=project)
        assert request.url.path == "/api/v4/projects/901/pipelines"
        assert request.url.params["page"] == "1" and request.url.params["per_page"] == "100"
        return httpx.Response(200, json=pipelines, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "pipeline", 1, 100, client=client)
        assert json.loads(base64.b64decode(result["payload"])) == pipelines
        assert result["complete"] and result["private"]
        pipelines[0]["project_id"] = 902
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline", 1, 100, client=client)
        pipelines[0]["project_id"] = 901
        headers["X-Next-Page"] = "2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline", 1, 100, client=client)
        headers["X-Next-Page"] = ""
        binding.namespace = "88"
        before = len(calls)
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline", 1, 100, client=client)
        assert len(calls) == before + 1
        binding.namespace = "77"
        project["namespace"]["kind"] = "user"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "project", 1, 100, client=client)
        project["namespace"]["kind"] = "group"
        secrets.current_version_id.return_value = "v2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "project", 1, 100, client=client)
        assert len(calls) == before + 2


@pytest.mark.asyncio
async def test_gitlab_native_details_are_bounded_scoped_and_pinned():
    credential = UserCredential(
        id="credential", org_id="tenant", provider="gitlab", service="gitlab", credential_type="api_key", secret_arn="fixture"
    )
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://gitlab.example.test", "token": "fixture-only"}), "v1")),
    )
    binding = SimpleNamespace(
        provider="gitlab",
        tenant_id="tenant",
        credential_id="credential",
        native_id="901",
        namespace="77",
        observed={"credential_version": "v1"},
    )
    summary = [{"id": 12, "project_id": 901, "sha": "abc", "status": "success"}]
    detail = b'{ "id":12, "project_id":901, "sha":"abc", "status":"success", "ref":"main" }'
    calls = []

    def transport(request):
        calls.append(request.url.path)
        assert request.headers["PRIVATE-TOKEN"] == "fixture-only"
        if request.url.path == "/api/v4/projects/901":
            return httpx.Response(200, json={"id": 901, "namespace": {"id": 77, "kind": "group"}, "visibility": "public"})
        if request.url.path == "/api/v4/projects/901/pipelines":
            assert request.url.params["per_page"] == "1" and request.url.params["page"] == "1"
            return httpx.Response(200, json=summary, headers={"X-Page": "1", "X-Per-Page": "1", "X-Next-Page": ""})
        assert request.url.path == "/api/v4/projects/901/pipelines/12"
        assert request.url.params["with_stats"] == "true"
        return httpx.Response(200, content=detail)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "pipeline_detail", 1, 1, client=client)
        assert base64.b64decode(result["payload"]) == b"[" + detail + b"]"
        assert result["complete"] is True and result["private"] is False
        assert len(calls) == 3
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline_detail", 1, 11, client=client)
        assert len(calls) == 3
        summary[0].pop("sha")
        before = len(calls)
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline_detail", 1, 1, client=client)
        assert calls[before:] == ["/api/v4/projects/901", "/api/v4/projects/901/pipelines"]
        summary[0]["sha"] = "abc"
        detail = b'{"id":12,"project_id":902,"sha":"abc","status":"success"}'
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline_detail", 1, 1, client=client)
        detail = b'{"id":12,"project_id":901,"sha":"changed","status":"success"}'
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline_detail", 1, 1, client=client)
        detail = b'{"id":12,"project_id":901,"sha":"abc","status":"success"}'
        secrets.current_version_id.return_value = "v2"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline_detail", 1, 1, client=client)
        secrets.current_version_id.return_value = "v1"
        binding.namespace = "88"
        before = len(calls)
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "pipeline_detail", 1, 1, client=client)
        assert calls[before:] == ["/api/v4/projects/901"]


@pytest.mark.asyncio
async def test_gitlab_personal_collection_checks_token_owner_before_project_read():
    credential = UserCredential(
        id="credential", org_id="tenant", provider="gitlab", service="gitlab", credential_type="api_key", secret_arn="fixture"
    )
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://gitlab.example.test", "token": "fixture-only"}), "v1")),
    )
    binding = SimpleNamespace(
        provider="gitlab",
        tenant_id="tenant",
        credential_id="credential",
        native_id="901",
        namespace="user:77",
        observed={"credential_version": "v1"},
    )
    user = {"id": 88}
    project = {"id": 901, "namespace": {"id": 123, "kind": "user"}, "owner": {"id": 77}, "visibility": "internal"}
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json=user)
        assert request.url.path == "/api/v4/projects/901"
        return httpx.Response(200, json=project)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "project", 1, 100, client=client)
        assert calls == ["/api/v4/user"]
        user["id"] = 77
        result = await provider_page(binding, credential, secrets, "project", 1, 100, client=client)
        assert result["private"] and result["complete"]
        project["owner"]["id"] = 88
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "project", 1, 100, client=client)
        assert len(calls) == 5
        project["owner"]["id"] = 77
        project["namespace"]["kind"] = "group"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "project", 1, 100, client=client)
        assert len(calls) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_type,authorization", [("api_key", "fixture-only"), ("bearer", "Bearer fixture-only")])
async def test_linear_selected_team_issues_bound_pages_and_rotation(credential_type, authorization):
    team_id = "123e4567-e89b-42d3-a456-426614174001"
    credential = UserCredential(
        id="credential", org_id="tenant", provider="linear", service="linear", credential_type=credential_type, secret_arn="fixture"
    )
    secrets = Mock(
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(json.dumps({"url": "https://api.linear.app", "token": "fixture-only"}), "v1")),
    )
    binding = SimpleNamespace(
        provider="linear",
        tenant_id="tenant",
        credential_id="credential",
        native_id=team_id,
        namespace="/",
        observed={"credential_version": "v1"},
    )
    selected = {"data": {"team": {"id": team_id, "name": "Team"}}}
    issues = {
        "data": {
            "team": {
                "id": team_id,
                "issues": {
                    "nodes": [{"id": "native-issue", "title": "Original"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": ""},
                },
            }
        }
    }
    seen = []

    def transport(request):
        seen.append(request)
        assert request.method == "POST" and request.headers["Authorization"] == authorization
        assert str(request.url) == "https://api.linear.app/graphql"
        document = json.loads(request.content)["query"]
        assert team_id in document
        return httpx.Response(200, json=issues if "issues(first:" in document else selected)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        assert json.loads(base64.b64decode(result["payload"])) == issues
        assert result["complete"] and not result["private"]
        issues["data"]["team"]["id"] = "foreign"
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        issues["data"]["team"]["id"] = team_id
        issues["data"]["team"]["issues"]["pageInfo"]["hasNextPage"] = True
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        issues["data"]["team"]["issues"]["pageInfo"]["hasNextPage"] = False
        secrets.current_version_id.return_value = "v2"
        before = len(seen)
        with pytest.raises(HTTPException):
            await provider_page(binding, credential, secrets, "issue", 1, 100, client=client)
        assert len(seen) == before


@pytest.mark.asyncio
async def test_jira_enumeration_and_verification_use_native_board(monkeypatch):
    credential = UserCredential(id="credential", org_id="tenant", provider="jira", service="jira", credential_type="basic_auth", secret_arn="fixture")
    secrets = Mock(
        get_secret=Mock(return_value=json.dumps({"url": "https://fixture.atlassian.net", "email": "fixture@example.test", "token": "fixture-only"}))
    )
    native = {"id": 91, "name": "Board"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"values": [native], "isLast": True, "startAt": 0}))
    ) as client:
        rows, cursor, coverage = await jira_boards(credential, secrets, client=client)
    assert (rows[0]["native_id"], cursor, coverage) == ("91", None, "complete")
    observed = AsyncMock(return_value=(rows, None, "complete"))
    monkeypatch.setattr("src.internal.connector_source_broker.jira_boards", observed)
    binding = SimpleNamespace(
        provider="jira", kind="tracker", credential_id=credential.id, tenant_id="tenant", native_id="91", namespace="https://fixture.atlassian.net"
    )
    result = await observe_binding(binding, credential)
    assert result["status"] == "healthy" and result["observed"]["source_revision"] == rows[0]["source_revision"]
    credential.org_id = "foreign-tenant"
    assert (await observe_binding(binding, credential))["status"] != "healthy"
    assert observed.await_count == 1


@pytest.mark.asyncio
async def test_github_installation_identity_and_redirect_denial():
    connection = ChannelTenantMap(provider="github", installation_id="17", provider_scope_id="27", org_id="tenant")
    binding = SimpleNamespace(provider="github", native_id="42", namespace="27")
    github = SimpleNamespace(
        get_installation_repository=AsyncMock(return_value={"id": 42, "owner": {"id": 27}, "private": False}),
        get_installation_token=AsyncMock(return_value="fixture-only"),
    )
    payload = b'[ {"id":7} ]'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))) as client:
        result = await provider_page(binding, connection, None, "issue", 1, 100, client=client, github_client=github)
        assert base64.b64decode(result["payload"]) == payload and result["complete"] is True
        github.get_installation_repository.return_value["owner"]["id"] = 99
        with pytest.raises(HTTPException):
            await provider_page(binding, connection, None, "issue", 1, 100, client=client, github_client=github)
    github.get_installation_repository.return_value["owner"]["id"] = 27
    github.get_installation_repository.side_effect = [
        {"id": 42, "owner": {"id": 27}, "private": False},
        {"id": 42, "owner": {"id": 27}, "private": True},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))) as client:
        with pytest.raises(HTTPException):
            await provider_page(binding, connection, None, "issue", 1, 100, client=client, github_client=github)
    github.get_installation_repository.side_effect = None
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.test"}))
    ) as client:
        with pytest.raises(HTTPException):
            await provider_page(binding, connection, None, "issue", 1, 100, client=client, github_client=github)


@pytest.mark.asyncio
async def test_broker_read_budget_and_current_authority(test_client, db_session, app, two_sources, monkeypatch):
    _, _, claim = await admit(test_client, two_sources)
    resolved = await test_client.post("/internal/v1/connector-contexts/resolve", json=claim["contexts"][0])
    assert resolved.status_code == 201, resolved.text
    handle = resolved.json()["handle"]
    native = b'{ "id":42 }'
    provider = AsyncMock(return_value={"payload": base64.b64encode(native).decode(), "complete": True, "private": False})
    monkeypatch.setattr("src.internal.connector_source_broker.provider_page", provider)
    app.dependency_overrides[get_secrets_manager] = lambda: None
    path = f"/internal/v1/connector-contexts/{handle}/page?kind=issue&page=1&limit=100"
    for _ in range(5):
        response = await test_client.get(path)
        assert response.status_code == 200, response.text
        assert base64.b64decode(response.json()["payload"]) == native
    exhausted = await test_client.get(path)
    assert exhausted.status_code == 409 and provider.await_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size,effective_limit", [(None, 10), (10, 10), (3, 3)])
async def test_gitlab_worker_page_limit_is_admitted(test_client, db_session, app, two_sources, monkeypatch, page_size, effective_limit):
    db_session.add(ConnectorModuleAdmission(tenant_id=two_sources["tenant"], enabled=True))
    credential = two_sources["credential"]
    credential.service = credential.provider = "gitlab"
    credential.credential_type = "api_key"
    grant = await db_session.get(ConnectorCollectionGrant, (two_sources["tenant"], two_sources["body"]["grant_ids"][0]))
    binding = await db_session.get(ProductSourceBinding, (two_sources["tenant"], grant.binding_id))
    binding.provider, binding.kind, binding.namespace, binding.native_id = "gitlab", "repository", "77", "901"
    grant.source_selections = [{**grant.source_selections[0], "record_type": "repository", "record_id": "901"}]
    if page_size is not None:
        two_sources["body"]["page_size"] = page_size
    await db_session.commit()
    _, _, claim = await admit(test_client, two_sources)
    assert claim["page_size"] == (page_size or 100)
    context = next(item for item in claim["contexts"] if item["grant_id"] == grant.id)
    resolved = await test_client.post("/internal/v1/connector-contexts/resolve", json=context)
    assert resolved.status_code == 201, resolved.text
    provider = AsyncMock(return_value={"payload": base64.b64encode(b"[]").decode(), "complete": True, "private": False})
    monkeypatch.setattr("src.internal.connector_source_broker.provider_page", provider)
    app.dependency_overrides[get_secrets_manager] = lambda: None
    path = f"/internal/v1/connector-contexts/{resolved.json()['handle']}/page?kind=pipeline_detail&page=1&limit="
    for invalid in (100, effective_limit + 1):
        denied = await test_client.get(path + str(invalid))
        assert denied.status_code == 409 and denied.json()["detail"]["code"] == "collection_page_bound"
    provider.assert_not_awaited()
    accepted = await test_client.get(path + str(effective_limit))
    assert accepted.status_code == 200, accepted.text
    assert provider.await_args.args[3:] == ("pipeline_detail", 1, effective_limit)

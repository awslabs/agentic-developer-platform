"""Workflow merges retain expected-head and live-authority checks on credential retry."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.agentauth.github_operations import OperationRefusedError
from src.agentauth.github_provider import WorkflowPermissionRequiredError
from src.orchestration.merge_provider import MergeProvider

HEAD = "a" * 40
MESSAGE = "refusing to allow a GitHub App to create or update workflow `.github/workflows/check.yml` without `workflows` permission"
BINDING = SimpleNamespace(org_id="acme", repo="acme/app", installation_id=42, provider_repository_id=123, pr_number=7, provider_pr_node_id="PR_7")
STATE = SimpleNamespace(head_ref="agent/issue-1", base_ref="main", head_sha=HEAD)


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setattr("src.orchestration.merge_provider.resolve_tenant_app_credentials", AsyncMock(return_value=(1, "test-key")))
    minted = []

    async def mint(app, key, installation, *, repositories, permissions):
        assert (app, key, installation, repositories) == (1, "test-key", 42, ["app"])
        minted.append(permissions)
        return f"test-token-{len(minted)}", (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    monkeypatch.setattr("src.orchestration.merge_provider.mint_installation_token_with_expiry", mint)
    return minted


async def perform(*, response, reauthorize=None):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "node_id": "PR_7",
                    "head": {"sha": HEAD, "ref": STATE.head_ref, "repo": {"id": 123}},
                    "base": {"ref": "main", "repo": {"id": 123}},
                },
            )
        assert request.method == "PUT" and request.url.path == "/repos/acme/app/pulls/7/merge"
        assert json.loads(request.content) == {"sha": HEAD, "merge_method": "squash"}
        return response(request)

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handle)) as client:
        result = await MergeProvider(client=client).perform(
            BINDING,
            STATE,
            method="squash",
            operation_key="authorized-merge",
            reauthorize=reauthorize or AsyncMock(),
        )
    return result, requests


async def test_workflow_refusal_retries_once_with_scoped_permission_and_fresh_authority(credentials):
    authorize = AsyncMock()
    calls = []

    def respond(request):
        calls.append(authorize.await_count)
        if request.headers["Authorization"] == "Bearer test-token-1":
            return httpx.Response(403, json={"message": MESSAGE})
        assert request.headers["Authorization"] == "Bearer test-token-2"
        return httpx.Response(200, json={"merged": True, "sha": "b" * 40})

    result, _ = await perform(response=respond, reauthorize=authorize)
    assert result == {"merged": True, "sha": "b" * 40}
    assert calls == [3, 6]
    assert credentials == [
        {"contents": "write", "pull_requests": "write", "metadata": "read"},
        {"contents": "write", "pull_requests": "write", "metadata": "read", "workflows": "write"},
    ]


async def test_ordinary_merge_needs_no_workflow_permission(credentials):
    result, _ = await perform(response=lambda request: httpx.Response(200, json={"merged": True, "sha": "b" * 40}))
    assert result["merged"]
    assert len(credentials) == 1 and "workflows" not in credentials[0]


@pytest.mark.parametrize(
    "status,message",
    [
        (403, "Resource not accessible by integration"),
        (401, MESSAGE),
        (403, "workflow permission missing"),
        (403, MESSAGE + " secret suffix"),
    ],
)
async def test_other_refusals_do_not_expand_credentials(credentials, status, message):
    with pytest.raises(OperationRefusedError) as exc:
        await perform(response=lambda request: httpx.Response(status, json={"message": message, "token": "secret"}))
    assert not isinstance(exc.value, WorkflowPermissionRequiredError)
    assert len(credentials) == 1
    assert message not in str(exc.value) and "secret" not in str(exc.value)


async def test_workflow_retry_cannot_loop_or_expose_provider_content(credentials):
    with pytest.raises(WorkflowPermissionRequiredError) as exc:
        await perform(response=lambda request: httpx.Response(403, json={"message": MESSAGE, "token": "secret"}))
    assert len(credentials) == 2
    assert "check.yml" not in str(exc.value) and "secret" not in str(exc.value)


async def test_revoked_authority_prevents_workflow_credential_retry(credentials):
    authorize = AsyncMock(side_effect=[None, None, None, OperationRefusedError("authority withdrawn")])
    with pytest.raises(OperationRefusedError, match="authority withdrawn"):
        await perform(response=lambda request: httpx.Response(403, json={"message": MESSAGE}), reauthorize=authorize)
    assert len(credentials) == 1


@pytest.mark.parametrize("write,evidence", [(False, False), (False, True), (True, True)])
async def test_workflow_permission_is_never_added_to_observation_tokens(credentials, write, evidence):
    with pytest.raises(ValueError, match="authorized merge mutation"):
        await MergeProvider().token(BINDING, write=write, evidence=evidence, workflows=True)
    assert not credentials

"""Resolve immutable PR identity using the tenant's scoped GitHub token."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.pr_identity import PrIdentityError, resolve_pr_identity


@pytest.fixture
def provider(monkeypatch):
    credentials = AsyncMock(return_value=("app-id", "private-key"))
    token = AsyncMock(return_value=("installation-token", "expiry"))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.knowledge.github_app_service.mint_installation_token_with_expiry", token)
    record = {"number": 12, "node_id": "PR_provider", "head": {"sha": "a" * 40}, "base": {"repo": {"id": 99, "full_name": "owner/repo"}}}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=record)

    client_type = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("src.orchestration.pr_identity.httpx.AsyncClient", client)
    return record, requests, credentials, token


async def test_identity_is_fetched_with_tenant_and_repository_scope(provider):
    _, requests, credentials, token = provider
    identity = await resolve_pr_identity(org_id="tenant", installation_id=7, repo="owner/repo", pr_number=12)
    assert (identity.provider_repository_id, identity.provider_pr_node_id, identity.head_sha) == (99, "PR_provider", "a" * 40)
    credentials.assert_awaited_once_with("tenant")
    token.assert_awaited_once_with(
        "app-id",
        "private-key",
        7,
        repositories=["repo"],
        permissions={"metadata": "read", "pull_requests": "read"},
    )
    assert str(requests[0].url) == "https://api.github.com/repos/owner/repo/pulls/12"


@pytest.mark.parametrize("mutation", ["missing_id", "missing_node", "missing_head", "wrong_number", "wrong_repo"])
async def test_provider_identity_must_be_complete_and_match_request(provider, mutation):
    record, *_ = provider
    if mutation == "missing_id":
        record["base"]["repo"]["id"] = None
    elif mutation == "missing_node":
        record["node_id"] = ""
    elif mutation == "missing_head":
        record["head"]["sha"] = ""
    elif mutation == "wrong_number":
        record["number"] = 13
    else:
        record["base"]["repo"]["full_name"] = "other/repo"
    with pytest.raises(PrIdentityError):
        await resolve_pr_identity(org_id="tenant", installation_id=7, repo="owner/repo", pr_number=12)


async def test_token_failure_does_not_expose_provider_error(provider):
    *_, token = provider
    token.side_effect = RuntimeError("private provider response")
    with pytest.raises(PrIdentityError, match="^Pull-request identity could not be verified\\.$"):
        await resolve_pr_identity(org_id="tenant", installation_id=7, repo="owner/repo", pr_number=12)

"""Branch reads use only the exact repository and verify immutable provider IDs."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.admin.connections.github_client import GitHubAppClient
from src.products import l5_branch_sources as branch_sources


@pytest.mark.parametrize("drift", [None, "repository", "owner", "ref", "object", "sha", "redirect", "access"])
async def test_resolution_has_narrow_token_and_exact_ref(monkeypatch, drift):
    calls = []

    def dispatch(request):
        calls.append(request)
        if request.url.path == "/app/installations/456/access_tokens":
            assert request.method == "POST"
            assert json.loads(request.content) == {"repository_ids": [321], "permissions": {"contents": "read", "metadata": "read"}}
            return httpx.Response(201, json={"token": "offline-repository-only", "expires_at": "2099-01-01T00:00:00Z"})
        assert request.method == "GET" and request.headers["authorization"] == "Bearer offline-repository-only"
        if request.url.path == "/repositories/321":
            return httpx.Response(
                200,
                json={
                    "id": 322 if drift == "repository" else 321,
                    "full_name": "example/repository",
                    "owner": {"id": 124 if drift == "owner" else 123, "login": "example"},
                },
            )
        assert request.url.path == "/repos/example/repository/git/ref/heads/release/current"
        assert request.url.raw_path.endswith(b"heads/release%2Fcurrent")
        if drift == "redirect":
            return httpx.Response(302, headers={"Location": "https://another.example/"})
        if drift == "access":
            return httpx.Response(403, json={"message": "refused"})
        return httpx.Response(
            200,
            json={
                "ref": "refs/heads/other" if drift == "ref" else "refs/heads/release/current",
                "object": {"type": "tag" if drift == "object" else "commit", "sha": "main" if drift == "sha" else "b" * 40},
            },
        )

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(dispatch), follow_redirects=False)
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("123", "offline-key"))
    monkeypatch.setattr("src.knowledge.github_app_service._mint_app_jwt", lambda *args: "offline-app-jwt")
    monkeypatch.setattr(branch_sources, "GitHubAppClient", lambda *args: GitHubAppClient(*args, http_client=http))
    binding = SimpleNamespace(native_id="321", namespace="123")
    mapping = SimpleNamespace(installation_id="456", provider_scope_id="123", installed_by_user_id="owner")
    selector = {
        "tenant_id": "tenant",
        "project_id": "project",
        "source_connection_id": "mapping",
        "record_id": "321",
        "record_type": "repository_branch",
        "record_revision": "release/current",
    }
    if drift:
        with pytest.raises(HTTPException) as denied:
            await branch_sources.resolve_branch(binding, mapping, selector)
        assert denied.value.status_code in {403, 409, 503}
    else:
        assert await branch_sources.resolve_branch(binding, mapping, selector) == {
            **selector,
            "record_type": "repository_commit",
            "record_revision": "b" * 40,
        }
    assert len(calls) in {2, 3}
    assert all(request.url.host == "api.github.com" for request in calls)

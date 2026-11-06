"""Bot lookup uses the verified installation's token, with errors propagated."""

import httpx
import pytest

from src.admin.connections.github_client import GitHubAppClient


@pytest.mark.parametrize("token_status, user_status", [(201, 200), (403, 200), (201, 404)])
async def test_bot_lookup_authentication(monkeypatch, token_status, user_status):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/app/installations/111/access_tokens":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "Bearer app-jwt"
            return httpx.Response(token_status, json={"token": "installation-token"})
        assert request.url.path == "/users/my-app[bot]"
        assert request.headers["Authorization"] == "Bearer installation-token"
        return httpx.Response(user_status, json={"id": 42, "login": "my-app[bot]", "type": "Bot"})

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as http:
        client = GitHubAppClient("app", "unused-key", http_client=http)
        monkeypatch.setattr(client, "_auth_headers", lambda: {"Authorization": "Bearer app-jwt"})
        if token_status >= 400 or user_status >= 400:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_bot_user("my-app[bot]", installation_id=111)
        else:
            assert (await client.get_bot_user("my-app[bot]", installation_id=111))["id"] == 42
        assert len(requests) == (1 if token_status >= 400 else 2)


async def test_repository_page_uses_only_installation_api(monkeypatch):
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/app/installations/111/access_tokens":
            assert request.headers["Authorization"] == "Bearer app-jwt"
            return httpx.Response(201, json={"token": "synthetic-installation-token"})
        assert request.url.path == "/installation/repositories"
        assert request.headers["Authorization"] == "token synthetic-installation-token"
        assert dict(request.url.params) == {"per_page": "100", "page": "2"}
        return httpx.Response(200, json={"repositories": [{"id": 35, "html_url": "http://127.0.0.1/private"}]})

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as http:
        client = GitHubAppClient("app", "unused-key", http_client=http)
        monkeypatch.setattr(client, "_auth_headers", lambda: {"Authorization": "Bearer app-jwt"})
        page = await client.list_installation_repositories_page(111, 2, 100)
        assert page["repositories"][0]["id"] == 35
    assert [path for _, path, _ in calls] == ["/app/installations/111/access_tokens", "/installation/repositories"]

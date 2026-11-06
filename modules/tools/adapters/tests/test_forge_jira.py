"""Forge authenticity, site binding and real HTTP request/response translation."""

import copy
import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from adp_tools.jira_context import jira_board_in_project
from cryptography.hazmat.primitives.asymmetric import rsa

from tool_adapters.forge_jira import SCHEMAS, JiraREST, read, verified_delivery

SITE = "12345678-1234-1234-1234-123456789abc"
APP = "ari:cloud:ecosystem::app/11111111-1111-1111-1111-111111111111"
INSTALL = "ari:cloud:ecosystem::installation/22222222-2222-2222-2222-222222222222"
ENV = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def delivery():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {
        "iss": "forge/invocation-token",
        "aud": APP,
        "iat": now,
        "exp": now + 60,
        "app": {
            "id": APP,
            "installationId": INSTALL,
            "environment": {"id": "ari:cloud:ecosystem::environment/" + APP.rsplit("/", 1)[1] + "/" + ENV},
            "apiBaseUrl": f"https://api.atlassian.com/ex/jira/{SITE}",
            "module": {"key": "adp-jira-setup"},
        },
        "context": {"cloudId": SITE, "extension": {"type": "jira:adminPage"}},
    }
    token = jwt.encode({"exp": now + 7200}, private, algorithm="RS256")
    body = SimpleNamespace(
        forge_app_id=APP,
        forge_environment_id=ENV,
        forge_installation_id=None,
        forge_action="pair",
        forge_token=token,
        site_id=SITE,
    )
    return private, claims, body


@pytest.mark.parametrize(
    "change",
    [
        None,
        "signature",
        "audience",
        "issuer",
        "expired",
        "app",
        "environment",
        "module",
        "host",
        "site",
        "installation",
        "token_expired",
    ],
)
def test_forge_delivery_requires_signature_and_pinned_app_site_and_environment(delivery, change):
    private, claims, body = delivery
    claims = copy.deepcopy(claims)
    if change == "audience":
        claims["aud"] = "other-app"
    if change == "issuer":
        claims["iss"] = "other-issuer"
    if change == "expired":
        claims["exp"] = int(time.time()) - 1
    if change == "app":
        claims["app"]["id"] = "other-app"
    if change == "environment":
        claims["app"]["environment"]["id"] = "other-env"
    if change == "module":
        claims["app"]["module"]["key"] = "other-endpoint"
    if change == "host":
        claims["app"]["apiBaseUrl"] = "https://attacker.example/ex/jira/" + SITE
    if change == "site":
        claims["context"]["cloudId"] = "other-site"
    if change == "installation":
        body.forge_installation_id = "another-installation"
    if change == "token_expired":
        body.forge_token = jwt.encode({"exp": 1}, private, algorithm="RS256")
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048) if change == "signature" else private
    body.forge_invocation = jwt.encode(claims, signing, algorithm="RS256")
    if change:
        with pytest.raises((ValueError, jwt.PyJWTError)):
            verified_delivery(body, key_resolver=lambda _: private.public_key())
    else:
        assert verified_delivery(body, key_resolver=lambda _: private.public_key()) == {
            "installation_id": INSTALL,
            "site_id": SITE,
            "expires_at": jwt.decode(body.forge_token, options={"verify_signature": False})["exp"],
        }


@pytest.mark.parametrize("operation,path", [("issues", "issue"), ("backlog", "backlog")])
def test_board_reads_use_enhanced_provider_pagination_and_project_filter(operation, path):
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/ex/jira/{SITE}/rest/software/1.0/board/5/{path}"
        assert request.url.params["jql"] == "project IN (100) ORDER BY Rank ASC"
        assert request.headers["Authorization"] == "Bearer app-token"
        if request.url.params.get("nextPageToken") == "provider-page-two":
            return httpx.Response(200, json={"issues": [{"id": "102", "key": "APP-2"}], "isLast": True})
        return httpx.Response(
            200, json={"issues": [{"id": "101", "key": "APP-1"}], "isLast": False, "nextPageToken": "provider-page-two"}
        )

    def factory(site, token, deadline):
        return JiraREST(site, token, deadline, transport=httpx.MockTransport(upstream))

    body = SimpleNamespace(
        site_id=SITE,
        operation=operation,
        schemas=SCHEMAS,
        deadline_at_ms=int(time.time() * 1000) + 10000,
        arguments={"boardId": 5, "jql": "project IN (100) ORDER BY Rank ASC", "maxResults": 20},
    )
    first = read(body, "app-token", client_factory=factory)
    body.arguments["nextPageToken"] = first["data"]["nextPageToken"]
    second = read(body, "app-token", client_factory=factory)
    assert second["data"]["issues"][0]["key"] == "APP-2"
    assert len(requests) == 2


@pytest.mark.parametrize("account_type", ["app", "atlassian"])
def test_verification_requires_app_identity_and_proves_project_and_search_access(account_type):
    requests = []

    def upstream(request):
        requests.append(request)
        if request.url.path.endswith("myself"):
            return httpx.Response(200, json={"accountId": "app-account", "accountType": account_type})
        if request.url.path.endswith("search/jql"):
            assert request.method == "POST"
            assert json.loads(request.content)["maxResults"] == 1
            return httpx.Response(200, json={"issues": [], "isLast": True})
        return httpx.Response(200, json={"values": []})

    body = SimpleNamespace(site_id=SITE, operation="verify", deadline_at_ms=int(time.time() * 1000) + 10000)

    def factory(site, token, deadline):
        return JiraREST(site, token, deadline, transport=httpx.MockTransport(upstream))

    if account_type == "app":
        assert read(body, "token", client_factory=factory)["account_id"] == "app-account"
        assert len(requests) == 3
    else:
        with pytest.raises(ValueError, match="app_identity"):
            read(body, "token", client_factory=factory)
        assert len(requests) == 1


def test_redirects_cannot_forward_app_tokens_to_other_hosts():
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example"})

    with pytest.raises(Exception):
        JiraREST(SITE, "private-token", time.monotonic() + 10, transport=httpx.MockTransport(upstream)).request(
            "/rest/api/3/myself"
        )
    assert len(calls) == 1 and calls[0].url.host == "api.atlassian.com"


@pytest.mark.parametrize(
    "location,expected_scope",
    [
        ({"projectId": 100, "projectKey": "APP"}, True),
        ({"projectId": 200, "projectKey": "OTHER"}, False),
        (None, False),
    ],
)
def test_board_type_and_scope_come_from_current_board_detail(location, expected_scope):
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/configuration"):
            return httpx.Response(
                200,
                json={
                    "id": 5,
                    "name": "Project board",
                    "estimation": {"type": "field"},
                    "location": {"type": "project", "id": "100", "key": "APP"},
                },
            )
        return httpx.Response(200, json={"id": 5, "type": "scrum", "location": location})

    def factory(site, token, deadline):
        return JiraREST(site, token, deadline, transport=httpx.MockTransport(upstream))

    body = SimpleNamespace(
        site_id=SITE,
        operation="board",
        schemas=SCHEMAS,
        arguments={"boardId": 5},
        deadline_at_ms=int(time.time() * 1000) + 10000,
    )
    result = read(body, "token", client_factory=factory)
    assert result["data"]["type"] == "scrum"
    assert result["data"]["estimation"]["type"] == "field"
    assert result["data"]["location"] == location
    assert jira_board_in_project(result["data"], "100", "APP") is expected_scope
    assert paths == [
        f"/ex/jira/{SITE}/rest/agile/1.0/board/5/configuration",
        f"/ex/jira/{SITE}/rest/agile/1.0/board/5",
    ]

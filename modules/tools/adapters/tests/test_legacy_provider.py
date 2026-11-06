"""Controlled provider oracles independent of the existing gateway handlers."""

import asyncio
import json
import socket

import httpx
import pytest
from fastapi import HTTPException

from tool_adapters import (
    legacy_github,
    legacy_jira,
    legacy_slack,
    legacy_slack_post,
    legacy_transport,
)

TOKEN = "fixture-token"
SITE_ID = "00000000-0000-4000-8000-000000000001"
USER_ID = "U12345678"
TEAM_ID = "T12345678"
CHANNEL_ID = "C12345678"
MARKER = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def provider(monkeypatch):
    calls = []
    scopes = "users:read,chat:write"

    def respond(request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        calls.append((request.method, request.url.path))
        if request.url.path == "/user":
            return httpx.Response(
                200,
                json={"id": 17, "login": "example", "type": "User"},
                headers={"x-oauth-scopes": "read:user"},
            )
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={"total_count": 1, "installations": [{"id": 74, "app_id": 51}]},
            )
        if request.url.path == "/me":
            return httpx.Response(
                200,
                json={
                    "account_type": "atlassian",
                    "account_status": "active",
                    "account_id": "account_1",
                },
            )
        if request.url.path == "/oauth/token/accessible-resources":
            return httpx.Response(200, json=[{"id": SITE_ID, "scopes": ["read:jira-work"]}])
        if request.url.path.endswith("/issue/EXAMPLE-42"):
            return httpx.Response(
                200,
                json={
                    "id": "42",
                    "key": "EXAMPLE-42",
                    "fields": {"summary": "Independent issue"},
                },
            )
        if request.url.path == "/api/auth.test":
            return httpx.Response(
                200,
                json={"ok": True, "user_id": USER_ID, "team_id": TEAM_ID},
                headers={"x-oauth-scopes": scopes},
            )
        if request.url.path == "/api/users.info":
            return httpx.Response(
                200,
                json={"ok": True, "user": {"id": USER_ID, "name": "Independent user"}},
                headers={"x-oauth-scopes": scopes},
            )
        if request.url.path == "/api/chat.postMessage":
            text = json.loads(request.content)["text"]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channel": CHANNEL_ID,
                    "ts": "1234567890.000001",
                    "message": {"user": USER_ID, "text": text},
                },
            )
        raise AssertionError(f"Unexpected outbound call: {request.url.path}")

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient
    for module in (legacy_github, legacy_jira, legacy_slack, legacy_slack_post):
        monkeypatch.setattr(module, "provider_destination", lambda url, host: httpx.URL(url))
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport, **kwargs),
    )
    return calls


async def _test_github_oauth_and_app_profile_oracles(provider):
    oauth = await legacy_github.observe_github_user(TOKEN)
    assert oauth.profile == {
        "id": 17,
        "login": "example",
        "html_url": "https://github.com/example",
    }
    assert oauth.scopes == ("read:user",)
    app = await legacy_github.observe_github_app_user(TOKEN, app_id="51")
    assert app.account_id == "17"
    assert app.resources[0]["installation_ids"] == [74]
    assert provider == [
        ("GET", "/user"),
        ("GET", "/user"),
        ("GET", "/user/installations"),
    ]


async def _test_legacy_jira_account_site_issue_oracle(provider):
    user = await legacy_jira.observe_jira_user(TOKEN)
    sites = await legacy_jira.observe_jira_sites(TOKEN)
    issue = await legacy_jira.read_jira_issue(
        TOKEN, protected_site=sites[0], cloud_id=SITE_ID, issue_id_or_key="EXAMPLE-42"
    )
    assert user.account_id == "account_1"
    assert sites == (legacy_jira.JiraSiteObservation(SITE_ID, ("read:jira-work",)),)
    assert issue == {
        "id": "42",
        "key": "EXAMPLE-42",
        "fields": {"summary": "Independent issue"},
    }
    assert provider == [
        ("GET", "/me"),
        ("GET", "/oauth/token/accessible-resources"),
        ("GET", f"/ex/jira/{SITE_ID}/rest/api/3/issue/EXAMPLE-42"),
    ]


async def _test_slack_read_and_one_fixture_write_oracles(provider, monkeypatch):
    identity = legacy_slack.SlackIdentityObservation(USER_ID, TEAM_ID, ("chat:write", "users:read"))
    profile = await legacy_slack.read_slack_user(TOKEN, protected_identity=identity, user_id=USER_ID)
    assert profile == {"id": USER_ID, "name": "Independent user"}
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
    monkeypatch.setenv(
        "ADP_TOOLS_SLACK_POST_FIXTURE",
        json.dumps(
            {
                "owner_id": "user-one",
                "team_id": TEAM_ID,
                "channel_id": CHANNEL_ID,
                "message": "Test message",
                "run_marker": MARKER,
                "approved": True,
                "isolated": True,
                "cleanup_method": "chat.delete",
            }
        ),
    )
    fixture = legacy_slack_post.approved_post_fixture(owner_id="user-one", team_id=TEAM_ID)
    text = legacy_slack_post.fixture_text(fixture, MARKER)
    result = await legacy_slack_post.post_slack_fixture(
        TOKEN,
        protected_identity=identity,
        fixture=fixture,
        owner_id="user-one",
        channel_id=CHANNEL_ID,
        run_marker=MARKER,
        text=text,
    )
    assert result == {
        "channel_id": CHANNEL_ID,
        "ts": "1234567890.000001",
        "user_id": USER_ID,
        "text": text,
        "run_marker": MARKER,
    }
    assert provider.count(("POST", "/api/chat.postMessage")) == 1
    with pytest.raises(HTTPException) as error:
        await legacy_slack_post.post_slack_fixture(
            TOKEN,
            protected_identity=identity,
            fixture=fixture,
            owner_id="user-one",
            channel_id=CHANNEL_ID,
            run_marker=MARKER,
            text="different",
        )
    assert error.value.status_code == 403
    assert provider.count(("POST", "/api/chat.postMessage")) == 1


def test_github_oauth_and_app_profile_oracles(provider):
    asyncio.run(_test_github_oauth_and_app_profile_oracles(provider))


def test_legacy_jira_account_site_issue_oracle(provider):
    asyncio.run(_test_legacy_jira_account_site_issue_oracle(provider))


def test_slack_read_and_one_fixture_write_oracles(provider, monkeypatch):
    asyncio.run(_test_slack_read_and_one_fixture_write_oracles(provider, monkeypatch))


def test_legacy_egress_refuses_private_dns_and_foreign_hosts(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(HTTPException) as error:
        legacy_transport.provider_destination("https://api.github.com/user", "api.github.com")
    assert error.value.status_code == 503
    with pytest.raises(HTTPException):
        legacy_transport.provider_destination("https://other.example/user", "api.github.com")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    assert legacy_transport.provider_destination("https://api.github.com/user", "api.github.com") == httpx.URL(
        "https://8.8.8.8/user"
    )


def test_legacy_execution_matches_protected_oracles(provider, monkeypatch):
    from tool_adapters.legacy_execution import execute_legacy

    async def verify():
        profile = await execute_legacy(TOKEN, "github", "get_user", {}, {"account_id": "17", "scopes": ["read:user"]})
        assert profile["login"] == "example"
        protected_app = {
            "account_id": "17",
            "scopes": [],
            "app_id": "51",
            "resources_sha256": "d551f65f3474a6e7ade034fc13bff6c57ca5b86d596f4855898ff051e093b7fd",
        }
        with pytest.raises(HTTPException) as unverified_app:
            await execute_legacy(TOKEN, "github", "get_user", {}, protected_app)
        assert unverified_app.value.detail == {"error": "github_app_grant_unverified"}
        with pytest.raises(HTTPException) as changed_app:
            await execute_legacy(
                TOKEN,
                "github",
                "get_user",
                {},
                {
                    **protected_app,
                    "resources_sha256": "06dc8f2e759c9caf35879829a427e53e00e69f3b1f1ebb149a1a6d58849ef2cd",
                },
            )
        assert changed_app.value.status_code == 403
        jira = await execute_legacy(
            TOKEN,
            "jira",
            "get_issue",
            {"cloudId": SITE_ID, "issueIdOrKey": "EXAMPLE-42"},
            {
                "account_id": "account_1",
                "scopes": ["read:jira-work"],
                "sites": (legacy_jira.JiraSiteObservation(SITE_ID, ("read:jira-work",)),),
            },
        )
        assert jira["fields"]["summary"] == "Independent issue"
        slack = {
            "account_id": TEAM_ID + ":" + USER_ID,
            "scopes": ["chat:write", "users:read"],
            "team_id": TEAM_ID,
            "user_id": USER_ID,
            "owner_id": "user-one",
        }
        user = await execute_legacy(TOKEN, "slack", "get_user", {"user": USER_ID}, slack)
        assert user["name"] == "Independent user"
        monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
        monkeypatch.setenv(
            "ADP_TOOLS_SLACK_POST_FIXTURE",
            json.dumps(
                {
                    "owner_id": "user-one",
                    "team_id": TEAM_ID,
                    "channel_id": CHANNEL_ID,
                    "message": "Test message",
                    "run_marker": MARKER,
                    "approved": True,
                    "isolated": True,
                    "cleanup_method": "chat.delete",
                }
            ),
        )
        marker = "Test message [run:" + MARKER + "]"
        with pytest.raises(HTTPException) as denial:
            await execute_legacy(
                TOKEN, "slack", "post_message", {"channel": CHANNEL_ID, "text": "forged", "run_marker": MARKER}, slack
            )
        assert denial.value.status_code == 403
        assert provider.count(("POST", "/api/chat.postMessage")) == 0
        post = await execute_legacy(
            TOKEN, "slack", "post_message", {"channel": CHANNEL_ID, "text": marker, "run_marker": MARKER}, slack
        )
        assert post["run_marker"] == MARKER
        assert provider.count(("POST", "/api/chat.postMessage")) == 1
        for connector, operation, arguments, protected in (
            ("github", "get_user", {}, {"account_id": "18", "scopes": ["read:user"]}),
            (
                "jira",
                "get_issue",
                {"cloudId": SITE_ID, "issueIdOrKey": "EXAMPLE-42"},
                {
                    "account_id": "other",
                    "scopes": ["read:jira-work"],
                    "sites": (legacy_jira.JiraSiteObservation(SITE_ID, ("read:jira-work",)),),
                },
            ),
            (
                "slack",
                "post_message",
                {"channel": CHANNEL_ID, "text": marker, "run_marker": MARKER},
                {**slack, "account_id": "other"},
            ),
        ):
            with pytest.raises(HTTPException) as error:
                await execute_legacy(TOKEN, connector, operation, arguments, protected)
            assert error.value.status_code == 403
        assert provider.count(("POST", "/api/chat.postMessage")) == 1

    asyncio.run(verify())

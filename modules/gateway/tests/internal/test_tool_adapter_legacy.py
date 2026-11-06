"""Core returns only current owned legacy bindings to the adapter."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.internal import tool_adapter_legacy

SITE_ID = "00000000-0000-4000-8000-000000000001"
OWNER = {"user_id": "user-one", "tenant_id": "tenant-one"}


@pytest.mark.parametrize(
    ("connector", "operation", "scope", "account"),
    [
        ("github", "get_user", "read:user", "17"),
        ("jira", "get_issue", "read:jira-work", "account_1"),
        ("slack", "get_user", "users:read", "T12345678:U12345678"),
        ("slack", "post_message", "chat:write", "T12345678:U12345678"),
    ],
)
def test_legacy_binding_reopens_owner_and_pins_provider(monkeypatch, connector, operation, scope, account):
    ledger = []
    claim = {
        "connection_id": "connection-one",
        "revision": "revision-one",
        "account_id": account,
        "scopes_sha256": "a" * 64,
        "credential_sha256": "b" * 64,
    }
    if connector == "jira":
        claim["resources_sha256"] = "c" * 64
    credential = SimpleNamespace(
        id="connection-one",
        secret_arn="agentcore-identity:adp-tools-test-oauth-user:" + ("atlassian" if connector == "jira" else connector),
        scopes={"oauth_scopes": [scope, "read:me"] if connector == "jira" else [scope]},
    )
    binding = SimpleNamespace(
        credential_id="connection-one",
        provider_account_id=account,
        observed_scopes=[scope, "read:me"] if connector == "jira" else [scope],
        observed_resources=[{"cloud_id": SITE_ID, "scopes": [scope]}] if connector == "jira" else None,
    )

    async def selected(db, *, user_id, tenant_id, expected):
        ledger.append((db, user_id, tenant_id, expected))
        return credential, binding, expected

    monkeypatch.setattr(tool_adapter_legacy, f"selected_{connector}_connection", selected)
    claims = {"connection": claim, connector + "_connection": claim, "connection_id": "connection-one"}
    if connector == "jira":
        claims["site_id"] = SITE_ID
    _, context = asyncio.run(
        tool_adapter_legacy.current_legacy_adapter_binding(
            object(),
            principal=OWNER,
            connector=connector,
            operation=operation,
            claims=claims,
        )
    )
    assert len(ledger) == 1
    assert ledger[0][1:] == ("user-one", "tenant-one", claim)
    assert context["provider_account_id"] == account
    assert scope in context["scopes"]
    assert context["workload"] == "adp-tools-test-oauth-user"
    assert context["provider"] == "adp-tools-test-" + ("atlassian" if connector == "jira" else connector) + "-user"
    assert context["site_id"] == (SITE_ID if connector == "jira" else None)
    if connector == "jira":
        assert context["site_scopes"] == ["read:jira-work"]


def test_foreign_connection_denied_before_any_database_lookup(monkeypatch):
    ledger = []

    async def selected(*args, **kwargs):
        ledger.append(kwargs)
        raise AssertionError("must not inspect another connection")

    monkeypatch.setattr(tool_adapter_legacy, "selected_slack_connection", selected)
    with pytest.raises(HTTPException) as denial:
        asyncio.run(
            tool_adapter_legacy.current_legacy_adapter_binding(
                object(),
                principal=OWNER,
                connector="slack",
                operation="post_message",
                claims={"connection": {"connection_id": "foreign"}, "slack_connection": {"connection_id": "owned"}, "connection_id": "owned"},
            )
        )
    assert denial.value.status_code == 403
    assert ledger == []


def test_app_binding_preserves_resource_digest_without_secret(monkeypatch):
    async def selected(db, *, user_id, tenant_id, expected):
        credential = SimpleNamespace(
            id="connection-one",
            scopes={"github_app_grant": {"schema_version": 1, "repository_access": False}},
            secret_arn="agentcore-identity:adp-tools-test-oauth-user:github",
        )
        binding = SimpleNamespace(
            credential_id="connection-one",
            provider_account_id="17",
            observed_scopes=[],
            observed_resources=[{"kind": "github_app_user", "app_id": 51}],
        )
        return credential, binding, expected

    monkeypatch.setattr(tool_adapter_legacy, "selected_github_connection", selected)
    connection = {"connection_id": "connection-one", "resources_sha256": "d" * 64}
    _, context = asyncio.run(
        tool_adapter_legacy.current_legacy_adapter_binding(
            object(),
            principal=OWNER,
            connector="github",
            operation="get_user",
            claims={"connection": connection, "github_connection": connection, "connection_id": "connection-one"},
        )
    )
    assert context["app_id"] == "51"
    assert context["resources_sha256"] == "d" * 64
    assert context["scopes"] == []

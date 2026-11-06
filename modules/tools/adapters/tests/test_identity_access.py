"""Only a current core admission can select a user's Identity grant."""

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from adp_tools.adapter_contracts import Admission
from fastapi import HTTPException

from tool_adapters import identity_access
from tool_adapters.config import AdapterConfiguration


def configuration(release):
    return AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/adp-core",
                "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                "edge_provenance_sha256": "a" * 64,
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
            },
        }
    )


def tool_admission(binding, *, user_id="alice"):
    now = int(time.time() * 1000)
    return Admission.model_validate(
        {
            "schema_version": "1.0",
            "admission_id": "receipt-1",
            "binding": binding,
            "purpose": "tool_call",
            "connector": "atlassian",
            "operation": "get_jira_issue",
            "identity": {
                "workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "provider": "adp-tools-dev-atlassian-mcp-user",
                "canonical_user_id": user_id,
            },
            "context": {
                "tenant_id": "org-acme",
                "canonical_user_id": user_id,
                "connection_id": "connection-" + user_id,
                "binding_revision": "revision-1",
                "site_id": "site-1",
                "scopes": ["read:jira:agent-interface"],
            },
            "issued_at_ms": now,
            "expires_at_ms": now + 5000,
            "deadline_at_ms": now + 10000,
        }
    )


def test_tool_access_uses_only_the_admitted_user_and_site_scopes(monkeypatch, release, binding):
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.side_effect = lambda **kwargs: {
        "workloadAccessToken": "private-" + kwargs["userId"],
    }
    identity.get_resource_oauth2_token.side_effect = lambda **kwargs: {
        "sessionStatus": "COMPLETED",
        "accessToken": "transient-" + kwargs["workloadIdentityToken"],
    }

    def client(service, *, config):
        assert service == "bedrock-agentcore"
        assert config.retries["total_max_attempts"] == 1
        return identity

    monkeypatch.setattr(identity_access.boto3, "client", client)
    configured = configuration(release)
    alice = asyncio.run(identity_access.retrieve_access_token(tool_admission(binding), configured))
    bob = asyncio.run(identity_access.retrieve_access_token(tool_admission(binding, user_id="bob"), configured))
    assert alice == "transient-private-alice" and bob == "transient-private-bob"
    assert [call.kwargs["userId"] for call in identity.get_workload_access_token_for_user_id.call_args_list] == [
        "alice",
        "bob",
    ]
    assert [call.kwargs["workloadIdentityToken"] for call in identity.get_resource_oauth2_token.call_args_list] == [
        "private-alice",
        "private-bob",
    ]
    for call in identity.get_resource_oauth2_token.call_args_list:
        assert call.kwargs["scopes"] == ["read:jira:agent-interface"]
        assert call.kwargs["resources"] == [identity_access.RESOURCE]
        assert call.kwargs["oauth2Flow"] == "USER_FEDERATION"
        assert "forceAuthentication" not in call.kwargs


@pytest.mark.parametrize(
    "fault",
    [
        "observation",
        "wrong_release",
        "wrong_owner",
        "missing_site",
        "expired",
        "foundation",
        "wrong_provider",
    ],
)
def test_unbound_or_stale_admission_never_retrieves_access(monkeypatch, release, binding, fault):
    configured = configuration(release)
    admission = tool_admission(binding)
    if fault == "observation":
        admission = admission.model_copy(update={"purpose": "connection_observe"})
    elif fault == "wrong_release":
        configured = configured.model_copy(
            update={"release": configured.release.model_copy(update={"group_generation": 2})}
        )
    elif fault == "wrong_owner":
        admission = admission.model_copy(
            update={
                "context": admission.context.model_copy(update={"canonical_user_id": "bob"}),
            }
        )
    elif fault == "missing_site":
        admission = admission.model_copy(update={"context": admission.context.model_copy(update={"site_id": None})})
    elif fault == "expired":
        admission = admission.model_copy(update={"expires_at_ms": int(time.time() * 1000) - 1})
    elif fault == "foundation":
        configured = configured.model_copy(update={"mode": "foundation"})
    else:
        admission = admission.model_copy(
            update={"identity": admission.identity.model_copy(update={"provider": "other"})}
        )
    sdk = MagicMock(side_effect=AssertionError("untrusted admission must not reach Identity"))
    monkeypatch.setattr(identity_access.boto3, "client", sdk)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(identity_access.retrieve_access_token(admission, configured))
    assert refused.value.status_code == 403
    sdk.assert_not_called()


@pytest.mark.parametrize("reply", ["FAILED", "IN_PROGRESS", "missing_token", "sdk_error"])
def test_invalid_grant_requires_reconnect_without_returning_a_token(monkeypatch, release, binding, reply):
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-alice"}
    if reply == "sdk_error":
        identity.get_resource_oauth2_token.side_effect = RuntimeError("private Identity error")
    else:
        identity.get_resource_oauth2_token.return_value = {
            "sessionStatus": "COMPLETED" if reply == "missing_token" else reply,
            "accessToken": None if reply == "missing_token" else "must-not-return",
        }
    monkeypatch.setattr(identity_access.boto3, "client", lambda service, **kwargs: identity)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(identity_access.retrieve_access_token(tool_admission(binding), configuration(release)))
    assert refused.value.status_code == 503
    assert refused.value.detail == {"error": "identity_unavailable"}
    assert "private" not in str(refused.value.detail)


@pytest.mark.parametrize(
    "connector,scopes",
    [
        ("github", ["read:user"]),
        ("github", []),
        ("jira", ["read:me", "read:jira-work"]),
        ("slack", ["users:read", "chat:write"]),
    ],
)
def test_legacy_identity_retrieves_only_core_admitted_grant(monkeypatch, release, binding, connector, scopes):
    configured = configuration(release)
    configured = configured.model_copy(
        update={
            "observation": configured.observation.model_copy(
                update={"legacy_identity_workload": "adp-tools-dev-oauth-user"}
            )
        }
    )
    original = tool_admission(binding)
    identity = original.identity.model_copy(
        update={
            "workload": "adp-tools-dev-oauth-user",
            "provider": f"adp-tools-dev-{'atlassian' if connector == 'jira' else connector}-user",
        }
    )
    legacy = {
        "provider_account_id": "17",
        "observed_scopes": scopes,
        "app_id": "5" if connector == "github" and not scopes else None,
        "resources_sha256": "a" * 64 if connector == "github" and not scopes else None,
    }
    admission = Admission.model_validate(
        {
            **original.model_dump(),
            "connector": connector,
            "operation": "get_user" if connector != "jira" else "get_issue",
            "identity": identity.model_dump(),
            "context": {
                **original.context.model_dump(),
                "site_id": "site-1" if connector == "jira" else None,
                "scopes": scopes,
                "legacy": legacy,
            },
        }
    )
    identity_client = MagicMock()
    identity_client.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "fixture-workload"}
    identity_client.get_resource_oauth2_token.return_value = {"accessToken": "fixture-user-token"}
    monkeypatch.setattr(identity_access.boto3, "client", lambda *args, **kwargs: identity_client)
    assert asyncio.run(identity_access.retrieve_access_token(admission, configured)) == "fixture-user-token"
    assert identity_client.get_workload_access_token_for_user_id.call_args.kwargs == {
        "workloadName": "adp-tools-dev-oauth-user",
        "userId": "alice",
    }
    resource_call = identity_client.get_resource_oauth2_token.call_args.kwargs
    assert resource_call["resourceCredentialProviderName"] == identity.provider
    assert resource_call["scopes"] == scopes
    assert "resources" not in resource_call
    invalid = admission.model_copy(update={"identity": identity.model_copy(update={"provider": "another-provider"})})
    with pytest.raises(HTTPException) as error:
        asyncio.run(identity_access.retrieve_access_token(invalid, configured))
    assert error.value.status_code == 403
    assert identity_client.get_resource_oauth2_token.call_count == 1

"""Policy observations use the current owner, grant and release, never readiness."""

import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Request

from src.internal import tool_adapter_authority
from src.internal import tool_adapter_policy as policy
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.github_oauth import credential_digest
from src.shared.identity.oauth_setup_store import read_tenant_policy_observation
from src.shared.models.oauth_setup import OAuthSetupState, OAuthTenantPolicyObservation
from src.shared.models.vault import OAuthConnectionBinding, UserCredential
from tests.internal.test_tool_adapter_authority import ADAPTER_ROLE, RELEASE, canonical, encode
from tests.internal.test_tool_adapter_authority import authority as base_authority

authority = base_authority


@pytest.fixture
async def policy_authority(authority, monkeypatch):
    client, db, credential, binding, kms = authority
    scopes = [tool_adapter_authority.TOOL_SCOPES[name] for name in policy.READS] + ["read:me", "read:account", "offline_access"]
    credential.scopes = {"oauth_scopes": scopes}
    binding.credential_digest = credential_digest(credential)
    binding.observed_scopes = scopes
    binding.observed_resources = [{"site_id": "site-1", "scopes": scopes}]
    db.add(
        OAuthSetupState(
            deployment_key="atlassian-mcp",
            tenant_id="org-acme",
            registration_mode="dynamic",
            config_digest="d" * 64,
            phase="prepared",
            updated_at=datetime.now(UTC),
        )
    )
    await db.commit()
    settings = SimpleNamespace(atlassian_metadata_sha256="c" * 64, oauth_workload_name="adp-tools-dev-oauth-user")
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(tool_adapter_authority, "get_settings", lambda: settings)
    monkeypatch.setattr(policy, "verified_caller_identity", lambda request: ADAPTER_ROLE)
    original = client.app.dependency_overrides[verify_internal_or_irsa]

    async def observation_iam(request: Request):
        await original(request)
        request.state.token_context.credential_scopes.add("tools:adapter:observe")

    client.app.dependency_overrides[verify_internal_or_irsa] = observation_iam
    yield authority


def authorization(**changes):
    deadline = int(time.time() * 1000) + 15000
    binding = {"operation_id": "probe", "receipt_id": "probe", "payload_sha256": "f" * 64, "release": RELEASE}
    claims = {
        "audience": "tool-adapters:observe",
        "caller_role_arn": "arn:aws:iam::123456789012:role/adp-core",
        "purpose": "policy_observe",
        "binding": binding,
        "deadline_at_ms": deadline,
        "probes_sha256": "f" * 64,
        "user_id": "alice",
        "tenant_id": "org-acme",
        "connection_id": "credential-alice",
        "binding_revision": "revision-1",
        "site_id": "site-1",
        "config_revision": "c" * 64,
        "provider_user_id": "provider-alice",
        "setup_digest": "d" * 64,
        **changes,
    }
    signed = encode(canonical({"claims": claims, "signature": encode(hmac.digest(b"fixture-key", canonical(claims), "sha256"))}))
    return {
        "schema_version": "1.0",
        "binding": binding,
        "purpose": "policy_observe",
        "connector": "atlassian",
        "operation": "policy_observe",
        "signed_assertion": signed,
        "deadline_at_ms": deadline,
    }


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"user_id": "bob"},
        {"tenant_id": "foreign"},
        {"site_id": "foreign"},
        {"binding_revision": "old"},
        {"config_revision": "b" * 64},
        {"provider_user_id": "foreign"},
        {"setup_digest": "b" * 64},
    ],
)
async def test_policy_authorization_requires_current_binding_without_consent_attempt(policy_authority, change):
    client, db, credential, binding, kms = policy_authority
    response = client.post("/internal/v1/tool-adapters/authorize", json=authorization(**change))
    assert response.status_code == (403 if change else 200)
    if not change:
        result = response.json()
        assert result["context"]["connection_id"] == credential.id
        assert result["purpose"] == "policy_observe"
        assert result["identity"]["canonical_user_id"] == "alice"


async def test_read_policy_observation_reuses_the_saved_write_consent_grant(policy_authority):
    client, db, credential, binding, _ = policy_authority
    consent_scopes = [*credential.scopes["oauth_scopes"], "write:jira:agent-interface"]
    credential.scopes = {"oauth_scopes": consent_scopes}
    binding.credential_digest = credential_digest(credential)
    await db.commit()
    response = client.post("/internal/v1/tool-adapters/authorize", json=authorization())
    assert response.status_code == 200, response.text
    assert response.json()["context"]["scopes"] == consent_scopes


@pytest.mark.parametrize("mutation", ["allowed", "blocked", "unknown", "stale", "future", "foreign", "reconnect", "missing"])
async def test_production_observation_returns_only_fresh_bound_results_without_tenant_wide_effects(policy_authority, monkeypatch, mutation):
    client, db, credential, binding, kms = policy_authority
    recorded = {}

    def signed(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(body=b"signed", headers={})

    monkeypatch.setattr(policy, "signed_observation_request", signed)

    async def post(self, *args, **kwargs):
        claims = recorded["claims"]
        result = {
            field: claims[field]
            for field in (
                "purpose",
                "binding",
                "connection_id",
                "binding_revision",
                "site_id",
                "config_revision",
                "user_id",
                "tenant_id",
                "provider_user_id",
            )
        }
        result.update(
            schema_version="1.0",
            observed_at_ms=int(time.time() * 1000),
            scopes=[tool_adapter_authority.TOOL_SCOPES[name] for name in policy.READS],
            probes={
                name: {"status": mutation if mutation in {"allowed", "blocked", "unknown"} else "allowed", "response_sha256": "e" * 64}
                for name in policy.READS
            },
        )
        if mutation == "stale":
            result["observed_at_ms"] -= 300001
        if mutation == "future":
            result["observed_at_ms"] += 10000
        if mutation == "foreign":
            result["tenant_id"] = "foreign"
        if mutation == "missing":
            result["probes"].pop("get_jira_issue")
        if mutation == "reconnect":
            binding.revision = "revision-2"
            await db.commit()
        return httpx.Response(200, json=result)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    body = policy.PolicyProbe(
        connection_revision="revision-1",
        site_id="site-1",
        probes={name: {"cloudId": "site-1"} for name in policy.READS},
        expected_results={name: "f" * 64 for name in policy.READS},
    )
    if mutation not in {"allowed", "blocked", "unknown"}:
        with pytest.raises(HTTPException):
            await policy.observe_policy(caller=SimpleNamespace(user_id="alice", org_id="org-acme"), body=body, connection_id=credential.id, db=db)
        assert await db.get(OAuthTenantPolicyObservation, ("atlassian-mcp", "org-acme")) is None
        return
    result = await policy.observe_policy(caller=SimpleNamespace(user_id="alice", org_id="org-acme"), body=body, connection_id=credential.id, db=db)
    assert result["status"] == mutation
    assert "signed" not in json.dumps(result)
    observed = await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="org-acme", now=datetime.now(UTC))
    assert observed is None
    second = UserCredential(
        id="credential-bob",
        label="Bob Atlassian fixture",
        org_id="org-acme",
        user_id="bob",
        service="atlassian-mcp",
        provider="agentcore-identity",
        credential_type="oauth_token",
        secret_arn="agentcore-identity:adp-tools-dev-atlassian-mcp-oauth-user:atlassian-mcp",
        scopes={
            "oauth_scopes": sorted(
                set(tool_adapter_authority.TOOL_SCOPES[name] for name in policy.READS) | {"read:me", "read:account", "offline_access"}
            )
        },
        strict=True,
        agent_consent=True,
    )
    db.add(second)
    await db.flush()
    db.add(
        OAuthConnectionBinding(
            credential_id=second.id,
            org_id="org-acme",
            user_id="bob",
            provider="atlassian-mcp",
            revision="bob-revision",
            credential_digest=credential_digest(second),
            provider_account_id="provider-bob",
            observed_scopes=second.scopes["oauth_scopes"],
            observed_resources=[{"site_id": "site-2", "scopes": second.scopes["oauth_scopes"]}],
            selected_site_id="site-2",
            verified_at=datetime.now(UTC),
        )
    )
    await db.commit()
    other = await policy.current_policy_binding(
        db,
        user_id="bob",
        tenant_id="org-acme",
        connection={"id": "credential-bob", "revision": "bob-revision", "site_id": "site-2"},
        config_revision="c" * 64,
    )
    assert other["provider_user_id"] == "provider-bob"
    assert not any(scope.startswith("write:") for scope in other["scopes"])
    assert await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="foreign", now=datetime.now(UTC)) is None
    assert (
        await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="org-acme", now=datetime.now(UTC) + timedelta(minutes=6))
        is None
    )

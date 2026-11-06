"""Migrated SQL producer and packaged worker with only external transports substituted."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.connections import oauth_apps
from src.shared.identity import atlassian_client_registration, oauth_setup_worker
from src.shared.identity.oauth_setup import OAuthAppSetup
from src.shared.models.oauth_setup import OAuthRecoveryJob, OAuthSetupState
from tests.migrations.conftest_postgres import to_async_url, upgrade

NOW = datetime(2026, 10, 22, tzinfo=UTC)
CALLBACK = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/isolated"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity_update", "lost_update_reply", "secret_persistence"])
async def test_real_producer_recovers_retained_client_without_reregistering(pg_url, monkeypatch, failure):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    issuer = "https://auth.atlassian.com/isolated-issuer"
    protected = {"resource": "https://mcp.atlassian.com/v2/mcp", "authorization_servers": [issuer]}
    authorization = {
        "issuer": issuer,
        "authorization_endpoint": "https://auth.atlassian.com/authorize",
        "token_endpoint": "https://auth.atlassian.com/oauth/token",
        "registration_endpoint": "https://auth.atlassian.com/register",
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }
    metadata_digest = hashlib.sha256(json.dumps([protected, authorization], sort_keys=True).encode()).hexdigest()
    secret_ref = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/mcp-app"
    plan = {
        "resource": protected["resource"],
        "issuer": issuer,
        "authorization_endpoint": authorization["authorization_endpoint"],
        "token_endpoint": authorization["token_endpoint"],
        "client_id": "",
        "app_secret_ref": secret_ref,
        "registration_mode": "dynamic-client-registration",
        "return_url": "https://dashboard.example.test/api/auth/connections/atlassian-mcp/return",
        "contract_revision": "test-v2",
        "protected_resource_metadata": protected,
        "authorization_server_metadata": authorization,
        "metadata_sha256": metadata_digest,
    }
    settings = SimpleNamespace(
        oauth_workload_name="adp-tools-dev-oauth-user",
        oauth_return_url="https://dashboard.example.test/api/auth/connections/oauth/return",
        aws_region="us-east-1",
        atlassian_config_ref="fixture-config",
        atlassian_contract_revision="test-v2",
        atlassian_metadata_sha256=metadata_digest,
        atlassian_identity_compatibility_verified=True,
    )
    control, secret_store = MagicMock(), MagicMock()
    stored = {"state": "unregistered"}
    identities = {"workload": None, "provider": None}
    registrations, updates = [], []

    def read_secret(**kwargs):
        if kwargs["SecretId"] == settings.atlassian_config_ref:
            return {"SecretString": json.dumps(plan)}
        assert kwargs["SecretId"] == secret_ref
        return {"SecretString": json.dumps(stored)}

    def save_secret(**kwargs):
        assert kwargs["SecretId"] == secret_ref
        new_value = json.loads(kwargs["SecretString"])
        if failure == "secret_persistence" and new_value["state"] == "registered":
            raise RuntimeError("secret storage reply lost")
        stored.clear()
        stored.update(new_value)

    def create_workload(**kwargs):
        identities["workload"] = {"allowedResourceOauth2ReturnUrls": kwargs["allowedResourceOauth2ReturnUrls"]}

    def create_provider(**kwargs):
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        identities["provider"] = {
            "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:oauth2credentialprovider/isolated",
            "credentialProviderVendor": "CustomOauth2",
            "callbackUrl": CALLBACK,
            "status": "READY",
            "clientSecretSource": "EXTERNAL",
            "oauth2ProviderConfigOutput": {
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": config["oauthDiscovery"],
                    "clientId": config["clientId"],
                    "clientAuthenticationMethod": config["clientAuthenticationMethod"],
                }
            },
        }
        control.list_tags_for_resource.return_value = {"tags": kwargs["tags"]}

    def update_provider(**kwargs):
        updates.append(kwargs["oauth2ProviderConfigInput"])
        if failure == "identity_update" and len(updates) == 1:
            raise RuntimeError("Identity update failed before effect")
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        provider = identities["provider"]
        provider["oauth2ProviderConfigOutput"]["customOauth2ProviderConfig"]["clientId"] = config["clientId"]
        provider["clientSecretSource"] = "EXTERNAL"
        provider["clientSecretArn"] = {"secretArn": secret_ref}
        provider["clientSecretJsonKey"] = "client_secret"
        if failure == "lost_update_reply" and len(updates) == 1:
            raise TimeoutError("Identity update reply lost")

    def register(url, *, json, timeout, follow_redirects):
        assert url == authorization["registration_endpoint"] and json["redirect_uris"] == [CALLBACK]
        assert json["token_endpoint_auth_method"] == "client_secret_basic" and stored["state"] == "registration_uncertain"
        assert timeout.connect == 2 and follow_redirects is False
        registrations.append(CALLBACK)
        response = {
            "client_id": "registered-client",
            "client_secret": "test-secret-sentinel",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "client_secret_basic",
        }
        return SimpleNamespace(status_code=201, content=b"controlled-registration", json=lambda: response)

    secret_store.get_secret_value.side_effect = read_secret
    secret_store.put_secret_value.side_effect = save_secret
    control.get_workload_identity.side_effect = lambda **kwargs: identities["workload"]
    control.get_oauth2_credential_provider.side_effect = lambda **kwargs: identities["provider"]
    control.create_workload_identity.side_effect = create_workload
    control.create_oauth2_credential_provider.side_effect = create_provider
    control.update_oauth2_credential_provider.side_effect = update_provider
    setup = OAuthAppSetup(settings, control, secret_store)
    monkeypatch.setattr(atlassian_client_registration, "_post_registration", register)
    monkeypatch.setattr(oauth_apps, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(oauth_apps, "datetime", SimpleNamespace(now=lambda timezone: NOW))
    original_worker = oauth_setup_worker.process_setup_once

    async def controlled_clock_worker(*args, **kwargs):
        return await original_worker(*args, **kwargs, clock=lambda: NOW)

    monkeypatch.setattr(oauth_apps, "process_setup_once", controlled_clock_worker)
    try:
        async with sessions() as db:
            result = await oauth_apps.prepare_atlassian_mcp(
                caller=SimpleNamespace(user_id="platform-owner", org_id="org-isolated"), db=db, setup=setup
            )
            assert result["status"] == "unavailable" and "test-secret-sentinel" not in json.dumps(result)
            await db.rollback()
        async with sessions() as db:
            job_id = (await db.execute(select(OAuthRecoveryJob.id))).scalar_one()
            job = await db.get(OAuthRecoveryJob, job_id)
            state = await db.get(OAuthSetupState, "atlassian-mcp")
            assert job.state == "pending" and job.last_error_category == "uncertain" and state.phase == "pending"
        restarted = OAuthAppSetup(settings, control, secret_store)
        result = await original_worker(
            sessions, setup=restarted, owner="successor", now=NOW + timedelta(seconds=60), clock=lambda: NOW + timedelta(seconds=60)
        )
        assert registrations == [CALLBACK]
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            state = await db.get(OAuthSetupState, "atlassian-mcp")
            if failure == "secret_persistence":
                assert result == "blocked" and job.state == "pending" and state.phase == "pending"
                assert stored["state"] == "registration_uncertain" and not updates
            else:
                assert result == "resolved" and job.state == "done" and state.phase == "prepared"
                assert stored["state"] == "registered"
                assert len(updates) == (2 if failure == "identity_update" else 1)
    finally:
        await engine.dispose()

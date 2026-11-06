"""Package-level worker restart checks against migrated PostgreSQL 16."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.identity import oauth_setup_worker as worker
from src.shared.identity.oauth_setup import OAuthAppSetup
from src.shared.identity.oauth_setup_store import enqueue_setup
from src.shared.models.oauth_setup import OAuthRecoveryJob, OAuthSetupState
from tests.migrations.conftest_postgres import to_async_url, upgrade

NOW = datetime(2026, 10, 22, tzinfo=UTC)


class ProviderTransport:
    def __init__(self):
        self.settings = SimpleNamespace()
        self.secret_store = object()
        self.ledger = []
        self.read_only_calls = 0

    def prepare_atlassian_mcp(self, *, before_effects):
        if before_effects is None:
            self.read_only_calls += 1
        if not self.ledger:
            if before_effects is None:
                return {"status": "setup_required"}
            before_effects()
            self.ledger.append("one-provider-registration")
            raise TimeoutError("provider reply was lost")
        return {"status": "prepared"}


@pytest.mark.asyncio
async def test_packaged_worker_survives_lost_reply_and_failed_persistence(pg_url, monkeypatch):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    transport = ProviderTransport()
    plan = {"resource": "synthetic-v2-resource"}
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(worker, "atlassian_mcp_config", lambda settings, secret_store: plan)
    try:
        async with sessions() as db:
            async with db.begin():
                job_id = await enqueue_setup(
                    db, setup_key="atlassian-mcp", config_digest=digest, registration_mode="retained-deployment-client", now=NOW
                )
        assert await worker.process_setup_once(sessions, setup=transport, owner="first", now=NOW, clock=lambda: NOW) == "uncertain"
        assert transport.ledger == ["one-provider-registration"]
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            assert job.state == "pending" and job.last_error_category == "uncertain"
        original_retire = worker.retire_setup

        async def fail_persistence(*args, **kwargs):
            raise RuntimeError("simulated failed SQL repair")

        monkeypatch.setattr(worker, "retire_setup", fail_persistence)
        with pytest.raises(RuntimeError, match="failed SQL repair"):
            await worker.process_setup_once(
                sessions, setup=transport, owner="second", now=NOW + timedelta(seconds=60), clock=lambda: NOW + timedelta(seconds=60)
            )
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            assert job.state == "leased" and job.last_error_category == "uncertain"
            assert job.terminal_at is None
        monkeypatch.setattr(worker, "retire_setup", original_retire)
        assert (
            await worker.process_setup_once(
                sessions, setup=transport, owner="successor", now=NOW + timedelta(seconds=105), clock=lambda: NOW + timedelta(seconds=105)
            )
            == "resolved"
        )
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            state = await db.get(OAuthSetupState, "atlassian-mcp")
            assert job.state == "done" and job.terminal_at == NOW + timedelta(seconds=105)
            assert state.phase == "prepared"
        assert transport.ledger == ["one-provider-registration"]
        assert transport.read_only_calls == 2
        assert (
            await worker.process_setup_once(
                sessions, setup=transport, owner="late", now=NOW + timedelta(seconds=106), clock=lambda: NOW + timedelta(seconds=106)
            )
            == "idle"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_before_effect", [True, False])
async def test_expired_setup_lease_cannot_start_effect_or_publish(pg_url, monkeypatch, expired_before_effect):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    plan = {"resource": "synthetic-v2-resource"}
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(worker, "atlassian_mcp_config", lambda settings, secret_store: plan)
    current = [NOW]

    class DelayedTransport:
        settings = SimpleNamespace()
        secret_store = object()
        effects = 0

        def prepare_atlassian_mcp(self, *, before_effects):
            if expired_before_effect:
                current[0] += timedelta(seconds=46)
            before_effects()
            self.effects += 1
            current[0] += timedelta(seconds=46)
            return {"status": "prepared"}

    transport = DelayedTransport()
    try:
        async with sessions() as db:
            async with db.begin():
                job_id = await enqueue_setup(
                    db, setup_key="atlassian-mcp", config_digest=digest, registration_mode="retained-deployment-client", now=NOW
                )
        result = await worker.process_setup_once(sessions, setup=transport, owner="stale", now=NOW, clock=lambda: current[0])
        assert result == "lost_lease"
        assert transport.effects == (0 if expired_before_effect else 1)
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            state = await db.get(OAuthSetupState, "atlassian-mcp")
            assert job.state == "leased" and job.terminal_at is None
            assert state.phase == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_setup_checks_lease_again_after_workload_creation(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    current = [NOW]
    issuer = "https://auth.atlassian.com/example-issuer"
    resource = "https://mcp.atlassian.com/v2/mcp"
    protected = {"resource": resource, "authorization_servers": [issuer]}
    authorization = {
        "issuer": issuer,
        "authorization_endpoint": "https://auth.atlassian.com/authorize",
        "token_endpoint": "https://auth.atlassian.com/oauth/token",
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }
    metadata_digest = hashlib.sha256(json.dumps([protected, authorization], sort_keys=True).encode()).hexdigest()
    plan = {
        "resource": resource,
        "issuer": issuer,
        "authorization_endpoint": authorization["authorization_endpoint"],
        "token_endpoint": authorization["token_endpoint"],
        "client_id": "precreated-client",
        "app_secret_ref": "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/mcp-app",
        "registration_mode": "retained-deployment-client",
        "return_url": "https://dashboard.example.test/api/auth/connections/atlassian-mcp/return",
        "contract_revision": "test-v2",
        "protected_resource_metadata": protected,
        "authorization_server_metadata": authorization,
        "metadata_sha256": metadata_digest,
    }
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    settings = SimpleNamespace(
        oauth_workload_name="adp-tools-dev-oauth-user",
        oauth_return_url="https://dashboard.example.test/api/auth/connections/oauth/return",
        aws_region="us-east-1",
        atlassian_config_ref="fixture-config",
        atlassian_contract_revision="test-v2",
        atlassian_metadata_sha256=metadata_digest,
    )
    control, secrets = MagicMock(), MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": json.dumps(plan)}
    control.get_workload_identity.return_value = None
    control.get_oauth2_credential_provider.return_value = None

    def create_workload(**kwargs):
        control.get_workload_identity.return_value = {"allowedResourceOauth2ReturnUrls": kwargs["allowedResourceOauth2ReturnUrls"]}
        current[0] += timedelta(seconds=46)

    control.create_workload_identity.side_effect = create_workload
    setup = OAuthAppSetup(settings, control, secrets)
    try:
        async with sessions() as db:
            async with db.begin():
                job_id = await enqueue_setup(
                    db,
                    setup_key="atlassian-mcp",
                    config_digest=digest,
                    registration_mode="retained-deployment-client",
                    now=NOW,
                )
        result = await worker.process_setup_once(sessions, setup=setup, owner="expired", now=NOW, clock=lambda: current[0])
        assert result == "lost_lease"
        control.create_workload_identity.assert_called_once()
        control.create_oauth2_credential_provider.assert_not_called()
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            state = await db.get(OAuthSetupState, "atlassian-mcp")
            assert job.state == "leased" and state.phase == "pending"
        assert await worker.process_setup_once(sessions, setup=setup, owner="successor", now=current[0], clock=lambda: current[0]) == "blocked"
        control.create_oauth2_credential_provider.assert_not_called()
    finally:
        await engine.dispose()

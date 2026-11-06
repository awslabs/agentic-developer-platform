"""Persisted revocation retries survive a worker restart without claiming provider proof."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth import atlassian_mcp_routes
from src.auth.middleware import get_current_user_context
from src.internal.github_oauth import credential_digest
from src.shared.database import get_db
from src.shared.identity.oauth_grant_worker import process_revocation_once
from src.shared.models.oauth_setup import OAuthGrantRevocation
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential
from tests.auth.test_connections_routes import caller
from tests.migrations.conftest_postgres import to_async_url, upgrade

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
RUN_REVOCATION_WORKER = """
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.shared.identity import oauth_grant_worker, oauth_setup_worker

engine = create_async_engine(os.environ['TEST_POSTGRES_URL'])
oauth_setup_worker.get_session_factory = lambda: async_sessionmaker(engine, expire_on_commit=False)
clock = datetime.fromisoformat(os.environ['TEST_CLOCK'])
oauth_setup_worker.datetime = SimpleNamespace(now=lambda timezone: clock)
oauth_grant_worker.datetime = SimpleNamespace(now=lambda timezone: clock)

def unavailable_setup():
    raise RuntimeError('revocation-only tick must not initialize Identity setup clients')

oauth_setup_worker.get_oauth_setup = unavailable_setup

if os.environ['TEST_EXACT_REPAIR'] == 'true':
    def revoke_exact(grant):
        assert grant['source_kind'] == 'attempt' and grant['owner_ref'] == 'user-alice'
        path = Path(os.environ['TEST_LEDGER_PATH'])
        ledger = json.loads(path.read_text())
        ledger['calls'].append(grant['grant_revision'])
        if grant['grant_revision'] not in ledger['effects']:
            ledger['effects'].append(grant['grant_revision'])
        path.write_text(json.dumps(ledger))
        return True

    original = oauth_grant_worker.process_revocation_once
    async def injected(sessions, *, owner, now):
        return await original(sessions, owner=owner, now=now, revoke_exact=revoke_exact)
    oauth_setup_worker.process_revocation_once = injected

if os.environ['TEST_FAIL_RETIRE'] == 'true':
    async def fail_retirement(*args, **kwargs):
        raise RuntimeError('controlled failed grant retirement')
    oauth_grant_worker.retire_revocation = fail_retirement

sys.argv = ['oauth_setup_worker', '--once']
oauth_setup_worker.main()
"""


def run_revocation_worker(*, postgres_url, now, ledger_path, repair=False, fail_retire=False):
    environment = {
        **os.environ,
        "TEST_POSTGRES_URL": postgres_url,
        "TEST_CLOCK": now.isoformat(),
        "TEST_LEDGER_PATH": str(ledger_path),
        "TEST_EXACT_REPAIR": "true" if repair else "false",
        "TEST_FAIL_RETIRE": "true" if fail_retire else "false",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    return subprocess.run(
        [sys.executable, "-c", RUN_REVOCATION_WORKER], cwd=GATEWAY_ROOT, env=environment, capture_output=True, text=True, timeout=25, check=False
    )


@pytest.mark.asyncio
async def test_revocation_worker_retries_outage_and_unsupported_api_after_restart(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    now = datetime(2026, 10, 24, tzinfo=UTC)
    grant = OAuthGrantRevocation(
        id="job-1",
        tenant_id="org-acme",
        owner_ref="user-alice",
        grant_revision="revision-1",
        source_kind="binding",
        connection_id="credential-1",
        provider_account_id="subject-alice",
        state="pending",
        due_at=now,
        attempts=0,
    )
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(grant)
            await db.commit()

        attempts = []

        def provider_down(binding):
            attempts.append(binding)
            raise RuntimeError("provider unavailable")

        first = await process_revocation_once(
            sessions,
            owner="worker-1",
            now=now,
            clock=lambda: now,
            revoke_exact=provider_down,
        )
        assert first == "uncertain"
        async with sessions() as db:
            retained = await db.get(OAuthGrantRevocation, "job-1")
            assert retained.state == "pending" and retained.last_error_category == "transport"
            assert retained.lease_owner is None and retained.terminal_at is None
            assert retained.attempts == 1
            next_due = retained.due_at
        assert len(attempts) == 1 and attempts[0]["owner_ref"] == "user-alice"
        assert "token" not in str(attempts[0]).lower()
    finally:
        await engine.dispose()

    engine = create_async_engine(to_async_url(pg_url))
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        unavailable = await process_revocation_once(sessions, owner="worker-2", now=next_due, clock=lambda: next_due)
        assert unavailable == "blocked"
        async with sessions() as db:
            retained = await db.get(OAuthGrantRevocation, "job-1")
            assert retained.state == "pending" and retained.last_error_category == "unsupported"
            assert retained.attempts == 2 and retained.terminal_at is None
            retry_due = retained.due_at
        assert len(attempts) == 1

        def exact_grant_repaired(binding):
            attempts.append(binding)
            return binding["grant_revision"] == "revision-1" and binding["connection_id"] == "credential-1"

        repaired = await process_revocation_once(
            sessions,
            owner="worker-3",
            now=retry_due,
            clock=lambda: retry_due,
            revoke_exact=exact_grant_repaired,
        )
        assert repaired == "resolved" and len(attempts) == 2
        async with sessions() as db:
            retained = await db.get(OAuthGrantRevocation, "job-1")
            assert retained.state == "done" and retained.terminal_at is not None
            assert retained.attempts == 3 and retained.lease_owner is None
        assert (
            await process_revocation_once(
                sessions,
                owner="worker-4",
                now=retry_due + timedelta(minutes=30),
                clock=lambda: retry_due,
                revoke_exact=exact_grant_repaired,
            )
            == "idle"
        )
        assert len(attempts) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_packaged_restart_retries_cancelled_migration_after_failed_retirement(pg_url, tmp_path, record_property):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ledger_path = tmp_path / "exact-grant-ledger.json"
    ledger_path.write_text(json.dumps({"calls": [], "effects": []}))
    try:
        async with sessions() as db:
            db.add(
                Organization(
                    id="org-acme", name="Acme", aws_accounts=[], role_mappings={}, settings={}, github_installation_ids=[], cognito_client_ids=[]
                )
            )
            db.add(Department(id="dept-eng", org_id="org-acme", name="Engineering"))
            db.add(Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Engineering"))
            db.add(User(id="user-alice", org_id="org-acme", team_id="team-eng", email="alice@example.test"))
            legacy = UserCredential(
                id="legacy-jira",
                org_id="org-acme",
                user_id="user-alice",
                service="jira",
                provider="agentcore-identity",
                credential_type="oauth_token",
                label="Jira OAuth",
                secret_arn="agentcore-identity:legacy:jira",
                scopes={"oauth_scopes": ["read:jira-work"]},
                strict=True,
                agent_consent=True,
            )
            db.add(legacy)
            await db.flush()
            db.add(
                OAuthConnectionBinding(
                    credential_id=legacy.id,
                    org_id="org-acme",
                    user_id="user-alice",
                    provider="atlassian",
                    revision="legacy-revision",
                    credential_digest=credential_digest(legacy),
                    provider_account_id="subject-alice",
                    observed_scopes=["read:jira-work"],
                    observed_resources=[],
                    selected_site_id=None,
                    verified_at=datetime.now(UTC),
                )
            )
            db.add(
                OAuthConnectionAttempt(
                    state_digest="b" * 64,
                    org_id="org-acme",
                    user_id="user-alice",
                    provider="atlassian-mcp",
                    session_uri="opaque-v2-session",
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            await db.commit()

        app = FastAPI()
        app.include_router(atlassian_mcp_routes.router)

        async def database():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user_context] = lambda: caller()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.test") as client:
            cancelled = await client.delete("/auth/connections/atlassian-mcp")
        assert cancelled.status_code == 200 and cancelled.json()["revocation_pending"] is True
        async with sessions() as db:
            pending = (await db.scalars(select(OAuthGrantRevocation))).one()
            job_id, first_at = pending.id, pending.due_at + timedelta(seconds=1)
            assert pending.source_kind == "attempt" and pending.grant_revision == "b" * 64
            assert (await db.get(OAuthConnectionAttempt, "b" * 64)).superseded_at is not None
            assert (await db.get(UserCredential, "legacy-jira")).agent_consent is True
            assert (await db.get(OAuthConnectionBinding, "legacy-jira")).revision == "legacy-revision"
            assert (await db.scalars(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).all() == []

        unsupported = run_revocation_worker(postgres_url=to_async_url(pg_url), now=first_at, ledger_path=ledger_path)
        assert unsupported.returncode == 0, unsupported.stderr
        assert json.loads(unsupported.stdout) == {"status": "blocked"}
        assert json.loads(ledger_path.read_text()) == {"calls": [], "effects": []}
        async with sessions() as db:
            pending = await db.get(OAuthGrantRevocation, job_id)
            assert pending.state == "pending" and pending.last_error_category == "unsupported"
            assert pending.terminal_at is None and pending.attempts == 1
            next_at = pending.due_at + timedelta(seconds=1)

        failed = run_revocation_worker(postgres_url=to_async_url(pg_url), now=next_at, ledger_path=ledger_path, repair=True, fail_retire=True)
        assert failed.returncode != 0 and "controlled failed grant retirement" in failed.stderr
        assert "opaque-v2-session" not in failed.stdout + failed.stderr
        async with sessions() as db:
            pending = await db.get(OAuthGrantRevocation, job_id)
            assert pending.state == "leased" and pending.terminal_at is None and pending.attempts == 2
            assert pending.lease_expires_at is not None
            retry_at = pending.lease_expires_at + timedelta(seconds=1)

        repaired = run_revocation_worker(postgres_url=to_async_url(pg_url), now=retry_at, ledger_path=ledger_path, repair=True)
        assert repaired.returncode == 0, repaired.stderr
        assert json.loads(repaired.stdout) == {"status": "resolved"}
        assert json.loads(ledger_path.read_text()) == {"calls": ["b" * 64, "b" * 64], "effects": ["b" * 64]}
        async with sessions() as db:
            finished = await db.get(OAuthGrantRevocation, job_id)
            assert finished.state == "done" and finished.terminal_at is not None and finished.attempts == 3
            assert (await db.get(UserCredential, "legacy-jira")).agent_consent is True
            assert (await db.get(OAuthConnectionBinding, "legacy-jira")).revision == "legacy-revision"
            assert (await db.scalars(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).all() == []
        idle = run_revocation_worker(postgres_url=to_async_url(pg_url), now=retry_at + timedelta(minutes=5), ledger_path=ledger_path, repair=True)
        assert idle.returncode == 0, idle.stderr
        assert json.loads(idle.stdout) == {"status": "idle"}
        assert len(json.loads(ledger_path.read_text())["calls"]) == 2
        assert "opaque-v2-session" not in repaired.stdout + repaired.stderr
        record_property(
            "identity_observation",
            json.dumps(
                {
                    "job": job_id,
                    "generation": finished.grant_revision,
                    "failed_repair_exit": failed.returncode,
                    "restart_exit": repaired.returncode,
                    "worker_state": finished.state,
                    "attempts": finished.attempts,
                    "ledger": json.loads(ledger_path.read_text()),
                    "legacy_revision": "legacy-revision",
                    "cancel_status": cancelled.status_code,
                }
            ),
        )
    finally:
        await engine.dispose()

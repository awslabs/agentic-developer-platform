"""Packaged setup consumer repairs the same retained client after process loss."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.connections import oauth_apps
from src.shared.identity import atlassian_client_registration
from src.shared.models.oauth_setup import OAuthRecoveryJob, OAuthSetupState
from tests.migrations.atlassian_worker_fixture import FileBackedTransport, initial_record
from tests.migrations.conftest_postgres import to_async_url, upgrade

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
RUN_WORKER = """
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.shared.identity import atlassian_client_registration, oauth_setup_worker
from tests.migrations.atlassian_worker_fixture import FileBackedTransport

transport = FileBackedTransport(Path(os.environ['TEST_LEDGER_PATH']))
atlassian_client_registration._post_registration = transport.post_registration
oauth_setup_worker.get_oauth_setup = transport.setup
engine = create_async_engine(os.environ['TEST_POSTGRES_URL'])
oauth_setup_worker.get_session_factory = lambda: async_sessionmaker(engine, expire_on_commit=False)
clock = datetime.fromisoformat(os.environ['TEST_CLOCK'])
oauth_setup_worker.datetime = SimpleNamespace(now=lambda timezone: clock)
if os.environ.get('FAIL_SQL_REPAIR') == 'true':
    async def fail_retirement(*args, **kwargs):
        raise RuntimeError('controlled failed SQL repair')
    oauth_setup_worker.retire_setup = fail_retirement
sys.argv = ['oauth_setup_worker', '--once']
oauth_setup_worker.main()
"""


def run_worker(*, ledger_path: Path, postgres_url: str, now: datetime, fail_repair=False):
    environment = os.environ.copy()
    environment.pop("AWS_PROFILE", None)
    environment.update(
        {
            "TEST_LEDGER_PATH": str(ledger_path),
            "TEST_POSTGRES_URL": postgres_url,
            "TEST_CLOCK": now.isoformat(),
            "FAIL_SQL_REPAIR": "true" if fail_repair else "false",
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    return subprocess.run(
        [sys.executable, "-c", RUN_WORKER],
        cwd=GATEWAY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )


@pytest.mark.asyncio
async def test_packaged_setup_consumer_survives_failed_repair_and_restart(pg_url, monkeypatch, tmp_path, record_property):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ledger_path = tmp_path / "independent-provider-ledger.json"
    ledger_path.write_text(json.dumps(initial_record()))
    transport = FileBackedTransport(ledger_path)
    monkeypatch.setattr(atlassian_client_registration, "_post_registration", transport.post_registration)
    monkeypatch.setattr(oauth_apps, "get_session_factory", lambda: sessions)
    try:
        async with sessions() as db:
            result = await oauth_apps.prepare_atlassian_mcp(
                caller=SimpleNamespace(user_id="fixture-platform-admin", org_id="fixture-tenant"), db=db, setup=transport.setup()
            )
            assert result["status"] == "unavailable"
            assert "sentinel-not-a-user-token" not in json.dumps(result)
            await db.commit()
        async with sessions() as db:
            job_id = (await db.execute(select(OAuthRecoveryJob.id))).scalar_one()
            job = await db.get(OAuthRecoveryJob, job_id)
            assert job.state == "pending" and job.last_error_category == "uncertain"
            first_restart = job.due_at + timedelta(seconds=1)
        first = run_worker(ledger_path=ledger_path, postgres_url=to_async_url(pg_url), now=first_restart, fail_repair=True)
        assert first.returncode != 0 and "controlled failed SQL repair" in first.stderr
        assert "sentinel-not-a-user-token" not in first.stdout + first.stderr
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            state = await db.get(OAuthSetupState, "atlassian-mcp")
            assert job.state == "leased" and job.terminal_at is None and state.phase == "pending"
            failed_repair_state = job.state
        assert len(transport.read()["registration_requests"]) == 1
        second = run_worker(ledger_path=ledger_path, postgres_url=to_async_url(pg_url), now=first_restart + timedelta(seconds=46))
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout) == {"status": "resolved"}
        assert "sentinel-not-a-user-token" not in second.stdout + second.stderr
        record = transport.read()
        assert record["provider_creates"] == 1
        assert record["registration_requests"] == [{"callback": record["provider"]["callbackUrl"], "method": "client_secret_basic"}]
        assert record["client"]["client_id"] == record["provider"]["oauth2ProviderConfigOutput"]["customOauth2ProviderConfig"]["clientId"]
        assert record["client"]["state"] == "registered"
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            state = await db.get(OAuthSetupState, "atlassian-mcp")
            assert job.state == "done" and job.terminal_at == first_restart + timedelta(seconds=46)
            assert state.phase == "prepared" and job.attempts == 3
            record_property(
                "foundation_observation",
                json.dumps(
                    {
                        "provider_creates": record["provider_creates"],
                        "registration_requests": len(record["registration_requests"]),
                        "failed_repair_exit": first.returncode,
                        "failed_repair_state": failed_repair_state,
                        "restart_exit": second.returncode,
                        "job_state": job.state,
                        "setup_phase": state.phase,
                        "attempts": job.attempts,
                    }
                ),
            )
    finally:
        await engine.dispose()
        ledger_path.unlink(missing_ok=True)
        assert not ledger_path.exists()
        record_property("foundation_ledger_remaining", json.dumps(ledger_path.exists()))

"""Real PostgreSQL proves continuation and worker claims serialize."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from l5_services.models.setup import SetupAuthorityContinuation, SetupWork
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.products.test_l5_setup_continuations import (
    api_setup,  # noqa: F401
    continue_request,
    l5_owner,  # noqa: F401
    prepared,
)

__all__ = ["api_setup", "l5_owner"]


@pytest.fixture
async def test_engine(pg_url):  # noqa: F811
    gateway = Path(__file__).resolve().parents[2]
    # Keep setup-python's relocated interpreter linked to its own libpython, as
    # the shared migration helper does. Scrub production DB/AWS configuration.
    # Explicit local package paths also test an uninstalled source checkout.
    runtime_env = {name: os.environ[name] for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PYTHONHOME", "PYTHONUSERBASE") if name in os.environ}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=gateway,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            **runtime_env,
            "PATH": os.defpath,
            "BG_DATABASE_URL": to_async_url(pg_url),
            "BG_RDS_IAM_AUTH": "false",
            "PYTHONPATH": os.pathsep.join(map(str, [gateway, gateway.parent / "L5-services/src", gateway.parents[1] / "libs/python/adp-common/src"])),
        },
    )
    assert result.returncode == 0, f"alembic upgrade failed ({result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    engine = create_async_engine(to_async_url(pg_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key", [True, False])
async def test_simultaneous_continuations_converge_or_refuse(test_client, api_setup, db_session, same_key):
    setup = api_setup
    intent, _, payload = await prepared(test_client, setup, db_session)
    responses = await asyncio.gather(
        continue_request(test_client, setup, intent, payload),
        continue_request(
            test_client, setup, intent, payload, key="devops_explicit_continuation_01" if same_key else "devops_competing_continuation_02"
        ),
    )
    assert sorted(response.status_code for response in responses) == ([200, 201] if same_key else [201, 409]), [r.text for r in responses]
    assert await db_session.scalar(select(func.count()).select_from(SetupAuthorityContinuation)) == 1
    work = await db_session.scalar(select(SetupWork))
    assert work.fence == 0 and work.lease_owner is None
    assert setup.native.mock_calls == [] and setup.assumed.call_count == 0


@pytest.mark.asyncio
async def test_worker_claim_wins_and_continuation_cannot_cross_it(test_client, api_setup, db_session):
    setup = api_setup
    intent, _, payload = await prepared(test_client, setup, db_session)
    claimed, release = asyncio.Event(), asyncio.Event()
    actual = setup.worker.binding_authorize

    async def hold_after_claim(*args):
        claimed.set()
        await asyncio.wait_for(release.wait(), 10)
        return await actual(*args)

    setup.worker.binding_authorize = hold_after_claim
    worker = asyncio.create_task(setup.worker.execute(setup.messages[0]))
    try:
        await asyncio.wait_for(claimed.wait(), 10)
        response = await continue_request(test_client, setup, intent, payload)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "setup_already_started"
    finally:
        release.set()
    assert not await asyncio.wait_for(worker, 10)
    assert setup.native.mock_calls == [] and setup.assumed.call_count == 0
    assert await db_session.scalar(select(func.count()).select_from(SetupAuthorityContinuation)) == 0


@pytest.mark.asyncio
async def test_continuation_lock_wins_before_worker_claim(test_client, api_setup, db_session, monkeypatch):
    from src import l5_setup_continuations

    setup = api_setup
    intent, _, payload = await prepared(test_client, setup, db_session)
    locked, release = asyncio.Event(), asyncio.Event()
    actual = l5_setup_continuations.current_devops_setup_scope

    async def hold_with_work_lock(*args, **kwargs):
        locked.set()
        await asyncio.wait_for(release.wait(), 10)
        return await actual(*args, **kwargs)

    monkeypatch.setattr(l5_setup_continuations, "current_devops_setup_scope", hold_with_work_lock)
    continuation = asyncio.create_task(continue_request(test_client, setup, intent, payload))
    await asyncio.wait_for(locked.wait(), 10)
    worker = asyncio.create_task(setup.worker.execute(setup.messages[0]))
    try:
        await asyncio.sleep(0.05)
        assert not worker.done()  # blocked by the real PostgreSQL work row lock
    finally:
        release.set()
    response, executed = await asyncio.wait_for(asyncio.gather(continuation, worker), 15)
    assert response.status_code == 201, response.text
    assert executed and setup.native.create_agent_space.call_count == 1

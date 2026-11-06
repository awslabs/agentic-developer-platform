"""Packaged, one-tick recovery of retained Identity setup from migrated SQL."""

import argparse
import asyncio
import hashlib
import json
import secrets
import socket
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from src.shared.database import get_session_factory
from src.shared.identity.oauth_grant_worker import process_revocation_once
from src.shared.identity.oauth_setup import atlassian_mcp_config, get_oauth_setup
from src.shared.identity.oauth_setup_store import claim_due_setup, has_setup_lease, retire_setup, retry_setup
from src.shared.models.oauth_setup import OAuthRecoveryJob, OAuthSetupState


async def process_setup_once(
    sessions: async_sessionmaker[AsyncSession], *, setup=None, owner: str, now: datetime, clock: Callable[[], datetime] | None = None
) -> str:
    clock = clock or (lambda: datetime.now(UTC))
    async with sessions() as db:
        async with db.begin():
            job_id = await claim_due_setup(db, owner=owner, now=now)
            if job_id is None:
                return "idle"
            job = await db.get(OAuthRecoveryJob, job_id)
            setup_key, revision, uncertain = job.setup_key, job.expected_revision, job.last_error_category == "uncertain"
            job.last_error_category = "uncertain"
    async with sessions() as db:
        state = await db.get(OAuthSetupState, setup_key)
        if state is None or state.config_digest != revision:
            async with sessions() as repair:
                async with repair.begin():
                    await retry_setup(repair, job_id=job_id, owner=owner, now=clock(), category="uncertain")
            return "blocked"
    setup = setup if setup is not None else get_oauth_setup()

    async def check_lease() -> None:
        async with sessions() as db:
            async with db.begin():
                if not await has_setup_lease(db, job_id=job_id, owner=owner, now=clock()):
                    raise RuntimeError("setup lease expired")

    event_loop = asyncio.get_running_loop()

    def before_effects() -> None:
        asyncio.run_coroutine_threadsafe(check_lease(), event_loop).result()

    try:
        plan = await run_in_threadpool(atlassian_mcp_config, setup.settings, setup.secret_store)
        if hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest() != revision:
            raise ValueError("setup configuration changed")
        if uncertain:
            await check_lease()
        options = {"before_effects": None if uncertain else before_effects}
        if plan.get("registration_mode") == "dynamic-client-registration":
            options = {"before_effects": before_effects, "registration_lease": before_effects, "recovery_only": uncertain}
        result = await run_in_threadpool(setup.prepare_atlassian_mcp, **options)
    except HTTPException as error:
        if error.status_code == 409 or error.detail == {"error": "atlassian_registration_uncertain"}:
            status = "blocked"
        else:
            status = "uncertain"
        result = None
    except Exception:
        status, result = "uncertain", None
    else:
        status = "resolved" if result.get("status") == "prepared" else "blocked" if uncertain else "uncertain"
    async with sessions() as db:
        async with db.begin():
            if status == "resolved":
                state = await db.get(OAuthSetupState, setup_key, with_for_update=True)
                if state is None or state.config_digest != revision:
                    status = "blocked"
                elif await retire_setup(db, job_id=job_id, owner=owner, now=clock()):
                    state.phase = "prepared"
                else:
                    status = "lost_lease"
            if status in {"blocked", "uncertain"}:
                if not await retry_setup(db, job_id=job_id, owner=owner, now=clock(), category="uncertain"):
                    status = "lost_lease"
    return status


async def _run() -> str:
    owner = f"{socket.gethostname()}-{secrets.token_hex(8)}"
    result = await process_setup_once(get_session_factory(), owner=owner, now=datetime.now(UTC))
    if result != "idle":
        return result
    return await process_revocation_once(get_session_factory(), owner=owner, now=datetime.now(UTC))


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile one due Atlassian MCP setup or grant job")
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps({"status": asyncio.run(_run())}))


if __name__ == "__main__":
    main()

"""Concurrent provisioning regressions against disposable PostgreSQL."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.admin.identity.schemas import UserCreateRequest
from src.admin.identity.users_service import UsersService
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.organization import Department, Team, User
from tests.auth.test_workspaces_postgres import sessions  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_WORKSPACE_POSTGRES_URL"), reason="Requires disposable PostgreSQL")


async def test_concurrent_create_for_same_org_and_email_uses_one_stable_user(sessions, monkeypatch):  # noqa: F811
    async with sessions() as seed:
        seed.add(Department(id="home-dept-default", org_id="home", name="Default"))
        seed.add(Team(id="home-team-default", org_id="home", department_id="home-dept-default", name="Default"))
        await seed.commit()
    row_flushed = asyncio.Event()
    release_first = asyncio.Event()
    sync = AsyncMock()
    sync.create_user_and_invite.return_value = {"Username": "concurrent@example.com", "Attributes": [{"Name": "sub", "Value": "concurrent-sub"}]}
    request = UserCreateRequest(email="concurrent@example.com", send_invite=False)
    async with sessions() as first, sessions() as second:
        flush = first.flush

        async def blocked_flush(*args, **kwargs):
            await flush(*args, **kwargs)
            row_flushed.set()
            await release_first.wait()

        monkeypatch.setattr(first, "flush", blocked_flush)
        task1 = asyncio.create_task(UsersService(first, cognito_sync=sync, identity_writer=AsyncMock()).create_user("home", request))
        await asyncio.wait_for(row_flushed.wait(), 5)
        task2 = asyncio.create_task(UsersService(second, cognito_sync=sync, identity_writer=AsyncMock()).create_user("home", request))
        try:
            # The second request must wait for the org lock before checking
            # for a row; without the lock it commits an ambiguous second user.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task2), 0.1)
        finally:
            release_first.set()
        created = await asyncio.wait_for(task1, 5)
        with pytest.raises(BedrockGatewayError) as error:
            await asyncio.wait_for(task2, 5)
        assert error.value.status_code == 409
        assert error.value.details["user_id"] == created.id
        await second.rollback()
    async with sessions() as check:
        rows = (await check.scalars(select(User).where(User.org_id == "home", User.email == request.email))).all()
        assert [row.id for row in rows] == [created.id]
    sync.create_user_and_invite.assert_awaited_once()

"""Tests for src.admin.connections.bot_identity.seed_bot_identity.

Covers the fix that folds platform/scripts/seed-bot-identities.py's manual
one-off into the install-callback flow: the agent's own bot account should
resolve as a known identity from the very first installation, instead of
403'ing as unknown_user on its own comment activity until someone notices
and runs the script by hand.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.admin.connections.bot_identity import seed_bot_identity
from src.shared.models.organization import Department, Team, User


def _github_client(bot_id: int = 317952797, bot_login: str = "es-adp[bot]"):
    client = MagicMock()
    client.get_bot_user = AsyncMock(return_value={"id": bot_id, "login": bot_login, "type": "Bot"})
    return client


@pytest.mark.asyncio
async def test_creates_department_team_and_bot_user(db_session):
    github_client = _github_client()

    await seed_bot_identity(
        org_id="org-001",
        app_slug="es-adp",
        github_client=github_client,
        db=db_session,
    )

    dept = (
        await db_session.execute(select(Department).where(Department.org_id == "org-001"))
    ).scalar_one()
    assert dept.name == "bot-agents"

    team = (await db_session.execute(select(Team).where(Team.org_id == "org-001"))).scalar_one()
    assert team.name == "bot-agents"
    assert team.department_id == dept.id

    user = (
        await db_session.execute(
            select(User).where(User.org_id == "org-001", User.user_kind == "bot")
        )
    ).scalar_one()
    assert user.bot_kind == "es-adp"
    assert user.team_id == team.id
    assert user.is_shadow is False


@pytest.mark.asyncio
async def test_writes_ddb_identity_index_with_bot_fields(db_session, monkeypatch):
    github_client = _github_client(bot_id=42, bot_login="my-app[bot]")

    mock_writer = MagicMock()
    mock_writer.put_user_identity = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.admin.connections.bot_identity.IdentityIndexWriter",
        lambda: mock_writer,
    )

    await seed_bot_identity(
        org_id="org-002",
        app_slug="my-app",
        github_client=github_client,
        db=db_session,
    )

    mock_writer.put_user_identity.assert_awaited_once()
    call_kwargs = mock_writer.put_user_identity.call_args.kwargs
    assert call_kwargs["provider_user_id"] == "42"
    assert call_kwargs["org_id"] == "org-002"
    assert call_kwargs["provider_username"] == "my-app[bot]"
    assert call_kwargs["user_kind"] == "bot"
    assert call_kwargs["bot_kind"] == "my-app"


@pytest.mark.asyncio
async def test_second_call_is_idempotent(db_session):
    """A reinstall (or the two install-callback call sites both firing) must
    not create duplicate department/team/user rows."""
    github_client = _github_client()

    await seed_bot_identity(org_id="org-003", app_slug="es-adp", github_client=github_client, db=db_session)
    await seed_bot_identity(org_id="org-003", app_slug="es-adp", github_client=github_client, db=db_session)

    depts = (
        await db_session.execute(select(Department).where(Department.org_id == "org-003"))
    ).scalars().all()
    users = (
        await db_session.execute(select(User).where(User.org_id == "org-003", User.user_kind == "bot"))
    ).scalars().all()
    assert len(depts) == 1
    assert len(users) == 1


@pytest.mark.asyncio
async def test_no_github_client_skips_without_raising(db_session):
    """No GitHub client available (e.g. app credentials not configured) —
    must not raise, since this is a best-effort seed that must never block
    the install it's attached to."""
    await seed_bot_identity(org_id="org-004", app_slug="es-adp", github_client=None, db=db_session)

    users = (
        await db_session.execute(select(User).where(User.org_id == "org-004"))
    ).scalars().all()
    assert users == []


@pytest.mark.asyncio
async def test_github_lookup_failure_skips_without_raising(db_session):
    """The bot's numeric id can't be resolved (rate-limited, network error,
    app not yet fully provisioned) — must degrade to a no-op, not raise."""
    github_client = MagicMock()
    github_client.get_bot_user = AsyncMock(side_effect=Exception("boom"))

    await seed_bot_identity(org_id="org-005", app_slug="es-adp", github_client=github_client, db=db_session)

    users = (
        await db_session.execute(select(User).where(User.org_id == "org-005"))
    ).scalars().all()
    assert users == []

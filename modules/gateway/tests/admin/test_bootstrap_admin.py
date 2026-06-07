"""Tests for the first-admin bootstrap (src/admin/onboarding/bootstrap_admin.py).

Mirrors the in-memory SQLite + USER_IDENTITY_INDEX_V2_WRITE patch + mocked
IdentityIndexWriter pattern used by test_onboarding_handler.py.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.onboarding.bootstrap_admin import bootstrap_first_admin
from src.shared.models.base import Base
from src.shared.models.organization import Department, Team, User
from src.shared.models.vault import UserIdentity

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
SUB = "cognito-sub-admin-1"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


def _writer():
    w = MagicMock()
    w.put_user_identity = AsyncMock(return_value=True)
    return w


@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_bootstrap_creates_org_and_admin_user(db_session):
    result = await bootstrap_first_admin(
        db_session,
        cognito_sub=SUB,
        org_id="platform-admin",
        display_name="platform-admin",
        email="admin@example.com",
        identity_writer=_writer(),
    )
    assert result["status"] == "bootstrapped"
    assert result["org_id"] == "platform-admin"

    user = await db_session.scalar(select(User).where(User.cognito_sub == SUB))
    assert user is not None
    assert user.role == "platform_admin"  # not the org_admin approve_request writes
    assert user.email == "admin@example.com"  # real email, not <login>@github.onboard
    assert user.org_id == "platform-admin"

    dept = (await db_session.execute(select(Department).where(Department.org_id == "platform-admin"))).scalar_one()
    assert dept.name == "Default"
    team = (await db_session.execute(select(Team).where(Team.org_id == "platform-admin"))).scalar_one()
    assert team.name == "Default"


@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_bootstrap_only_cognito_identity(db_session):
    """The stray github identity approve_request creates must be removed."""
    await bootstrap_first_admin(
        db_session,
        cognito_sub=SUB,
        org_id="platform-admin",
        display_name="platform-admin",
        email="admin@example.com",
        identity_writer=_writer(),
    )
    user = await db_session.scalar(select(User).where(User.cognito_sub == SUB))
    idents = (await db_session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).scalars().all()
    assert len(idents) == 1
    assert idents[0].provider == "cognito"


@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_bootstrap_idempotent_rerun(db_session):
    first = await bootstrap_first_admin(
        db_session,
        cognito_sub=SUB,
        org_id="platform-admin",
        display_name="platform-admin",
        email="admin@example.com",
        identity_writer=_writer(),
    )
    assert first["status"] == "bootstrapped"

    second = await bootstrap_first_admin(
        db_session,
        cognito_sub=SUB,
        org_id="platform-admin",
        display_name="platform-admin",
        email="admin@example.com",
        identity_writer=_writer(),
    )
    assert second["status"] == "already-bootstrapped"

    # No duplicate user rows for this sub.
    users = (await db_session.execute(select(User).where(User.cognito_sub == SUB))).scalars().all()
    assert len(users) == 1


@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_bootstrap_makes_user_registered(db_session):
    """After bootstrap, a User exists for the sub — the gate's 'registered' condition."""
    await bootstrap_first_admin(
        db_session,
        cognito_sub=SUB,
        org_id="platform-admin",
        display_name="platform-admin",
        email="admin@example.com",
        identity_writer=_writer(),
    )
    # get_access_status returns "registered" iff this query is non-None.
    user = await db_session.scalar(select(User).where(User.cognito_sub == SUB))
    assert user is not None


@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_bootstrap_role_override(db_session):
    await bootstrap_first_admin(
        db_session,
        cognito_sub=SUB,
        org_id="platform-admin",
        display_name="platform-admin",
        email="admin@example.com",
        role="org_admin",
        identity_writer=_writer(),
    )
    user = await db_session.scalar(select(User).where(User.cognito_sub == SUB))
    assert user.role == "org_admin"


@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_bootstrap_rejects_reserved_org(db_session):
    with pytest.raises(ValueError):
        await bootstrap_first_admin(
            db_session,
            cognito_sub=SUB,
            org_id="platform",  # reserved
            display_name="platform",
            email="admin@example.com",
            identity_writer=_writer(),
        )


@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "false"})
async def test_bootstrap_requires_v2_flag(db_session):
    with pytest.raises(RuntimeError):
        await bootstrap_first_admin(
            db_session,
            cognito_sub=SUB,
            org_id="platform-admin",
            display_name="platform-admin",
            email="admin@example.com",
            identity_writer=_writer(),
        )

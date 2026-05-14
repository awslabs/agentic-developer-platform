"""Tests for org_id_resolver helper (Issue #600).

Coverage:
  - Token has org_id → passthrough (no DB query)
  - Token empty + DB has org_id → fallback with warning
  - Both empty → HTTP 409
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.org_id_resolver import resolve_effective_org_id
from src.shared.models.base import Base
from src.shared.models.organization import Organization, Team, User
from src.shared.schemas.auth import TokenContext

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def _make_context(
    user_id: str = "sub-test-user",
    org_id: str = "org-acme",
    team_id: str = "team-eng",
) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id=team_id,
        department_id="dept-eng",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def session_factory():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            org = Organization(id="org-acme", name="Acme Corp")
            session.add(org)
            team = Team(id="team-eng", name="Eng", org_id="org-acme", department_id="dept-eng")
            session.add(team)
            # User with org_id populated
            session.add(
                User(
                    id="db-id-user1",
                    org_id="org-acme",
                    team_id="team-eng",
                    email="user1@example.com",
                    name="User One",
                    cognito_sub="sub-user-with-org",
                )
            )
            # User with empty org_id (shouldn't happen, but tests the 409 path)
            session.add(
                User(
                    id="db-id-user2",
                    org_id="",
                    team_id="team-eng",
                    email="user2@example.com",
                    name="User Two",
                    cognito_sub="sub-user-no-org",
                )
            )
            await session.commit()

    asyncio.get_event_loop().run_until_complete(_setup())
    return factory


class TestResolveEffectiveOrgId:
    """Tests for resolve_effective_org_id."""

    def test_passthrough_when_token_has_org_id(self, session_factory):
        """If token_context.org_id is non-empty, return it directly."""

        async def _run():
            ctx = _make_context(org_id="org-acme")
            async with session_factory() as db:
                result = await resolve_effective_org_id(ctx, db)
            assert result == "org-acme"

        asyncio.get_event_loop().run_until_complete(_run())

    def test_fallback_to_db_when_token_empty(self, session_factory):
        """If token org_id is empty, resolve from users.org_id via cognito_sub."""

        async def _run():
            ctx = _make_context(user_id="sub-user-with-org", org_id="")
            async with session_factory() as db:
                result = await resolve_effective_org_id(ctx, db)
            assert result == "org-acme"

        asyncio.get_event_loop().run_until_complete(_run())

    def test_409_when_both_empty(self, session_factory):
        """If token and DB both have empty org_id, raise HTTP 409."""
        from fastapi import HTTPException

        async def _run():
            ctx = _make_context(user_id="sub-user-no-org", org_id="")
            async with session_factory() as db:
                with pytest.raises(HTTPException) as exc_info:
                    await resolve_effective_org_id(ctx, db)
                assert exc_info.value.status_code == 409
                assert exc_info.value.detail["error"] == "user_not_assigned_to_org"

        asyncio.get_event_loop().run_until_complete(_run())

    def test_409_when_user_not_found(self, session_factory):
        """If no user row matches the cognito_sub, raise HTTP 409."""
        from fastapi import HTTPException

        async def _run():
            ctx = _make_context(user_id="sub-nonexistent", org_id="")
            async with session_factory() as db:
                with pytest.raises(HTTPException) as exc_info:
                    await resolve_effective_org_id(ctx, db)
                assert exc_info.value.status_code == 409

        asyncio.get_event_loop().run_until_complete(_run())

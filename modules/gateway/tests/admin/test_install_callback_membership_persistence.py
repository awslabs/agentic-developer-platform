"""Regression test: install-callback membership survives session close.

Issue #3058: The original bug was that _create_installer_membership called
db.flush() instead of db.commit(). This made the INSERT visible within the
same session but caused it to roll back when the request session closed
(get_db yields a session with no commit at teardown). The 9 existing tests
in test_install_callback_membership.py cannot catch this bug class because
they query inside the same still-open session where the flush is visible.

This test exercises the REAL lifecycle:
  1. Seed org/user/nonce in a committed session.
  2. Run install_callback inside a second session, then CLOSE that session
     WITHOUT committing (mirrors get_db teardown behavior).
  3. Open a THIRD fresh session and assert the TenantMembership row for
     (user, tenant) exists with role=member, joined_via=app_install.

This test MUST fail if db.commit() is reverted to db.flush() in
_create_installer_membership.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import (
    _PROVIDER_GITHUB_INSTALL,
    install_callback,
)
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import MagicLinkNonce

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _configure_github_app(monkeypatch):
    """Block Secrets Manager and DDB in unit tests."""
    from src.admin.connections.github_app_provider import _reset_provider_for_testing

    monkeypatch.setenv("BG_GITHUB_APP_SLUG", "test-adp-agent")
    _reset_provider_for_testing(None)
    with patch(
        "src.admin.connections.github_app_provider.boto3.client",
        side_effect=RuntimeError("Secrets Manager blocked in unit tests"),
    ):
        with patch(
            "src.admin.connections.service._write_installation_identity_index",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield
    _reset_provider_for_testing(None)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        import src.admin.models  # noqa: F401
        import src.shared.models.organization  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(db_engine) -> async_sessionmaker:
    return async_sessionmaker(db_engine, expire_on_commit=False)


def _mock_github_client() -> MagicMock:
    client = MagicMock(spec=GitHubAppClient)
    client.get_installation = AsyncMock(
        return_value={
            "id": 124731131,
            "account": {
                "type": "Organization",
                "login": "sophos-test",
                "id": 98765,
            },
            "repository_selection": "selected",
            "created_at": "2026-05-01T10:00:00Z",
        }
    )
    client.delete_installation = AsyncMock(return_value=None)
    client.list_installation_repositories = AsyncMock(return_value=2)
    client.list_installation_repository_names = AsyncMock(return_value=["acme/repo-one", "acme/repo-two"])
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMembershipPersistenceAcrossSessions:
    """Regression test: membership must survive session close.

    This test class verifies that the membership INSERT is actually committed
    to the database, not merely flushed. The bug (issue #3058) caused the row
    to vanish when the request session closed because flush != commit.
    """

    async def test_membership_survives_session_close(self, session_factory):
        """The membership row persists in a fresh session after the
        install_callback session is closed without an explicit commit.

        Lifecycle:
          Session 1 (seed): create org + user + nonce, COMMIT.
          Session 2 (callback): run install_callback, then CLOSE (no commit
              from the test — mirrors get_db which yields then closes).
          Session 3 (verify): open fresh session, assert row exists.

        This test FAILS with db.flush() and PASSES with db.commit().
        """
        # --- Session 1: Seed data ---
        async with session_factory() as seed_session:
            org = Organization(
                id="org-persist-001",
                name="Persist Test Org",
                aws_accounts=[],
                role_mappings={},
                settings={},
            )
            seed_session.add(org)
            await seed_session.commit()

            user = User(
                id="user-persist-001",
                org_id="org-persist-001",
                team_id="team-persist-001",
                email="persist@test.local",
                cognito_sub="sub-persist-001",
            )
            seed_session.add(user)
            await seed_session.commit()

            nonce = MagicLinkNonce(
                jti="persist-jti-001",
                provider=_PROVIDER_GITHUB_INSTALL,
                provider_user_id="sub-persist-001",
                channel_context=None,
                target_user_id="user-persist-001",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
                consumed_at=None,
            )
            seed_session.add(nonce)
            await seed_session.commit()

        # --- Session 2: Run install_callback, then close WITHOUT committing ---
        # This mirrors the real get_db lifecycle: the session is yielded to the
        # endpoint, which calls install_callback, then the session is closed by
        # the dependency teardown (no commit at teardown).
        async with session_factory() as callback_session:
            gh = _mock_github_client()
            result = await install_callback(
                installation_id=124731131,
                setup_action="install",
                state="persist-jti-001",
                db=callback_session,
                github_client=gh,
            )
            assert result["success"] is True
            # DO NOT commit here — this is the whole point of the test.
            # get_db closes the session without committing.

        # --- Session 3: Verify from a completely fresh session ---
        async with session_factory() as verify_session:
            stmt = select(TenantMembership).where(
                TenantMembership.user_id == "user-persist-001",
                TenantMembership.tenant_id == "org-persist-001",
            )
            membership = (await verify_session.execute(stmt)).scalar_one_or_none()

            assert membership is not None, (
                "Membership row did not survive session close — "
                "likely db.flush() instead of db.commit() in "
                "_create_installer_membership (issue #3058)"
            )
            assert membership.role == "member"
            assert membership.joined_via == "app_install"
            assert membership.github_org_id == "sophos-test"

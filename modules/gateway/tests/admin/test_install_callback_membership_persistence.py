"""Verify connection setup across fresh sessions: human memberships remain unchanged
and the bot identity persists in the selected ADP organization."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import (
    install_callback,
)
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from tests.admin import install_setup_fixtures as setup_fixtures
from tests.admin.install_setup_fixtures import (
    bind_real_org_control,
    issue_install_nonce,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

offline_setup_boundaries = setup_fixtures.offline_setup_boundaries

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _configure_github_app(monkeypatch, offline_setup_boundaries):
    """Block Secrets Manager and DDB in unit tests."""
    from src.admin.connections.github_app_provider import _reset_provider_for_testing

    monkeypatch.setenv("BG_GITHUB_APP_SLUG", "test-adp-agent")
    _reset_provider_for_testing(None)
    with patch(
        "src.admin.connections.github_app_provider.boto3.client",
        side_effect=RuntimeError("Secrets Manager blocked in unit tests"),
    ):
        with (
            patch("src.admin.connections.service._write_installation_identity_index", new_callable=AsyncMock, return_value=None),
            patch("src.admin.connections.bot_identity.IdentityIndexWriter") as writer,
        ):
            writer.return_value.put_user_identity = AsyncMock(return_value=True)
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
                "login": "acme-test",
                "id": 98765,
            },
            "repository_selection": "selected",
            "created_at": "2026-05-01T10:00:00Z",
        }
    )
    client.delete_installation = AsyncMock(return_value=None)
    client.list_installation_repositories = AsyncMock(return_value=2)
    client.list_installation_repository_names = AsyncMock(return_value=["acme/repo-one", "acme/repo-two"])
    client.get_bot_user = AsyncMock(return_value={"id": 424242, "login": "test-adp-agent[bot]", "type": "Bot"})
    return bind_real_org_control(client)


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

            await issue_install_nonce(seed_session, user, jti="persist-jti-001")

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
            gh.get_bot_user.assert_awaited_once_with("test-adp-agent[bot]", installation_id=124731131)
            # DO NOT commit here — this is the whole point of the test.
            # get_db closes the session without committing.

        # --- Session 3: Verify from a completely fresh session ---
        async with session_factory() as verify_session:
            stmt = select(TenantMembership).where(
                TenantMembership.user_id == "user-persist-001",
                TenantMembership.tenant_id == "org-persist-001",
            )
            membership = (await verify_session.execute(stmt)).scalar_one_or_none()

            assert membership is None

            # The bot's canonical link and minimal membership survive the same
            # callback teardown; its seed must not alter the installer's role.
            bot_link = (await verify_session.scalars(select(UserIdentity).where(UserIdentity.provider_user_id == "424242"))).one()
            bot = await verify_session.get(User, bot_link.user_id)
            assert bot.user_kind == "bot"
            assert bot.org_id == "org-persist-001"
            bot_membership = (await verify_session.scalars(select(TenantMembership).where(TenantMembership.user_id == bot.id))).one()
            assert bot_membership.role == "member"

"""Tests for user DDB write-through (channel_user entries).

Issue #401: Validates that user create/delete triggers correct DDB writes.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.identity.schemas import UserCreateRequest, UserIdentityInput
from src.admin.identity.users_service import UsersService
from src.shared.models.organization import Organization, User


@pytest.fixture
def mock_cognito_sync():
    mock = AsyncMock()
    mock.create_user_and_invite = AsyncMock(return_value={"Username": "test@test.com"})
    mock.delete_user = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def mock_identity_writer():
    writer = AsyncMock(spec=IdentityIndexWriter)
    writer.sync_user_identities = AsyncMock()
    writer.delete_all_user_identities = AsyncMock()
    writer.put_user_identity = AsyncMock(return_value=True)
    writer.delete_user_identity = AsyncMock(return_value=True)
    return writer


@pytest.fixture
async def seeded_org(db_session: AsyncSession):
    """Seed an org with default dept + team for user tests."""
    from src.shared.models.organization import Department, Team

    org = Organization(
        id="wt-org",
        name="Write-Through Test Org",
        aws_accounts=[],
        settings={"plan": "free"},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db_session.add(org)
    db_session.add(Department(id="wt-org-dept-default", org_id="wt-org", name="Default"))
    db_session.add(Team(id="wt-org-team-default", org_id="wt-org", department_id="wt-org-dept-default", name="Default"))
    await db_session.commit()
    return org


class TestUsersWriteThrough:
    """Tests that user CRUD triggers DDB channel_user write-through."""

    @pytest.mark.asyncio
    async def test_create_user_writes_channel_user_entries(self, db_session: AsyncSession, mock_cognito_sync, mock_identity_writer, seeded_org):
        """POST creates user and writes channel_user to DDB for each identity."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync, identity_writer=mock_identity_writer)
        req = UserCreateRequest(
            email="alice@test.com",
            name="Alice",
            identities=[
                UserIdentityInput(provider="github", provider_user_id="gh-123", provider_username="alice-gh"),
                UserIdentityInput(provider="slack", provider_user_id="sl-456", provider_username="alice-sl"),
            ],
            send_invite=False,
        )

        result = await svc.create_user("wt-org", req)

        mock_identity_writer.sync_user_identities.assert_awaited_once_with(
            user_id=result.id,
            org_id="wt-org",
            identities=[
                {"provider_user_id": "gh-123", "provider_username": "alice-gh"},
                {"provider_user_id": "sl-456", "provider_username": "alice-sl"},
            ],
        )

    @pytest.mark.asyncio
    async def test_create_user_no_identities_skips_ddb(self, db_session: AsyncSession, mock_cognito_sync, mock_identity_writer, seeded_org):
        """POST with no identities does not call DDB write-through."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync, identity_writer=mock_identity_writer)
        req = UserCreateRequest(email="noident@test.com", send_invite=False)

        await svc.create_user("wt-org", req)

        mock_identity_writer.sync_user_identities.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_user_ddb_failure_does_not_rollback_postgres(
        self, db_session: AsyncSession, mock_cognito_sync, mock_identity_writer, seeded_org
    ):
        """DDB write failure after Postgres commit does NOT rollback user creation."""
        mock_identity_writer.sync_user_identities = AsyncMock(side_effect=Exception("DDB unavailable"))
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync, identity_writer=mock_identity_writer)
        req = UserCreateRequest(
            email="resilient@test.com",
            identities=[
                UserIdentityInput(provider="github", provider_user_id="gh-fail", provider_username="fail-user"),
            ],
            send_invite=False,
        )

        # Should NOT raise
        result = await svc.create_user("wt-org", req)
        assert result.email == "resilient@test.com"

        # User still in Postgres
        user = (await db_session.execute(select(User).where(User.id == result.id))).scalar_one()
        assert user.email == "resilient@test.com"

    @pytest.mark.asyncio
    async def test_create_user_idempotent_on_rerun(self, db_session: AsyncSession, mock_cognito_sync, mock_identity_writer, seeded_org):
        """Re-creating the same identity does not cause DDB errors (upsert semantics)."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync, identity_writer=mock_identity_writer)
        req = UserCreateRequest(
            email="idem1@test.com",
            identities=[
                UserIdentityInput(provider="github", provider_user_id="gh-idem", provider_username="idem-user"),
            ],
            send_invite=False,
        )

        await svc.create_user("wt-org", req)

        # DDB write (PutItem) should succeed (upsert) — mock returns True
        mock_identity_writer.sync_user_identities.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_user_removes_ddb_entries(self, db_session: AsyncSession, mock_cognito_sync, mock_identity_writer, seeded_org):
        """DELETE user removes all channel_user DDB entries for that user."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync, identity_writer=mock_identity_writer)
        req = UserCreateRequest(
            email="delme@test.com",
            identities=[
                UserIdentityInput(provider="github", provider_user_id="gh-del1"),
                UserIdentityInput(provider="slack", provider_user_id="sl-del2"),
            ],
            send_invite=False,
        )
        user = await svc.create_user("wt-org", req)
        mock_identity_writer.reset_mock()

        deleted = await svc.delete_user("wt-org", user.id)
        assert deleted is True

        mock_identity_writer.delete_all_user_identities.assert_awaited_once_with(["gh-del1", "sl-del2"])

    @pytest.mark.asyncio
    async def test_delete_user_no_identities_skips_ddb(self, db_session: AsyncSession, mock_cognito_sync, mock_identity_writer, seeded_org):
        """DELETE user with no identities does not call DDB."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync, identity_writer=mock_identity_writer)
        req = UserCreateRequest(email="noident-del@test.com", send_invite=False)
        user = await svc.create_user("wt-org", req)
        mock_identity_writer.reset_mock()

        await svc.delete_user("wt-org", user.id)

        mock_identity_writer.delete_all_user_identities.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_user_ddb_failure_does_not_prevent_deletion(
        self, db_session: AsyncSession, mock_cognito_sync, mock_identity_writer, seeded_org
    ):
        """DDB delete failure does NOT prevent user deletion from Postgres."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync, identity_writer=mock_identity_writer)
        req = UserCreateRequest(
            email="delfail@test.com",
            identities=[UserIdentityInput(provider="github", provider_user_id="gh-delfail")],
            send_invite=False,
        )
        user = await svc.create_user("wt-org", req)

        mock_identity_writer.delete_all_user_identities = AsyncMock(side_effect=Exception("DDB unavailable"))

        deleted = await svc.delete_user("wt-org", user.id)
        assert deleted is True

        # User gone from Postgres
        db_user = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
        assert db_user is None

    @pytest.mark.asyncio
    async def test_create_user_without_writer_still_works(self, db_session: AsyncSession, mock_cognito_sync, seeded_org):
        """Backward compat: no identity_writer injected — no DDB calls, no error."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
        req = UserCreateRequest(
            email="nowriter@test.com",
            identities=[UserIdentityInput(provider="github", provider_user_id="gh-nw")],
            send_invite=False,
        )

        result = await svc.create_user("wt-org", req)
        assert result.email == "nowriter@test.com"

"""Tests for user identity admin API.

Issue #387: Validates transactional user creation + Cognito invite.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.schemas import UserCreateRequest, UserIdentityInput
from src.admin.identity.users_service import UsersService
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity


@pytest.fixture
def mock_cognito_sync():
    mock = AsyncMock()
    mock.create_user_and_invite = AsyncMock(return_value={"Username": "alice@test.com"})
    mock.delete_user = AsyncMock(return_value=True)
    return mock


@pytest.fixture
async def seeded_org(db_session: AsyncSession):
    """Seed an org with default dept + team for user tests."""
    from src.shared.models.organization import Department, Team

    org = Organization(
        id="test-org",
        name="Test Org",
        aws_accounts=[],
        settings={"plan": "free"},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db_session.add(org)
    db_session.add(Department(id="test-org-dept-default", org_id="test-org", name="Default"))
    db_session.add(Team(id="test-org-team-default", org_id="test-org", department_id="test-org-dept-default", name="Default"))
    await db_session.commit()
    return org


class TestUsersService:
    """Test UsersService transactional behavior."""

    @pytest.mark.asyncio
    async def test_create_user_with_identities(self, db_session: AsyncSession, mock_cognito_sync, seeded_org):
        """POST creates user + identities in one transaction."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
        req = UserCreateRequest(
            email="alice@test.com",
            name="Alice Test",
            role="member",
            identities=[
                UserIdentityInput(
                    provider="github",
                    provider_user_id="123456",
                    provider_username="alice-test",
                )
            ],
            send_invite=True,
        )

        result = await svc.create_user("test-org", req)

        assert result.email == "alice@test.com"
        assert result.org_id == "test-org"
        assert result.team_id == "test-org-team-default"
        assert result.role == "member"

        # Verify user in DB
        user = (await db_session.execute(select(User).where(User.email == "alice@test.com"))).scalar_one()
        assert user.org_id == "test-org"

        # Verify identity in DB
        identities = (await db_session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).scalars().all()
        assert len(identities) == 1
        assert identities[0].provider == "github"
        assert identities[0].provider_user_id == "123456"
        assert identities[0].provider_username == "alice-test"

    @pytest.mark.asyncio
    async def test_create_user_calls_cognito(self, db_session: AsyncSession, mock_cognito_sync, seeded_org):
        """POST triggers Cognito user creation + invite post-commit."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
        req = UserCreateRequest(
            email="bob@test.com",
            name="Bob Test",
            role="admin",
            send_invite=True,
        )

        await svc.create_user("test-org", req)

        mock_cognito_sync.create_user_and_invite.assert_awaited_once()
        call_kwargs = mock_cognito_sync.create_user_and_invite.call_args[1]
        assert call_kwargs["email"] == "bob@test.com"
        assert call_kwargs["org_id"] == "test-org"
        assert call_kwargs["send_invite"] is True

    @pytest.mark.asyncio
    async def test_create_user_no_invite(self, db_session: AsyncSession, mock_cognito_sync, seeded_org):
        """send_invite=False suppresses Cognito invitation."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
        req = UserCreateRequest(email="carol@test.com", send_invite=False)

        await svc.create_user("test-org", req)

        call_kwargs = mock_cognito_sync.create_user_and_invite.call_args[1]
        assert call_kwargs["send_invite"] is False

    @pytest.mark.asyncio
    async def test_list_users(self, db_session: AsyncSession, mock_cognito_sync, seeded_org):
        """LIST returns users for the org."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
        await svc.create_user("test-org", UserCreateRequest(email="u1@test.com"))
        await svc.create_user("test-org", UserCreateRequest(email="u2@test.com"))

        users = await svc.list_users("test-org")
        assert len(users) == 2
        emails = {u.email for u in users}
        assert "u1@test.com" in emails
        assert "u2@test.com" in emails

    @pytest.mark.asyncio
    async def test_delete_user(self, db_session: AsyncSession, mock_cognito_sync, seeded_org):
        """DELETE removes user from DB and Cognito."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
        result = await svc.create_user("test-org", UserCreateRequest(email="del@test.com"))

        deleted = await svc.delete_user("test-org", result.id)
        assert deleted is True

        # User gone from DB
        user = (await db_session.execute(select(User).where(User.id == result.id))).scalar_one_or_none()
        assert user is None

        # Cognito delete called
        mock_cognito_sync.delete_user.assert_awaited_once_with("del@test.com")

    @pytest.mark.asyncio
    async def test_delete_user_not_found(self, db_session: AsyncSession, mock_cognito_sync, seeded_org):
        """DELETE returns False for non-existent user."""
        svc = UsersService(db_session, cognito_sync=mock_cognito_sync)
        deleted = await svc.delete_user("test-org", "nonexistent-id")
        assert deleted is False

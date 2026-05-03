"""Tests for identity DDB write-through (channel_user entries).

Issue #401: Validates that add/delete identity triggers correct DDB writes.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.identities_service import IdentitiesService
from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.identity.schemas import IdentityCreateRequest
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity


@pytest.fixture
def mock_identity_writer():
    writer = AsyncMock(spec=IdentityIndexWriter)
    writer.put_user_identity = AsyncMock(return_value=True)
    writer.delete_user_identity = AsyncMock(return_value=True)
    return writer


@pytest.fixture
async def seeded_user(db_session: AsyncSession):
    """Seed org + user for identity tests."""
    from src.shared.models.organization import Department, Team

    org = Organization(
        id="id-org",
        name="Identity Test Org",
        aws_accounts=[],
        settings={"plan": "free"},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db_session.add(org)
    db_session.add(Department(id="id-org-dept-default", org_id="id-org", name="Default"))
    db_session.add(Team(id="id-org-team-default", org_id="id-org", department_id="id-org-dept-default", name="Default"))

    user = User(
        org_id="id-org",
        team_id="id-org-team-default",
        email="idtest@test.com",
        name="Identity Test User",
        role="member",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


class TestIdentitiesWriteThrough:
    """Tests that identity add/delete triggers DDB channel_user write-through."""

    @pytest.mark.asyncio
    async def test_add_identity_writes_channel_user(self, db_session: AsyncSession, mock_identity_writer, seeded_user):
        """POST identity writes channel_user entry to DDB."""
        svc = IdentitiesService(db_session, identity_writer=mock_identity_writer)
        req = IdentityCreateRequest(
            provider="github",
            provider_user_id="gh-999",
            provider_username="testuser-gh",
        )

        result = await svc.add_identity(seeded_user.id, req)

        assert result is not None
        assert result.provider_user_id == "gh-999"
        mock_identity_writer.put_user_identity.assert_awaited_once_with(
            provider_user_id="gh-999",
            user_id=seeded_user.id,
            org_id="id-org",
            provider_username="testuser-gh",
        )

    @pytest.mark.asyncio
    async def test_add_identity_no_username(self, db_session: AsyncSession, mock_identity_writer, seeded_user):
        """POST identity without provider_username passes None."""
        svc = IdentitiesService(db_session, identity_writer=mock_identity_writer)
        req = IdentityCreateRequest(
            provider="slack",
            provider_user_id="sl-888",
        )

        result = await svc.add_identity(seeded_user.id, req)

        assert result is not None
        mock_identity_writer.put_user_identity.assert_awaited_once_with(
            provider_user_id="sl-888",
            user_id=seeded_user.id,
            org_id="id-org",
            provider_username=None,
        )

    @pytest.mark.asyncio
    async def test_add_identity_user_not_found(self, db_session: AsyncSession, mock_identity_writer, seeded_user):
        """POST identity for nonexistent user returns None and skips DDB."""
        svc = IdentitiesService(db_session, identity_writer=mock_identity_writer)
        req = IdentityCreateRequest(provider="github", provider_user_id="gh-000")

        result = await svc.add_identity("nonexistent-user-id", req)

        assert result is None
        mock_identity_writer.put_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_add_identity_ddb_failure_does_not_rollback(self, db_session: AsyncSession, mock_identity_writer, seeded_user):
        """DDB write failure does NOT rollback identity creation in Postgres."""
        mock_identity_writer.put_user_identity = AsyncMock(side_effect=Exception("DDB unavailable"))
        svc = IdentitiesService(db_session, identity_writer=mock_identity_writer)
        req = IdentityCreateRequest(provider="github", provider_user_id="gh-fail")

        result = await svc.add_identity(seeded_user.id, req)

        assert result is not None
        assert result.provider_user_id == "gh-fail"

        # Identity still in Postgres
        ident = (await db_session.execute(select(UserIdentity).where(UserIdentity.provider_user_id == "gh-fail"))).scalar_one()
        assert ident.user_id == seeded_user.id

    @pytest.mark.asyncio
    async def test_delete_identity_removes_ddb_entry(self, db_session: AsyncSession, mock_identity_writer, seeded_user):
        """DELETE identity removes the channel_user DDB row."""
        # First create an identity
        svc = IdentitiesService(db_session, identity_writer=mock_identity_writer)
        req = IdentityCreateRequest(provider="github", provider_user_id="gh-del")
        created = await svc.add_identity(seeded_user.id, req)
        mock_identity_writer.reset_mock()

        # Delete it
        deleted = await svc.delete_identity(seeded_user.id, created.id)
        assert deleted is True

        mock_identity_writer.delete_user_identity.assert_awaited_once_with("gh-del")

    @pytest.mark.asyncio
    async def test_delete_identity_not_found(self, db_session: AsyncSession, mock_identity_writer, seeded_user):
        """DELETE nonexistent identity returns False and skips DDB."""
        svc = IdentitiesService(db_session, identity_writer=mock_identity_writer)

        deleted = await svc.delete_identity(seeded_user.id, "nonexistent-id")
        assert deleted is False

        mock_identity_writer.delete_user_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_identity_ddb_failure_does_not_prevent_deletion(self, db_session: AsyncSession, mock_identity_writer, seeded_user):
        """DDB delete failure does NOT prevent identity deletion from Postgres."""
        svc = IdentitiesService(db_session, identity_writer=mock_identity_writer)
        req = IdentityCreateRequest(provider="github", provider_user_id="gh-delfail")
        created = await svc.add_identity(seeded_user.id, req)

        mock_identity_writer.delete_user_identity = AsyncMock(side_effect=Exception("DDB timeout"))

        deleted = await svc.delete_identity(seeded_user.id, created.id)
        assert deleted is True

        # Identity gone from Postgres
        ident = (await db_session.execute(select(UserIdentity).where(UserIdentity.id == created.id))).scalar_one_or_none()
        assert ident is None

    @pytest.mark.asyncio
    async def test_add_identity_without_writer_still_works(self, db_session: AsyncSession, seeded_user):
        """Backward compat: no identity_writer injected — no DDB calls."""
        svc = IdentitiesService(db_session)
        req = IdentityCreateRequest(provider="github", provider_user_id="gh-nowriter")

        result = await svc.add_identity(seeded_user.id, req)
        assert result is not None
        assert result.provider_user_id == "gh-nowriter"

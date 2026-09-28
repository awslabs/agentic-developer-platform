"""Tests for identity linkage admin API.

Issue #387: Cross-channel identity management (add/remove provider identities).
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.identities_service import IdentitiesService
from src.admin.identity.schemas import IdentityCreateRequest
from src.shared.models.organization import Department, Organization, Team, User


@pytest.fixture
async def seeded_user(db_session: AsyncSession):
    """Seed an org + user for identity linkage tests."""
    org = Organization(
        id="id-org",
        name="ID Org",
        aws_accounts=[],
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db_session.add(org)
    db_session.add(Department(id="id-org-dept", org_id="id-org", name="Default"))
    db_session.add(Team(id="id-org-team", org_id="id-org", department_id="id-org-dept", name="Default"))

    user = User(
        id="user-alice",
        org_id="id-org",
        team_id="id-org-team",
        email="alice@id-org.com",
        name="Alice",
        role="member",
    )
    db_session.add(user)
    await db_session.commit()
    return user


class TestIdentitiesService:
    """Test IdentitiesService CRUD."""

    @pytest.mark.asyncio
    async def test_add_identity(self, db_session: AsyncSession, seeded_user):
        """POST adds a new identity to an existing user."""
        svc = IdentitiesService(db_session)
        req = IdentityCreateRequest(
            provider="slack",
            provider_user_id="U12345",
            provider_username="alice-slack",
        )

        result = await svc.add_identity("user-alice", req)

        assert result is not None
        assert result.user_id == "user-alice"
        assert result.provider == "slack"
        assert result.provider_user_id == "U12345"
        assert result.provider_username == "alice-slack"
        assert result.verification_method == "admin_manual"
        assert result.org_id == "id-org"
        assert result.team_id == "id-org-team"

    @pytest.mark.asyncio
    async def test_add_identity_user_not_found(self, db_session: AsyncSession, seeded_user):
        """POST returns None for non-existent user."""
        svc = IdentitiesService(db_session)
        req = IdentityCreateRequest(provider="github", provider_user_id="999")

        result = await svc.add_identity("nonexistent", req)
        assert result is None

    @pytest.mark.asyncio
    async def test_list_identities(self, db_session: AsyncSession, seeded_user):
        """GET lists all identities for a user."""
        svc = IdentitiesService(db_session)

        # Add two identities
        await svc.add_identity("user-alice", IdentityCreateRequest(provider="github", provider_user_id="gh-1"))
        await svc.add_identity("user-alice", IdentityCreateRequest(provider="slack", provider_user_id="sl-1"))

        identities = await svc.list_identities("user-alice")
        assert len(identities) == 2
        providers = {i.provider for i in identities}
        assert providers == {"github", "slack"}

    @pytest.mark.asyncio
    async def test_delete_identity(self, db_session: AsyncSession, seeded_user):
        """DELETE removes an identity from a user."""
        svc = IdentitiesService(db_session)
        created = await svc.add_identity(
            "user-alice",
            IdentityCreateRequest(provider="discord", provider_user_id="dc-1"),
        )

        deleted = await svc.delete_identity("user-alice", created.id)
        assert deleted is True

        # Verify gone
        identities = await svc.list_identities("user-alice")
        assert len(identities) == 0

    @pytest.mark.asyncio
    async def test_delete_identity_not_found(self, db_session: AsyncSession, seeded_user):
        """DELETE returns False for non-existent identity."""
        svc = IdentitiesService(db_session)
        deleted = await svc.delete_identity("user-alice", "nonexistent-id")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_cross_channel_linkage(self, db_session: AsyncSession, seeded_user):
        """Multiple provider identities can point to the same user."""
        svc = IdentitiesService(db_session)

        await svc.add_identity("user-alice", IdentityCreateRequest(provider="github", provider_user_id="gh-alice"))
        await svc.add_identity("user-alice", IdentityCreateRequest(provider="slack", provider_user_id="sl-alice"))
        await svc.add_identity("user-alice", IdentityCreateRequest(provider="whatsapp", provider_user_id="wa-alice"))

        identities = await svc.list_identities("user-alice")
        assert len(identities) == 3
        # All point to same user
        assert all(i.user_id == "user-alice" for i in identities)

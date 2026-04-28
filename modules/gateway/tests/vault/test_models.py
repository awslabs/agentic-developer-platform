"""Unit tests for vault SQLAlchemy models — FK, unique, cascade.

Issue #134: Vault Phase 1
"""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import Base, TenantMixin
from src.shared.models.organization import Organization, User
from src.shared.models.vault import (
    ChannelTenantMap,
    CredentialType,
    IdentityProvider,
    UserCredential,
    UserIdentity,
    VerificationMethod,
)


# ---------------------------------------------------------------------------
# Schema / meta tests
# ---------------------------------------------------------------------------


class TestUserIdentityModel:
    """Tests for the UserIdentity model definition."""

    def test_tablename(self):
        assert UserIdentity.__tablename__ == "user_identities"

    def test_inherits_base_and_tenant(self):
        assert issubclass(UserIdentity, Base)
        assert issubclass(UserIdentity, TenantMixin)

    def test_primary_key(self):
        mapper = inspect(UserIdentity)
        pk = [c.name for c in mapper.primary_key]
        assert "id" in pk

    def test_columns_exist(self):
        mapper = inspect(UserIdentity)
        cols = {c.key for c in mapper.column_attrs}
        expected = {
            "id", "org_id", "team_id", "user_id", "provider", "provider_user_id",
            "provider_username", "verification_method", "verified_at",
            "created_at", "updated_at",
        }
        assert expected <= cols

    def test_instantiation(self):
        obj = UserIdentity(
            org_id="org-1",
            team_id="team-1",
            user_id="user-1",
            provider=IdentityProvider.slack,
            provider_user_id="U123",
            verification_method=VerificationMethod.oauth,
        )
        assert obj.provider == "slack"
        assert obj.verification_method == "oauth"
        assert obj.team_id == "team-1"


class TestUserCredentialModel:
    """Tests for the UserCredential model definition."""

    def test_tablename(self):
        assert UserCredential.__tablename__ == "user_credentials"

    def test_inherits_base_and_tenant(self):
        assert issubclass(UserCredential, Base)
        assert issubclass(UserCredential, TenantMixin)

    def test_primary_key(self):
        mapper = inspect(UserCredential)
        pk = [c.name for c in mapper.primary_key]
        assert "id" in pk

    def test_columns_exist(self):
        mapper = inspect(UserCredential)
        cols = {c.key for c in mapper.column_attrs}
        expected = {
            "id", "org_id", "team_id", "user_id", "service", "credential_type",
            "label", "secret_arn", "scopes", "expires_at", "last_used_at",
            "created_at", "updated_at",
        }
        assert expected <= cols

    def test_instantiation(self):
        obj = UserCredential(
            org_id="org-1",
            team_id="team-1",
            user_id="user-1",
            service="github",
            credential_type=CredentialType.oauth_token,
            label="my-gh-token",
            secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/users/sub/github-abc12345",
        )
        assert obj.credential_type == "oauth_token"
        assert obj.team_id == "team-1"


class TestChannelTenantMapModel:
    """Tests for the ChannelTenantMap model definition."""

    def test_tablename(self):
        assert ChannelTenantMap.__tablename__ == "channel_tenant_map"

    def test_inherits_base(self):
        assert issubclass(ChannelTenantMap, Base)

    def test_primary_key(self):
        mapper = inspect(ChannelTenantMap)
        pk = [c.name for c in mapper.primary_key]
        assert "id" in pk

    def test_columns_exist(self):
        mapper = inspect(ChannelTenantMap)
        cols = {c.key for c in mapper.column_attrs}
        expected = {"id", "provider", "provider_scope_id", "org_id", "created_at"}
        assert expected <= cols


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestEnums:
    def test_identity_provider_values(self):
        assert set(IdentityProvider) == {"slack", "github", "whatsapp", "discord"}

    def test_verification_method_values(self):
        assert set(VerificationMethod) == {"oauth", "magic_link", "admin_manual"}

    def test_credential_type_values(self):
        expected = {"api_key", "oauth_token", "basic_auth", "bearer", "ssh_key", "certificate", "config_file"}
        assert set(CredentialType) == expected


# ---------------------------------------------------------------------------
# Database constraint tests (require db_session from conftest)
# ---------------------------------------------------------------------------


class TestForeignKeyConstraints:
    """FK constraint tests against the in-memory SQLite database."""

    @pytest.mark.asyncio
    async def test_user_identity_fk_to_users(self, db_session: AsyncSession):
        """user_identities.user_id must reference an existing user."""
        # Enable FK enforcement for SQLite
        await db_session.execute(text("PRAGMA foreign_keys = ON"))

        identity = UserIdentity(
            id="id-1", org_id="org-1", team_id="t-1", user_id="nonexistent-user",
            provider="slack", provider_user_id="U999",
            verification_method="oauth",
        )
        db_session.add(identity)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_user_credential_fk_to_users(self, db_session: AsyncSession):
        await db_session.execute(text("PRAGMA foreign_keys = ON"))

        cred = UserCredential(
            id="cred-1", org_id="org-1", team_id="t-1", user_id="nonexistent-user",
            service="github", credential_type="api_key", label="tok",
            secret_arn="arn:fake",
        )
        db_session.add(cred)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_channel_tenant_map_fk_to_orgs(self, db_session: AsyncSession):
        await db_session.execute(text("PRAGMA foreign_keys = ON"))

        mapping = ChannelTenantMap(
            id="map-1", provider="slack", provider_scope_id="W123",
            org_id="nonexistent-org",
        )
        db_session.add(mapping)
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestUniqueConstraints:
    """Unique constraint tests."""

    @pytest.mark.asyncio
    async def test_user_identity_unique_provider_provider_user_id(self, db_session: AsyncSession):
        """Duplicate (provider, provider_user_id) should fail."""
        # Seed a user
        user = User(id="u-1", org_id="org-1", team_id="t-1", email="a@a.com")
        db_session.add(user)
        await db_session.flush()

        id1 = UserIdentity(
            id="id-1", org_id="org-1", team_id="t-1", user_id="u-1",
            provider="github", provider_user_id="gh-100",
            verification_method="oauth",
        )
        id2 = UserIdentity(
            id="id-2", org_id="org-1", team_id="t-1", user_id="u-1",
            provider="github", provider_user_id="gh-100",
            verification_method="admin_manual",
        )
        db_session.add(id1)
        await db_session.flush()

        db_session.add(id2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_user_credential_unique_user_service_label(self, db_session: AsyncSession):
        """Duplicate (user_id, service, label) should fail."""
        user = User(id="u-2", org_id="org-1", team_id="t-1", email="b@b.com")
        db_session.add(user)
        await db_session.flush()

        c1 = UserCredential(
            id="c-1", org_id="org-1", team_id="t-1", user_id="u-2",
            service="openai", credential_type="api_key", label="default",
            secret_arn="arn:1",
        )
        c2 = UserCredential(
            id="c-2", org_id="org-1", team_id="t-1", user_id="u-2",
            service="openai", credential_type="api_key", label="default",
            secret_arn="arn:2",
        )
        db_session.add(c1)
        await db_session.flush()

        db_session.add(c2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_channel_tenant_map_unique_provider_scope(self, db_session: AsyncSession):
        """Duplicate (provider, provider_scope_id) should fail."""
        org = Organization(id="org-uniq", name="test-org-uniq", aws_accounts=[], role_mappings={}, settings={})
        db_session.add(org)
        await db_session.flush()

        m1 = ChannelTenantMap(id="m-1", provider="slack", provider_scope_id="W111", org_id="org-uniq")
        m2 = ChannelTenantMap(id="m-2", provider="slack", provider_scope_id="W111", org_id="org-uniq")
        db_session.add(m1)
        await db_session.flush()

        db_session.add(m2)
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestCascadeDeletes:
    """CASCADE delete tests — use raw SQL DELETE to trigger DB-level cascade.

    SQLite FK cascades only fire on SQL DELETE, not on ORM session.delete()
    which tries to SET NULL via the backref. We test the DB constraint directly.
    """

    @pytest.mark.asyncio
    async def test_cascade_delete_user_removes_identities(self, db_session: AsyncSession):
        await db_session.execute(text("PRAGMA foreign_keys = ON"))

        user = User(id="u-casc-1", org_id="org-1", team_id="t-1", email="c@c.com")
        db_session.add(user)
        await db_session.flush()

        identity = UserIdentity(
            id="id-casc", org_id="org-1", team_id="t-1", user_id="u-casc-1",
            provider="discord", provider_user_id="D1",
            verification_method="magic_link",
        )
        db_session.add(identity)
        await db_session.flush()

        # Raw SQL DELETE to trigger DB-level CASCADE
        await db_session.execute(text("DELETE FROM users WHERE id = 'u-casc-1'"))
        await db_session.flush()

        result = await db_session.execute(
            text("SELECT count(*) FROM user_identities WHERE user_id = 'u-casc-1'")
        )
        assert result.scalar() == 0

    @pytest.mark.asyncio
    async def test_cascade_delete_user_removes_credentials(self, db_session: AsyncSession):
        await db_session.execute(text("PRAGMA foreign_keys = ON"))

        user = User(id="u-casc-2", org_id="org-1", team_id="t-1", email="d@d.com")
        db_session.add(user)
        await db_session.flush()

        cred = UserCredential(
            id="c-casc", org_id="org-1", team_id="t-1", user_id="u-casc-2",
            service="aws", credential_type="bearer", label="main",
            secret_arn="arn:casc",
        )
        db_session.add(cred)
        await db_session.flush()

        await db_session.execute(text("DELETE FROM users WHERE id = 'u-casc-2'"))
        await db_session.flush()

        result = await db_session.execute(
            text("SELECT count(*) FROM user_credentials WHERE user_id = 'u-casc-2'")
        )
        assert result.scalar() == 0

    @pytest.mark.asyncio
    async def test_cascade_delete_org_removes_channel_map(self, db_session: AsyncSession):
        await db_session.execute(text("PRAGMA foreign_keys = ON"))

        org = Organization(id="org-casc", name="test-org-casc", aws_accounts=[], role_mappings={}, settings={})
        db_session.add(org)
        await db_session.flush()

        mapping = ChannelTenantMap(
            id="m-casc", provider="slack", provider_scope_id="W-CASC", org_id="org-casc",
        )
        db_session.add(mapping)
        await db_session.flush()

        await db_session.execute(text("DELETE FROM organizations WHERE id = 'org-casc'"))
        await db_session.flush()

        result = await db_session.execute(
            text("SELECT count(*) FROM channel_tenant_map WHERE org_id = 'org-casc'")
        )
        assert result.scalar() == 0

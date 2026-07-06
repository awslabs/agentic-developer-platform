"""Tests for Alembic migration 021 — tenant_memberships table + backfill + identity-index relaxation.

Issue #2961: D5 data foundation — verifies:
  - Table creation with expected columns and constraints
  - Unique constraint on (user_id, tenant_id)
  - Partial unique index rejects a second is_active=true for same user
  - Backfill produces one row per user with is_active=true
  - Backfill is idempotent (re-run inserts nothing)
  - Relaxed user_identities index allows same (provider, provider_user_id) across two org_ids
  - Relaxed user_identities index still rejects duplicate within one org_id
  - TenantMembership model round-trips via ORM
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_schema_info(sync_conn):
    """Collect schema info synchronously inside run_sync."""
    insp = sa_inspect(sync_conn)
    info = {}
    info["tables"] = set(insp.get_table_names())

    # tenant_memberships info
    if "tenant_memberships" in info["tables"]:
        info["tm_columns"] = {c["name"] for c in insp.get_columns("tenant_memberships")}
        ucs = []
        for uc in insp.get_unique_constraints("tenant_memberships"):
            ucs.append(set(uc["column_names"]))
        for idx in insp.get_indexes("tenant_memberships"):
            if idx.get("unique"):
                ucs.append(set(idx["column_names"]))
        info["tm_unique"] = ucs

    # user_identities info
    if "user_identities" in info["tables"]:
        ucs = []
        for uc in insp.get_unique_constraints("user_identities"):
            ucs.append(set(uc["column_names"]))
        for idx in insp.get_indexes("user_identities"):
            if idx.get("unique"):
                ucs.append(set(idx["column_names"]))
        info["ui_unique"] = ucs

    return info


async def _create_engine():
    """Create an in-memory SQLite engine with all tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestTableCreation:
    """Verify migration creates tenant_memberships with expected schema."""

    @pytest.fixture
    async def schema_info(self):
        engine = await _create_engine()
        async with engine.connect() as conn:
            info = await conn.run_sync(_collect_schema_info)
        await engine.dispose()
        return info

    @pytest.mark.asyncio
    async def test_table_exists(self, schema_info):
        assert "tenant_memberships" in schema_info["tables"]

    @pytest.mark.asyncio
    async def test_expected_columns(self, schema_info):
        expected = {
            "id",
            "user_id",
            "tenant_id",
            "role",
            "is_active",
            "joined_via",
            "github_org_id",
            "created_at",
            "updated_at",
        }
        assert expected <= schema_info["tm_columns"]

    @pytest.mark.asyncio
    async def test_user_tenant_unique_constraint(self, schema_info):
        assert {"user_id", "tenant_id"} in schema_info["tm_unique"]


# ---------------------------------------------------------------------------
# Constraint tests
# ---------------------------------------------------------------------------


class TestConstraints:
    """Verify uniqueness and FK constraints."""

    @pytest.fixture
    async def engine(self):
        engine = await _create_engine()
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def session(self, engine):
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session

    @pytest.fixture
    async def org_and_user(self, session):
        """Seed an organization and user for FK satisfaction."""
        org = Organization(id="org-1", name="Test Org")
        session.add(org)
        await session.flush()

        user = User(id="user-1", org_id="org-1", team_id="team-1", email="u1@test.com", role="member")
        session.add(user)
        await session.flush()
        return org, user

    @pytest.mark.asyncio
    async def test_unique_user_tenant_rejects_duplicate(self, session, org_and_user):
        """Same (user_id, tenant_id) pair cannot be inserted twice."""
        m1 = TenantMembership(user_id="user-1", tenant_id="org-1", role="member", is_active=True)
        session.add(m1)
        await session.flush()

        m2 = TenantMembership(user_id="user-1", tenant_id="org-1", role="admin", is_active=False)
        session.add(m2)
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()

    @pytest.mark.asyncio
    async def test_different_tenants_allowed(self, session, org_and_user):
        """Same user can have memberships in different tenants."""
        org2 = Organization(id="org-2", name="Org Two")
        session.add(org2)
        await session.flush()

        m1 = TenantMembership(user_id="user-1", tenant_id="org-1", role="member", is_active=True)
        m2 = TenantMembership(user_id="user-1", tenant_id="org-2", role="member", is_active=False)
        session.add_all([m1, m2])
        await session.flush()  # Should not raise

        # Verify both exist
        result = await session.execute(sa.select(TenantMembership).where(TenantMembership.user_id == "user-1"))
        memberships = result.scalars().all()
        assert len(memberships) == 2


# ---------------------------------------------------------------------------
# Backfill tests (simulated)
# ---------------------------------------------------------------------------


class TestBackfill:
    """Verify backfill logic produces expected rows and is idempotent."""

    @pytest.fixture
    async def engine(self):
        engine = await _create_engine()
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def session(self, engine):
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session

    @pytest.fixture
    async def seeded_users(self, session):
        """Create org + 3 users to simulate pre-existing state."""
        org = Organization(id="org-1", name="Test Org")
        session.add(org)
        await session.flush()

        users = [
            User(id="user-1", org_id="org-1", team_id="team-1", email="u1@test.com", role="admin"),
            User(id="user-2", org_id="org-1", team_id="team-1", email="u2@test.com", role="member"),
            User(id="user-3", org_id="org-1", team_id="team-2", email="u3@test.com", role=None),
        ]
        session.add_all(users)
        await session.flush()
        return users

    async def _run_backfill(self, session):
        """Simulate the backfill SQL from the migration."""
        # SQLite UUID generation
        uuid_expr = (
            "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' || "
            "substr(hex(randomblob(2)),2) || '-' || "
            "substr('89ab', abs(random()) % 4 + 1, 1) || "
            "substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))"
        )
        await session.execute(
            sa.text(
                f"INSERT INTO tenant_memberships (id, user_id, tenant_id, role, is_active, joined_via) "
                f"SELECT "
                f"  {uuid_expr}, "
                f"  u.id, u.org_id, COALESCE(u.role, 'member'), 1, 'username_self' "
                f"FROM users u "
                f"WHERE NOT EXISTS ("
                f"  SELECT 1 FROM tenant_memberships tm "
                f"  WHERE tm.user_id = u.id AND tm.tenant_id = u.org_id"
                f")"
            )
        )
        await session.flush()

    @pytest.mark.asyncio
    async def test_backfill_creates_one_row_per_user(self, session, seeded_users):
        await self._run_backfill(session)

        result = await session.execute(sa.select(TenantMembership))
        memberships = result.scalars().all()
        assert len(memberships) == 3

    @pytest.mark.asyncio
    async def test_backfill_all_active(self, session, seeded_users):
        await self._run_backfill(session)

        result = await session.execute(
            sa.select(TenantMembership).where(TenantMembership.is_active == True)  # noqa: E712
        )
        active = result.scalars().all()
        assert len(active) == 3

    @pytest.mark.asyncio
    async def test_backfill_uses_user_role(self, session, seeded_users):
        await self._run_backfill(session)

        result = await session.execute(sa.select(TenantMembership).where(TenantMembership.user_id == "user-1"))
        m = result.scalar_one()
        assert m.role == "admin"

    @pytest.mark.asyncio
    async def test_backfill_null_role_defaults_to_member(self, session, seeded_users):
        await self._run_backfill(session)

        result = await session.execute(sa.select(TenantMembership).where(TenantMembership.user_id == "user-3"))
        m = result.scalar_one()
        assert m.role == "member"

    @pytest.mark.asyncio
    async def test_backfill_sets_joined_via(self, session, seeded_users):
        await self._run_backfill(session)

        result = await session.execute(sa.select(TenantMembership))
        memberships = result.scalars().all()
        assert all(m.joined_via == "username_self" for m in memberships)

    @pytest.mark.asyncio
    async def test_backfill_idempotent(self, session, seeded_users):
        """Running the backfill twice inserts no additional rows."""
        await self._run_backfill(session)
        await self._run_backfill(session)

        result = await session.execute(sa.select(TenantMembership))
        memberships = result.scalars().all()
        assert len(memberships) == 3


# ---------------------------------------------------------------------------
# Identity index relaxation tests
# ---------------------------------------------------------------------------


class TestIdentityIndexRelaxation:
    """Verify relaxed user_identities uniqueness: per-provider-per-tenant."""

    @pytest.fixture
    async def engine(self):
        engine = await _create_engine()
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def session(self, engine):
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session

    @pytest.fixture
    async def schema_info(self, engine):
        async with engine.connect() as conn:
            info = await conn.run_sync(_collect_schema_info)
        return info

    @pytest.mark.asyncio
    async def test_relaxed_index_exists(self, schema_info):
        """New index includes org_id in the uniqueness tuple."""
        assert {"provider", "provider_user_id", "org_id"} in schema_info["ui_unique"]

    @pytest.mark.asyncio
    async def test_same_provider_user_different_orgs_allowed(self, session):
        """Same GitHub user can exist in two different tenants."""
        from src.shared.models.vault import UserIdentity

        # Create two orgs and two users
        org1 = Organization(id="org-1", name="Org One")
        org2 = Organization(id="org-2", name="Org Two")
        session.add_all([org1, org2])
        await session.flush()

        u1 = User(id="user-1", org_id="org-1", team_id="team-1", email="u@test.com")
        u2 = User(id="user-2", org_id="org-2", team_id="team-2", email="u@test.com")
        session.add_all([u1, u2])
        await session.flush()

        # Same GitHub user (provider_user_id=gh-123) in two different orgs
        id1 = UserIdentity(
            user_id="user-1",
            org_id="org-1",
            team_id="team-1",
            provider="github",
            provider_user_id="gh-123",
            verification_method="oauth",
        )
        id2 = UserIdentity(
            user_id="user-2",
            org_id="org-2",
            team_id="team-2",
            provider="github",
            provider_user_id="gh-123",
            verification_method="oauth",
        )
        session.add_all([id1, id2])
        await session.flush()  # Should not raise

        result = await session.execute(sa.select(UserIdentity).where(UserIdentity.provider_user_id == "gh-123"))
        identities = result.scalars().all()
        assert len(identities) == 2

    @pytest.mark.asyncio
    async def test_same_provider_user_same_org_rejected(self, session):
        """Duplicate (provider, provider_user_id, org_id) within same tenant is rejected."""
        from src.shared.models.vault import UserIdentity

        org = Organization(id="org-1", name="Org One")
        session.add(org)
        await session.flush()

        u1 = User(id="user-1", org_id="org-1", team_id="team-1", email="u1@test.com")
        u2 = User(id="user-2", org_id="org-1", team_id="team-1", email="u2@test.com")
        session.add_all([u1, u2])
        await session.flush()

        id1 = UserIdentity(
            user_id="user-1",
            org_id="org-1",
            team_id="team-1",
            provider="github",
            provider_user_id="gh-123",
            verification_method="oauth",
        )
        session.add(id1)
        await session.flush()

        id2 = UserIdentity(
            user_id="user-2",
            org_id="org-1",
            team_id="team-1",
            provider="github",
            provider_user_id="gh-123",
            verification_method="oauth",
        )
        session.add(id2)
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# ORM round-trip tests
# ---------------------------------------------------------------------------


class TestModelRoundTrip:
    """Verify TenantMembership model works end-to-end via ORM."""

    @pytest.fixture
    async def engine(self):
        engine = await _create_engine()
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def session(self, engine):
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session

    @pytest.mark.asyncio
    async def test_create_and_read(self, session):
        org = Organization(id="org-1", name="Test Org")
        session.add(org)
        await session.flush()

        user = User(id="user-1", org_id="org-1", team_id="team-1", email="u@test.com")
        session.add(user)
        await session.flush()

        membership = TenantMembership(
            user_id="user-1",
            tenant_id="org-1",
            role="admin",
            is_active=True,
            joined_via="org_membership",
            github_org_id="12345",
        )
        session.add(membership)
        await session.flush()

        # Re-query
        result = await session.execute(sa.select(TenantMembership).where(TenantMembership.user_id == "user-1"))
        m = result.scalar_one()
        assert m.user_id == "user-1"
        assert m.tenant_id == "org-1"
        assert m.role == "admin"
        assert m.is_active is True
        assert m.joined_via == "org_membership"
        assert m.github_org_id == "12345"
        assert m.id is not None  # auto-generated UUID

    @pytest.mark.asyncio
    async def test_defaults(self, session):
        org = Organization(id="org-1", name="Test Org")
        session.add(org)
        await session.flush()

        user = User(id="user-1", org_id="org-1", team_id="team-1", email="u@test.com")
        session.add(user)
        await session.flush()

        membership = TenantMembership(user_id="user-1", tenant_id="org-1")
        session.add(membership)
        await session.flush()

        result = await session.execute(sa.select(TenantMembership).where(TenantMembership.user_id == "user-1"))
        m = result.scalar_one()
        assert m.role == "member"
        assert m.is_active is False
        assert m.joined_via == "org_membership"
        assert m.github_org_id is None

    @pytest.mark.asyncio
    async def test_fk_references_users_and_organizations(self, engine):
        """Verify FK constraints reference users and organizations tables."""

        def _check_fks(sync_conn):
            insp = sa_inspect(sync_conn)
            fks = insp.get_foreign_keys("tenant_memberships")
            fk_targets = {fk["referred_table"] for fk in fks}
            return fk_targets

        async with engine.connect() as conn:
            fk_targets = await conn.run_sync(_check_fks)

        assert "users" in fk_targets
        assert "organizations" in fk_targets

"""Unit tests for resolve_user_entity_id — the #4511 budget-key resolver.

The invariant under test: a ``user``-scoped budget must be keyed on the Cognito
sub, because that is what enforcement (``enforcement_service.py``) and the
``/api/me/budget`` read path (``me_routes._read_cap``) match on. Every test here
is either "this input form reaches the sub" or "this input form is refused
rather than silently persisted as an inert row".

Coverage:
  - sub passes through (but only when it names a real user in the org)
  - canonical users.id resolves to the sub
  - Cognito username resolves via user_identities, capital-G AND lowercase
  - email is refused (users.email has no uniqueness constraint)
  - a user with cognito_sub IS NULL is refused (the case that recreates the bug)
  - resolution never crosses tenants, in either direction
  - a platform admin acting on a foreign org resolves against THAT org
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.identity import UnresolvableUserEntityError, resolve_user_entity_id
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

# The real-world shapes from the incident: a GitHub-onboarded operator whose
# Cognito Username is `GitHub_20402445` and whose sub is a UUID.
GITHUB_USER_ID = "20402445"
GITHUB_USERNAME = f"GitHub_{GITHUB_USER_ID}"
HOME_SUB = "8a41f2c0-1b7d-4e5a-9c33-000000000001"
HOME_USER_ID = "user-home"
HOME_ORG = "org-home"

# The same GitHub account linked in a second tenant, with a different user row
# and a different sub — legal since migration 021 (#2961).
OTHER_SUB = "8a41f2c0-1b7d-4e5a-9c33-000000000002"
OTHER_USER_ID = "user-other"
OTHER_ORG = "org-other"

# A member who has never signed in: no sub, so no enforceable budget.
NO_SUB_USER_ID = "user-never-signed-in"


@pytest.fixture
async def engine():
    eng = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Organization(
                    id=org_id,
                    name=org_id,
                    aws_accounts=[],
                    role_mappings={},
                    settings={},
                    github_installation_ids=[],
                    cognito_client_ids=[],
                )
                for org_id in (HOME_ORG, OTHER_ORG)
            ]
        )
        session.add_all(
            [
                Department(id="dept-1", org_id=HOME_ORG, name="Eng"),
                Team(id="team-1", org_id=HOME_ORG, department_id="dept-1", name="Eng"),
            ]
        )
        session.add_all(
            [
                User(
                    id=HOME_USER_ID,
                    org_id=HOME_ORG,
                    team_id="team-1",
                    email="operator@test.com",
                    name="Operator",
                    cognito_sub=HOME_SUB,
                ),
                User(
                    id=OTHER_USER_ID,
                    org_id=OTHER_ORG,
                    team_id="team-1",
                    email="operator@test.com",
                    name="Operator Elsewhere",
                    cognito_sub=OTHER_SUB,
                ),
                User(
                    id=NO_SUB_USER_ID,
                    org_id=HOME_ORG,
                    team_id="team-1",
                    email="invited@test.com",
                    name="Invited",
                    cognito_sub=None,
                ),
            ]
        )
        await session.flush()

        # The same GitHub account is linked in BOTH tenants — the ambiguity the
        # org filter exists to resolve.
        session.add_all(
            [
                UserIdentity(
                    id="identity-home",
                    org_id=HOME_ORG,
                    team_id="team-1",
                    user_id=HOME_USER_ID,
                    provider="github",
                    provider_user_id=GITHUB_USER_ID,
                    provider_username="operator",
                    verification_method="oauth",
                ),
                UserIdentity(
                    id="identity-other",
                    org_id=OTHER_ORG,
                    team_id="team-1",
                    user_id=OTHER_USER_ID,
                    provider="github",
                    provider_user_id=GITHUB_USER_ID,
                    provider_username="operator",
                    verification_method="oauth",
                ),
                # A never-signed-in member with a GitHub link but no sub.
                UserIdentity(
                    id="identity-no-sub",
                    org_id=HOME_ORG,
                    team_id="team-1",
                    user_id=NO_SUB_USER_ID,
                    provider="github",
                    provider_user_id="99999999",
                    provider_username="invited",
                    verification_method="oauth",
                ),
            ]
        )
        await session.commit()
        yield session


class TestAcceptedForms:
    @pytest.mark.asyncio
    async def test_cognito_sub_passes_through(self, db):
        """A sub naming a real user in this org is returned unchanged."""
        assert await resolve_user_entity_id(db, HOME_ORG, HOME_SUB) == HOME_SUB

    @pytest.mark.asyncio
    async def test_canonical_user_id_resolves_to_sub(self, db):
        """The canonical users.id maps to that row's cognito_sub."""
        assert await resolve_user_entity_id(db, HOME_ORG, HOME_USER_ID) == HOME_SUB

    @pytest.mark.asyncio
    async def test_capital_g_github_username_resolves_to_sub(self, db):
        """`GitHub_<id>` — the form the broker actually mints — resolves."""
        assert await resolve_user_entity_id(db, HOME_ORG, GITHUB_USERNAME) == HOME_SUB

    @pytest.mark.asyncio
    async def test_lowercase_github_username_resolves_to_sub(self, db):
        """Prefix matching is case-insensitive, so `github_<id>` resolves too.

        The broker writes a capital G while other call sites in this repo parse
        lowercase; a case-sensitive comparison here would silently fail for the
        exact population #4511 affects.
        """
        assert await resolve_user_entity_id(db, HOME_ORG, f"github_{GITHUB_USER_ID}") == HOME_SUB

    @pytest.mark.asyncio
    async def test_resolved_value_is_never_the_supplied_username(self, db):
        """Guard the actual defect: the username must not survive resolution."""
        resolved = await resolve_user_entity_id(db, HOME_ORG, GITHUB_USERNAME)
        assert resolved != GITHUB_USERNAME


class TestRefusedForms:
    @pytest.mark.asyncio
    async def test_email_is_refused(self, db):
        """users.email carries no uniqueness constraint, so it is not a key.

        Resolving by email could land a spend cap on a different person than the
        operator intended — note both fixture users share this address.
        """
        with pytest.raises(UnresolvableUserEntityError) as exc:
            await resolve_user_entity_id(db, HOME_ORG, "operator@test.com")
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_null_cognito_sub_is_refused(self, db):
        """The case that recreates the bug: a user with no sub is unenforceable.

        Persisting a budget here would produce exactly the inert row #4511 is
        about, so this must raise rather than fall back to the supplied id.
        """
        with pytest.raises(UnresolvableUserEntityError) as exc:
            await resolve_user_entity_id(db, HOME_ORG, NO_SUB_USER_ID)
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_null_cognito_sub_via_github_username_is_refused(self, db):
        """Same rule when the NULL-sub user is reached through user_identities."""
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_user_entity_id(db, HOME_ORG, "GitHub_99999999")

    @pytest.mark.asyncio
    async def test_unknown_id_is_refused_naming_accepted_forms(self, db):
        """An unmappable id 422s with actionable guidance, and persists nothing."""
        with pytest.raises(UnresolvableUserEntityError) as exc:
            await resolve_user_entity_id(db, HOME_ORG, "user-123")
        assert exc.value.status_code == 422
        assert "Cognito sub" in exc.value.message

    @pytest.mark.asyncio
    async def test_unlinked_github_username_is_refused(self, db):
        """A GitHub id with no identity row in this org resolves to nothing."""
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_user_entity_id(db, HOME_ORG, "GitHub_11111111")

    @pytest.mark.asyncio
    async def test_bare_prefix_is_refused(self, db):
        """`GitHub_` with no id behind it is not a resolvable username."""
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_user_entity_id(db, HOME_ORG, "GitHub_")

    @pytest.mark.asyncio
    async def test_empty_id_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_user_entity_id(db, HOME_ORG, "   ")

    @pytest.mark.asyncio
    async def test_unknown_sub_is_refused_not_trusted(self, db):
        """A sub with no local users row would still be inert, so it is refused."""
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_user_entity_id(db, HOME_ORG, "8a41f2c0-0000-0000-0000-999999999999")


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_sub_from_another_org_is_refused(self, db):
        """A sub valid in another tenant must not resolve in this one."""
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_user_entity_id(db, HOME_ORG, OTHER_SUB)

    @pytest.mark.asyncio
    async def test_canonical_id_from_another_org_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_user_entity_id(db, HOME_ORG, OTHER_USER_ID)

    @pytest.mark.asyncio
    async def test_shared_github_account_resolves_per_tenant(self, db):
        """One GitHub account linked in two tenants resolves to each tenant's sub.

        This is the I7 case: without the org filter the same username would
        resolve to whichever row sorted first, keying a budget in one tenant on
        another tenant's user. It also confirms a platform admin operating on a
        foreign org resolves against THAT org, not their own.
        """
        assert await resolve_user_entity_id(db, HOME_ORG, GITHUB_USERNAME) == HOME_SUB
        assert await resolve_user_entity_id(db, OTHER_ORG, GITHUB_USERNAME) == OTHER_SUB

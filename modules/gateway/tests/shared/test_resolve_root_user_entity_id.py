"""Unit tests for resolve_root_user_entity_id — the #4536 cloud-agent budget key.

The invariant under test: a ``root_user``-scoped budget must be keyed on the
canonical ``users.id``, because that is what the budget-usage tracker writes
(#4300) and what the ``/api/me/budget`` read path derives
(``me_routes._resolve_root_principal``). A ``root_user`` cap in any other
namespace is inert in exactly the way #4511 was one ledger over.

The companion suite ``test_resolve_user_entity_id.py`` pins the *other* target of
the same lookup (the Cognito sub). Two properties are specific to this one and
are what these tests exist for:

  - a member with ``cognito_sub IS NULL`` IS resolvable here, where the direct-use
    resolver refuses them: agent spend is attributed from the run's lineage, not a
    signed-in session, so the cap is enforceable
  - a ``service:``-qualified root principal (#4344) passes through untouched — it
    is already a canonical ``root_user`` key and has no ``users`` row by design
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.identity import UnresolvableUserEntityError, resolve_root_user_entity_id
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

GITHUB_USER_ID = "20402445"
GITHUB_USERNAME = f"GitHub_{GITHUB_USER_ID}"
HOME_SUB = "8a41f2c0-1b7d-4e5a-9c33-000000000001"
HOME_USER_ID = "user-home"
HOME_ORG = "org-home"

OTHER_SUB = "8a41f2c0-1b7d-4e5a-9c33-000000000002"
OTHER_USER_ID = "user-other"
OTHER_ORG = "org-other"

# A member who has never signed in. Unlike the direct-use ledger, this person CAN
# be given a cloud-agent cap: their canonical id exists and is the key.
NO_SUB_USER_ID = "user-never-signed-in"
NO_SUB_GITHUB_ID = "99999999"


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
                UserIdentity(
                    id="identity-no-sub",
                    org_id=HOME_ORG,
                    team_id="team-1",
                    user_id=NO_SUB_USER_ID,
                    provider="github",
                    provider_user_id=NO_SUB_GITHUB_ID,
                    provider_username="invited",
                    verification_method="oauth",
                ),
            ]
        )
        await session.commit()
        yield session


class TestAcceptedForms:
    """Every form the picker or an operator can supply reaches the canonical id."""

    @pytest.mark.asyncio
    async def test_canonical_id_passes_through(self, db):
        """The canonical id is already the key, so it is returned unchanged.

        This is what the person picker submits for a cloud-agent budget.
        """
        assert await resolve_root_user_entity_id(db, HOME_ORG, HOME_USER_ID) == HOME_USER_ID

    @pytest.mark.asyncio
    async def test_cognito_sub_resolves_to_canonical_id(self, db):
        """A sub is accepted but NOT persisted: it is the other ledger's key.

        The trap this closes is the mirror image of #4511 — a `root_user` row keyed
        on a sub matches nothing the tracker ever writes.
        """
        resolved = await resolve_root_user_entity_id(db, HOME_ORG, HOME_SUB)
        assert resolved == HOME_USER_ID
        assert resolved != HOME_SUB

    @pytest.mark.asyncio
    async def test_capital_g_github_username_resolves_to_canonical_id(self, db):
        """`GitHub_<id>` — the form the auth broker mints — resolves."""
        assert await resolve_root_user_entity_id(db, HOME_ORG, GITHUB_USERNAME) == HOME_USER_ID

    @pytest.mark.asyncio
    async def test_lowercase_github_username_resolves_to_canonical_id(self, db):
        """Prefix matching is case-insensitive, as on the direct-use path."""
        assert await resolve_root_user_entity_id(db, HOME_ORG, f"github_{GITHUB_USER_ID}") == HOME_USER_ID

    @pytest.mark.asyncio
    async def test_whitespace_is_trimmed(self, db):
        """A pasted id with stray whitespace resolves rather than 422ing."""
        assert await resolve_root_user_entity_id(db, HOME_ORG, f"  {HOME_USER_ID}  ") == HOME_USER_ID


class TestNullSubMemberIsResolvable:
    """The one asymmetry with the direct-use ledger, and the reason it exists."""

    @pytest.mark.asyncio
    async def test_member_without_sub_resolves_by_canonical_id(self, db):
        """A never-signed-in member CAN hold a cloud-agent cap.

        `resolve_user_entity_id` refuses this person because their direct traffic
        has no matchable identity. Cloud spend is attributed from the run's
        lineage, so the canonical id is a real, enforceable key — refusing it here
        would deny a budget that works.
        """
        assert await resolve_root_user_entity_id(db, HOME_ORG, NO_SUB_USER_ID) == NO_SUB_USER_ID

    @pytest.mark.asyncio
    async def test_member_without_sub_resolves_via_github_username(self, db):
        """Same acceptance when that member is reached through user_identities."""
        assert await resolve_root_user_entity_id(db, HOME_ORG, f"GitHub_{NO_SUB_GITHUB_ID}") == NO_SUB_USER_ID


class TestServicePrincipalPassthrough:
    """`service:`-qualified root principals are already canonical keys (#4344)."""

    @pytest.mark.asyncio
    async def test_service_principal_passes_through_unmodified(self, db):
        """An unattended trigger has no `users` row, so it is not looked up."""
        supplied = "service:eventbridge:adp-dev-high-error-rate"
        assert await resolve_root_user_entity_id(db, HOME_ORG, supplied) == supplied

    @pytest.mark.asyncio
    async def test_service_principal_is_trimmed_not_rejected(self, db):
        """Trimming matches the person path, so keys agree byte-for-byte."""
        assert await resolve_root_user_entity_id(db, HOME_ORG, "  service:ci:nightly  ") == "service:ci:nightly"

    @pytest.mark.asyncio
    async def test_bare_qualifier_is_refused(self, db):
        """`service:` with nothing after it names no principal enforcement could
        ever write, so persisting it would be an inert cap (#4511 class)."""
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, "service:")

    @pytest.mark.asyncio
    async def test_whitespace_only_principal_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, "service:   ")

    @pytest.mark.asyncio
    async def test_whitespace_padded_principal_is_refused(self, db):
        """Enforcement writes `service:{id}` with no interior padding, so a padded
        principal can only ever be a typo that would never match."""
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, "service: nightly")

    @pytest.mark.asyncio
    async def test_qualifier_matches_the_canonical_constant(self):
        """The local copy of the qualifier must equal the budget module's.

        Duplicated to keep `src.shared` from importing `src.budget`; pinned here so
        the two cannot drift and start disagreeing about what a service key is.
        """
        from src.budget.schemas import SERVICE_PRINCIPAL_QUALIFIER
        from src.shared.identity.resolver import _SERVICE_PRINCIPAL_QUALIFIER

        assert _SERVICE_PRINCIPAL_QUALIFIER == SERVICE_PRINCIPAL_QUALIFIER


class TestRefusedForms:
    @pytest.mark.asyncio
    async def test_email_is_refused(self, db):
        """users.email has no uniqueness constraint, so it is not a key.

        Both fixture users share this address — resolving by it could cap the wrong
        person's agents.
        """
        with pytest.raises(UnresolvableUserEntityError) as exc:
            await resolve_root_user_entity_id(db, HOME_ORG, "operator@test.com")
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_id_is_refused_naming_accepted_forms(self, db):
        with pytest.raises(UnresolvableUserEntityError) as exc:
            await resolve_root_user_entity_id(db, HOME_ORG, "user-123")
        assert exc.value.status_code == 422
        assert "Cognito sub" in exc.value.message

    @pytest.mark.asyncio
    async def test_empty_id_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, "   ")

    @pytest.mark.asyncio
    async def test_bare_github_prefix_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, "GitHub_")

    @pytest.mark.asyncio
    async def test_unlinked_github_username_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, "GitHub_11111111")


class TestTenantIsolation:
    """Cross-tenant keying is the blast radius called out in the issue."""

    @pytest.mark.asyncio
    async def test_canonical_id_from_another_org_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, OTHER_USER_ID)

    @pytest.mark.asyncio
    async def test_sub_from_another_org_is_refused(self, db):
        with pytest.raises(UnresolvableUserEntityError):
            await resolve_root_user_entity_id(db, HOME_ORG, OTHER_SUB)

    @pytest.mark.asyncio
    async def test_shared_github_account_resolves_per_tenant(self, db):
        """One GitHub account linked in two tenants resolves to each tenant's user.

        Without the org filter the same username would resolve to whichever row
        sorted first, keying a budget in one tenant on another tenant's person.
        """
        assert await resolve_root_user_entity_id(db, HOME_ORG, GITHUB_USERNAME) == HOME_USER_ID
        assert await resolve_root_user_entity_id(db, OTHER_ORG, GITHUB_USERNAME) == OTHER_USER_ID

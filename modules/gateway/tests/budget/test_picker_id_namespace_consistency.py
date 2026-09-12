"""A config authored from the governance picker is one enforcement finds — Issue #4948.

The shared ``EntitySelector`` used to source its org / department / team options from
**Cognito** (org: a hardcoded single option naming the caller's own org; departments: the
distinct ``custom:department_id`` values scraped off signed-in users; teams: Cognito
*group names*). Enforcement has never looked anything up in Cognito. It compares the
stored config against the caller's token claims with raw string equality and no
translation whatsoever, in three namespaces:

===============  =============================================================
config key       what enforcement compares it against
===============  =============================================================
``org``          ``context.attributed_org_id`` — the ``custom:org_id`` claim,
                 which is an ``organizations.id``
``department``   ``context.department_id`` — ``custom:department_id``, synced
                 from the user's ``team.department_id``, a ``departments.id``
``team``         ``context.team_id`` — ``custom:team_id``, a projection of
                 ``users.team_id``, itself the pointer at the user's primary
                 ``team_memberships`` row, i.e. a ``teams.id``
===============  =============================================================

So the old picker's options were drawn from a **different namespace than the one the
lookup uses**, and a platform-native org had no option at all. Both failures are silent:
the row stores, the management table renders it as a live cap, and nothing is ever
matched. That is #4511's inert-config class, and it is why the operator on this issue
called a green UI with unmatched configs *worse* than the reported gap.

**This suite is the pin for that, and it is the non-negotiable part of #4948.** One test
per entity type. Each one:

1. builds a real tenancy world (``organizations`` / ``departments`` / ``teams`` /
   ``users`` / ``team_memberships`` rows) — the platform-native kind the picker now
   lists, not a synthetic id;
2. authors a config **through the real admin service**, passing exactly what the fixed
   picker sends: the partition is the **picked org** and the entity id is the
   ``organizations.id`` / ``departments.id`` / ``teams.id`` the list emitted;
3. drives the **real enforcement lookup** for a member of that entity, with a
   ``TokenContext`` whose claims are **read back out of the seeded rows** rather than
   restated;
4. asserts the request is DENIED and names that entity.

Point 3 is what makes them worth having. The pre-existing hierarchy test in
``tests/integration/test_budget_enforcement.py`` asserts claim == row id *by
construction* — it hands the context the same literal it seeded — so it passes whatever
namespace the picker uses and could never have caught this. Here, the team claim comes
from ``users.team_id`` **after** ``add_membership`` wrote the primary
``team_memberships`` row, and the department claim comes from ``team.department_id``: the
same derivations the Cognito claim-sync performs in production. A picker that emitted a
group name, a display name, or an id from the wrong namespace produces a context that
does not match, and the deny never happens.

**Per the #4068 gate, the load-bearing assertion is the DENIAL**, computed against a cap
already over its settled spend — a test that only asserted "the row exists" would be
satisfied by the broken code, since the broken code stored rows just fine.

``TestWrongPartitionIsNotEnforced`` is the companion in the other direction and pins the
trap that made this issue more than a dropdown change. Both governance forms post to
``/admin/organizations/{orgId}/budgets``, and enforcement matches on the org partition
**and** the entity id. While the org option could only ever be the caller's own org, the
partition was consistent by coincidence. Offering the full org list breaks that
coincidence, so the picked org has to drive the *write partition* too — otherwise the fix
to the dropdown ships precisely the inert configs it was meant to eliminate. That test
authors a real team's cap in the *wrong* partition and asserts the request is ALLOWED:
it is the bug, reproduced, and it fails if anyone re-points the forms at the caller's own
org.

Deliberately NOT covered: org-level **rate limits**. ``src/ratelimit/models.py`` spells
that entity type ``"organization"`` while the admin API and this suite's budget rows spell
it ``"org"``, so an org rate limit can never be matched regardless of which picker
authored it — a pre-existing defect, already pinned as finding 3 in
``platform/evals/budget-ratelimit/README.md``, with stored-row migration implications
outside this issue's scope. Asserting it here would either fail for a reason #4948 did
not cause or quietly imply it works.

Harness: real in-memory SQLite, the real ``AdminService`` for the write and the real
``BudgetEnforcementService`` for the read, with reservations off — the subject is which
key the settled-ledger lookup matches, and a live Redis denominator would only add a
second reason for a request to be denied.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.schemas import BudgetCreateRequest
from src.admin.service import AdminService
from src.admin.team_memberships import add_membership
from src.budget.config import BudgetConfig as BudgetFeatureConfig
from src.budget.enforcement_service import BudgetEnforcementService
from src.shared.models.base import Base
from src.shared.models.budget import BudgetUsage
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

# The reported repro: an org created platform-natively through the #4841 tenancy
# panels, which the Cognito-sourced picker could not show at all.
NATIVE_ORG = "sophos-it"

# The org the admin is signed into. Distinct from NATIVE_ORG throughout, because a
# same-org fixture cannot tell a correct partition from the caller's own — which is
# exactly the coincidence that hid the partition trap.
ADMIN_ORG = "acme-corp"

# Sized so ONE request crosses the cap on the settled total alone: the estimate is
# model/size-aware and small, so the arithmetic under test is the ledger, not the
# estimate.
CAP = Decimal("10.00")
SETTLED = Decimal("50.00")


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


def _feature_config() -> BudgetFeatureConfig:
    """A REAL BudgetConfig with reservations off (the #4046 trap: never a MagicMock).

    A fully-patched config asserts a guarantee it never exercised. Only the
    reservation flag moves, and only because a live Redis denominator would give a
    request a second reason to be denied — which would let a test pass with the
    settled-ledger lookup, the actual subject, still broken.
    """
    config = BudgetFeatureConfig()
    object.__setattr__(config, "budget_reservation_enabled", False)
    return config


class _World:
    """The seeded tenancy rows, plus the claims DERIVED from them.

    The claims are read back off the rows (``user.team_id``, ``team.department_id``,
    ``user.org_id``) rather than restated from the literals used to seed, so a config
    keyed in the wrong namespace cannot be matched by a context that was handed the
    same wrong value. That derivation is the whole point of this harness.
    """

    def __init__(self, org: Organization, dept: Department, team: Team, member: User):
        self.org = org
        self.dept = dept
        self.team = team
        self.member = member

    def context(self) -> TokenContext:
        return TokenContext(
            # Cognito sub of the member. Never equal to any tenancy id here, so a
            # lookup that fell back to the user rung cannot satisfy a team/dept/org
            # assertion.
            user_id=f"sub-of-{self.member.id}",
            org_id=self.member.org_id,
            # `custom:team_id` — the projection of `users.team_id`, which
            # `add_membership` pointed at the primary `team_memberships` row.
            team_id=self.member.team_id,
            # `custom:department_id` — synced from the user's team's department
            # (src/admin/onboarding/approval.py does exactly this).
            department_id=self.team.department_id,
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="cognito",
            attributed_org_id=self.member.org_id,
        )


async def seed_world(session: AsyncSession, org_id: str) -> _World:
    """A platform-native org with a department, a team, and a member of that team.

    Ids and names are deliberately DIFFERENT strings. A picker that emitted the name
    instead of the id — which is what the Cognito team list did — produces a config
    this world's derived claims cannot match, and the deny under test does not happen.
    """
    org = Organization(id=org_id, name=f"{org_id} Display Name")
    session.add(org)
    dept = Department(id=f"dept-{org_id}-platform", org_id=org_id, name="Platform Engineering")
    session.add(dept)
    team = Team(id=f"team-{org_id}-sre", org_id=org_id, department_id=dept.id, name="SRE")
    session.add(team)
    # `team_id=""`: the member starts with NO primary team, so `add_membership` below
    # is what points `users.team_id` at the team — the server-maintained derivation the
    # claim is a projection of. Seeding the pointer directly would assert the claim by
    # construction, which is the weakness this suite exists to avoid.
    member = User(id=f"user-{org_id}-01", org_id=org_id, team_id="", email=f"member@{org_id}.example.com")
    session.add(member)
    await session.flush()

    await add_membership(session, user_id=member.id, team_id=team.id, org_id=org_id, is_primary=True)
    await session.commit()

    # Read the pointer back rather than trusting the call: this is the value the claim
    # is a projection of, and it is what the team assertion turns on.
    refreshed = await session.get(User, member.id)
    assert refreshed is not None
    return _World(org=org, dept=dept, team=team, member=refreshed)


async def author_config_as_picker_does(
    session: AsyncSession,
    *,
    partition_org_id: str,
    entity_type: str,
    entity_id: str,
) -> None:
    """Author a budget through the REAL admin service, the way the fixed form posts.

    ``partition_org_id`` is the org the picker SELECTED (``scopeOrgId`` in
    ``BudgetFormModal``), not the caller's own — that distinction is the #4948
    partition fix. Going through ``AdminService.create_budget`` rather than inserting a
    row keeps ``_resolve_person_entity_id`` and the conflict probe in the path, so this
    exercises the same write the UI performs.
    """
    admin = AdminService(session)
    await admin.create_budget(
        partition_org_id,
        BudgetCreateRequest(
            entity_type=entity_type,
            entity_id=entity_id,
            period_type=PeriodType.MONTHLY,
            budget_amount_usd=CAP,
            enforcement_mode="hard",
        ),
    )


async def seed_settled_spend(session: AsyncSession, *, org_id: str, entity_type: str, entity_id: str) -> None:
    """Put the entity's settled monthly ledger over its cap.

    Monthly only: that is the period the config above was authored for, and
    ``_check_entity_budget`` reads the ledger for the period it was asked about, so a
    daily/weekly row would not participate in the assertion.
    """
    from src.budget.utils import get_period_start_end

    period_start, _ = get_period_start_end(PeriodType.MONTHLY)
    session.add(
        BudgetUsage(
            org_id=org_id,
            entity_type=entity_type,
            entity_id=entity_id,
            period_type=PeriodType.MONTHLY.value,
            period_start=period_start,
            total_cost_usd=SETTLED,
        )
    )
    await session.commit()


async def check(session: AsyncSession, context: TokenContext):
    """Run the real hierarchy check against the seeded session."""
    service = BudgetEnforcementService()
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", _feature_config()):
            return await service.check_budget_hierarchy(context, estimated_cost=Decimal("0.01"))


# =============================================================================
# One test per entity type: authored via the picker's values -> enforcement finds it
# =============================================================================


@pytest.mark.asyncio
async def test_org_config_from_picker_is_found_by_enforcement(session: AsyncSession):
    """An org budget keyed by ``organizations.id`` denies a member of that org.

    The old picker could not offer this org at all — its ORGANIZATION branch was a
    single hardcoded option naming the caller's own org, so a platform-native tenant
    like ``sophos-it`` was unreachable from every governance form. This is the value
    the fixed picker emits (``org.id``, never ``org.name``) matched against
    ``attributed_org_id``.
    """
    world = await seed_world(session, NATIVE_ORG)

    await author_config_as_picker_does(
        session,
        partition_org_id=world.org.id,
        entity_type="org",
        entity_id=world.org.id,
    )
    await seed_settled_spend(session, org_id=world.org.id, entity_type="org", entity_id=world.org.id)

    result = await check(session, world.context())

    assert result.allowed is False
    assert result.exceeded_entity_type == EntityType.ORGANIZATION
    assert result.exceeded_entity_id == world.org.id
    # The name is a LABEL. If it ever becomes the stored key, this row stops matching.
    assert result.exceeded_entity_id != world.org.name


@pytest.mark.asyncio
async def test_department_config_from_picker_is_found_by_enforcement(session: AsyncSession):
    """A department budget keyed by ``departments.id`` denies a member of that dept.

    The old picker listed the distinct ``custom:department_id`` values found on an
    org's signed-in Cognito users, so a department created platform-natively was
    invisible until somebody in it had logged in — and one nobody had joined yet could
    never be governed at all. The claim under test is derived from the member's team's
    ``department_id``, which is what the production claim-sync writes.
    """
    world = await seed_world(session, NATIVE_ORG)

    await author_config_as_picker_does(
        session,
        partition_org_id=world.org.id,
        entity_type="department",
        entity_id=world.dept.id,
    )
    await seed_settled_spend(session, org_id=world.org.id, entity_type="department", entity_id=world.dept.id)

    result = await check(session, world.context())

    assert result.allowed is False
    assert result.exceeded_entity_type == EntityType.DEPARTMENT
    assert result.exceeded_entity_id == world.dept.id
    assert result.exceeded_entity_id != world.dept.name


@pytest.mark.asyncio
async def test_team_config_from_picker_is_found_by_enforcement(session: AsyncSession):
    """A team budget keyed by ``teams.id`` denies a member of that team.

    The sharpest case. The old picker listed Cognito **group names**; the claim is a
    projection of ``users.team_id``, a ``teams.id``. The two only ever agreed for orgs
    whose groups happened to be named after their team ids.

    The claim here is whatever ``add_membership`` left in ``users.team_id`` when it
    wrote the primary ``team_memberships`` row — asserted below to be the team's id and
    not its name, so this test cannot pass by having been handed the answer.
    """
    world = await seed_world(session, NATIVE_ORG)

    # The derivation, stated: the claim's source is the membership pointer.
    assert world.member.team_id == world.team.id
    assert world.member.team_id != world.team.name

    await author_config_as_picker_does(
        session,
        partition_org_id=world.org.id,
        entity_type="team",
        entity_id=world.team.id,
    )
    await seed_settled_spend(session, org_id=world.org.id, entity_type="team", entity_id=world.team.id)

    result = await check(session, world.context())

    assert result.allowed is False
    assert result.exceeded_entity_type == EntityType.TEAM
    assert result.exceeded_entity_id == world.team.id


@pytest.mark.asyncio
async def test_team_config_keyed_by_cognito_group_name_is_inert(session: AsyncSession):
    """The bug, reproduced: a cap keyed by the team's NAME enforces nothing.

    This is the pre-#4948 picker's output for the team rung. It stores without
    complaint and would render in Budget Management as a live cap. The request is
    ALLOWED, at ten times the cap in settled spend, because no lookup in the system
    translates a group name to a ``teams.id``.

    Kept alongside the passing case because either test alone can be satisfied by an
    accident: this one fails the moment anything starts normalising names to ids, which
    would mean the namespace contract had moved and the tests above needed rewriting.
    """
    world = await seed_world(session, NATIVE_ORG)

    await author_config_as_picker_does(
        session,
        partition_org_id=world.org.id,
        entity_type="team",
        # What the Cognito group list emitted — a display name, not an id.
        entity_id=world.team.name,
    )
    await seed_settled_spend(session, org_id=world.org.id, entity_type="team", entity_id=world.team.name)

    result = await check(session, world.context())

    assert result.allowed is True


# =============================================================================
# The partition trap — the half the issue's Design does not name
# =============================================================================


class TestWrongPartitionIsNotEnforced:
    """Both columns must come from the picked org, not just the entity id.

    ``_check_entity_budget`` matches ``org_id`` AND ``entity_id``. The forms post to
    ``/admin/organizations/{orgId}/budgets``, so before #4948 the partition was always
    the caller's own org — consistent only because the org option could not be anything
    else. These two tests are the before and after of pointing the write at
    ``scopeOrgId``.
    """

    @pytest.mark.asyncio
    async def test_correct_partition_denies(self, session: AsyncSession):
        """Authored into the picked org's partition: the cap is enforced."""
        await seed_world(session, ADMIN_ORG)
        target = await seed_world(session, NATIVE_ORG)

        await author_config_as_picker_does(
            session,
            # `scopeOrgId` — the org the operator picked, which is NOT the admin's own.
            partition_org_id=target.org.id,
            entity_type="team",
            entity_id=target.team.id,
        )
        await seed_settled_spend(session, org_id=target.org.id, entity_type="team", entity_id=target.team.id)

        result = await check(session, target.context())

        assert result.allowed is False
        assert result.exceeded_entity_type == EntityType.TEAM
        assert result.exceeded_entity_id == target.team.id

    @pytest.mark.asyncio
    async def test_callers_own_partition_is_inert(self, session: AsyncSession):
        """Authored into the ADMIN's partition: the same cap enforces nothing.

        The entity id is correct and platform-native; only the partition is the
        caller's own. The row stores, reads back as a cap, and is never matched. This is
        what shipping the dropdown fix without the ``onScopeOrgChange`` write change
        would have produced — and it fails if either form is ever re-pointed at
        ``orgId``.
        """
        admin_world = await seed_world(session, ADMIN_ORG)
        target = await seed_world(session, NATIVE_ORG)

        await author_config_as_picker_does(
            session,
            # The pre-#4948 behaviour: the caller's own org.
            partition_org_id=admin_world.org.id,
            entity_type="team",
            entity_id=target.team.id,
        )
        await seed_settled_spend(session, org_id=admin_world.org.id, entity_type="team", entity_id=target.team.id)

        result = await check(session, target.context())

        assert result.allowed is True


# =============================================================================
# Legacy configs are FLAGGED, never hidden
# =============================================================================


class TestUnresolvedConfigsAreFlaggedNotHidden:
    """A config whose entity no longer resolves stays in the list, marked.

    The rule from the issue. Such a row is not enforced, but it IS a spend control
    somebody believes is in force — filtering it out of Budget Management removes the
    only place they could ever find out otherwise, which is #4511 with the evidence
    deleted.
    """

    @pytest.mark.asyncio
    async def test_resolvable_rows_are_named_and_not_flagged(self, session: AsyncSession):
        world = await seed_world(session, NATIVE_ORG)
        for entity_type, entity_id in (("org", world.org.id), ("department", world.dept.id), ("team", world.team.id)):
            await author_config_as_picker_does(
                session,
                partition_org_id=world.org.id,
                entity_type=entity_type,
                entity_id=entity_id,
            )

        response = await AdminService(session).get_budgets_list(world.org.id)

        by_type = {item.entity_type: item for item in response.items}
        assert set(by_type) == {"org", "department", "team"}
        assert all(item.entity_unresolved is False for item in response.items)
        # The NAME is the label; the id stays the key.
        assert by_type["team"].entity_display_name == world.team.name
        assert by_type["team"].entity_id == world.team.id
        assert by_type["department"].entity_display_name == world.dept.name
        assert by_type["org"].entity_display_name == world.org.name

    @pytest.mark.asyncio
    async def test_stale_team_id_is_listed_and_flagged(self, session: AsyncSession):
        """A team cap authored by the OLD picker: still listed, flagged unresolved."""
        world = await seed_world(session, NATIVE_ORG)
        await author_config_as_picker_does(
            session,
            partition_org_id=world.org.id,
            entity_type="team",
            entity_id=world.team.name,  # a Cognito group name, the pre-fix output
        )

        response = await AdminService(session).get_budgets_list(world.org.id)

        assert len(response.items) == 1, "an unenforceable config must not be filtered out of the list"
        assert response.items[0].entity_id == world.team.name
        assert response.items[0].entity_unresolved is True
        assert response.items[0].entity_display_name is None

    @pytest.mark.asyncio
    async def test_foreign_org_team_is_flagged_not_borrowed(self, session: AsyncSession):
        """A team id from ANOTHER tenant must read unresolved, not resolve to its name.

        The resolver is org-scoped for exactly this reason: naming a foreign team here
        would present a cross-tenant row as healthy and leak the other tenant's team
        name into this org's screen.
        """
        world = await seed_world(session, NATIVE_ORG)
        foreign = await seed_world(session, ADMIN_ORG)

        await author_config_as_picker_does(
            session,
            partition_org_id=world.org.id,
            entity_type="team",
            entity_id=foreign.team.id,
        )

        response = await AdminService(session).get_budgets_list(world.org.id)

        assert len(response.items) == 1
        assert response.items[0].entity_unresolved is True
        assert response.items[0].entity_display_name is None

    @pytest.mark.asyncio
    async def test_org_row_naming_a_different_org_is_flagged(self, session: AsyncSession):
        """An ``org`` row whose entity id is not its own partition can never match.

        Enforcement fills both columns from the same ``attributed_org_id``, so however
        real the other org is, this row is unmatchable. Resolving it by id alone would
        make it read as healthy.
        """
        world = await seed_world(session, NATIVE_ORG)
        other = await seed_world(session, ADMIN_ORG)

        await author_config_as_picker_does(
            session,
            partition_org_id=world.org.id,
            entity_type="org",
            entity_id=other.org.id,
        )

        response = await AdminService(session).get_budgets_list(world.org.id)

        assert len(response.items) == 1
        assert response.items[0].entity_unresolved is True

    @pytest.mark.asyncio
    async def test_person_scoped_rows_are_never_flagged(self, session: AsyncSession):
        """``user`` rows keep their own resolution (#4511) and never set the flag.

        Their keys live in namespaces this check does not know how to verify (a Cognito
        sub, a canonical ``users.id``). Running them through a tenancy lookup would flag
        every healthy person cap on the screen.
        """
        world = await seed_world(session, NATIVE_ORG)
        sub = "cognito-sub-of-a-real-person"
        # Give the member a sub so `_resolve_person_entity_id` accepts it, then author
        # the cap the way the person picker does.
        member = await session.get(User, world.member.id)
        member.cognito_sub = sub
        await session.commit()

        await author_config_as_picker_does(
            session,
            partition_org_id=world.org.id,
            entity_type="user",
            entity_id=sub,
        )

        response = await AdminService(session).get_budgets_list(world.org.id)

        assert len(response.items) == 1
        assert response.items[0].entity_type == "user"
        assert response.items[0].entity_unresolved is False

    @pytest.mark.asyncio
    async def test_rate_limit_list_flags_the_same_way(self, session: AsyncSession):
        """The rate-limit list carries the flag too — the same forms author both."""
        from src.admin.schemas import RateLimitCreateRequest

        world = await seed_world(session, NATIVE_ORG)
        admin = AdminService(session)
        await admin.create_ratelimit(
            world.org.id,
            RateLimitCreateRequest(entity_type="team", entity_id=world.team.id, rpm=60),
        )
        await admin.create_ratelimit(
            world.org.id,
            RateLimitCreateRequest(entity_type="team", entity_id=world.team.name, rpm=60),
        )

        response = await admin.get_ratelimits_list(world.org.id)

        by_id = {item.entity_id: item for item in response.items}
        assert len(by_id) == 2, "a stale rate limit must not be filtered out of the list"
        assert by_id[world.team.id].entity_unresolved is False
        assert by_id[world.team.id].entity_display_name == world.team.name
        assert by_id[world.team.name].entity_unresolved is True


# =============================================================================
# The lists the picker now reads emit exactly those keys
# =============================================================================


@pytest.mark.asyncio
async def test_picker_source_lists_emit_the_enforcement_keys(session: AsyncSession):
    """The three endpoints the fixed ``EntitySelector`` calls emit the matched ids.

    ``getOrganizations`` -> ``list_organizations``, ``getDepartments`` ->
    ``list_departments``, ``getOrgTeams`` -> ``list_org_teams``. The tests above prove
    the ids match; this proves the picker's *sources* are where those ids come from, so
    the frontend cannot be reading a list that merely looks right.
    """
    world = await seed_world(session, NATIVE_ORG)
    admin = AdminService(session)

    orgs, _ = await admin.list_organizations(org_ids=[world.org.id])
    assert [o.id for o in orgs] == [world.org.id]

    depts, _ = await admin.list_departments(world.org.id)
    assert [d.id for d in depts] == [world.dept.id]

    teams, _ = await admin.list_org_teams(world.org.id)
    assert [t.id for t in teams] == [world.team.id]

    # And each id is the value the member's derived claims carry.
    context = world.context()
    assert orgs[0].id == context.attributed_org_id
    assert depts[0].id == context.department_id
    assert teams[0].id == context.team_id


@pytest.mark.asyncio
async def test_org_list_is_not_restricted_to_the_callers_own_org(session: AsyncSession):
    """The org list can name an org other than the caller's — the reported gap.

    Authority scoping still applies one layer up (``get_accessible_organizations``
    returns ``[allowed_org_id]`` for an org admin and ``None``/all for a platform
    admin), so this is not a widening; it is the reason a platform admin's dropdown can
    contain ``sophos-it`` at all.
    """
    await seed_world(session, ADMIN_ORG)
    await seed_world(session, NATIVE_ORG)

    orgs, total = await AdminService(session).list_organizations()

    assert total == 2
    assert {o.id for o in orgs} == {ADMIN_ORG, NATIVE_ORG}


@pytest.mark.asyncio
async def test_membership_pointer_is_the_team_claim_source(session: AsyncSession):
    """``users.team_id`` follows the primary ``team_memberships`` row.

    The claim the team rung matches is a projection of this pointer, so a second,
    non-primary team must NOT move it — otherwise a team cap would silently start
    enforcing against a different team than the one the operator picked.
    """
    world = await seed_world(session, NATIVE_ORG)
    second = Team(id=f"team-{NATIVE_ORG}-data", org_id=NATIVE_ORG, department_id=world.dept.id, name="Data")
    session.add(second)
    await session.flush()

    await add_membership(session, user_id=world.member.id, team_id=second.id, org_id=NATIVE_ORG)
    await session.commit()

    member = await session.get(User, world.member.id)
    assert member.team_id == world.team.id, "a non-primary membership must not re-point the claim"

    primary = (
        await session.execute(
            select(User).where(User.id == world.member.id, User.team_id == world.team.id),
        )
    ).scalar_one_or_none()
    assert primary is not None

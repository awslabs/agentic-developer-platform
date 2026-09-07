"""Default person limits and the fallback ladder — Issue #4690 (person-limits · D1).

C3/C4 (#4629/#4630) gave a person one ceiling on their total agent spend across
every org, and taught enforcement to read it. Both start from an individual
``person_budget_configs`` row, so **"no personal row" still meant "unlimited"** — a
platform admin could not bound a population, only wait for each person to bound
themselves. This suite pins the rung ladder that closes it:

    individual row  >  team default  >  org default  >  platform default

**Two properties are load-bearing and each is asserted as a denial, not a lookup**
(the #4068 gate — a test that only reads a resolver can be satisfied by deleting the
enforcement branch):

* ``TestDefaultDeniesWithNoPersonalRow`` — a person with **no** ``person_budget_configs``
  row at all is stopped by a platform default. This is the issue in one sentence and
  it fails on pre-#4690 code.
* ``TestIndividualRowShadowsPerPeriod`` — a personal row overrides the default **for
  its own period only**. The tempting shortcut ("any personal row means defaults do
  not apply") would let anybody escape a daily platform ceiling by authoring an
  unrelated monthly limit on themselves, and it would pass a per-person test.

**Precedence is a first-match walk, and "tightest" means two different things** at
two different places — the pair of rules most likely to be conflated, so each gets
its own class:

* ACROSS rungs (``TestRungPrecedence``): the more specific rung wins **even when it
  is more generous**. A team default of $5,000 beats a platform default of $1,000,
  because "unless we say otherwise" is what a specific rule is for.
* WITHIN one rung (``TestLowestWinsWithinARung``): the **lowest amount** wins. A
  person in two orgs that both carry an org default matches two peer rules; taking
  the highest would mean a ceiling could be escaped by joining a more generous org,
  which is not a ceiling. Operator ruling, 2026-09-07.

**The team rung is matched on the PAIR** ``(org_id, team_id)`` — see
``TestTeamRungMatchesPairsNotACrossProduct``. ``teams`` carries ``TenantMixin``, so a
``teams.id`` is unique only inside its org; the cross-product form
(``org IN (...) AND team IN (...)``) would let one tenant's rule govern a same-id
team in an unrelated tenant, invisibly to that tenant's admin. That is the #4511
wrong-key class in its damaging direction: not an inert cap, but a cap governing
somebody it was never authored for.

**The hot path is pinned too** (``TestZeroCostWhenNoRulesExist``). The #4689
regression was several sequential identity queries on every JWT model invoke; the
existence gate now answers about BOTH tables in one round trip and reports them
separately, so an install with individual caps and no defaults never pays the
org/team fan-out. Asserted by counting real queries against the real session, not
by reading the source.

Harness: real in-memory SQLite with real ``PersonBudgetDefault``/``PersonBudgetConfig``/
``BudgetUsage``/``User``/``TenantMembership``/``UserIdentity`` rows, driven through the
real pure-ASGI middleware — the ``test_person_cap_enforcement.py`` shape, imported
from it rather than re-declared so the two suites cannot drift on what the topology
means. Config overrides are a REAL ``BudgetConfig`` via ``object.__setattr__``, never
a ``MagicMock`` (the #4046 trap).
"""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.person_ledger import (
    resolve_applicable_person_limits,
    resolve_person_default_limits,
    resolve_person_team_keys,
)
from src.shared.models.base import Base
from src.shared.models.budget import PersonBudgetConfig, PersonBudgetDefault
from src.shared.models.organization import User
from src.shared.schemas.budget import PeriodType

# The enforcement harness, reused verbatim. Importing it is the point: these tests
# must exercise the SAME middleware path, the same seeding semantics and the same
# topology constants as #4630's suite, or "the ladder denies" would be a claim about
# a second, friendlier stack.
from .test_person_cap_enforcement import (
    CALLER_CANONICAL_ID,
    CALLER_GITHUB_ID,
    CALLER_SUB,
    HOME_ORG,
    PERSON_ANCHOR,
    RUN_ORG,
    _drive,
    _service,
    agent_context,
    seed_github_identity,
    seed_membership,
    seed_org,
    seed_person_cap,
    seed_root_usage,
    seed_user,
)

# A third tenant the person IS a member of, so the org rung can legitimately match
# two rules at once — the multi-org "lowest wins" case.
SECOND_ORG = "org-globex"

# A tenant the person is NOT a member of. Its defaults must never govern them: the
# rung's matching key comes from server-derived memberships, so a rule from here
# reaching the person means the partition list came from somewhere it should not.
FOREIGN_ORG = "org-not-a-member"

# `teams.id` values. ENG is deliberately reused across two orgs, because a team id is
# unique only inside its org and the cross-product bug hides in exactly that overlap.
TEAM_ENG = "team-eng"
TEAM_OPS = "team-ops"

AUTHOR_ID = "00000000-0000-4000-8000-00000000adm1"


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


@pytest.fixture
def redis_client():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def seed_default(
    session: AsyncSession,
    *,
    scope_type: str,
    amount: str,
    scope_id_org: str | None = None,
    scope_id_team: str | None = None,
    period_type: str = "monthly",
    row_id: str | None = None,
    enforcement_mode: str = "hard",
) -> None:
    """One ``person_budget_defaults`` row.

    ``row_id`` is settable because ties within a rung break on ``id`` — a test about
    equal amounts must be able to say which row it expects to win rather than depend
    on a generated value.
    """
    row = PersonBudgetDefault(
        scope_type=scope_type,
        scope_id_org=scope_id_org,
        scope_id_team=scope_id_team,
        period_type=period_type,
        budget_amount_usd=Decimal(amount),
        enforcement_mode=enforcement_mode,
        authored_by_user_id=AUTHOR_ID,
    )
    if row_id is not None:
        row.id = row_id
    session.add(row)
    await session.commit()


async def set_team(session: AsyncSession, user_id: str, team_id: str) -> None:
    """Put an existing ``users`` row on a team.

    The shared ``seed_user`` writes ``team_id=""`` (what several provisioning paths
    really do), so the team rung needs this to have anything to match on — and the
    empty-string case stays exercised by every test that does NOT call it.
    """
    user = await session.get(User, user_id)
    user.team_id = team_id
    await session.commit()


@pytest.fixture
async def person_topology(session) -> None:
    """The #4630 §2 scenario, unchanged: one ``users`` row, GitHub identity, two tenants.

    Home tenant ``HOME_ORG``, agent runs execute in ``RUN_ORG``, no limit of any kind
    seeded — each test authors the rungs it is about. ``team_id`` is left ``""`` so
    the team rung is off unless a test opts in via ``set_team``.
    """
    await seed_org(session, HOME_ORG, "Pranav Sharma")
    await seed_org(session, RUN_ORG, "AWS-E")
    await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
    await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
    await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)
    await seed_membership(session, CALLER_CANONICAL_ID, RUN_ORG)


# =============================================================================
# GATE — the headline case. Must fail on pre-#4690 code.
# =============================================================================


class TestDefaultDeniesWithNoPersonalRow:
    """A person with NO ``person_budget_configs`` row is stopped by a platform default.

    The issue in one statement. Before #4690 this exact request returned 200 — the
    enforcement layer skipped entirely once the individual cap read missed, so an
    admin's platform-wide ceiling was authored, displayed, and governed nobody.

    Deliberately asserted as a **denial that never reaches the app**
    (``app_invoked is False``), per the #4068 gate: a test that merely called the
    resolver and inspected a dict would stay green if the enforcement branch were
    deleted.
    """

    async def test_platform_default_denies_a_person_with_no_row_of_their_own(self, session, redis_client, person_topology):
        await seed_default(session, scope_type="platform", amount="100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "150.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.app_invoked is False

    async def test_the_default_still_spans_every_partition(self, session, redis_client, person_topology):
        """The ladder inherits C4's cross-org denominator; it does not restart it.

        $60 in each of two tenants against a $100 platform default: neither partition
        alone crosses it. An implementation that resolved the default correctly but
        summed one partition would return a plausible 200.
        """
        await seed_default(session, scope_type="platform", amount="100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "60.00")
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "60.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.app_invoked is False

    async def test_a_person_under_the_default_is_still_allowed(self, session, redis_client, person_topology):
        """The companion in the other direction — the ladder is not a blanket denial.

        Without this, deleting the comparison and denying unconditionally would pass
        every other test in this class.
        """
        await seed_default(session, scope_type="platform", amount="100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "10.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_the_402_names_the_source_rung_and_the_person(self, session, redis_client, person_topology):
        """The denial text says WHICH rule stopped them, and whose it is.

        A person stopped at a platform default they never authored, told only
        "personal spending limit exceeded", goes looking for a limit of their own that
        does not exist — then files a ticket nobody can action. ``scope="person"``
        alone says neither which person nor which rule (the #4630 pin, widened here).
        """
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        # `body["message"]` is where the middleware renders `blocked_reason` — the
        # same field `test_person_cap_enforcement.py`'s #4630 anchor pin reads.
        message = harness.body["message"]
        assert "platform default" in message
        assert PERSON_ANCHOR in message
        assert "monthly" in message

    async def test_the_402_names_the_org_that_set_an_org_default(self, session, redis_client, person_topology):
        """On the org rung the rung alone is not enough — the org id is named.

        "An org default" does not tell a multi-org person which of their orgs set it,
        and that is precisely what they need in order to go ask somebody.
        """
        await seed_default(session, scope_type="org", scope_id_org=RUN_ORG, amount="10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert f"org default for {RUN_ORG}" in harness.body["message"]

    async def test_scope_stays_person_so_the_worker_classifies_the_stop(self, session, redis_client, person_topology):
        """A default is still a PERSON limit — ``scope`` must not become a new value.

        The worker classifies the stop by this discriminator; an unthreaded new scope
        silently misreports as ``hierarchy_cap_exceeded`` and sends the operator to
        raise an ORG budget, which is the wrong knob and would not lift this denial.
        """
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.details["scope"] == "person"
        # Still no `EntityType` minted for a person (the #4630 pin): an
        # `entity_type="person"` would be authorable through `budget_configs` as a cap
        # nothing enforces — the #4511 class this EPIC exists to remove.
        assert harness.details.get("exceeded_entity_type") in (None, "")


# =============================================================================
# Precedence — the two senses of "tightest", one class each
# =============================================================================


class TestRungPrecedence:
    """ACROSS rungs the more specific rule wins — even when it is more GENEROUS.

    ``_DEFAULT_RUNG_ORDER`` is team → org → platform and the walk stops at the first
    rung that matched. A team default of $5,000 beats a platform default of $1,000 for
    that team's members, deliberately: that is what "unless we say otherwise" means,
    and an override that could only ever tighten would make the org and team rungs
    unable to express the exemption they exist for.

    Tested through the resolver rather than the middleware because the subject is the
    choice among four candidate rules; the denial is pinned above.
    """

    async def test_team_default_beats_org_and_platform(self, session, person_topology):
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_default(session, scope_type="org", scope_id_org=HOME_ORG, amount="20.00")
        await seed_default(session, scope_type="team", scope_id_org=HOME_ORG, scope_id_team=TEAM_ENG, amount="5000.00")
        await set_team(session, CALLER_CANONICAL_ID, TEAM_ENG)

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [(HOME_ORG, TEAM_ENG)])

        assert limits["monthly"].source == "team_default"
        assert limits["monthly"].amount == Decimal("5000.00")

    async def test_org_default_beats_platform(self, session, person_topology):
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_default(session, scope_type="org", scope_id_org=HOME_ORG, amount="900.00")

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [])

        assert limits["monthly"].source == "org_default"
        assert limits["monthly"].amount == Decimal("900.00")

    async def test_platform_default_applies_when_it_is_the_only_rung(self, session, person_topology):
        await seed_default(session, scope_type="platform", amount="10.00")

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [])

        assert limits["monthly"].source == "platform_default"
        assert limits["monthly"].scope_label == "platform default"

    async def test_a_more_generous_specific_rung_really_lifts_the_denial(self, session, redis_client, person_topology):
        """The upward override, end-to-end through enforcement.

        The resolver test above could be satisfied by a resolver that returns the
        right rung while enforcement compares against a different one. Spend of $50
        crosses the $10 platform default and clears the $5,000 team default; a 200
        here is the whole point of a specific rung.
        """
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_default(session, scope_type="team", scope_id_org=HOME_ORG, scope_id_team=TEAM_ENG, amount="5000.00")
        await set_team(session, CALLER_CANONICAL_ID, TEAM_ENG)
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_rungs_are_resolved_per_period_independently(self, session, person_topology):
        """A person can be on the team rung monthly and the platform rung daily.

        The reason the walk is per period rather than per person: resolving one rung
        for the whole person would silently drop the daily ceiling for anybody whose
        team happened to set a monthly one.
        """
        await seed_default(session, scope_type="platform", amount="10.00", period_type="daily")
        await seed_default(session, scope_type="platform", amount="100.00", period_type="monthly")
        await seed_default(session, scope_type="team", scope_id_org=HOME_ORG, scope_id_team=TEAM_ENG, amount="500.00", period_type="monthly")

        limits = await resolve_person_default_limits(session, [HOME_ORG], [(HOME_ORG, TEAM_ENG)])

        assert limits["daily"].source == "platform_default"
        assert limits["monthly"].source == "team_default"


class TestLowestWinsWithinARung:
    """WITHIN one rung the LOWEST amount governs — operator ruling, 2026-09-07.

    The case is real, not hypothetical: a person who belongs to two orgs that both
    carry an org default matches two rules at the org rung. These matches are PEERS —
    neither is more specific than the other — so there is no precedence to appeal to,
    and a default is a ceiling. Taking the highest, or the first row the database
    happened to return, would mean a ceiling could be escaped by joining a second,
    more generous org.

    Note this is the OPPOSITE resolution to ``TestRungPrecedence``, and deliberately
    so. Conflating the two is the most likely misreading of this feature, which is why
    they are separate classes rather than adjacent asserts.
    """

    async def test_lower_of_two_org_defaults_governs_a_multi_org_person(self, session, person_topology):
        await seed_default(session, scope_type="org", scope_id_org=HOME_ORG, amount="900.00")
        await seed_default(session, scope_type="org", scope_id_org=RUN_ORG, amount="50.00")

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [])

        assert limits["monthly"].amount == Decimal("50.00")
        assert limits["monthly"].source == "org_default"
        assert f"org default for {RUN_ORG}" in limits["monthly"].scope_label

    async def test_the_result_does_not_depend_on_the_order_of_the_org_list(self, session, person_topology):
        """Reversing the caller's partition list changes nothing.

        Guards the failure mode this rule exists to prevent: a "first match wins"
        implementation passes the test above roughly half the time, depending on row
        insertion order and the query plan.
        """
        await seed_default(session, scope_type="org", scope_id_org=HOME_ORG, amount="900.00")
        await seed_default(session, scope_type="org", scope_id_org=RUN_ORG, amount="50.00")

        forward = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [])
        reverse = await resolve_person_default_limits(session, [RUN_ORG, HOME_ORG], [])

        assert forward["monthly"].amount == reverse["monthly"].amount == Decimal("50.00")

    async def test_lower_of_two_team_defaults_governs_a_multi_team_person(self, session, person_topology):
        """The same rule one rung down — a person fused across two orgs has two teams."""
        await seed_default(session, scope_type="team", scope_id_org=HOME_ORG, scope_id_team=TEAM_ENG, amount="800.00")
        await seed_default(session, scope_type="team", scope_id_org=RUN_ORG, scope_id_team=TEAM_OPS, amount="30.00")

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [(HOME_ORG, TEAM_ENG), (RUN_ORG, TEAM_OPS)])

        assert limits["monthly"].amount == Decimal("30.00")
        assert limits["monthly"].source == "team_default"

    async def test_equal_amounts_break_the_tie_deterministically(self, session, person_topology):
        """Two peers with the SAME amount always yield the same row, hence the same label.

        Not cosmetic: the ``scope_label`` goes into the 402 an operator reads. Being
        told "org default for acme" on one request and "org default for globex" on the
        next, for one unchanged configuration, makes the message untrustworthy and the
        report unactionable.
        """
        await seed_default(session, scope_type="org", scope_id_org=HOME_ORG, amount="75.00", row_id="pbd-aaa")
        await seed_default(session, scope_type="org", scope_id_org=RUN_ORG, amount="75.00", row_id="pbd-zzz")

        first = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [])
        second = await resolve_person_default_limits(session, [RUN_ORG, HOME_ORG], [])

        assert first["monthly"].scope_label == second["monthly"].scope_label
        assert first["monthly"].scope_label == f"org default for {HOME_ORG}"

    async def test_a_lower_rung_wins_on_specificity_not_on_being_lowest(self, session, person_topology):
        """The two rules meeting: a GENEROUS team rule and a TIGHT org rule.

        The team rung is more specific, so $5,000 governs even though $50 is lower —
        proving "lowest wins" is scoped to one rung and did not leak into the walk. An
        implementation that took the global minimum across all rungs would pass every
        other test in this class and fail here.
        """
        await seed_default(session, scope_type="org", scope_id_org=HOME_ORG, amount="50.00")
        await seed_default(session, scope_type="team", scope_id_org=HOME_ORG, scope_id_team=TEAM_ENG, amount="5000.00")

        limits = await resolve_person_default_limits(session, [HOME_ORG], [(HOME_ORG, TEAM_ENG)])

        assert limits["monthly"].amount == Decimal("5000.00")
        assert limits["monthly"].source == "team_default"


# =============================================================================
# Matching keys — whose rules may govern this person at all
# =============================================================================


class TestTeamRungMatchesPairsNotACrossProduct:
    """The team rung matches ``(org_id, team_id)`` PAIRS.

    ``teams`` carries ``TenantMixin``, so a ``teams.id`` is unique inside its org and
    not globally — two tenants legitimately hold a ``team-eng``. The cross-product
    predicate ``scope_id_org IN (...) AND scope_id_team IN (...)`` matches org A's
    rule for ``team-eng`` against a person who is in org A and in ``team-eng`` **of
    org B**: a rule governing somebody it was never authored for, in a tenant whose
    admin cannot see it. The #4511 wrong-key class in its more damaging direction.
    """

    async def test_a_teams_rule_does_not_govern_a_same_id_team_in_another_org(self, session, person_topology):
        """The cross-product bug, isolated: the person is in HOME_ORG and RUN_ORG's team.

        Only ``(RUN_ORG, TEAM_ENG)`` is a real pair for them. ``HOME_ORG``'s rule for
        its own ``team-eng`` must not match, and a cross-product predicate would match
        it because both halves appear in the two lists.
        """
        await seed_default(session, scope_type="team", scope_id_org=HOME_ORG, scope_id_team=TEAM_ENG, amount="1.00")

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [(RUN_ORG, TEAM_ENG)])

        assert "monthly" not in limits

    async def test_the_matching_pair_does_govern(self, session, person_topology):
        """The other direction, so the predicate is not simply broken.

        Without this, a team predicate that matched nothing at all would pass the test
        above — the classic vacuous-isolation pass.
        """
        await seed_default(session, scope_type="team", scope_id_org=RUN_ORG, scope_id_team=TEAM_ENG, amount="1.00")

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [(RUN_ORG, TEAM_ENG)])

        assert limits["monthly"].source == "team_default"
        assert limits["monthly"].scope_label == f"team default for {TEAM_ENG} in {RUN_ORG}"

    async def test_team_keys_come_from_the_persons_own_users_rows(self, session, person_topology):
        """``resolve_person_team_keys`` projects the fusion, both halves paired."""
        await set_team(session, CALLER_CANONICAL_ID, TEAM_ENG)

        keys = await resolve_person_team_keys(session, [CALLER_CANONICAL_ID])

        assert keys == [(HOME_ORG, TEAM_ENG)]

    async def test_an_empty_team_id_is_dropped_rather_than_matched(self, session, person_topology):
        """``users.team_id`` is written ``""`` by some provisioning paths.

        An empty team id is not a team: matched as a key it would be a shared bogus
        pair every shadow user in the tenant collides on — so a single team default
        authored with an empty id would govern all of them. Same argument
        ``resolve_person_subs`` makes for a NULL ``cognito_sub``.
        """
        keys = await resolve_person_team_keys(session, [CALLER_CANONICAL_ID])

        assert keys == []


class TestOnlyTheirOwnOrgsRulesApply:
    """A default from a tenant the person is not in never governs them.

    The org rung's matching key is the server-derived partition list, which is the
    entire authorization boundary of this read. A rule from ``FOREIGN_ORG`` reaching
    the person means that list came from somewhere other than their memberships.
    """

    async def test_a_foreign_orgs_default_is_not_applied(self, session, person_topology):
        await seed_default(session, scope_type="org", scope_id_org=FOREIGN_ORG, amount="0.01")

        limits = await resolve_person_default_limits(session, [HOME_ORG, RUN_ORG], [])

        assert limits == {}

    async def test_a_foreign_orgs_default_does_not_deny_end_to_end(self, session, redis_client, person_topology):
        """And it does not deny through the middleware either.

        A $0.01 ceiling from a tenant the person has nothing to do with would stop
        every request they make; asserting the 200 is what proves the partition list
        gates the rung and not merely the resolver's return value.
        """
        await seed_default(session, scope_type="org", scope_id_org=FOREIGN_ORG, amount="0.01")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "500.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_a_person_with_no_github_identity_is_still_governed_by_a_default(self, session, redis_client):
        """No linked GitHub account → no individual row possible → the default still applies.

        The #4630 behaviour was to skip the layer entirely for this caller, which was
        right when the only limit was an individually-authored row keyed on a
        ``github:`` anchor they could not have. A default is a rule about the person's
        org and team membership, not about their GitHub link, so skipping the top rung
        must not skip the ladder.
        """
        await seed_org(session, RUN_ORG, "AWS-E")
        await seed_user(session, CALLER_CANONICAL_ID, RUN_ORG, sub=CALLER_SUB)
        await seed_membership(session, CALLER_CANONICAL_ID, RUN_ORG, is_active=True)
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.app_invoked is False

    async def test_a_service_principal_is_never_charged_against_a_default(self, session, redis_client, person_topology):
        """A default is a rule about PEOPLE.

        EventBridge / scheduled / CI principals are not people, have no GitHub anchor
        and no team; charging them against a platform default would deny automation on
        a ceiling nobody set for it — and §7.3's double-count guard already excludes
        them from the individual rung for the same reason.
        """
        await seed_default(session, scope_type="platform", amount="0.01")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "500.00")

        harness = await _drive(
            _service(redis_client),
            session,
            context=agent_context(attributed_user_id="service:eventbridge-scheduler"),
        )

        assert harness.status == 200
        assert harness.app_invoked is True


# =============================================================================
# The top rung's relationship to the rest of the ladder
# =============================================================================


class TestIndividualRowShadowsPerPeriod:
    """A personal row overrides the default for ITS OWN period only.

    The shortcut this class exists to forbid — "the person has a row, so defaults do
    not apply" — is per PERSON, and it passes any test that only ever seeds one
    period. It would let anybody escape a daily platform ceiling by authoring an
    unrelated monthly limit on themselves, which is a self-service privilege
    escalation dressed as a config change.
    """

    async def test_a_personal_row_shadows_the_default_for_that_period(self, session, person_topology):
        await seed_default(session, scope_type="platform", amount="100.00")
        await seed_person_cap(session, "25.00")

        limits = await resolve_applicable_person_limits(
            session,
            person_anchor=PERSON_ANCHOR,
            org_ids=[HOME_ORG, RUN_ORG],
            team_keys=[],
        )

        assert limits["monthly"].amount == Decimal("25.00")
        assert limits["monthly"].is_default is False

    async def test_a_monthly_personal_row_does_not_lift_a_daily_default(self, session, person_topology):
        """The headline of this class.

        A monthly personal limit and a daily platform default coexist: the person is
        governed by their own number monthly and by the platform's daily.
        """
        await seed_default(session, scope_type="platform", amount="5.00", period_type="daily")
        await seed_person_cap(session, "9999.00", period_type="monthly")

        limits = await resolve_applicable_person_limits(
            session,
            person_anchor=PERSON_ANCHOR,
            org_ids=[HOME_ORG, RUN_ORG],
            team_keys=[],
        )

        assert limits["daily"].source == "platform_default"
        assert limits["daily"].amount == Decimal("5.00")
        assert limits["monthly"].amount == Decimal("9999.00")

    async def test_the_unshadowed_daily_default_really_denies(self, session, redis_client, person_topology):
        """End-to-end: a generous monthly personal row does not buy daily headroom."""
        await seed_default(session, scope_type="platform", amount="5.00", period_type="daily")
        await seed_person_cap(session, "9999.00", period_type="monthly")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00", period_type=PeriodType.DAILY)

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.app_invoked is False

    async def test_a_personal_row_above_the_default_is_honoured_by_enforcement(self, session, redis_client, person_topology):
        """Enforcement obeys a stored individual row even when it exceeds the default.

        The ceiling rule is an AUTHORING-time check on the self-service route, not a
        second clamp at enforcement time. A platform admin may deliberately grant an
        individual an allowance above the default (that is the escape hatch the whole
        rule is designed around), and enforcement re-clamping it to the default would
        silently void every such grant.
        """
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_person_cap(session, "1000.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_the_enforcement_path_reports_an_individual_row_as_admin_authored(self, session, person_topology):
        """With no ``self_authored_by``, an individual row is ``admin``, never ``own``.

        Enforcement resolves limits for a person it is not serving a read to, so it
        cannot know whether the row is theirs. Defaulting to ``own`` would eventually
        tell somebody on a read surface that they may lower a number only an admin can
        change.
        """
        await seed_person_cap(session, "25.00")

        limits = await resolve_applicable_person_limits(
            session,
            person_anchor=PERSON_ANCHOR,
            org_ids=[HOME_ORG],
            team_keys=[],
        )

        assert limits["monthly"].source == "admin"

    async def test_a_self_authored_row_is_reported_as_own(self, session, person_topology):
        """The read surface passes the caller's canonical id and gets ``own``.

        The split is what the UI turns "lower this yourself" against "ask a platform
        admin" on, and it is the difference the ceiling rule exists to express.
        """
        await seed_person_cap(session, "25.00")

        limits = await resolve_applicable_person_limits(
            session,
            person_anchor=PERSON_ANCHOR,
            org_ids=[HOME_ORG],
            team_keys=[],
            self_authored_by=CALLER_CANONICAL_ID,
        )

        assert limits["monthly"].source == "own"
        assert limits["monthly"].scope_label == "your own limit"


# =============================================================================
# Hot path — the #4689 lesson, re-pinned for the widened gate
# =============================================================================


class TestZeroCostWhenNoRulesExist:
    """No rows in EITHER table → the person layer costs one cached round trip.

    The #4689 regression was several sequential identity/limit queries on every JWT
    model invoke, which landed on the gateway latency path. #4690 widened the gate to
    a second table, and the requirement stated in the issue is explicit: **ladder
    reads ride the existence gate — zero added hot-path queries when no rows exist.**

    Counted against the real session rather than read off the source, because "one
    round trip" is a property of what executes, and a source-inspection test would
    stay green if a helper started issuing its own query.
    """

    async def test_the_gate_asks_about_both_tables_in_one_round_trip(self, session, person_topology):
        service = BudgetEnforcementService()

        individual, defaults = await service._person_limit_sources_exist(session)

        assert (individual, defaults) == (False, False)

    async def test_an_empty_pair_short_circuits_the_whole_layer(self, session, person_topology):
        """No rules anywhere → ``None``, before any identity work.

        ``None`` is the only honest "unlimited", and since #4690 it describes a
        strictly narrower set of callers than before.
        """
        service = BudgetEnforcementService()

        assert await service._resolve_person_limits(session, agent_context()) is None

    async def test_individual_caps_without_defaults_do_not_pay_the_defaults_fan_out(self, session, person_topology):
        """The reason the gate returns a PAIR rather than a pre-``or``ed bool.

        An install with personal caps and no defaults must behave exactly as it did
        before #4690 — the ladder provably degenerates to its top rung, so resolving
        the person's orgs and teams could only discover an empty set. Asserted as a
        query COUNT so a future "simplification" that collapses the pair shows up as a
        number, not a code-review opinion.
        """
        await seed_person_cap(session, "500.00")
        service = BudgetEnforcementService()
        # Warm the gate first, so the count below measures the ladder and not the
        # one-off existence probe.
        await service._person_limit_sources_exist(session)

        counted: list[str] = []
        original = session.execute

        async def counting_execute(statement, *args, **kwargs):
            counted.append(str(statement))
            return await original(statement, *args, **kwargs)

        session.execute = counting_execute
        try:
            resolved = await service._resolve_person_limits(session, agent_context())
        finally:
            session.execute = original

        assert resolved is not None
        # Non-vacuity first: the interception must have observed the ladder actually
        # running, or "no defaults query" would be true of a broken monkeypatch.
        assert any("person_budget_configs" in statement for statement in counted)
        assert not any("person_budget_defaults" in statement for statement in counted), (
            "the defaults table must not be queried on an install that has none — that is the #4689 regression"
        )

    async def test_a_default_alone_is_enough_to_open_the_gate(self, session, person_topology):
        """One default row and no individual cap still runs the layer.

        A gate that asked only about ``person_budget_configs`` would skip the layer on
        precisely the install a platform admin had just bounded everybody on: the
        default authored, displayed, and governing nobody — the #4511 inert-cap class
        at platform scale.
        """
        await seed_default(session, scope_type="platform", amount="100.00")
        service = BudgetEnforcementService()

        individual, defaults = await service._person_limit_sources_exist(session)

        assert (individual, defaults) == (False, True)
        assert await service._any_person_caps_exist(session) is True


# =============================================================================
# Headers — the displayed number IS the enforced number, one rung further down
# =============================================================================


class TestHeadersReportTheApplicableDefault:
    """``X-Budget-*`` reports the rule that will actually stop the caller.

    Since #4690 the reported limit may be a default the person never authored. That is
    the requirement, not a leak: these headers are read by exactly the population
    defaults exist to bound, and advertising headroom enforcement will not honour is
    the FR-1.4 class of defect — the #4620 ruling's "displayed == enforced" property,
    one rung further down the ladder.
    """

    async def test_headroom_is_computed_against_the_platform_default(self, session, person_topology):
        await seed_default(session, scope_type="platform", amount="100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "30.00")
        service = BudgetEnforcementService()

        headroom = await service._person_cap_headroom(session, agent_context())

        assert headroom is not None
        remaining, limit, _period_end = headroom
        assert limit == Decimal("100.00")
        assert remaining == Decimal("70.00")

    async def test_headroom_follows_the_rung_that_wins(self, session, person_topology):
        """A team default that overrides the platform one is what gets reported.

        Reporting the platform figure here would understate the caller's headroom by
        two orders of magnitude and have them throttling themselves against a ceiling
        that does not apply.
        """
        await seed_default(session, scope_type="platform", amount="10.00")
        await seed_default(session, scope_type="team", scope_id_org=HOME_ORG, scope_id_team=TEAM_ENG, amount="5000.00")
        await set_team(session, CALLER_CANONICAL_ID, TEAM_ENG)
        service = BudgetEnforcementService()

        headroom = await service._person_cap_headroom(session, agent_context())

        assert headroom is not None
        assert headroom[1] == Decimal("5000.00")

    async def test_a_personal_row_is_reported_over_the_default(self, session, person_topology):
        await seed_default(session, scope_type="platform", amount="100.00")
        await seed_person_cap(session, "40.00")
        service = BudgetEnforcementService()

        headroom = await service._person_cap_headroom(session, agent_context())

        assert headroom is not None
        assert headroom[1] == Decimal("40.00")

    async def test_no_rule_anywhere_reports_no_person_headroom(self, session, person_topology):
        """``None``, so no ``X-Budget-*`` header is emitted at all.

        Omission is the honest signal — a fabricated limit here is what #4392 was
        about.
        """
        service = BudgetEnforcementService()

        assert await service._person_cap_headroom(session, agent_context()) is None


# =============================================================================
# Period hygiene, inherited from #4328
# =============================================================================


class TestNonCalendarDefaultsAreFilteredNotFaulted:
    """A ``run``/``chain`` default row is skipped, never faulted on.

    ``get_period_start_end`` RAISES for a non-calendar period, so an unfiltered row
    would take the person layer to its containment wrapper — where the fault is
    swallowed into an allow. The result: authoring one junk row silently disables the
    ladder for every person it matched, including their legitimate monthly ceiling.
    """

    async def test_a_run_scoped_default_is_ignored(self, session, person_topology):
        await seed_default(session, scope_type="platform", amount="1.00", period_type="run")

        limits = await resolve_person_default_limits(session, [HOME_ORG], [])

        assert limits == {}

    async def test_a_junk_row_does_not_suppress_a_real_one(self, session, person_topology):
        """The consequence spelled out: the monthly rule keeps working.

        This is what distinguishes filtering from faulting — a fault-and-swallow
        implementation returns ``{}`` here and passes the test above.
        """
        await seed_default(session, scope_type="platform", amount="1.00", period_type="run")
        await seed_default(session, scope_type="platform", amount="100.00", period_type="monthly")

        limits = await resolve_person_default_limits(session, [HOME_ORG], [])

        assert set(limits) == {"monthly"}
        assert limits["monthly"].amount == Decimal("100.00")


# =============================================================================
# Source-level invariants
# =============================================================================


class TestSourceLevelInvariants:
    """Properties no behavioural test can reach, pinned against the source."""

    def test_defaults_are_partition_free(self):
        """``person_budget_defaults`` carries no ``org_id`` and no ``TenantMixin``.

        The platform rung has no tenant at all, so a partition column would make the
        top of the ladder unrepresentable. ``scope_id_org`` is a scope the row
        DECLARES, which is not a partition it LIVES in.
        """
        names = {column.name for column in PersonBudgetDefault.__table__.columns}
        assert "org_id" not in names
        assert "tenant_id" not in names

    def test_the_rung_order_is_one_greppable_tuple(self):
        """Precedence is data, not the shape of an if/elif chain.

        Adding the ``department`` rung (a non-goal of #4690) must be one edit here plus
        a migration widening the CHECK — not a hunt through branches, where a rung
        placed in the wrong order is a silent precedence inversion.
        """
        from src.budget.person_ledger import _DEFAULT_RUNG_ORDER

        assert _DEFAULT_RUNG_ORDER == ("team", "org", "platform")

    def test_every_rung_has_a_wire_source_name(self):
        """No rung can be resolved that the wire enum cannot name.

        A ``PersonLimit`` whose ``source`` is absent from ``PersonLimitSource`` would
        serialize as an unexpected string to the UI and read as "unknown" — a limit a
        person is held to and cannot be told the origin of.
        """
        from typing import get_args

        from src.budget.person_ledger import _DEFAULT_RUNG_ORDER, _SOURCE_BY_SCOPE_TYPE, PersonLimitSource

        assert set(_SOURCE_BY_SCOPE_TYPE) == set(_DEFAULT_RUNG_ORDER)
        assert set(_SOURCE_BY_SCOPE_TYPE.values()) <= set(get_args(PersonLimitSource))

    def test_is_default_covers_exactly_the_default_rungs(self):
        """The discriminator the ceiling rule turns on.

        A rung missing from ``is_default`` would be a default a person could raise
        their own limit above — the escalation the whole rule exists to prevent — and
        an individual source wrongly included would make an admin's grant unraisable.
        """
        from src.budget.person_ledger import PersonLimit

        def _limit(source):
            return PersonLimit(period_type="monthly", amount=Decimal("1"), enforcement_mode="hard", source=source, scope_label="x")

        assert [_limit(source).is_default for source in ("team_default", "org_default", "platform_default")] == [True, True, True]
        assert [_limit(source).is_default for source in ("own", "admin")] == [False, False]

    def test_the_ladder_lives_in_person_ledger_not_in_a_routes_module(self):
        """The #4689 lesson, restated as a check.

        Enforcement and the read surface must import the SAME resolution, and a
        routes-module underscore-private carries no stability contract for a second
        consumer — which is how the two came to drift last time. ``person_ledger`` is
        a deliberate leaf: it imports models only, so both consumers can import it at
        module level without touching their existing cycle.
        """
        import src.budget.person_ledger as ledger

        assert ledger.resolve_applicable_person_limits.__module__ == "src.budget.person_ledger"
        source = open(ledger.__file__).read()
        assert "me_routes" not in source.split('"""')[2], "the ledger leaf must not import a routes module"

    def test_the_individual_rung_reads_the_key_the_authoring_surface_writes(self):
        """One anchor namespace, or the top rung is inert (#4511).

        A ladder that looked up individual rows under a re-derived key would find
        nothing and silently fall through to the default — a person's own, tighter
        limit replaced by a more generous org ceiling, with no error anywhere.
        """
        assert PersonBudgetConfig.__table__.c.person_anchor is not None
        assert PERSON_ANCHOR.startswith("github:")

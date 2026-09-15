"""Tests for the Bedrock routing resolution ladder.

Issue #4743 (#4692 · R2 · routing foundation), design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §1.2, §2.2,
§3.4, §4.4.

The resolver answers one question — *which AWS account should serve this call* —
and in this release nothing acts on the answer (shadow mode). That makes these
tests the only thing standing between a wrong ladder and a wrong ladder that gets
switched on for real by #4744. Every property below is one that, once enforcement
lands, is the difference between correct billing and charging the wrong customer's
account:

  1. ``TestLadderPrecedence``       — narrowest wins: user > team > org > platform (§1.2)
  2. ``TestTeamRungNeedsBothIds``   — the cross-tenant one: `teams.id` is unique
                                      only inside its org, so the team rung matches
                                      on the PAIR and never on the team alone
  3. ``TestAttributionCannotSteer`` — `attributed_org_id` must not change the answer
                                      (§3.4, the #4132 class as a credential bug)
  4. ``TestUnusableDestinations``   — unverified / non-routing-capable is NO MATCH,
                                      falls through to the next rung (§4.4)
  5. ``TestExistenceGate``          — zero mappings ⇒ ZERO queries per call (§2.2)
  6. ``TestCanonicalUserId``        — a Cognito sub resolves to `users.id` (#4647)
  7. ``TestPlatformRungIsAnAnswer`` — rung 4 is a labelled result, not None

SQLite in-memory with the ORM metadata, matching the rest of the suite. The ladder
is pure query logic, so the substrate is fair: what is under test is which row the
resolver picks, not how any particular engine stores it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.proxy.bedrock_routing import (
    _MAPPING_EXISTENCE_TTL_SECONDS,
    BedrockRoutingResolver,
    BedrockTarget,
)
from src.shared.models.base import Base
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockDestinationRegistry
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext

PLATFORM_ACCOUNT = "999988887777"
USER_ACCOUNT = "111111111111"
TEAM_ACCOUNT = "222222222222"
ORG_ACCOUNT = "333333333333"
OTHER_ACCOUNT = "444444444444"

CANONICAL_USER_ID = "user-canonical-1"
COGNITO_SUB = "cognito-sub-abc"
ORG_ID = "org-acme"
TEAM_ID = "team-eng"


@pytest.fixture
def settings_with_platform_account(monkeypatch):
    """Give the platform rung an account id to label itself with.

    Set through the environment rather than by patching ``get_settings``, so the
    real ``Settings`` parsing (``BG_`` prefix included) is exercised — a flag the
    deployment sets via configmap but which the code reads under a different name
    is the #4511 inert-config class.
    """
    monkeypatch.setenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", PLATFORM_ACCOUNT)
    return PLATFORM_ACCOUNT


@pytest.fixture
async def session_factory():
    """An in-memory database with the routing tables and `users`."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _context(**overrides) -> TokenContext:
    """A JWT-path context: `user_id` is a COGNITO SUB, as it is in production.

    Deliberately not the canonical id. Building the default context out of a
    canonical id would make every user-rung test pass while the real JWT path
    resolved nothing — the inert-mapping failure with no visible symptom.
    """
    values = {
        "user_id": COGNITO_SUB,
        "org_id": ORG_ID,
        "team_id": TEAM_ID,
        "department_id": "dept-1",
        "account_type": "human",
        "is_admin": False,
        "expires_at": datetime.now(UTC) + timedelta(hours=12),
        **overrides,
    }
    return TokenContext(**values)


def _destination(
    dest_id: str,
    account_id: str,
    *,
    routing_capable: bool = True,
    verified: bool = True,
    region: str = "us-east-1",
    owner_org_id: str | None = ORG_ID,
) -> BedrockDestinationRegistry:
    return BedrockDestinationRegistry(
        id=dest_id,
        account_id=account_id,
        role_arn=f"arn:aws:iam::{account_id}:role/adp-bedrock-routing",
        owner_org_id=owner_org_id,
        is_platform_registered=owner_org_id is None,
        routing_capable=routing_capable,
        verified_at=datetime(2026, 9, 1, tzinfo=UTC) if verified else None,
        region=region,
        label=dest_id,
        registered_by_user_id="user-admin",
    )


def _mapping(map_id: str, destination_id: str, **scope) -> BedrockAccountMapping:
    return BedrockAccountMapping(
        id=map_id,
        destination_id=destination_id,
        authored_by_user_id="user-admin",
        **scope,
    )


def _user_scope(user_id: str = CANONICAL_USER_ID) -> dict:
    return {"scope_type": "user", "scope_id_user": user_id}


def _team_scope(org_id: str = ORG_ID, team_id: str = TEAM_ID) -> dict:
    return {"scope_type": "team", "scope_id_org": org_id, "scope_id_team": team_id}


def _org_scope(org_id: str = ORG_ID) -> dict:
    return {"scope_type": "org", "scope_id_org": org_id}


async def _seed(session_factory, *rows):
    async with session_factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()


async def _seed_canonical_user(session_factory, *, user_id: str = CANONICAL_USER_ID, cognito_sub: str | None = COGNITO_SUB, org_id=ORG_ID):
    """The `users` row that maps the JWT's Cognito sub to a canonical id."""
    await _seed(
        session_factory,
        User(id=user_id, org_id=org_id, team_id=TEAM_ID, email=f"{user_id}@example.com", cognito_sub=cognito_sub),
    )


async def _resolve(session_factory, context=None, *, resolver=None, **kwargs) -> BedrockTarget:
    resolver = resolver or BedrockRoutingResolver()
    async with session_factory() as session:
        return await resolver.resolve(session, context or _context(), **kwargs)


# ============================================================================
# 1. Ladder precedence — §1.2
# ============================================================================


class TestLadderPrecedence:
    """First match wins, narrowest first: user > team > org > platform.

    The ordering is the whole feature. If the org rung could beat the user rung, an
    operator granting one contractor their own account would silently keep billing
    the org's — and the contractor's spend would land where the design says it must
    not.
    """

    @pytest.mark.asyncio
    async def test_user_rung_beats_team_and_org(self, session_factory, settings_with_platform_account):
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT),
            _destination("dest-team", TEAM_ACCOUNT),
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope()),
            _mapping("map-team", "dest-team", **_team_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "user"
        assert target.account_id == USER_ACCOUNT

    @pytest.mark.asyncio
    async def test_team_rung_beats_org(self, session_factory, settings_with_platform_account):
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-team", TEAM_ACCOUNT),
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-team", "dest-team", **_team_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "team"
        assert target.account_id == TEAM_ACCOUNT

    @pytest.mark.asyncio
    async def test_org_rung_applies_when_it_is_the_only_match(self, session_factory, settings_with_platform_account):
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "org"
        assert target.account_id == ORG_ACCOUNT

    @pytest.mark.asyncio
    async def test_platform_rung_when_nothing_matches_this_principal(self, session_factory, settings_with_platform_account):
        """Mappings exist, but none for this caller — the fallback must still fire.

        Distinct from the zero-mapping case: here the existence gate passes and the
        query runs, so this is what proves the *query* is scoped to the caller
        rather than matching whatever rows happen to be in the table.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-other", OTHER_ACCOUNT, owner_org_id="org-globex"),
            _mapping("map-other", "dest-other", **_org_scope("org-globex")),
        )
        target = await _resolve(session_factory)
        assert target.rung == "platform"
        assert target.account_id == PLATFORM_ACCOUNT

    @pytest.mark.asyncio
    async def test_resolution_carries_the_destination_id_and_region(self, session_factory, settings_with_platform_account):
        """The rung alone is not debuggable; the row and region come with it.

        An operator reading shadow-mode output needs to know WHICH row to fix.
        "account …1234 via the team rung, mapping map-team" is actionable where a
        bare account id sends them hunting through three scopes — the labelled-source
        discipline #4691 applied to budget limits.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-team", TEAM_ACCOUNT, region="eu-west-1"),
            _mapping("map-team", "dest-team", **_team_scope()),
        )
        target = await _resolve(session_factory)
        assert target.destination_id == "dest-team"
        assert target.region == "eu-west-1"

    @pytest.mark.asyncio
    async def test_result_is_frozen(self, session_factory, settings_with_platform_account):
        """A resolution result cannot be edited after the fact.

        Mutating one would let the logged account and the rung that chose it
        disagree, and the audit trail's only job is that they agree.
        """
        target = await _resolve(session_factory)
        with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
            target.account_id = "000000000000"


# ============================================================================
# 2. The team rung is an (org, team) PAIR — the cross-tenant property
# ============================================================================


class TestTeamRungNeedsBothIds:
    """`teams.id` is unique only inside its org, so the pair is the identity.

    ``Team`` carries ``TenantMixin``, so two tenants can legitimately hold a team
    with the same id. Matching on ``scope_id_team`` alone would serve org A's rule
    to a person in org B — a cross-tenant routing decision, and under enforcement a
    cross-account credential acquisition. The migration's tests pin the *storage*
    side (both orgs may author a rule for "team-eng"); this pins the *matching*
    side.
    """

    @pytest.mark.asyncio
    async def test_another_orgs_rule_for_a_same_named_team_never_matches(self, session_factory, settings_with_platform_account):
        """The load-bearing test of this class.

        Org globex has a rule for its team "team-eng". Our caller is in org acme's
        team "team-eng". Same team id, different tenant — this must NOT resolve to
        globex's account, and with no other rule the answer is the platform rung.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-globex", OTHER_ACCOUNT, owner_org_id="org-globex"),
            _mapping("map-globex", "dest-globex", **_team_scope(org_id="org-globex", team_id=TEAM_ID)),
        )
        target = await _resolve(session_factory)
        assert target.account_id != OTHER_ACCOUNT
        assert target.rung == "platform"

    @pytest.mark.asyncio
    async def test_the_pair_matches_when_both_halves_agree(self, session_factory, settings_with_platform_account):
        """The positive control for the test above — both ids must still match."""
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-team", TEAM_ACCOUNT),
            _mapping("map-team", "dest-team", **_team_scope()),
        )
        assert (await _resolve(session_factory)).account_id == TEAM_ACCOUNT

    @pytest.mark.asyncio
    async def test_a_caller_with_no_team_never_matches_a_team_rule(self, session_factory, settings_with_platform_account):
        """An empty `team_id` must not become a wildcard.

        Service accounts and some agent contexts carry no team. If the predicate
        were built anyway, the empty string would be compared against real team ids
        — and any rule authored for a team literally named "" would match everyone
        in that org.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-team", TEAM_ACCOUNT),
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-team", "dest-team", **_team_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory, _context(team_id=""))
        assert target.rung == "org"
        assert target.account_id == ORG_ACCOUNT

    @pytest.mark.asyncio
    async def test_a_caller_in_another_org_is_never_served_by_this_orgs_rule(self, session_factory, settings_with_platform_account):
        """Tenant isolation at the org rung, the simplest form of the same property."""
        await _seed_canonical_user(session_factory, user_id="user-globex", cognito_sub="sub-globex", org_id="org-globex")
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory, _context(user_id="sub-globex", org_id="org-globex", team_id="team-other"))
        assert target.rung == "platform"


# ============================================================================
# 3. Attribution cannot steer routing — §3.4 (the #4132 class)
# ============================================================================


class TestAttributionCannotSteer:
    """Routing reads `org_id`; `attributed_org_id` must not change the answer.

    The single most important isolation property in the module.
    ``attributed_org_id`` is caller-influenced by design — an internal-plane agent
    may point it at the tenant that triggered a run so *billing* lands there.
    Keying routing off it would let that same header choose **which AWS account
    gets charged**, turning #4132 from an accounting bug into a cross-account
    credential-acquisition one once #4744 makes the answer load-bearing.
    """

    @pytest.mark.asyncio
    async def test_attributed_org_pointing_elsewhere_does_not_change_the_rung(self, session_factory, settings_with_platform_account):
        """The attack shape: authenticated as acme, attribution set to globex.

        Globex has an org rule. Acme has none. The answer must be the platform
        rung — if attribution were read, the caller would have selected globex's
        account with a header.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-globex", OTHER_ACCOUNT, owner_org_id="org-globex"),
            _mapping("map-globex", "dest-globex", **_org_scope("org-globex")),
        )
        target = await _resolve(session_factory, _context(attributed_org_id="org-globex"))
        assert target.account_id != OTHER_ACCOUNT
        assert target.rung == "platform"

    @pytest.mark.asyncio
    async def test_attribution_cannot_suppress_the_authenticated_orgs_rule(self, session_factory, settings_with_platform_account):
        """The mirror image: attribution must not make a caller's own rule miss.

        Reading `attributed_org_id` would break BOTH directions — pointing it at an
        org with no rule would silently drop the caller back to the platform
        account. That is the quieter half of the bug, and just as wrong.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory, _context(attributed_org_id="org-globex"))
        assert target.rung == "org"
        assert target.account_id == ORG_ACCOUNT

    @pytest.mark.asyncio
    async def test_attributed_user_id_cannot_select_a_user_rung_mapping(self, session_factory, settings_with_platform_account):
        """`attributed_user_id` is attribution-owned and must not route either.

        It is a canonical `users.id` — exactly the shape `scope_id_user` holds — so
        it is the field a future refactor is most likely to reach for by mistake. A
        sub-agent could then be routed as the human who triggered it.
        """
        await _seed_canonical_user(session_factory, user_id="user-victim", cognito_sub="sub-victim")
        await _seed_canonical_user(session_factory, user_id="user-agent", cognito_sub="sub-agent")
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope("user-victim")),
        )
        target = await _resolve(session_factory, _context(user_id="sub-agent", attributed_user_id="user-victim"))
        assert target.account_id != USER_ACCOUNT
        assert target.rung == "platform"

    @pytest.mark.asyncio
    async def test_resolution_does_not_mutate_the_context(self, session_factory, settings_with_platform_account):
        """Routing is a strict consumer: it writes none of the auth/attribution fields.

        A resolver that "helpfully" normalised `attributed_org_id` would change what
        the metering path writes for this same request — routing silently editing
        the billing record.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        context = _context(attributed_org_id="org-globex")
        before = context.model_dump()
        await _resolve(session_factory, context)
        assert context.model_dump() == before


# ============================================================================
# 4. Unusable destinations are NO MATCH — §4.4
# ============================================================================


class TestUnusableDestinations:
    """An unverified or non-capable destination is skipped, not failed.

    Under the eventual fail-closed rule this is what stops merely *starting* an
    AWS-connect flow from taking a principal's model access down: a broken
    user-rung destination must fall through to the team rung, never fail the
    request. Both halves of `is_usable_for_routing` are checked here because they
    fail for different reasons — never verified vs. a role that assumes fine but
    cannot invoke Bedrock (§5.0).
    """

    @pytest.mark.asyncio
    async def test_unverified_user_destination_falls_through_to_the_org_rung(self, session_factory, settings_with_platform_account):
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT, verified=False),
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "org"
        assert target.account_id == ORG_ACCOUNT

    @pytest.mark.asyncio
    async def test_non_routing_capable_destination_falls_through(self, session_factory, settings_with_platform_account):
        """The state EVERY destination is in until #4742 ships the capability probe.

        A verified-but-not-capable role is the ReadOnlyAccess connection today's
        connect flow creates: it assumes fine and cannot invoke Bedrock. Matching it
        would be the inert-mapping bug (#4511) with money attached.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-team", TEAM_ACCOUNT, routing_capable=False),
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-team", "dest-team", **_team_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "org"

    @pytest.mark.asyncio
    async def test_all_rungs_unusable_resolves_to_the_platform_rung(self, session_factory, settings_with_platform_account):
        """Never an exception, and never a match it then cannot use.

        The request still has to be served. In shadow mode a raise here would be a
        model call failed by an observation; after #4744 it is a decision the
        enforcement layer owns, not this one.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT, verified=False),
            _destination("dest-org", ORG_ACCOUNT, routing_capable=False),
            _mapping("map-user", "dest-user", **_user_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "platform"
        assert target.account_id == PLATFORM_ACCOUNT

    @pytest.mark.asyncio
    async def test_a_mapping_pointing_at_a_missing_destination_is_not_a_match(self, session_factory, settings_with_platform_account):
        """The dangling reference the deliberate absence of an FK permits.

        `credential_id`/`destination_id` carry no FK on purpose (§8.3): a cascade
        that silently deleted destinations would convert every mapped principal's
        traffic into an outage. The cost of that choice is a mapping that can point
        at nothing, and the resolver's inner join is what makes that a fall-through
        rather than a crash.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-user", "dest-vanished", **_user_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "org"


# ============================================================================
# 5. The existence gate — §2.2, the #4689 lesson
# ============================================================================


class _CountingSession:
    """Wraps a real session and counts `execute` calls.

    A counting wrapper rather than a mock: the queries still run against the real
    database, so the count is of work actually performed and the resolution result
    is still real.
    """

    def __init__(self, inner):
        self._inner = inner
        self.executions = 0

    async def execute(self, *args, **kwargs):
        self.executions += 1
        return await self._inner.execute(*args, **kwargs)


class TestExistenceGate:
    """Zero mappings ⇒ ZERO queries per model call.

    The latency decision that lets this ship default-on. Every model call would
    otherwise pay an indexed lookup to learn "no mapping" — and the install with no
    mappings is *every* install on day one. This mirrors
    ``BudgetEnforcementService._person_limit_sources_exist`` (#4689) down to the
    unlocked process-local cache and the bounded-staleness trade.
    """

    @pytest.mark.asyncio
    async def test_first_call_with_no_mappings_costs_exactly_one_query(self, session_factory, settings_with_platform_account):
        """One `SELECT EXISTS`, and nothing else — no user lookup, no ladder query.

        The canonical-user lookup in particular must sit BEHIND the gate: it is a
        second round trip, and paying it on an install with no mappings would
        double the cost the gate exists to eliminate.
        """
        resolver = BedrockRoutingResolver()
        async with session_factory() as session:
            counting = _CountingSession(session)
            target = await resolver.resolve(counting, _context())
        assert target.rung == "platform"
        assert counting.executions == 1

    @pytest.mark.asyncio
    async def test_subsequent_calls_with_no_mappings_cost_zero_queries(self, session_factory, settings_with_platform_account):
        """The actual claim: the steady state is ZERO queries, not "one cheap one"."""
        resolver = BedrockRoutingResolver()
        async with session_factory() as session:
            await resolver.resolve(_CountingSession(session), _context())
            counting = _CountingSession(session)
            for _ in range(5):
                assert (await resolver.resolve(counting, _context())).rung == "platform"
        assert counting.executions == 0

    @pytest.mark.asyncio
    async def test_a_newly_authored_mapping_is_observed_after_the_ttl(self, session_factory, settings_with_platform_account):
        """Staleness is bounded in the direction that matters.

        A first-ever mapping starts being observed within the TTL rather than
        instantly. Verified by expiring the stamp rather than sleeping 60s — a real
        sleep would put a minute into CI for a clock the test can simply move.
        """
        resolver = BedrockRoutingResolver()
        await _seed_canonical_user(session_factory)
        async with session_factory() as session:
            assert (await resolver.resolve(session, _context())).rung == "platform"

        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )

        # Still cached: the mapping exists but the gate has not re-checked.
        async with session_factory() as session:
            assert (await resolver.resolve(session, _context())).rung == "platform"

        cached_verdict, stamp = resolver._mappings_exist_cache
        assert cached_verdict is False
        resolver._mappings_exist_cache = (cached_verdict, stamp - _MAPPING_EXISTENCE_TTL_SECONDS - 1)

        async with session_factory() as session:
            assert (await resolver.resolve(session, _context())).rung == "org"

    @pytest.mark.asyncio
    async def test_the_gate_does_not_suppress_resolution_once_a_mapping_exists(self, session_factory, settings_with_platform_account):
        """The gate must be a fast path, never a filter.

        A gate that cached "no mappings" from a *different* process's view, or that
        keyed on the wrong thing, would make every mapping in the install inert —
        and in shadow mode the only symptom would be a column that stays NULL,
        which reads as "not captured".
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        resolver = BedrockRoutingResolver()
        async with session_factory() as session:
            for _ in range(3):
                assert (await resolver.resolve(session, _context())).rung == "org"

    @pytest.mark.asyncio
    async def test_the_ladder_is_one_query_not_one_per_rung(self, session_factory, settings_with_platform_account):
        """Three rungs, resolved most-specific-first in Python from ONE read (§2.2).

        Walking the ladder with a query per rung would answer no faster and would
        triple the hot-path cost for exactly the installs that adopted the feature.
        Budget: 1 existence check + 1 canonical-user lookup + 1 ladder query.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT),
            _destination("dest-team", TEAM_ACCOUNT),
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope()),
            _mapping("map-team", "dest-team", **_team_scope()),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        resolver = BedrockRoutingResolver()
        async with session_factory() as session:
            counting = _CountingSession(session)
            target = await resolver.resolve(counting, _context())
        assert target.rung == "user"
        assert counting.executions == 3

    @pytest.mark.asyncio
    async def test_passing_a_known_user_id_skips_the_lookup(self, session_factory, settings_with_platform_account):
        """The `user_id=` shortcut saves the second round trip for callers that know it.

        Not used by the proxy paths today, but it is the seam #4744's enforcement
        path (which resolves the caller anyway) uses to avoid resolving twice.
        """
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope()),
        )
        resolver = BedrockRoutingResolver()
        async with session_factory() as session:
            counting = _CountingSession(session)
            target = await resolver.resolve(counting, _context(), user_id=CANONICAL_USER_ID)
        assert target.rung == "user"
        assert counting.executions == 2


# ============================================================================
# 6. Cognito sub vs canonical users.id — #4647
# ============================================================================


class TestCanonicalUserId:
    """`scope_id_user` is a canonical `users.id`; the token carries a Cognito sub.

    Comparing the two directly would make every user-rung mapping silently never
    fire — the #4511 inert-config class with no visible symptom at all: no error,
    no log, just a rung that never applies while the UI shows the mapping as
    active.
    """

    @pytest.mark.asyncio
    async def test_a_cognito_sub_resolves_through_the_users_table(self, session_factory, settings_with_platform_account):
        """The production JWT path: the token's `user_id` is not the mapping's id."""
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "user"
        assert target.account_id == USER_ACCOUNT

    @pytest.mark.asyncio
    async def test_a_canonical_id_in_the_token_also_resolves(self, session_factory, settings_with_platform_account):
        """IAM / service-account callers already carry a canonical id.

        Keying only on `cognito_sub` would resolve those callers to nothing — which
        is why the lookup is `or_(cognito_sub == x, id == x)`, the same two-step
        `AccessControlService` uses.
        """
        await _seed_canonical_user(session_factory, cognito_sub=None)
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope()),
        )
        target = await _resolve(session_factory, _context(user_id=CANONICAL_USER_ID))
        assert target.rung == "user"

    @pytest.mark.asyncio
    async def test_a_mapping_written_with_a_cognito_sub_never_matches(self, session_factory, settings_with_platform_account):
        """Guards the id-namespace contract from the authoring side.

        A mapping stored with a Cognito sub in `scope_id_user` routes nobody. This
        test is what makes that a documented, tested consequence rather than a
        surprise for whoever writes the authoring API in #4745.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-user", USER_ACCOUNT),
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-user", "dest-user", **_user_scope(COGNITO_SUB)),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.rung == "org"

    @pytest.mark.asyncio
    async def test_a_principal_with_no_canonical_row_skips_the_user_rung(self, session_factory, settings_with_platform_account):
        """A correct miss, not a wrong match.

        Legitimate for service accounts (§7.2), which have no `users` row. The user
        rung is skipped and the walk continues, so such a caller still gets its
        org's rule.
        """
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory, _context(user_id="sub-with-no-row", account_type="service"))
        assert target.rung == "org"

    @pytest.mark.asyncio
    async def test_a_caller_with_no_identity_at_all_resolves_to_the_platform_rung(self, session_factory, settings_with_platform_account):
        """No canonical user, no org ⇒ no predicates ⇒ platform, not an open query.

        The dangerous alternative is an unfiltered `WHERE` that matches EVERY
        mapping in the table and returns some other tenant's account. Building no
        predicate and answering "platform" is the only safe response.
        """
        await _seed(
            session_factory,
            _destination("dest-other", OTHER_ACCOUNT, owner_org_id="org-globex"),
            _mapping("map-other", "dest-other", **_org_scope("org-globex")),
        )
        target = await _resolve(session_factory, _context(user_id="", org_id="", team_id=""))
        assert target.rung == "platform"
        assert target.account_id == PLATFORM_ACCOUNT


# ============================================================================
# 7. The platform rung is an answer, not an absence
# ============================================================================


class TestPlatformRungIsAnAnswer:
    """Rung 4 is returned as a labelled `BedrockTarget`, never as None.

    "This call went to the platform account, and that was the correct answer" is a
    different fact from "we did not look", and the audit trail has to distinguish
    them — a NULL column cannot.
    """

    @pytest.mark.asyncio
    async def test_platform_target_is_labelled(self, session_factory, settings_with_platform_account):
        target = await _resolve(session_factory)
        assert isinstance(target, BedrockTarget)
        assert target.is_platform
        assert target.rung == "platform"
        assert target.destination_id is None

    @pytest.mark.asyncio
    async def test_an_unset_platform_account_is_none_never_a_placeholder(self, session_factory, monkeypatch):
        """Null-discipline: absent config means NULL, never a fabricated id.

        The repo is explicit about this for `client_tool` and the cache-token
        counters. A made-up account id in an audit column is worse than an absent
        one, because it reads as evidence — an operator would conclude the call was
        checked and served by that account.
        """
        monkeypatch.delenv("BG_PLATFORM_BEDROCK_ACCOUNT_ID", raising=False)
        target = await _resolve(session_factory)
        assert target.rung == "platform"
        assert target.account_id is None

    @pytest.mark.asyncio
    async def test_a_resolved_rung_is_not_flagged_as_platform(self, session_factory, settings_with_platform_account):
        """`is_platform` must track the rung, not merely "did we get an account"."""
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-org", ORG_ACCOUNT),
            _mapping("map-org", "dest-org", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert not target.is_platform

    @pytest.mark.asyncio
    async def test_the_platform_account_is_never_confused_with_a_mapped_one(self, session_factory, settings_with_platform_account):
        """A mapped destination that happens to hold the platform account id.

        The rung, not the account id, is what says whether routing applied. An
        operator triaging a mapping that (deliberately or by mistake) points back at
        the platform account needs to see "org rung" so they know a rule fired.
        """
        await _seed_canonical_user(session_factory)
        await _seed(
            session_factory,
            _destination("dest-loop", PLATFORM_ACCOUNT),
            _mapping("map-org", "dest-loop", **_org_scope()),
        )
        target = await _resolve(session_factory)
        assert target.account_id == PLATFORM_ACCOUNT
        assert target.rung == "org"
        assert not target.is_platform

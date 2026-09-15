"""The person-level cap DENIES on a cross-org settled denominator — Issue #4630 (#4620 · C4).

C3 (#4629) gave a person one place to store a ceiling on their total agent spend.
Nothing read it. This suite pins the layer that makes it real: a cap authored
against ``person_budget_configs`` now stops the person's agents in **every** org
their runs execute in, on a **settled-ledger** denominator, per design note
``docs/design-notes/4620-cross-org-person-budgets.md`` §5.3 + §5.5.

**Why the headline test is a two-org sum.** Every budget row on this platform is
keyed ``org_id``-first, so the failure this unit fixes is arithmetic: a person under
a cap in each of two orgs is under no ceiling on the sum (#4620). ``TestTwoOrgDenominator``
seeds spend in both partitions such that *neither alone* crosses the cap and the sum
does — an implementation that reads one partition returns a plausible 200 and fails
only here.

**Per the #4068 gate, the load-bearing tests assert the DENIAL** — a 402 that never
reaches the downstream app (``app_invoked is False``) — so they cannot be satisfied
by deleting the enforcement branch. The companion in the other direction is
``TestPerOrgCapsStillIndependent``, which asserts requests are still ALLOWED and
still denied by *org* caps with the person layer present.

**Two silent-failure traps get a test each, not a comment**, because both return a
believable response when broken (a missing ledger row is indistinguishable from "no
spend"):

* **§3.3 identity fusion.** ``users`` carries ``TenantMixin``, so one GitHub account
  legitimately holds a different ``users.id`` per tenant. Summing by canonical id
  alone under-reports for exactly the multi-org population this ships for. See
  ``TestSplitIdentityFusion``.
* **§5.5 the overshoot bound.** The layer takes NO Redis reservation — a person key
  cannot carry the ``{org_id}`` hash tag the atomic Lua needs, and the #4620 ruling
  forbids a second non-atomic call. ``TestOvershootBoundIsDocumentedNotFixed`` pins
  both halves: concurrent requests all clear the same settled total, *and* no person
  key appears among the reservation targets. A future person reservation key must
  therefore arrive with a deliberate test change rather than silently.

Harness: real in-memory SQLite with real ``PersonBudgetConfig``/``BudgetUsage``/
``User``/``TenantMembership``/``UserIdentity`` rows (the ``test_cross_org_person_budget.py``
shape), driven through the real pure-ASGI middleware (the ``test_org_settled_cap.py``
shape). Config overrides are a REAL ``BudgetConfig`` via ``object.__setattr__``,
never a ``MagicMock`` — a fully-patched config asserts a guarantee it never
exercised (the #4046 trap).
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.budget.config import BudgetConfig as BudgetFeatureConfig
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore
from src.shared.identity.providers import IdentityProvider
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage, PersonBudgetConfig
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

OPUS = "anthropic.claude-3-opus-20240229-v1:0"

RESERVATION_TTL = 120

# The design note's §2 walkthrough, so a failure here reads against the reported
# scenario: home tenant `pranavsharma1000`, agent runs execute in `aws-e`.
HOME_ORG = "pranavsharma1000"
RUN_ORG = "aws-e"

# A tenant the person is NOT a member of. Seeded with their own entity id in the
# isolation test, so a partition list that came from anywhere other than the
# person's server-derived memberships reads it and fails loudly.
FOREIGN_ORG = "org-not-a-member"

CALLER_SUB = "sub-caller-4630"
CALLER_CANONICAL_ID = "11111111-1111-4111-8111-111111111111"
CALLER_GITHUB_ID = "8675309"
PERSON_ANCHOR = f"github:{CALLER_GITHUB_ID}"

# A colleague in the SAME partition with their own root_user rows. The denominator
# must be the person's, not the partition's.
COLLEAGUE_CANONICAL_ID = "99999999-9999-4999-8999-999999999999"

# Small enough that the estimated cost of a tiny body never dominates the
# arithmetic a test is asserting.
_SMALL_BODY = b'{"messages":[{"role":"user","content":"hi"}]}'


# =============================================================================
# Harness
# =============================================================================


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
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _config(**overrides) -> BudgetFeatureConfig:
    """A REAL BudgetConfig with only the named fields overridden (#4046).

    Reservations are OFF by default here: the person layer deliberately takes no
    reservation, so most tests are about the settled-ledger arithmetic and a live
    Redis denominator would only add a second reason for a request to be denied.
    ``TestOvershootBoundIsDocumentedNotFixed`` turns them on precisely because its
    subject is which keys do and do not get reserved.
    """
    config = BudgetFeatureConfig()
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return config


class _Harness:
    """Drives the pure-ASGI budget middleware and records what happened."""

    def __init__(self, service: BudgetEnforcementService):
        self.app_invoked = False
        self.messages: list[dict] = []
        self.middleware = BudgetEnforcementMiddleware(self._inner_app, enforcement_service=service)

    async def _inner_app(self, scope, receive, send):
        self.app_invoked = True
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"message":"success"}'})

    async def post(self, *, token_context, body=_SMALL_BODY, request_id="req-1"):
        scope = {
            "type": "http",
            "path": f"/model/{OPUS}/invoke",
            "method": "POST",
            "headers": [(b"content-length", str(len(body)).encode())],
            "state": {"token_context": token_context, "request_id": request_id},
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            self.messages.append(message)

        await self.middleware(scope, receive, send)

    @property
    def status(self) -> int:
        return next(m["status"] for m in self.messages if m["type"] == "http.response.start")

    @property
    def body(self) -> dict:
        raw = b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")
        return json.loads(raw)

    @property
    def details(self) -> dict:
        return self.body["details"]


def _service(redis_client) -> BudgetEnforcementService:
    return BudgetEnforcementService(
        reservations=ReservationStore(
            redis_url=None,
            ttl_seconds=RESERVATION_TTL,
            clock=lambda: 1_000.0,
            client=redis_client,
        )
    )


async def _drive(service, session, *, context, config=None, request_id="req-1", body=_SMALL_BODY) -> _Harness:
    """Run one request through the middleware against the real SQLite session."""
    harness = _Harness(service)
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", config or _config(budget_reservation_enabled=False)):
            await harness.post(token_context=context, body=body, request_id=request_id)
    return harness


# =============================================================================
# Seeding
# =============================================================================


async def seed_org(session: AsyncSession, org_id: str, name: str | None = None) -> None:
    session.add(Organization(id=org_id, name=name or org_id))
    await session.commit()


async def seed_user(session: AsyncSession, user_id: str, org_id: str, sub: str | None = None) -> None:
    session.add(User(id=user_id, cognito_sub=sub, email=f"{user_id}@example.com", org_id=org_id, team_id=""))
    await session.commit()


async def seed_membership(session: AsyncSession, user_id: str, tenant_id: str, is_active: bool = False) -> None:
    session.add(TenantMembership(user_id=user_id, tenant_id=tenant_id, is_active=is_active))
    await session.commit()


async def seed_github_identity(session: AsyncSession, user_id: str, org_id: str, github_id: str) -> None:
    session.add(
        UserIdentity(
            user_id=user_id,
            org_id=org_id,
            team_id="",
            provider=IdentityProvider.github.value,
            provider_user_id=github_id,
            verification_method="oauth",
        )
    )
    await session.commit()


async def seed_root_usage(
    session: AsyncSession,
    org_id: str,
    entity_id: str,
    amount: str,
    period_type: PeriodType = PeriodType.MONTHLY,
    entity_type: EntityType = EntityType.ROOT_USER,
) -> None:
    from src.budget.utils import get_period_start_end

    session.add(
        BudgetUsage(
            org_id=org_id,
            entity_type=entity_type.value,
            entity_id=entity_id,
            period_type=period_type.value,
            period_start=get_period_start_end(period_type)[0],
            total_cost_usd=Decimal(amount),
            total_tokens=1000,
            request_count=1,
        )
    )
    await session.commit()


async def seed_org_cap(
    session: AsyncSession,
    org_id: str,
    entity_id: str,
    amount: str,
    entity_type: EntityType = EntityType.ROOT_USER,
    period_type: PeriodType = PeriodType.MONTHLY,
) -> None:
    """A per-org `budget_configs` cap — the Layer 1 row this unit must not disturb."""
    session.add(
        BudgetConfig(
            org_id=org_id,
            entity_type=entity_type.value,
            entity_id=entity_id,
            period_type=period_type.value,
            budget_amount_usd=Decimal(amount),
            enforcement_mode="hard",
        )
    )
    await session.commit()


async def seed_person_cap(
    session: AsyncSession,
    amount: str,
    *,
    anchor: str = PERSON_ANCHOR,
    period_type: str = "monthly",
    enforcement_mode: str = "hard",
) -> None:
    """The partition-free C3 row this unit teaches enforcement to read."""
    session.add(
        PersonBudgetConfig(
            person_anchor=anchor,
            period_type=period_type,
            budget_amount_usd=Decimal(amount),
            enforcement_mode=enforcement_mode,
            authored_by_user_id=CALLER_CANONICAL_ID,
        )
    )
    await session.commit()


def agent_context(*, attributed_org_id: str = RUN_ORG, attributed_user_id: str = CALLER_CANONICAL_ID, **overrides) -> TokenContext:
    """An agent run: IAM-authenticated, attributed to the person who set it in motion.

    ``attributed_user_id`` is what #4300 publishes off the verified run binding and
    is the input the person layer resolves its anchor from. Set directly here rather
    than driven through a run binding, because this suite's subject is the person
    denominator, not the binding — ``test_root_human_envelope.py`` owns that.
    """
    defaults = {
        "user_id": "arn:aws:sts::1234:assumed-role/agent-worker",
        "org_id": attributed_org_id,
        "team_id": "",
        "department_id": "",
        "account_type": "service",
        "is_admin": False,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "auth_source": "iam",
        "attributed_org_id": attributed_org_id,
        "attributed_user_id": attributed_user_id,
    }
    return TokenContext(**{**defaults, **overrides})


@pytest.fixture
async def person_topology(session) -> None:
    """The §2 scenario: one ``users`` row, GitHub identity, membership in both tenants.

    Spend accrues in ``RUN_ORG`` (where the agents run) and in ``HOME_ORG``. No cap
    of any kind is seeded — each test authors the one it is about.
    """
    await seed_org(session, HOME_ORG, "Pranav Sharma")
    await seed_org(session, RUN_ORG, "AWS-E")
    await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
    await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
    await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)
    await seed_membership(session, CALLER_CANONICAL_ID, RUN_ORG)


# =============================================================================
# GATE — the headline case. Must fail on pre-#4630 code.
# =============================================================================


class TestTwoOrgDenominator:
    """The cap counts the person's spend in EVERY org, not the active partition's.

    This is the issue in one arithmetic statement, and the reason the fix is not a
    config change: with $60 in one tenant and $60 in another against a $100 cap,
    every per-org view and every per-org cap is satisfied, and the person has spent
    $120 against a ceiling of $100.
    """

    async def test_spend_across_two_orgs_sums_into_one_denominator(self, session, redis_client, person_topology):
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "60.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "60.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.app_invoked is False, "the request must be stopped BEFORE the model call, not merely reported"
        # The FIGURE, not just the denial: a single-partition read that happens to
        # deny (e.g. against a cap below $60) would otherwise pass this test.
        assert harness.details["spent_usd"] == 120.0
        assert harness.details["budget_usd"] == 100.0

    async def test_neither_partition_alone_would_deny(self, session, redis_client, person_topology):
        """The control: $60 against a $100 cap is allowed, so the denial above IS the sum.

        Without this, `test_spend_across_two_orgs_sums_into_one_denominator` is
        satisfiable by an implementation that denies too eagerly for some unrelated
        reason.
        """
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "60.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_denial_holds_from_either_partition_the_agent_runs_in(self, session, redis_client, person_topology):
        """The cap denies "anywhere", so the active partition must not change the verdict.

        Same seeded ledger, driven once attributed to each tenant. A person cap that
        only fired in the partition holding the larger figure would be a per-org cap
        wearing a person's name.
        """
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "60.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "60.00")

        for partition in (HOME_ORG, RUN_ORG):
            harness = await _drive(
                _service(redis_client),
                session,
                context=agent_context(attributed_org_id=partition),
            )
            assert harness.status == 402, f"the person cap must deny in {partition}"
            assert harness.details["spent_usd"] == 120.0


# =============================================================================
# The 402 contract
# =============================================================================


class TestDenialNamesThePersonScope:
    """The denial says which knob to turn — and it is not an org budget.

    The #4596-review class: the worker classifies the stop by matching `scope` in
    the 402 body, and `agent-worker.ts` has a CLOSED regex. A person denial that
    arrives without its own scope is reported as `hierarchy_cap_exceeded`, which
    tells the operator to raise an ORG budget — a knob that cannot raise this
    ceiling and belongs to somebody else.
    """

    async def test_402_details_carry_scope_person(self, session, redis_client, person_topology):
        await seed_person_cap(session, "10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.app_invoked is False
        assert harness.details["scope"] == "person"
        assert harness.details["scope_cap_usd"] == 10.0
        assert harness.details["enforcement_mode"] == "hard"

    async def test_message_names_the_anchor_and_period(self, session, redis_client, person_topology):
        """`scope="person"` alone does not say WHICH person or which period.

        An operator reading the log or the response needs the anchor to find the
        row, so it is in the human-readable message rather than only in a metric.
        """
        await seed_person_cap(session, "10.00", period_type="monthly")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert PERSON_ANCHOR in harness.body["message"]
        assert "monthly" in harness.body["message"]

    async def test_no_entity_type_is_minted_for_a_person(self, session, redis_client, person_topology):
        """`entity_type` stays null — deliberately, not as an oversight (#4511).

        A person is not a budget `EntityType`. Minting one would make
        `entity_type="person"` authorable through `budget_configs`, i.e. a cap a
        person could set in the Budget Management screen that no code enforces —
        the inert-cap class this EPIC exists to remove.
        """
        await seed_person_cap(session, "10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.details["entity_type"] is None
        assert harness.details["entity_id"] is None

        assert not hasattr(EntityType, "PERSON"), "a person EntityType would make an inert `budget_configs` cap authorable (#4511)"


# =============================================================================
# Layer 1 is untouched — in both directions
# =============================================================================


class TestPerOrgCapsStillIndependent:
    """Each org's own cap keeps firing, first and on its own denominator (§5.3).

    The residual risk of a denying person layer is that it becomes the only layer
    that matters. These tests are the counterweight: the org's ceiling still stops
    spend with no person row present at all, and still stops it when the person has
    headroom to spare.
    """

    async def test_org_cap_denies_with_no_person_row(self, session, redis_client, person_topology):
        """Byte-identical to pre-#4630: no person cap authored, org cap denies as `root_user`."""
        await seed_org_cap(session, RUN_ORG, CALLER_CANONICAL_ID, "10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.details["scope"] == "root_user", "the per-org cap must keep its own scope, not be relabelled"
        assert harness.details["entity_type"] == EntityType.ROOT_USER.value

    async def test_org_cap_denies_even_when_the_person_has_headroom(self, session, redis_client, person_topology):
        """A generous personal ceiling does not raise an org's cap.

        The inverse of the authority-inversion hazard: the person layer must not
        become a way to spend past a limit the paying tenant set.
        """
        await seed_person_cap(session, "100000.00")
        await seed_org_cap(session, RUN_ORG, CALLER_CANONICAL_ID, "10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.details["scope"] == "root_user", "the ORG cap must win the attribution: it was hit first"

    async def test_no_caps_at_all_is_allowed(self, session, redis_client, person_topology):
        """The overwhelmingly common path stays open, with spend on the ledger."""
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "5000.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True


# =============================================================================
# §5.5 — the overshoot bound, and the reservation that is deliberately absent
# =============================================================================


class TestOvershootBoundIsDocumentedNotFixed:
    """The person cap is bounded, not atomic — and that is the shipped contract.

    ``ReservationTarget.key()`` embeds ``{org_id}`` as a Redis Cluster hash tag so
    the multi-key atomic Lua stays single-slot; a partition-spanning person key
    cannot carry one, and the #4620 ruling forbids adding a second, non-atomic
    reservation call. So the layer reads Postgres only and the overshoot is real.

    Both halves are asserted together on purpose: pinning the concurrency behaviour
    without pinning the absent key would let somebody "fix" the overshoot by adding
    the very second reservation call the ruling rejected, and the suite would stay
    green.
    """

    async def test_concurrent_requests_all_clear_the_same_settled_total(self, session, redis_client, person_topology):
        """Three requests under the cap individually all pass, because settlement lags.

        This is the documented bound, asserted as behaviour rather than trusted to a
        comment: the settled ledger does not move during these requests (the tracker
        Lambda writes it minutes later), so each one sees $90 of $100 and is allowed.
        """
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "90.00")

        service = _service(redis_client)
        for index in range(3):
            harness = await _drive(service, session, context=agent_context(), request_id=f"req-{index}")
            assert harness.status == 200, "the settled denominator cannot see in-flight spend — this is the documented bound"

    async def test_no_person_reservation_key_is_ever_taken(self, session, redis_client, person_topology):
        """No reservation target names the person; the org's `root_user` cap still gets one.

        The positive half matters as much as the negative: it shows reservations were
        genuinely ON for this request, so the absence of a person key is a property
        of the person layer and not of a disabled feature.
        """
        await seed_person_cap(session, "100.00")
        await seed_org_cap(session, RUN_ORG, CALLER_CANONICAL_ID, "100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "1.00")

        service = _service(redis_client)
        reserved: list = []
        real_reserve = ReservationStore.reserve

        async def capturing_reserve(store_self, request_id, cost, targets):
            reserved.extend(targets)
            return await real_reserve(store_self, request_id, cost, targets)

        with patch.object(ReservationStore, "reserve", capturing_reserve):
            harness = await _drive(
                service,
                session,
                context=agent_context(),
                config=_config(budget_reservation_enabled=True),
            )

        assert harness.status == 200
        assert reserved, "reservations must be ON here, or the assertion below proves nothing"
        assert any(t.entity_type == EntityType.ROOT_USER.value for t in reserved), "the per-org cap keeps its live denominator (#4287)"
        for target in reserved:
            assert PERSON_ANCHOR not in target.key(), f"the person layer must take no reservation key (§5.5): {target.key()}"
            assert "person" not in target.entity_type


# =============================================================================
# §3.3 — identity fusion, and the isolation boundary
# =============================================================================


class TestSplitIdentityFusion:
    """Two ``users.id`` under one GitHub account sum into ONE denominator (§3.3).

    ``users`` carries ``TenantMixin`` and ``user_identities`` is unique per
    ``(provider, provider_user_id, org_id)``, so a person independently onboarded
    into two orgs legitimately holds two canonical ids — and therefore two
    ``root_user`` ledger keys. Summing by the caller's own id alone under-reports
    for precisely the population this feature exists for, and does so invisibly.
    """

    SECOND_CANONICAL_ID = "22222222-2222-4222-8222-222222222222"

    @pytest.fixture
    async def split_identity(self, session) -> None:
        await seed_org(session, HOME_ORG, "Home")
        await seed_org(session, RUN_ORG, "AWS-E")

        # The row their session resolves to, in their home tenant.
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
        await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)

        # The SAME human, onboarded separately into the run tenant: different
        # `users.id`, same GitHub numeric id. This is the row a canonical-id-only
        # sum misses.
        await seed_user(session, self.SECOND_CANONICAL_ID, RUN_ORG)
        await seed_github_identity(session, self.SECOND_CANONICAL_ID, RUN_ORG, CALLER_GITHUB_ID)
        await seed_membership(session, self.SECOND_CANONICAL_ID, RUN_ORG)

    async def test_both_canonical_ids_count_toward_the_person_cap(self, session, redis_client, split_identity):
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "60.00")
        await seed_root_usage(session, RUN_ORG, self.SECOND_CANONICAL_ID, "60.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.details["spent_usd"] == 120.0, "the second users.id's spend must be in the denominator (§3.3)"

    async def test_the_other_ids_spend_alone_can_deny(self, session, redis_client, split_identity):
        """A person whose spend is ENTIRELY under their other canonical id is still capped.

        The sharpest form of the fusion requirement: the caller's own id has no
        ledger row at all here, so an implementation that reads only
        ``attributed_user_id`` sees $0 and allows.
        """
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, RUN_ORG, self.SECOND_CANONICAL_ID, "150.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.details["spent_usd"] == 150.0


class TestDenominatorIsThePersonsOwnSpend:
    """The sum is scoped to this person — not the partition, not other entity types."""

    async def test_a_colleagues_spend_in_the_same_partition_does_not_count(self, session, redis_client, person_topology):
        """Another member's `root_user` rows are somebody else's dollars.

        A widened `org_id` predicate that forgot to keep the `entity_id` filter
        would read the whole tenant's agent spend into one person's ceiling.
        """
        await seed_person_cap(session, "100.00")
        await seed_user(session, COLLEAGUE_CANONICAL_ID, RUN_ORG)
        await seed_root_usage(session, RUN_ORG, COLLEAGUE_CANONICAL_ID, "5000.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "10.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200, "a colleague's spend must not exhaust this person's cap"

    async def test_partitions_the_person_is_not_a_member_of_are_not_read(self, session, redis_client, person_topology):
        """The partition set is server-derived from memberships (§7.3).

        `FOREIGN_ORG` holds a row under the person's OWN entity id, so this fails
        loudly if the denominator ever came from an unbounded scan rather than the
        membership list. Reading it would also be a cross-tenant disclosure, which
        is why `TenantMixin` applying no query filter makes the hand-written
        predicate the only guard there is.
        """
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, FOREIGN_ORG, CALLER_CANONICAL_ID, "5000.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "10.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200

    async def test_coarser_grain_and_mis_keyed_rows_are_not_summed(self, session, redis_client, person_topology):
        """Two rows that look like the person's and are not (the #4322 family).

        Since #4396 the denominator is ``root_user`` + ``user``, so this test is about
        the rows that still must NOT enter it:

        * the **organization** total describes the same dollars at a coarser grain —
          the person's own spend re-aggregated, plus every colleague's;
        * a ``user`` row keyed by the person's **canonical id** is not the direct
          ledger, which is keyed by Cognito ``sub``. Reading the `user` half by
          canonical id would both miss the real rows and pick up whatever else
          happens to sit under that id.

        Both are seeded far above the cap, so summing either denies and fails here.
        ``test_fused_person_envelope.py`` owns the positive half: the ``user`` row
        keyed by the person's real sub DOES count.
        """
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "5000.00", entity_type=EntityType.USER)
        await seed_root_usage(session, RUN_ORG, RUN_ORG, "9000.00", entity_type=EntityType.ORGANIZATION)

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200, "only the person's two person-grain ledgers belong in the denominator (§7.3)"

    async def test_shadow_user_partition_is_included(self, session, redis_client, session_scoped_shadow_user=None):
        """A partition known only from `users.org_id` still counts (§7.3).

        `POST /resolve-user` auto-provisions users with `users.org_id` set and NO
        `tenant_memberships` row, so a membership-only partition list silently omits
        a tenant where real spend accrued — under-reporting exactly like the bug
        being fixed.
        """
        await seed_org(session, HOME_ORG, "Home")
        await seed_org(session, RUN_ORG, "AWS-E")
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
        # Deliberately NO membership row anywhere: HOME_ORG is reachable only
        # through the `users.org_id` fallback.
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "150.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.details["spent_usd"] == 150.0


# =============================================================================
# Who the layer does NOT apply to
# =============================================================================


class TestPrincipalsWithNoPersonCeiling:
    """Three callers the person layer must skip, each for a distinct reason."""

    async def test_service_rooted_principal_is_skipped(self, session, redis_client, person_topology):
        """A service principal is not a person (§7.3's double-count guard).

        An EventBridge/scheduled/CI-rooted run carries a `service:`-prefixed key in
        `attributed_user_id`, has no GitHub anchor, and must not be charged against
        anybody's personal ceiling. The cap here is seeded tiny and the ledger large,
        so an implementation that tried to resolve this principal as a person would
        deny and fail.
        """
        await seed_person_cap(session, "0.01")
        await seed_root_usage(session, RUN_ORG, "service:eventbridge:nightly", "500.00")

        harness = await _drive(
            _service(redis_client),
            session,
            context=agent_context(attributed_user_id="service:eventbridge:nightly"),
        )

        assert harness.status == 200

    async def test_unattributed_service_account_request_is_skipped(self, session, redis_client, person_topology):
        """A service account with no attribution belongs to nobody's personal ceiling.

        Since #4396 an *unattributed* request is no longer skipped on that ground
        alone — a direct human caller now falls back to their token identity and IS
        enforced (``test_fused_person_envelope.py``). This one is skipped for a
        different and narrower reason: ``account_type="service"`` has no ``users``
        row, so there is no person to charge. The cap is seeded tiny and the ledger
        large, so a fallback that resolved a service account to *some* person would
        deny and fail here.
        """
        await seed_person_cap(session, "0.01")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "500.00")

        harness = await _drive(
            _service(redis_client),
            session,
            context=agent_context(attributed_user_id=""),
        )

        assert harness.status == 200

    async def test_native_individual_budget_is_enforced(self, session, redis_client):
        """Native users' individual budgets deny requests using the same person ledger."""
        await seed_org(session, RUN_ORG, "AWS-E")
        await seed_user(session, CALLER_CANONICAL_ID, RUN_ORG, sub=CALLER_SUB)
        await seed_membership(session, CALLER_CANONICAL_ID, RUN_ORG, is_active=True)
        await seed_person_cap(session, "0.01", anchor=f"users:{CALLER_CANONICAL_ID}")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "500.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402


# =============================================================================
# Modes and periods
# =============================================================================


class TestSoftModeWarnsAndNeverDenies:
    """A `soft` row is informational — the C3-era mode, preserved on purpose.

    C3's shipped UI told those users in as many words that "requests are not
    blocked". Converting the row to a denial on deploy would break that promise
    silently and at once, for everybody who had already typed a number. Re-saving
    the limit writes `hard` (see `test_person_cap_routes.py`).
    """

    async def test_soft_cap_over_limit_is_allowed(self, session, redis_client, person_topology):
        await seed_person_cap(session, "10.00", enforcement_mode="soft")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "500.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_hard_cap_with_the_same_figures_denies(self, session, redis_client, person_topology):
        """The control for the test above: only the mode differs."""
        await seed_person_cap(session, "10.00", enforcement_mode="hard")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "500.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402


class TestPeriods:
    """Each authored period is evaluated against its own window."""

    async def test_a_daily_cap_denies_on_daily_spend(self, session, redis_client, person_topology):
        await seed_person_cap(session, "10.00", period_type="daily")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00", period_type=PeriodType.DAILY)

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert "daily" in harness.body["message"]

    async def test_a_monthly_cap_does_not_read_the_daily_ledger(self, session, redis_client, person_topology):
        """Periods are not interchangeable: a monthly cap must not be tripped by a daily row.

        `budget_usage` is keyed on `period_type` AND `period_start`, so reading the
        wrong one is a silent mismatch — the row simply is not found, or the wrong
        one is.
        """
        await seed_person_cap(session, "100.00", period_type="monthly")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "150.00", period_type=PeriodType.DAILY)

        harness = await _drive(_service(redis_client), session, context=agent_context())

        # The daily row is real spend but belongs to a different period key; the
        # monthly denominator is $0.
        assert harness.status == 200

    async def test_a_non_calendar_row_is_filtered_not_faulted(self, session, redis_client, person_topology):
        """A stray lifetime-scoped row must not fault the layer.

        `get_period_start_end` raises for run/chain periods. C3's API cannot author
        one (a `Literal` rejects it at the HTTP boundary), so this is defence
        against a hand-written row rather than a reachable API path — but the failure
        mode if unfiltered is the layer raising on every request for that person.
        """
        await seed_person_cap(session, "10.00", period_type="run")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "500.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200


# =============================================================================
# Fault containment — the layer may only ever break ITSELF
# =============================================================================


class TestFaultsAreContainedToThePersonLayer:
    """A fault in the person layer must not void the run, chain and org verdicts.

    ``check_budget_hierarchy``'s shared handler (``_handle_check_failure``) fails
    **open** for code-level faults, deliberately: a deterministic bug must not be
    able to permanently down all inference. Composed with a new layer inside that
    same ``try``, though, it becomes a way for one faulty read to switch off budget
    enforcement platform-wide while requests keep returning 200 — the concrete
    trigger being an environment where C3's migration has not landed, where the cap
    read raises on every single request.
    """

    async def test_a_person_layer_fault_leaves_the_org_cap_enforcing(self, session, redis_client, person_topology):
        await seed_org_cap(session, RUN_ORG, CALLER_CANONICAL_ID, "10.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        service = _service(redis_client)
        with patch.object(
            BudgetEnforcementService,
            "_check_person_budget",
            side_effect=RuntimeError("person_budget_configs does not exist"),
        ):
            harness = await _drive(service, session, context=agent_context())

        assert harness.status == 402, "the ORG cap must still deny when the person layer faults"
        assert harness.details["scope"] == "root_user"
        assert harness.app_invoked is False

    async def test_a_person_layer_fault_does_not_deny_an_in_budget_request(self, session, redis_client, person_topology):
        """Containment is not fail-closed either: the person cap goes advisory, that is all."""
        service = _service(redis_client)
        with patch.object(
            BudgetEnforcementService,
            "_check_person_budget",
            side_effect=RuntimeError("person_budget_configs does not exist"),
        ):
            harness = await _drive(service, session, context=agent_context())

        assert harness.status == 200

    async def test_the_fault_is_alarmed_with_its_own_outcome(self, session, redis_client, person_topology):
        """Reported as `person_layer_skipped`, not as one of the fail-open outcomes.

        Nothing was allowed *because* of this fault, so reusing
        `allowed_fail_open` would overstate the blast radius and hide the one thing
        that actually stopped working.
        """
        service = _service(redis_client)
        with patch("src.budget.enforcement_service.emit_budget_check_failure") as emit:
            with patch.object(
                BudgetEnforcementService,
                "_check_person_budget",
                side_effect=RuntimeError("boom"),
            ):
                await _drive(service, session, context=agent_context())

        assert emit.call_args_list, "a skipped enforcement layer must alarm"
        assert any(call.kwargs.get("outcome") == "person_layer_skipped" for call in emit.call_args_list)


# =============================================================================
# §8.4 — rollback stays "stop reading the table"
# =============================================================================


class TestRollbackShape:
    """Nothing on the write path changed, so there is nothing to roll back but a read."""

    async def test_no_person_row_means_no_extra_ledger_reads(self, session, redis_client, person_topology):
        """The common path skips the fan-out entirely (cap first, denominator second).

        A hot-path cost check as much as a correctness one: with no cap authored,
        the person layer must not fan out over partitions or read a single spend row.
        """
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "50.00")

        from src.budget import me_routes

        with patch.object(me_routes, "_resolve_member_partitions", side_effect=AssertionError("must not fan out with no cap row")):
            harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200

    async def test_the_layer_writes_nothing(self, session, redis_client, person_topology):
        """Person-level spend stays DERIVED, never accumulated (§4.1 / §8.4).

        A second accumulator would be a denormalised duplicate of the same dollars —
        the #4322 double-count family — and would give the rollback something to
        undo.
        """
        await seed_person_cap(session, "100.00")
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "60.00")

        await _drive(_service(redis_client), session, context=agent_context())

        rows = (await session.execute(select(PersonBudgetConfig))).scalars().all()
        assert len(rows) == 1
        assert rows[0].budget_amount_usd == Decimal("100.00"), "the cap row must not be mutated by a read"

        usage = (await session.execute(select(BudgetUsage).where(BudgetUsage.org_id == RUN_ORG))).scalars().all()
        assert len(usage) == 1, "the person layer must accumulate nothing of its own"


# =============================================================================
# Static guards — properties a future edit must not quietly drop
# =============================================================================


class TestSourceLevelInvariants:
    """Two properties that a test exercising behaviour alone cannot pin."""

    def test_the_person_layer_takes_no_reservation_target(self):
        """`_check_person_budget` returns an EnforcementResult, never a target (§5.5).

        Signature-level, because the runtime assertion in
        `TestOvershootBoundIsDocumentedNotFixed` can only observe the keys a given
        request happened to produce.
        """
        import inspect

        signature = inspect.signature(BudgetEnforcementService._check_person_budget)
        annotation = str(signature.return_annotation)
        assert "ReservationTarget" not in annotation, "a person reservation key needs the #4620 ruling revisited first"
        assert "EnforcementResult" in annotation

    def test_the_overshoot_bound_is_documented(self):
        """The bound is stated in the operator-facing doc, not only in code comments.

        The issue's requirement is explicitly that the 402 docs state the bound
        rather than implying atomicity, so the doc is part of the deliverable and
        this asserts it exists.
        """
        from pathlib import Path

        doc = Path(__file__).resolve().parents[2] / "docs" / "budget-ratelimit.md"
        text = doc.read_text()

        assert '"scope": "person"' in text, "the 402 contract must document the person scope"
        assert "Overshoot bound" in text
        assert "settled" in text.lower()

    def test_enforcement_reads_the_key_c3_writes(self):
        """The anchor format is shared, not restated (#4511 / the inert-cap class).

        The enforcement layer resolving a differently-shaped anchor than the
        authoring API stores is the defect that makes a cap silently govern nothing,
        so both sides go through `format_person_anchor`'s `github:` namespace.
        """
        from src.shared.identity.person_anchor import format_person_anchor

        assert format_person_anchor(CALLER_GITHUB_ID) == PERSON_ANCHOR


class TestReviewPinsOn4689:
    """Pins for the #4689 review fixes."""

    def test_direct_lookup_never_uses_the_fault_swallowing_resolver(self):
        """The direct-caller person key must come from a STRICT lookup.

        `resolve_canonical_user_id` swallows SQLAlchemyError into a raw-sub
        fallback; routed through it, a transient DB error became a silent
        fail-open the PersonBudgetLayerSkipped pager never saw. A fault must
        propagate to the containment wrapper.
        """
        import inspect

        # #4690 moved the preamble into `_resolve_person_limits`, so inspecting
        # `_check_person_budget` alone would make this pin vacuously pass while the
        # lookup it guards lives elsewhere. Both are checked, and any future split
        # must extend this list rather than let the pin go quiet.
        source = inspect.getsource(BudgetEnforcementService._check_person_budget) + inspect.getsource(BudgetEnforcementService._resolve_person_limits)
        # Call sites, not mentions: the fix's own comment names the resolver.
        assert "resolve_canonical_user_id(" not in source
        assert "_resolve_root_principal(" not in source

    @pytest.mark.asyncio
    async def test_existence_gate_answers_from_cache_within_the_ttl(self):
        """One query per TTL window, not per request."""
        from unittest.mock import AsyncMock, MagicMock

        service = BudgetEnforcementService(db_session=MagicMock())
        session = MagicMock()
        empty = MagicMock()
        # Since #4690 the gate asks both existence questions as two EXISTS
        # subqueries in ONE statement, so the result is a single row of two bools.
        empty.one.return_value = (False, False)
        session.execute = AsyncMock(return_value=empty)

        assert await service._any_person_caps_exist(session) is False
        assert await service._any_person_caps_exist(session) is False
        assert session.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_existence_gate_expires_and_rechecks(self):
        from unittest.mock import AsyncMock, MagicMock

        import src.budget.enforcement_service as module

        service = BudgetEnforcementService(db_session=MagicMock())
        session = MagicMock()
        row = MagicMock()
        row.one.return_value = (True, False)
        session.execute = AsyncMock(return_value=row)

        assert await service._any_person_caps_exist(session) is True
        # Age the cache past the TTL and confirm a fresh read happens.
        cached_value, cached_at = service._person_caps_exist_cache
        service._person_caps_exist_cache = (cached_value, cached_at - module._PERSON_CAPS_EXISTENCE_TTL_SECONDS - 1)
        assert await service._any_person_caps_exist(session) is True
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_widened_gate_stays_one_round_trip_and_keeps_the_rungs_apart(self):
        """#4690's widening must not add a hot-path query, nor pre-``or`` the answers.

        Two failure modes this pins at once. A second `SELECT` for the defaults
        table would put a round trip back on every JWT model invoke — the #4689
        regression. Collapsing the pair into one bool before caching would make
        every install with a personal cap pay the defaults ladder's org/team
        fan-out, which is work only a default's existence justifies.
        """
        from unittest.mock import AsyncMock, MagicMock

        service = BudgetEnforcementService(db_session=MagicMock())
        session = MagicMock()
        result = MagicMock()
        result.one.return_value = (False, True)
        session.execute = AsyncMock(return_value=result)

        assert await service._person_limit_sources_exist(session) == (False, True)
        # A default alone opens the gate: this is the whole point of #4690.
        assert await service._any_person_caps_exist(session) is True
        assert session.execute.await_count == 1

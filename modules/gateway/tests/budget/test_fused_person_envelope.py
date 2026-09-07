"""The person envelope is FUSED: direct + cloud, displayed == enforced — Issue #4396.

The operator ruling of 2026-09-05: *a person's limit governs their TOTAL spend —
their own direct use plus the cloud agents they trigger, across all their GitHub
orgs — and the person sees ONE number tracked against it.*

Before this unit the platform had two half-answers that each looked complete:

* ``/me/budget``'s ``person_envelope`` summed only ``root_user`` rows, so a person
  who mostly works interactively saw a cross-org total far below what they had
  actually spent — and the figure carried a note saying it covered "your agents",
  which is not what a reader of a personal spending page assumes.
* ``_check_person_budget`` returned ``None`` for any request with no
  ``attributed_user_id``, i.e. **every** interactive request. A person under a
  personal limit could sit at 100% of it and keep spending from their own machine
  indefinitely, with the cap reporting itself satisfied.

**Why the two are one suite.** The ruling's requirement is not "widen the read and
widen the check" — it is that the number displayed IS the number enforced. Two
independently-correct sums are a bug in waiting the moment one of them is edited, so
``TestDisplayedIsEnforced`` asserts the two surfaces agree on the same seeded ledger
rather than each matching a hand-written constant.

**Why no double-count, asserted rather than argued.** The two ledgers summed are
disjoint by namespace: direct spend settles under ``(user, cognito_sub)``, agent
spend under ``(root_user, canonical users.id)``, in a table uniquely keyed including
``entity_type``. The write paths cannot produce both for one dollar — the tracker's
``!= user_id`` gate (#4391) suppresses the ``root_user`` row on a direct request, a
hosted run's ``user`` row is keyed by the shared *worker* identity rather than the
person's sub, and the #4591 IAM-only attribution guard covers the header-replay case.
``TestEachDollarCountsOnce`` seeds each of those shapes and asserts the total, so the
argument is pinned by behaviour and not only by a comment.

Harness: the ``test_cross_org_person_budget.py`` in-memory-SQLite seeding shape for
the read half and the ``test_person_cap_enforcement.py`` real-ASGI-middleware shape
for the enforcement half, deliberately in one file so both run against one seeding
vocabulary. Config overrides are a REAL ``BudgetConfig`` via ``object.__setattr__``,
never a ``MagicMock`` (the #4046 trap).
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.auth.dependencies import get_current_user
from src.budget.config import BudgetConfig as BudgetFeatureConfig
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.me_routes import router as me_budget_router
from src.budget.reservations import ReservationStore
from src.shared.database import get_db
from src.shared.identity.providers import IdentityProvider
from src.shared.models.base import Base
from src.shared.models.budget import BudgetUsage, PersonBudgetConfig
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

OPUS = "anthropic.claude-3-opus-20240229-v1:0"

# The design note's §2 topology, so a failure reads against the reported scenario.
HOME_ORG = "pranavsharma1000"
RUN_ORG = "aws-e"

CALLER_SUB = "sub-caller-4396"
CALLER_CANONICAL_ID = "11111111-1111-4111-8111-111111111111"
CALLER_GITHUB_ID = "4396001"
PERSON_ANCHOR = f"github:{CALLER_GITHUB_ID}"

# The shared agent-worker principal. A hosted run's DIRECT (`user`) ledger row is
# keyed by this — never by the triggering person's sub — which is the structural
# reason fusing the two ledgers cannot count one agent dollar twice.
WORKER_SUB = "arn:aws:sts::1234:assumed-role/agent-worker"

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
            ttl_seconds=120,
            clock=lambda: 1_000.0,
            client=redis_client,
        )
    )


async def _drive(service, session, *, context, config=None, request_id="req-1") -> _Harness:
    """Run one request through the real middleware against the real SQLite session."""
    harness = _Harness(service)
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", config or _config(budget_reservation_enabled=False)):
            await harness.post(token_context=context, request_id=request_id)
    return harness


def build_app(session: AsyncSession, context: TokenContext) -> FastAPI:
    app = FastAPI()
    app.include_router(me_budget_router)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def read_envelope(session: AsyncSession, context: TokenContext) -> dict:
    app = build_app(session, context)
    async with client_for(app) as client:
        response = await client.get("/me/budget")
    assert response.status_code == 200
    return response.json()


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


async def seed_usage(
    session: AsyncSession,
    org_id: str,
    entity_type: EntityType,
    entity_id: str,
    amount: str,
    period_type: PeriodType = PeriodType.MONTHLY,
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


async def seed_cloud_spend(session: AsyncSession, org_id: str, amount: str, entity_id: str = CALLER_CANONICAL_ID) -> None:
    """A dollar the person's AGENTS spent: ``root_user``, keyed by canonical id."""
    await seed_usage(session, org_id, EntityType.ROOT_USER, entity_id, amount)


async def seed_direct_spend(session: AsyncSession, org_id: str, amount: str, entity_id: str = CALLER_SUB) -> None:
    """A dollar the person spent THEMSELVES: ``user``, keyed by Cognito sub."""
    await seed_usage(session, org_id, EntityType.USER, entity_id, amount)


async def seed_person_cap(session: AsyncSession, amount: str, *, anchor: str = PERSON_ANCHOR, period_type: str = "monthly") -> None:
    session.add(
        PersonBudgetConfig(
            person_anchor=anchor,
            period_type=period_type,
            budget_amount_usd=Decimal(amount),
            enforcement_mode="hard",
            authored_by_user_id=CALLER_CANONICAL_ID,
        )
    )
    await session.commit()


def direct_context(**overrides) -> TokenContext:
    """A human at their own keyboard: JWT, no run binding, so NO ``attributed_user_id``.

    This is the caller the person layer skipped entirely before #4396, and the whole
    reason the cap governed nothing for interactive use.
    """
    defaults = {
        "user_id": CALLER_SUB,
        "org_id": HOME_ORG,
        "team_id": "",
        "department_id": "",
        "account_type": "human",
        "is_admin": False,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "auth_source": "cognito",
    }
    return TokenContext(**{**defaults, **overrides})


def agent_context(*, attributed_org_id: str = RUN_ORG, attributed_user_id: str = CALLER_CANONICAL_ID, **overrides) -> TokenContext:
    """A hosted run: IAM-authenticated worker, attributed to the person who triggered it."""
    defaults = {
        "user_id": WORKER_SUB,
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
    """One ``users`` row, a GitHub identity, membership in both tenants. No caps."""
    await seed_org(session, HOME_ORG, "Pranav Sharma")
    await seed_org(session, RUN_ORG, "AWS-E")
    await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
    await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
    await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)
    await seed_membership(session, CALLER_CANONICAL_ID, RUN_ORG)


# =============================================================================
# GATE — must fail on pre-#4396 code, in both directions
# =============================================================================


class TestDirectSpendIsInsideThePersonLimit:
    """A person's own interactive spend counts against their personal ceiling.

    The reported gap in one sentence: the limit said "your total spend" and governed
    only the agent half, so the person most likely to be surprised by their bill —
    the one who mostly uses the platform directly — was the one it did not cover.
    """

    async def test_a_direct_only_person_is_denied_by_their_own_limit(self, session, redis_client, person_topology):
        """No agent spend at ALL, and the cap still fires. Pre-#4396 this was a 200.

        Two things fail on the old code: the interactive request never reached the
        person layer, and the denominator would have been ``$0`` even if it had.
        """
        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, HOME_ORG, "150.00")

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 402
        assert harness.app_invoked is False, "the request must be stopped BEFORE the model call, not merely reported"
        assert harness.details["scope"] == "person", "the person scope is how the worker classifies the stop (#4596)"
        assert harness.details["spent_usd"] == 150.0
        assert harness.details["budget_usd"] == 100.0

    async def test_direct_and_cloud_sum_into_one_denominator(self, session, redis_client, person_topology):
        """Neither half alone crosses the cap; the person's TOTAL does.

        This is the ruling as arithmetic, and the reason it is not a copy change:
        $60 of interactive use and $60 of agent runs against a $100 limit satisfies
        every figure the platform showed before, while the person has spent $120.
        """
        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, HOME_ORG, "60.00")
        await seed_cloud_spend(session, RUN_ORG, "60.00")

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 402
        assert harness.details["spent_usd"] == 120.0

    async def test_neither_half_alone_would_deny(self, session, redis_client, person_topology):
        """The control: $60 against a $100 cap is allowed, so the denial above IS the sum."""
        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, HOME_ORG, "60.00")

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_a_hosted_run_is_denied_by_the_persons_direct_spend(self, session, redis_client, person_topology):
        """The widening is symmetric: interactive spend can stop the person's AGENTS.

        Enforcing the fused total on the interactive path only would leave the two
        surfaces disagreeing about the same ceiling depending on who asked.
        """
        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, HOME_ORG, "150.00")

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 402
        assert harness.details["spent_usd"] == 150.0


class TestTheEnvelopeShowsTheTotal:
    """``/me/budget``'s one number is direct + cloud, and says so."""

    async def test_the_envelope_sums_both_halves_across_partitions(self, session, person_topology):
        await seed_direct_spend(session, HOME_ORG, "10.500000")
        await seed_direct_spend(session, RUN_ORG, "0.250000")
        await seed_cloud_spend(session, RUN_ORG, "264.600000")

        body = await read_envelope(session, direct_context())
        envelope = body["person_envelope"]

        assert Decimal(envelope["spend_usd"]) == Decimal("275.350000")
        assert Decimal(envelope["direct_spend_usd"]) == Decimal("10.750000")
        assert Decimal(envelope["cloud_spend_usd"]) == Decimal("264.600000")

    async def test_the_components_are_reported_per_partition(self, session, person_topology):
        """A person seeing one total still needs to know which workspace it came from."""
        await seed_direct_spend(session, RUN_ORG, "1.500000")
        await seed_cloud_spend(session, RUN_ORG, "2.500000")

        body = await read_envelope(session, direct_context())
        line = next(line for line in body["per_org"] if line["org_id"] == RUN_ORG)

        assert Decimal(line["direct_spend_usd"]) == Decimal("1.500000")
        assert Decimal(line["cloud_spend_usd"]) == Decimal("2.500000")

    async def test_the_note_names_both_halves(self, session, person_topology):
        """The copy is part of the contract: a total described as "your agents" misleads.

        The tile in the dashboard renders this string, and the ruling's one UI change
        is exactly this widening of what the figure claims to cover.
        """
        body = await read_envelope(session, direct_context())
        note = body["person_envelope"]["note"].lower()

        assert "direct" in note, "the note must say the figure includes the person's own use"
        assert "limit" in note, "the note must say the figure is what their limit is enforced against"

    async def test_the_envelope_still_carries_no_bindable_denominator(self, session, person_topology):
        """The cap has ONE home, and it is not this object (#4322).

        The ceiling is real now, but it is served by ``GET /me/budget/person-cap``.
        Restating it here would create two surfaces that can disagree about the same
        limit — so the new component fields must not have smuggled a cap in with them.
        """
        body = await read_envelope(session, direct_context())
        envelope = body["person_envelope"]

        assert envelope["is_budget"] is False
        for forbidden in ("cap_usd", "remaining_usd", "headroom_usd", "utilization_pct", "band", "enforcement_mode"):
            assert forbidden not in envelope, f"{forbidden} belongs to the person-cap endpoint, not the envelope"


class TestDisplayedIsEnforced:
    """The number the person reads IS the number they are denied against.

    Asserted as agreement between the two surfaces on one seeded ledger rather than
    each matching a constant: a constant keeps passing when one side is edited, which
    is precisely the drift the ruling exists to prevent.
    """

    async def test_the_402_figure_equals_the_envelope_figure(self, session, redis_client, person_topology):
        await seed_person_cap(session, "50.00")
        await seed_direct_spend(session, HOME_ORG, "33.330000")
        await seed_direct_spend(session, RUN_ORG, "0.070000")
        await seed_cloud_spend(session, RUN_ORG, "40.600000")

        body = await read_envelope(session, direct_context())
        displayed = Decimal(body["person_envelope"]["spend_usd"])

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 402
        assert Decimal(str(harness.details["spent_usd"])) == displayed, "the displayed total and the enforced denominator have drifted apart"

    async def test_both_surfaces_agree_when_the_person_is_under_the_cap(self, session, redis_client, person_topology):
        """The same agreement in the allow direction, where no denial body is emitted.

        Without this, the assertion above is satisfiable by a layer that only agrees
        on the values large enough to deny.
        """
        await seed_person_cap(session, "1000.00")
        await seed_direct_spend(session, HOME_ORG, "12.340000")
        await seed_cloud_spend(session, RUN_ORG, "1.660000")

        body = await read_envelope(session, direct_context())
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("14.000000")

        harness = await _drive(_service(redis_client), session, context=direct_context())
        assert harness.status == 200


# =============================================================================
# No double-count — one dollar, one row, on every write path
# =============================================================================


class TestEachDollarCountsOnce:
    """The two ledgers are disjoint, so summing them adds no dollar twice.

    Each test here is one write path from the ruling's no-double-count discipline
    (#4322), seeded as the rows that path actually produces.
    """

    async def test_a_hosted_runs_worker_row_is_not_the_persons_direct_spend(self, session, redis_client, person_topology):
        """The agent's own ``user`` row belongs to the WORKER identity, not the person.

        This is the structural reason the fusion is safe: a hosted run debits
        ``(root_user, canonical id)`` for the person and ``(user, worker sub)`` for
        the shared worker principal. Reading the direct half by the person's subs
        cannot pick the second one up. Seeded far above the cap, so an implementation
        that keyed the direct half off ``context.user_id`` denies and fails here.
        """
        await seed_person_cap(session, "100.00")
        await seed_cloud_spend(session, RUN_ORG, "10.00")
        await seed_direct_spend(session, RUN_ORG, "5000.00", entity_id=WORKER_SUB)

        harness = await _drive(_service(redis_client), session, context=agent_context())

        assert harness.status == 200, "the shared worker's direct row must not land in a person's envelope"

    async def test_a_direct_request_is_charged_once_not_twice(self, session, redis_client, person_topology):
        """One interactive dollar appears in the total exactly once.

        A direct request writes only the ``user`` row: the tracker's ``!= user_id``
        gate (#4391) suppresses the ``root_user`` row for a self-rooted request, and
        the ``_get_entity_hierarchy`` equality skip does the same on the enforcement
        side. So the fused sum is $60, not $120 — which is why #4396 needed no write
        change and no offsetting skip.
        """
        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, HOME_ORG, "60.00")

        body = await read_envelope(session, direct_context())
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("60.000000")

        harness = await _drive(_service(redis_client), session, context=direct_context())
        assert harness.status == 200, "the person's own dollar was counted in both halves"

    async def test_a_replayed_run_id_does_not_debit_the_person_twice(self, session, redis_client, person_topology):
        """A JWT human carrying an agent run header is still one person's one dollar.

        ``verify_row_matches_caller`` deliberately never compares caller identity, so
        the #4591 guard — attribution is published for ``auth_source == "iam"`` only —
        is what stops a replayed ``X-Agent-RunId`` being charged as both the caller
        and the run's root human. #4396 widened the READ over rows that already
        exist and left that guard exactly as narrow, which this pins: the same
        canonical id arriving as attribution on a *cognito* request adds nothing.
        """
        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, HOME_ORG, "60.00")

        harness = await _drive(
            _service(redis_client),
            session,
            context=direct_context(attributed_user_id=CALLER_CANONICAL_ID),
        )

        assert harness.status == 200
        assert harness.app_invoked is True

    async def test_coarser_grain_rows_never_enter_the_total(self, session, person_topology):
        """``organization`` rows are the same dollars re-aggregated (plus colleagues').

        The fusion widened the KEY set, not the predicate: exactly two entity types
        are person-grain, and a third would multiply the total.
        """
        await seed_direct_spend(session, RUN_ORG, "1.000000")
        await seed_cloud_spend(session, RUN_ORG, "2.000000")
        await seed_usage(session, RUN_ORG, EntityType.ORGANIZATION, RUN_ORG, "9000.000000")

        body = await read_envelope(session, direct_context())

        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("3.000000")
        assert "9000" not in json.dumps(body), "an org-grain row entered the person total"

    async def test_a_colleagues_direct_spend_is_not_read(self, session, redis_client, person_topology):
        """The widened key set is still only THIS person's keys.

        The direct half is keyed by sub, and a shared partition holds every
        colleague's. Dropping the ``entity_id`` filter while widening would charge one
        person for the whole tenant's interactive spend.
        """
        colleague_id = "99999999-9999-4999-8999-999999999999"
        await seed_person_cap(session, "100.00")
        await seed_user(session, colleague_id, RUN_ORG, sub="sub-colleague-4396")
        await seed_membership(session, colleague_id, RUN_ORG, is_active=True)
        await seed_direct_spend(session, RUN_ORG, "5000.00", entity_id="sub-colleague-4396")
        await seed_direct_spend(session, HOME_ORG, "10.00")

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 200, "a colleague's direct spend must not exhaust this person's cap"


# =============================================================================
# §3.3 — the fusion is REUSED, not re-derived
# =============================================================================


class TestSplitIdentityFusionCoversBothLedgers:
    """One GitHub account, two tenants, two ``users`` rows — and two SUBS to match.

    ``users`` carries ``TenantMixin``, so the §3.3 case that made the cloud half need
    an identity fusion applies verbatim to the direct half: the person's second
    tenant row can carry its own ``cognito_sub``, and a direct read keyed only off
    the caller's own token silently omits it. The subs are therefore a *projection*
    of the same fusion rather than a second derivation of "the same person" — a third
    independent derivation is the #4511 inert-key class.
    """

    SECOND_CANONICAL_ID = "22222222-2222-4222-8222-222222222222"
    SECOND_SUB = "sub-caller-4396-second"

    @pytest.fixture
    async def split_identity(self, session) -> None:
        await seed_org(session, HOME_ORG, "Home")
        await seed_org(session, RUN_ORG, "AWS-E")

        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
        await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)

        # The SAME human, onboarded separately into the run tenant: different
        # `users.id`, different `cognito_sub`, same GitHub numeric id.
        await seed_user(session, self.SECOND_CANONICAL_ID, RUN_ORG, sub=self.SECOND_SUB)
        await seed_github_identity(session, self.SECOND_CANONICAL_ID, RUN_ORG, CALLER_GITHUB_ID)
        await seed_membership(session, self.SECOND_CANONICAL_ID, RUN_ORG)

    async def test_direct_spend_under_the_other_sub_still_counts(self, session, redis_client, split_identity):
        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, RUN_ORG, "150.00", entity_id=self.SECOND_SUB)

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 402, "the person's other tenant identity's direct spend must be in the denominator (§3.3)"
        assert harness.details["spent_usd"] == 150.0

    async def test_a_user_row_with_no_sub_is_skipped_not_keyed_on_empty(self, session, redis_client, split_identity):
        """Shadow users have ``cognito_sub = NULL``; that is not a ledger key.

        ``POST /resolve-user`` provisions rows with no sub. Keying the direct read on
        ``""`` would read a bogus shared line that every such user in the tenant
        collides on — attributing strangers' spend to each other. Seeded far above the
        cap so a denial proves the empty key was read.

        The shadow row is provisioned in a THIRD tenant: ``user_identities`` is unique
        per ``(provider, provider_user_id, org_id)`` since migration 021, so one
        person's rows are one-per-partition by construction.
        """
        third_org = "shadow-tenant-4396"
        third_id = "33333333-3333-4333-8333-333333333333"
        await seed_person_cap(session, "100.00")
        await seed_org(session, third_org)
        await seed_user(session, third_id, third_org, sub=None)
        await seed_github_identity(session, third_id, third_org, CALLER_GITHUB_ID)
        await seed_membership(session, third_id, third_org)
        await seed_direct_spend(session, third_org, "5000.00", entity_id="")

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 200, "a NULL cognito_sub was keyed as the empty string"


# =============================================================================
# Who the widened layer still does NOT apply to
# =============================================================================


class TestPrincipalsTheWideningDoesNotReach:
    """Admitting direct callers must not admit non-people, and must not guess."""

    async def test_a_service_account_caller_has_no_personal_ceiling(self, session, redis_client, person_topology):
        """Unattended CI is nobody's personal spend.

        ``account_type="service"`` has no ``users`` row by design, so there is no
        person to charge. This is the blast-radius row the widening most had to avoid:
        resolving a service account to *some* person would put automated spend inside
        a human's limit and deny them for it.
        """
        await seed_person_cap(session, "0.01")
        await seed_direct_spend(session, HOME_ORG, "500.00")

        harness = await _drive(
            _service(redis_client),
            session,
            context=direct_context(user_id="svc-account-1", account_type="service"),
        )

        assert harness.status == 200

    async def test_an_unprovisioned_identity_skips_the_layer(self, session, redis_client, person_topology):
        """No ``users`` row → the resolver hands back the raw sub → skip, do not enforce.

        ``resolve_canonical_user_id`` degrades to its input when nothing matches, and
        a sub matches no ``root_user`` row. Enforcing against that would apply a cap
        to a denominator that is structurally incomplete — an unenforceable ceiling
        reporting itself satisfied, the #4511 class one layer over.
        """
        await seed_person_cap(session, "0.01")
        await seed_direct_spend(session, HOME_ORG, "500.00", entity_id="sub-unknown-4396")

        harness = await _drive(
            _service(redis_client),
            session,
            context=direct_context(user_id="sub-unknown-4396"),
        )

        assert harness.status == 200

    async def test_a_service_rooted_run_is_still_skipped(self, session, redis_client, person_topology):
        """A ``service:``-prefixed attribution is not a person (§7.3's guard, unchanged)."""
        await seed_person_cap(session, "0.01")
        await seed_cloud_spend(session, RUN_ORG, "500.00", entity_id="service:eventbridge:nightly")

        harness = await _drive(
            _service(redis_client),
            session,
            context=agent_context(attributed_user_id="service:eventbridge:nightly"),
        )

        assert harness.status == 200

    async def test_a_caller_with_no_github_identity_is_skipped(self, session, redis_client):
        """No linked GitHub account → no ``github:`` anchor → no cap row can match."""
        await seed_org(session, HOME_ORG, "Home")
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)
        await seed_person_cap(session, "0.01", anchor=f"users:{CALLER_CANONICAL_ID}")
        await seed_direct_spend(session, HOME_ORG, "500.00")

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 200


# =============================================================================
# The layers below are untouched
# =============================================================================


class TestNothingElseChanged:
    """The ruling is a person-layer read+enforce widening. These pin the "else"."""

    async def test_the_per_org_user_ledger_is_unchanged(self, session, redis_client, person_topology):
        """A direct caller's own per-org ``(user, sub)`` cap still denies as itself.

        The person layer runs strictly after the per-org hierarchy, so an org's own
        ceiling keeps firing first and keeps its own scope — not relabelled as a
        personal limit, which would send the operator to a knob they cannot turn.
        """
        from src.shared.models.budget import BudgetConfig

        session.add(
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.USER.value,
                entity_id=CALLER_SUB,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("10.00"),
                enforcement_mode="hard",
            )
        )
        await session.commit()
        await seed_person_cap(session, "100000.00")
        await seed_direct_spend(session, HOME_ORG, "50.00")

        harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 402
        assert harness.details["entity_type"] == EntityType.USER.value, "the per-org cap must be attributed to its own entity"
        assert harness.details["entity_id"] == CALLER_SUB
        assert harness.details.get("scope") != "person", "an org's own cap must not be reported as a personal limit"

    async def test_no_cap_row_means_no_spend_read_for_a_direct_caller(self, session, redis_client, person_topology):
        """Cap first, denominator second — on the newly-admitted path too.

        Direct requests are the platform's hot path, so admitting them to the person
        layer must not add a partition fan-out to every one of them. With no cap
        authored, nothing beyond the identity and cap lookups may run.
        """
        await seed_direct_spend(session, HOME_ORG, "50.00")

        from src.budget import me_routes

        with patch.object(me_routes, "_resolve_member_partitions", side_effect=AssertionError("must not fan out with no cap row")):
            harness = await _drive(_service(redis_client), session, context=direct_context())

        assert harness.status == 200

    async def test_the_layer_still_takes_no_reservation(self, session, redis_client, person_topology):
        """The overshoot bound is unchanged: no person key is ever reserved (§5.5).

        A person key cannot carry the ``{org_id}`` Redis Cluster hash tag the atomic
        Lua needs, and the #4620 ruling forbids a second non-atomic call. Widening the
        denominator does not change that, and must not be "fixed" by adding one.

        The generous per-org cap is what makes the negative assertion meaningful: it
        produces a real reservation target, so the absence of a person key is a
        property of the person layer rather than of a request that reserved nothing.
        """
        from src.shared.models.budget import BudgetConfig

        await seed_person_cap(session, "100.00")
        session.add(
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.USER.value,
                entity_id=CALLER_SUB,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("10000.00"),
                enforcement_mode="hard",
            )
        )
        await session.commit()
        await seed_direct_spend(session, HOME_ORG, "1.00")

        reserved: list = []
        real_reserve = ReservationStore.reserve

        async def capturing_reserve(store_self, request_id, cost, targets):
            reserved.extend(targets)
            return await real_reserve(store_self, request_id, cost, targets)

        with patch.object(ReservationStore, "reserve", capturing_reserve):
            harness = await _drive(
                _service(redis_client),
                session,
                context=direct_context(),
                config=_config(budget_reservation_enabled=True),
            )

        assert harness.status == 200
        assert reserved, "reservations must be ON here, or the assertion below proves nothing"
        for target in reserved:
            assert PERSON_ANCHOR not in target.key(), f"the person layer must take no reservation key (§5.5): {target.key()}"

    async def test_the_layer_writes_nothing(self, session, redis_client, person_topology):
        """Person-level spend stays DERIVED. The fusion is a read; it accumulates nothing."""
        from sqlalchemy import select

        await seed_person_cap(session, "100.00")
        await seed_direct_spend(session, HOME_ORG, "60.00")

        await _drive(_service(redis_client), session, context=direct_context())

        usage = (await session.execute(select(BudgetUsage))).scalars().all()
        assert len(usage) == 1, "the person layer must accumulate nothing of its own"
        assert usage[0].total_cost_usd == Decimal("60.00")
        assert usage[0].period_start == date.today().replace(day=1)

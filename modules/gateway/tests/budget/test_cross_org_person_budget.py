"""Cross-org person view on ``/me/budget`` — Issue #4626 (C1 of #4620).

The defect: a person whose agent runs execute in a tenant other than the one their
session is attributed to sees ``$0`` on their own budget page while real dollars
accrue in the other tenant's partition. ``budget_usage`` is uniquely keyed
``(org_id, entity_type, entity_id, …)``, the tracker writes every ``root_user`` row
into the run's *attributed* tenant (#4132), and the endpoint read exactly one
partition. This suite pins the fix: ``per_org[]`` lines plus an informational
``person_envelope``, self-scope only.

**The isolation tests come first**, before any happy path, for the same reason
``test_me_budget_routes.py`` orders them that way: this unit deliberately WIDENS an
``org_id`` predicate, which is the one change on this surface that could turn a
self-scope read into a cross-tenant disclosure. ``TenantMixin`` applies no query
filter (``src/shared/models/base.py:12-15``), so the hand-written predicate is the
only guard there is (design note §7.3).

The two under-reporting traps from the note are each a test rather than a comment,
because both fail *silently* — a missing ledger row is indistinguishable from "no
spend", so an implementation that omits a partition still returns a plausible 200:

* **§3.3, the multi-``users``-row case.** ``users`` carries ``TenantMixin`` and
  ``user_identities`` is unique per ``(provider, provider_user_id, org_id)`` since
  migration 021, so one GitHub account legitimately holds a DIFFERENT ``users.id``
  per tenant. Summing by canonical id alone under-reports for exactly the multi-org
  population this ships for. See ``TestSplitIdentityFusion``.
* **§7.3, the shadow-user gap.** ``POST /resolve-user`` auto-provisions users with
  ``users.org_id`` set but **no** ``tenant_memberships`` row, so a membership-only
  partition list omits partitions where spend really accrued. See
  ``TestShadowUserFallback``.

Harness: the same real-in-memory-SQLite shape as ``test_me_budget_routes.py``, with
real ``BudgetConfig``/``BudgetUsage``/``User``/``TenantMembership``/``UserIdentity``
rows. Rows are inserted as raw models rather than through a service, because the
point is what happens when these values are already in the database.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.auth.dependencies import get_current_user
from src.budget.me_routes import router as me_budget_router
from src.budget.schemas import SPEND_PLACES, format_money
from src.shared.database import get_db
from src.shared.identity.providers import IdentityProvider
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

# The operator's real topology, from the design note's §2 walkthrough: home tenant
# `pranavsharma1000`, runs execute in `aws-e`. Named after it so a failure here reads
# against the scenario the issue reports.
HOME_ORG = "pranavsharma1000"
RUN_ORG = "aws-e"

# A third tenant the caller is NOT a member of. Every isolation test seeds the
# caller's own entity_id in here, so a partition list that came from anywhere other
# than the caller's memberships reads it and fails loudly.
FOREIGN_ORG = "org-not-a-member"

CALLER_SUB = "sub-caller-4626"
CALLER_CANONICAL_ID = "11111111-1111-4111-8111-111111111111"

# The cross-org anchor (design note §3.3): the GitHub numeric id. This is the join
# key that fuses a person's several `users.id` values, and it is what
# `person_envelope.anchor` reports.
CALLER_GITHUB_ID = "77700001"

# A colleague sharing the RUN_ORG partition. Their rows sit alongside the caller's
# under the same `org_id`, so widening the org predicate without keeping the
# `entity_id` filter returns their spend. Deliberately seeded with a figure that
# could not be confused for the caller's.
OTHER_CANONICAL_ID = "00000000-0000-4000-8000-000000000001"
OTHER_SPEND = "999.990000"


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


async def seed_org(session: AsyncSession, org_id: str, name: str | None = None) -> None:
    """An ``organizations`` row, so ``per_org[].org_name`` has something to read.

    Deliberately NOT seeded for every partition in every test: the shadow-user path
    can produce a partition id with no org row, and ``org_name`` must fall back to
    the id rather than crash or render blank.
    """
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


async def seed_root_usage(session: AsyncSession, org_id: str, entity_id: str, amount: str, period_type: PeriodType = PeriodType.MONTHLY) -> None:
    from src.budget.utils import get_period_start_end

    session.add(
        BudgetUsage(
            org_id=org_id,
            entity_type=EntityType.ROOT_USER.value,
            entity_id=entity_id,
            period_type=period_type.value,
            period_start=get_period_start_end(period_type)[0],
            total_cost_usd=Decimal(amount),
            total_tokens=1000,
            request_count=1,
        )
    )
    await session.commit()


async def seed_root_cap(session: AsyncSession, org_id: str, entity_id: str, amount: str, period_type: PeriodType = PeriodType.MONTHLY) -> None:
    session.add(
        BudgetConfig(
            org_id=org_id,
            entity_type=EntityType.ROOT_USER.value,
            entity_id=entity_id,
            period_type=period_type.value,
            budget_amount_usd=Decimal(amount),
            enforcement_mode="hard",
        )
    )
    await session.commit()


def caller_context(**overrides) -> TokenContext:
    """The caller, signed into their HOME org — the partition with no spend."""
    defaults = {
        "user_id": CALLER_SUB,
        "org_id": HOME_ORG,
        "team_id": "",
        "department_id": "",
        "account_type": "human",
        "expires_at": date(2099, 1, 1),
    }
    return TokenContext(**{**defaults, **overrides})


def build_app(session: AsyncSession, context: TokenContext | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(me_budget_router)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    if context is not None:
        app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def operator_topology(session) -> None:
    """The issue's exact scenario: home org has the cap, RUN_ORG has the spend.

    One ``users`` row, a GitHub identity, membership in both tenants. This is the
    ordinary (single-canonical-id) case; ``TestSplitIdentityFusion`` builds the §3.3
    variant separately.
    """
    await seed_org(session, HOME_ORG, "Pranav Sharma")
    await seed_org(session, RUN_ORG, "AWS-E")
    await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
    await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
    await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)
    await seed_membership(session, CALLER_CANONICAL_ID, RUN_ORG)

    # The dormant cap, authored in the partition where nothing runs (note §2 row 1).
    await seed_root_cap(session, HOME_ORG, CALLER_CANONICAL_ID, "5000.00")
    # The real spend, in the partition where runs execute (note §2 row 2).
    await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "264.600000")


def line_for(body: dict, org_id: str) -> dict | None:
    return next((line for line in body["per_org"] if line["org_id"] == org_id), None)


# ===========================================================================
# Isolation — FIRST, because this unit widens an org_id predicate
# ===========================================================================


class TestTenantIsolation:
    """The partition set is the entire authorization boundary of the cross-org read.

    Design note §7.3: ``check_permission`` accepts a single ``target_org_id`` and
    cannot express "authorized across N tenants", so the guard is the hand-written,
    server-derived partition list. These tests are what prove it is actually the
    guard.
    """

    async def test_a_non_member_partition_is_never_read(self, session, operator_topology):
        """The caller's OWN entity_id in a tenant they do not belong to stays invisible.

        This is the test that distinguishes "widened to the caller's memberships"
        from "widened to every partition". The seeded row matches on
        ``entity_type`` and ``entity_id`` — only membership excludes it — so an
        implementation that dropped the partition filter returns it and fails on the
        value, not merely on a count.
        """
        await seed_org(session, FOREIGN_ORG)
        await seed_root_usage(session, FOREIGN_ORG, CALLER_CANONICAL_ID, "4242.000000")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code == 200
        body = response.json()
        assert line_for(body, FOREIGN_ORG) is None, "a partition the caller is not a member of appears in per_org"
        assert "4242" not in response.text, "spend from a non-member partition leaked into the cross-org total"

    async def test_a_colleague_in_a_shared_partition_is_never_read(self, session, operator_topology):
        """Caller B never sees caller A's rows, even in a tenant both belong to.

        The colleague's ``root_user`` row shares the caller's ``org_id`` and
        ``entity_type``; only ``entity_id`` separates them. Dropping that filter from
        the widened read would return whichever row the database hands back first —
        which is why the decoy carries an unmistakable figure rather than a
        plausible one.
        """
        await seed_user(session, OTHER_CANONICAL_ID, RUN_ORG, sub="sub-colleague-4626")
        await seed_membership(session, OTHER_CANONICAL_ID, RUN_ORG, is_active=True)
        await seed_root_usage(session, RUN_ORG, OTHER_CANONICAL_ID, OTHER_SPEND)
        await seed_root_cap(session, RUN_ORG, OTHER_CANONICAL_ID, "8000.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code == 200
        body = response.json()
        assert "999.99" not in response.text, "a colleague's cloud spend leaked; entity_id is missing from the widened read"
        assert "8000" not in response.text, "a colleague's cap leaked into per_org"
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("264.600000")

    async def test_an_org_id_param_naming_another_tenant_is_ignored(self, session, operator_topology):
        """The IDOR negative for this unit, mirroring T2/T3 one field over.

        The partition list is server-derived, so a request parameter naming a tenant
        is not read at all — there is no parameter to abuse (design note §7.3). This
        pins that the widening did not introduce one.
        """
        await seed_org(session, FOREIGN_ORG)
        await seed_root_usage(session, FOREIGN_ORG, CALLER_CANONICAL_ID, "4242.000000")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget", params={"org_id": FOREIGN_ORG, "entity_id": OTHER_CANONICAL_ID})

        assert response.status_code == 200
        assert "4242" not in response.text, "an org_id query parameter was honoured; the partition list is not server-derived"
        assert {line["org_id"] for line in response.json()["per_org"]} == {HOME_ORG, RUN_ORG}

    async def test_only_the_two_person_grain_ledgers_are_summed(self, session, operator_topology):
        """Coarser-grain rows never enter the total; the two person ledgers both do.

        Since #4396 the total is ``root_user`` (cloud, keyed by canonical id) **plus**
        ``user`` (direct, keyed by Cognito sub). Those two are disjoint by namespace,
        so summing one row of each counts each dollar exactly once. An
        ``organization`` row is a different grain — it is the *same* dollars
        re-aggregated, plus every colleague's — so adding it is the #4322
        double-count. Both decoys sit in a partition the caller genuinely belongs to,
        so membership cannot be what separates them; only ``entity_type`` can.
        """
        session.add(
            BudgetUsage(
                org_id=RUN_ORG,
                entity_type=EntityType.USER.value,
                entity_id=CALLER_SUB,
                period_type=PeriodType.MONTHLY.value,
                period_start=date.today().replace(day=1),
                total_cost_usd=Decimal("111.000000"),
                total_tokens=1,
                request_count=1,
            )
        )
        session.add(
            BudgetUsage(
                org_id=RUN_ORG,
                entity_type=EntityType.ORGANIZATION.value,
                entity_id=RUN_ORG,
                period_type=PeriodType.MONTHLY.value,
                period_start=date.today().replace(day=1),
                total_cost_usd=Decimal("222.000000"),
                total_tokens=1,
                request_count=1,
            )
        )
        await session.commit()

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        run_line = line_for(body, RUN_ORG)
        assert Decimal(run_line["cloud_spend_usd"]) == Decimal("264.600000")
        assert Decimal(run_line["direct_spend_usd"]) == Decimal("111.000000")
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("375.600000"), (
            "the fused total is wrong: it must be cloud + direct, and must not pick up the org-grain row"
        )
        assert "222" not in response.text, "an organization-grain row entered the person total; the same dollar is counted twice"


# ===========================================================================
# The reported defect — per-org lines and the envelope
# ===========================================================================


class TestPerOrgLines:
    async def test_the_operators_scenario_resolves(self, session, operator_topology):
        """The issue's smoke test: spend in ``aws-e`` and a non-zero envelope.

        Before this unit the response described only ``pranavsharma1000`` — the
        partition with the dormant cap and no runs — so the page read ``$0``. Both
        lines must now be present: the mis-partition is only legible when the tenant
        holding the cap and the tenant holding the spend are visible side by side.
        """
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code == 200
        body = response.json()

        run_line = line_for(body, RUN_ORG)
        home_line = line_for(body, HOME_ORG)
        assert run_line is not None and home_line is not None

        # The tenant with the spend and no cap.
        assert Decimal(run_line["cloud_spend_usd"]) == Decimal("264.600000")
        assert run_line["cap_usd"] is None
        assert run_line["org_name"] == "AWS-E"

        # The tenant with the cap and no spend — the dormant-cap signature.
        assert Decimal(home_line["cloud_spend_usd"]) == Decimal("0")
        assert home_line["cap_usd"] == "5000.00"

        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("264.600000")

    async def test_the_active_partition_is_listed_first_and_flagged(self, session, operator_topology):
        """``lines``/``binding`` describe one partition; ``per_org`` must agree on which.

        A client rendering both needs to say "this workspace" without re-deriving it
        from the token, and the ordering keeps the two blocks visually consistent.
        """
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        per_org = response.json()["per_org"]
        assert per_org[0]["org_id"] == HOME_ORG
        assert per_org[0]["is_active_partition"] is True
        assert [line["is_active_partition"] for line in per_org[1:]] == [False]

    async def test_a_zero_spend_member_partition_is_still_listed(self, session, operator_topology):
        """A true ``$0`` line is a measurement and must not be suppressed.

        Suppressing empty partitions would hide exactly the dormant-cap half of the
        diagnosis (note §8.2): a cap authored where nothing runs is only visible as a
        line with a cap and no spend.
        """
        await seed_org(session, "org-third")
        await seed_membership(session, CALLER_CANONICAL_ID, "org-third")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        third = line_for(body, "org-third")
        assert third is not None
        assert Decimal(third["cloud_spend_usd"]) == Decimal("0")
        assert body["person_envelope"]["partition_count"] == 3

    async def test_the_active_partition_is_listed_without_a_membership_row(self, session):
        """A caller with no membership rows at all still sees their own session's tenant.

        Otherwise ``per_org`` would be empty for a single-tenant user while ``lines``
        above showed real figures — two blocks on one screen disagreeing about
        whether spend exists.
        """
        await seed_org(session, HOME_ORG, "Home")
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "12.500000")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert [line["org_id"] for line in body["per_org"]] == [HOME_ORG]
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("12.500000")

    async def test_a_partition_with_no_organizations_row_falls_back_to_the_id(self, session):
        """``org_name`` degrades to the id rather than rendering blank.

        Reachable on the shadow-user path, where the partition came from
        ``users.org_id`` and no ``organizations`` row need exist. The id is what an
        operator greps for, so it is the right fallback.
        """
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "1.000000")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert line_for(response.json(), HOME_ORG)["org_name"] == HOME_ORG

    async def test_each_period_reads_its_own_cross_org_rows(self, session, operator_topology):
        """``period_type`` narrows the cross-org read too, not just the active partition.

        A widened read that ignored the period would report monthly figures on the
        daily view — the same class of bug as reading the wrong partition, one axis
        over.
        """
        await seed_root_usage(session, RUN_ORG, CALLER_CANONICAL_ID, "3.000000", period_type=PeriodType.DAILY)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            daily = await client.get("/me/budget", params={"period_type": "daily"})
            monthly = await client.get("/me/budget", params={"period_type": "monthly"})

        assert Decimal(daily.json()["person_envelope"]["spend_usd"]) == Decimal("3.000000")
        assert Decimal(monthly.json()["person_envelope"]["spend_usd"]) == Decimal("264.600000")


class TestPersonEnvelope:
    async def test_the_envelope_is_the_exact_sum_of_the_lines(self, session, operator_topology):
        """Exact, and in ``Decimal`` — sub-cent precision must survive (``NUMERIC(14,6)``).

        Figures chosen so a float round-trip would show: ``0.1 + 0.2 != 0.3`` in
        binary floating point, and the ledger column holds 6dp.
        """
        await seed_org(session, "org-third")
        await seed_membership(session, CALLER_CANONICAL_ID, "org-third")
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "0.100000")
        await seed_root_usage(session, "org-third", CALLER_CANONICAL_ID, "0.200000")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        expected = sum((Decimal(line["cloud_spend_usd"]) for line in body["per_org"]), Decimal("0"))
        assert body["person_envelope"]["spend_usd"] == format_money(expected, SPEND_PLACES)
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("264.900000")

    async def test_the_envelope_carries_no_bindable_denominator(self, session, operator_topology):
        """No cap, no headroom, no band — enforced by the model's SHAPE (§7.1).

        No person-level cap table exists yet (note §4.1) and whether one may deny is
        an open ruling (§5.7). A denominator here would advertise a ceiling nothing
        enforces — the "cap that caps nothing" defect #4620 reports, inverted. So the
        fields must be absent from the wire, not merely null.
        """
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        envelope = response.json()["person_envelope"]
        for forbidden in ("cap_usd", "remaining_usd", "headroom_usd", "utilization_pct", "band", "enforcement_mode"):
            assert forbidden not in envelope, f"person_envelope carries {forbidden!r}; a client can bind a progress bar to it"
        assert envelope["is_budget"] is False
        assert envelope["note"]

    async def test_the_anchor_is_the_github_numeric_id(self, session, operator_topology):
        """The cross-org join key is ``provider_user_id``, and it is reported (§3.3)."""
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.json()["person_envelope"]["anchor"] == f"github:{CALLER_GITHUB_ID}"

    async def test_a_caller_with_no_github_identity_still_gets_lines(self, session):
        """No linked identity is not a failure — there is simply nothing to fuse.

        The anchor names the canonical id instead. Returning no lines here would hide
        a non-GitHub caller's own spend, which is a regression rather than caution.
        """
        await seed_org(session, HOME_ORG, "Home")
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "5.000000")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        envelope = response.json()["person_envelope"]
        assert envelope["anchor"] == f"users:{CALLER_CANONICAL_ID}"
        assert Decimal(envelope["spend_usd"]) == Decimal("5.000000")

    async def test_the_headline_is_not_the_cross_org_total(self, session, operator_topology):
        """FR-2.3's rule, one scope up: the headline stays the active partition's binding line.

        Folding the cross-org total into ``spend_usd`` would make the headline a
        figure no ledger row holds and no cap enforces — precisely what EPIC #4324
        exists to prevent. The cross-org view is additive, never part of the
        headline.
        """
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert Decimal(body["spend_usd"]) == Decimal("0"), "the headline absorbed cross-org spend; it must describe the active partition only"
        assert body["cap_usd"] == "5000.00"
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("264.600000")


# ===========================================================================
# §3.3 — the multi-`users`-row case this issue exists for
# ===========================================================================


class TestSplitIdentityFusion:
    """One GitHub account, two ``users`` rows, two ``root_user`` ledger keys.

    ``users`` carries ``TenantMixin`` and ``user_identities`` is unique per
    ``(provider, provider_user_id, org_id)`` since migration 021, so a person
    independently onboarded into two orgs legitimately holds a different canonical id
    in each — pinned by ``tests/shared/test_resolve_root_user_entity_id.py::
    test_shared_github_account_resolves_per_tenant``. A cross-org sum over one
    canonical id under-reports for exactly this population, and does so silently.
    """

    @pytest.fixture
    async def split_identity(self, session) -> str:
        second_canonical_id = "22222222-2222-4222-8222-222222222222"

        await seed_org(session, HOME_ORG, "Home")
        await seed_org(session, RUN_ORG, "AWS-E")

        # The row their session resolves to, in their home tenant.
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
        await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)

        # The SAME person's independently-onboarded row in the run tenant — a
        # different `users.id`, a different sub, the same GitHub anchor.
        await seed_user(session, second_canonical_id, RUN_ORG, sub="sub-caller-4626-in-aws-e")
        await seed_github_identity(session, second_canonical_id, RUN_ORG, CALLER_GITHUB_ID)
        await seed_membership(session, second_canonical_id, RUN_ORG)

        await seed_root_usage(session, HOME_ORG, CALLER_CANONICAL_ID, "10.000000")
        await seed_root_usage(session, RUN_ORG, second_canonical_id, "200.000000")
        return second_canonical_id

    async def test_both_canonical_ids_are_summed(self, session, split_identity):
        """The §3.3 fixture. A ``GROUP BY entity_id`` alone reports $10, not $210."""
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("210.000000"), (
            "the second users.id's spend is missing; the sum resolved by canonical id instead of by provider_user_id (§3.3)"
        )
        assert Decimal(line_for(body, RUN_ORG)["cloud_spend_usd"]) == Decimal("200.000000")

    async def test_the_second_rows_partition_is_authorized_by_its_own_membership(self, session, split_identity):
        """The partition list spans every fused ``users.id``, not just the session's.

        The RUN_ORG membership belongs to the *second* row, so a partition list built
        from the session's canonical id alone would omit the tenant holding almost
        all of the spend.
        """
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert {line["org_id"] for line in response.json()["per_org"]} == {HOME_ORG, RUN_ORG}

    async def test_an_unrelated_persons_github_anchor_is_not_fused(self, session, split_identity):
        """Fusion is by ``provider_user_id`` value, not by "has a GitHub identity".

        A different anchor in a shared tenant must not be pulled into the caller's
        key set — that would sum a colleague's cloud spend into the caller's own
        envelope.
        """
        await seed_user(session, OTHER_CANONICAL_ID, RUN_ORG, sub="sub-someone-else")
        await seed_github_identity(session, OTHER_CANONICAL_ID, RUN_ORG, "88800002")
        await seed_root_usage(session, RUN_ORG, OTHER_CANONICAL_ID, OTHER_SPEND)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert "999.99" not in response.text, "a different GitHub anchor was fused into the caller's key set"
        assert Decimal(response.json()["person_envelope"]["spend_usd"]) == Decimal("210.000000")


# ===========================================================================
# §7.3 — the shadow-user gap
# ===========================================================================


class TestShadowUserFallback:
    async def test_a_partition_with_no_membership_row_is_still_read(self, session):
        """``users.org_id`` is unioned in, exactly as ``provenance_routes.py:180-190`` does.

        ``POST /resolve-user`` auto-provisions users with ``users.org_id`` set but no
        ``tenant_memberships`` row, and only three code paths create memberships. A
        membership-only partition list therefore omits partitions where spend really
        accrued — silently, since a missing ledger row looks like no spend.
        """
        second_canonical_id = "33333333-3333-4333-8333-333333333333"

        await seed_org(session, HOME_ORG, "Home")
        await seed_org(session, RUN_ORG, "AWS-E")
        await seed_user(session, CALLER_CANONICAL_ID, HOME_ORG, sub=CALLER_SUB)
        await seed_github_identity(session, CALLER_CANONICAL_ID, HOME_ORG, CALLER_GITHUB_ID)
        await seed_membership(session, CALLER_CANONICAL_ID, HOME_ORG, is_active=True)

        # The shadow row: org_id set, NO membership row anywhere.
        await seed_user(session, second_canonical_id, RUN_ORG, sub=None)
        await seed_github_identity(session, second_canonical_id, RUN_ORG, CALLER_GITHUB_ID)
        await seed_root_usage(session, RUN_ORG, second_canonical_id, "77.000000")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert line_for(body, RUN_ORG) is not None, "the shadow user's partition is missing; users.org_id was not unioned in (§7.3)"
        assert Decimal(body["person_envelope"]["spend_usd"]) == Decimal("77.000000")


# ===========================================================================
# Degradation — an unmeasured figure is never rendered as $0
# ===========================================================================


class TestDegradation:
    async def test_an_unresolved_identity_yields_no_fabricated_lines(self, session):
        """No ``users`` row → no cross-org view, and NOT a ``$0`` single-tenant line.

        ``resolve_canonical_user_id`` falls back to the raw Cognito sub, which matches
        no ``root_user`` row. Rendering that as ``per_org: [{$0}]`` would assert a
        measurement that was never taken — the false-$0 failure this EPIC exists to
        end. ``identity_status`` already carries which case it is.
        """
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert body["identity_status"] == "unresolved"
        assert body["per_org"] == []
        assert body["person_envelope"] is None

    async def test_a_service_account_caller_gets_no_person_view(self, session):
        """A service account is not a person and has no canonical row by design.

        ``service:``-qualified principals are excluded structurally rather than by a
        filter: the fused key list is built from ``users`` primary keys, which can
        never carry the qualifier (#4344).
        """
        app = build_app(session, caller_context(user_id="sa-4626", account_type="service"))
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert body["identity_status"] == "not_applicable"
        assert body["per_org"] == []
        assert body["person_envelope"] is None

    async def test_a_failed_cross_org_read_is_a_503_not_a_zero_total(self, session, operator_topology, monkeypatch):
        """The read failed, so the answer is "ask again" — never a ``$0`` envelope.

        A cross-org total of ``$0`` returned with a 200 during an outage is the exact
        defect #4620 reports, dressed up as a successful response. Patched at the
        membership query so the failure lands inside the cross-org path specifically,
        after the active partition's reads have already succeeded.
        """
        from sqlalchemy.exc import OperationalError

        import src.budget.me_routes as me_routes

        async def boom(*_args, **_kwargs):
            raise OperationalError("SELECT tenant_memberships", {}, Exception("connection reset"))

        monkeypatch.setattr(me_routes, "_resolve_member_partitions", boom)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code == 503
        assert "spend_usd" not in response.text

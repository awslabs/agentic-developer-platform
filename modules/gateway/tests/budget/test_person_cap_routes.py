"""Person-level cap authoring API — Issue #4629 (#4620 · C3).

Design note ``docs/design-notes/4620-cross-org-person-budgets.md`` §4.

**The authority tests come first**, before any happy path, because the whole point
of this unit is *who may author a partition-free cap* (§4.2):

  T1  org admin -> another person's cap        -> 403  (the authority inversion)
  T2  org admin -> a member of their OWN org   -> 403  (still no; not a softer door)
  T3  plain member -> another person's cap     -> 403
  T4  the person -> their own cap              -> 200  (self-service)
  T5  platform admin -> anybody's cap          -> 200
  T6  the stored key is `github:<numeric_id>`, never a `users.id`
  T7  `hard` is the only mode any path writes since C4 (#4630); never client-settable
  T8  the self path has NO target parameter at any position
  T9  unresolvable / malformed anchors -> 422, and nothing is written
  T10 upsert idempotence, and DELETE vs a `0` cap
  T11 ledger faults -> 503, never a 200 reading as "no limit"

Harness notes:

* Real in-memory SQLite with real ``User`` / ``UserIdentity`` /
  ``PersonBudgetConfig`` rows — the harness ``test_me_budget_routes.py`` and
  ``test_managed_scope_budget.py`` established. ``AccessControl`` is **not**
  mocked: it is constructed inside the route against the request's real session,
  so ``require_platform_admin`` runs for real. A mocked authority check asserts a
  guarantee it never exercised (the #4046 trap), and authority is the thing under
  test here.
* The ``BedrockGatewayError`` handler is registered on the test app exactly as
  ``src/app.py`` registers it, so a raised ``AccessDeniedError`` becomes the same
  ``403`` a client sees rather than an unhandled 500.
* Two people are seeded with different GitHub ids and unmistakably different cap
  amounts, so a route that authored against the wrong anchor fails loudly instead
  of coincidentally matching.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.auth.dependencies import get_current_user
from src.budget.person_cap_routes import router as person_cap_router
from src.budget.utils import get_period_start_end
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.identity.providers import IdentityProvider
from src.shared.models.base import Base
from src.shared.models.budget import BudgetUsage, PersonBudgetConfig, PersonBudgetDefault
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import PeriodType

ORG_ID = "org-4629"
OTHER_ORG_ID = "org-4629-other"
TEAM_ID = "team-4629"

# ---------------------------------------------------------------------------
# The cast. Every person has a canonical `users.id` AND a GitHub numeric id, and
# the two are deliberately unmistakable for each other — T6 depends on it. A cap
# keyed on the `users.id` would be the #4511 inert-cap class one layer up: a
# person onboarded into two orgs has two of those, so it would miss their spend in
# the other org.
# ---------------------------------------------------------------------------
PERSON_SUB = "sub-4629-person"
PERSON_CANONICAL = "46290000-0000-4000-8000-000000000001"
PERSON_GITHUB_ID = "5550001"
PERSON_ANCHOR = f"github:{PERSON_GITHUB_ID}"

COLLEAGUE_SUB = "sub-4629-colleague"
COLLEAGUE_CANONICAL = "46290000-0000-4000-8000-000000000002"
COLLEAGUE_GITHUB_ID = "5550002"
COLLEAGUE_ANCHOR = f"github:{COLLEAGUE_GITHUB_ID}"

ORG_ADMIN_SUB = "sub-4629-orgadmin"
ORG_ADMIN_CANONICAL = "46290000-0000-4000-8000-000000000003"
ORG_ADMIN_GITHUB_ID = "5550003"

PLATFORM_ADMIN_SUB = "sub-4629-platformadmin"
PLATFORM_ADMIN_CANONICAL = "46290000-0000-4000-8000-000000000004"
PLATFORM_ADMIN_GITHUB_ID = "5550004"

# A person in a DIFFERENT tenant from the org admin. Their cap is the one an org
# admin most obviously has no business authoring — but T2 proves even their own
# org's members are off limits.
FOREIGN_SUB = "sub-4629-foreign"
FOREIGN_CANONICAL = "46290000-0000-4000-8000-000000000005"
FOREIGN_GITHUB_ID = "5550005"
FOREIGN_ANCHOR = f"github:{FOREIGN_GITHUB_ID}"

# A GitHub id that is linked to nobody. Used by T9: an anchor pointing at it can
# never match a settled ledger row, so storing a cap against it would display a
# limit and govern nothing.
UNLINKED_GITHUB_ID = "9999999"
UNLINKED_ANCHOR = f"github:{UNLINKED_GITHUB_ID}"


@pytest.fixture
async def engine():
    """In-memory SQLite with the real schema."""
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
async def seeded(session) -> None:
    """Users, their GitHub identity links, and their tenant roles.

    Authority lives in ``tenant_memberships``, never in the token — the org-admin
    denial tests below would be meaningless if the role came from a claim the
    caller could set.

    Organization rows exist for the seeded orgs because the default PUT now
    validates scope EXISTENCE at write time (review fix on #4696).
    """
    from src.shared.models.organization import Organization

    session.add(Organization(id=ORG_ID, name="Org 4629"))
    session.add(Organization(id=OTHER_ORG_ID, name="Other Org 4629"))
    cast = [
        (PERSON_CANONICAL, PERSON_SUB, PERSON_GITHUB_ID, ORG_ID, "member"),
        (COLLEAGUE_CANONICAL, COLLEAGUE_SUB, COLLEAGUE_GITHUB_ID, ORG_ID, "member"),
        (ORG_ADMIN_CANONICAL, ORG_ADMIN_SUB, ORG_ADMIN_GITHUB_ID, ORG_ID, "org_admin"),
        (PLATFORM_ADMIN_CANONICAL, PLATFORM_ADMIN_SUB, PLATFORM_ADMIN_GITHUB_ID, ORG_ID, "platform_admin"),
        (FOREIGN_CANONICAL, FOREIGN_SUB, FOREIGN_GITHUB_ID, OTHER_ORG_ID, "member"),
    ]
    for canonical, sub, github_id, org_id, role in cast:
        session.add(User(id=canonical, cognito_sub=sub, email=f"{sub}@example.com", org_id=org_id, team_id=TEAM_ID))
        session.add(TenantMembership(user_id=canonical, tenant_id=org_id, role=role, is_active=True))
        session.add(
            UserIdentity(
                user_id=canonical,
                org_id=org_id,
                team_id=TEAM_ID,
                provider=IdentityProvider.github.value,
                provider_user_id=github_id,
                provider_username=sub,
                verification_method="oauth",
            )
        )
    await session.commit()


def context_for(sub: str, *, org_id: str = ORG_ID, is_admin: bool = False, **overrides) -> TokenContext:
    """A token context.

    ``is_admin`` means **platform** admin and nothing else: ``auth/dependencies.py``
    deliberately excludes ``org_admin`` from it. So an org admin's context has
    ``is_admin=False``, which is precisely why ``require_platform_admin`` denies
    them — and why T1/T2 are not testing a coincidence.
    """
    defaults = {
        "user_id": sub,
        "org_id": org_id,
        "team_id": TEAM_ID,
        "department_id": "",
        "account_type": "human",
        "is_admin": is_admin,
        "expires_at": date(2099, 1, 1),
    }
    return TokenContext(**{**defaults, **overrides})


def build_app(session: AsyncSession, context: TokenContext | None = None) -> FastAPI:
    """Mount the person-cap router alone, with auth and db overridden.

    ``AccessControl`` is deliberately NOT overridden — the route constructs it
    against the request's real session, so the platform-admin check below runs for
    real. That is the unit.
    """
    app = FastAPI()
    app.include_router(person_cap_router)

    # The same handler src/app.py registers. Without it a raised
    # AccessDeniedError surfaces as an unhandled 500 and the 403 tests would be
    # asserting against the test app's gap rather than the route's behaviour.
    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    if context is not None:
        app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(session: AsyncSession, context: TokenContext | None = None) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=build_app(session, context)), base_url="http://test")


async def stored_caps(session: AsyncSession) -> list[PersonBudgetConfig]:
    """Every cap row, so a test can assert on what was (or was not) written."""
    session.expire_all()
    result = await session.scalars(sa.select(PersonBudgetConfig).order_by(PersonBudgetConfig.person_anchor, PersonBudgetConfig.period_type))
    return list(result)


async def seed_cap(
    session: AsyncSession, anchor: str, amount: str, *, period_type: str = "monthly", authored_by: str = "seed", enforcement_mode: str = "soft"
) -> PersonBudgetConfig:
    row = PersonBudgetConfig(
        person_anchor=anchor,
        period_type=period_type,
        budget_amount_usd=Decimal(amount),
        enforcement_mode=enforcement_mode,
        authored_by_user_id=authored_by,
    )
    session.add(row)
    await session.commit()
    return row


# ===========================================================================
# T1 — org admin -> another person's cap: 403. The authority inversion.
# ===========================================================================


async def test_t1_org_admin_cannot_author_another_persons_cap(session, seeded):
    """An org admin authoring a person-level cap is denied (§4.2).

    This is the security property of the unit. The row is partition-free, so an
    org admin who could write it would be setting a ceiling that governs the
    person's spend in every OTHER tenant they work in — tenants the org admin has
    no membership in, cannot see, and cannot be audited by. The ruling on #4620
    forbids it, which is why the only targeted route is platform-admin-gated.

    Note the caller here is a real ``org_admin`` by ``tenant_memberships``, and one
    who legitimately holds ``BUDGET_UPDATE`` *inside their own org*. That is
    exactly why the gate is ``require_platform_admin`` and not
    ``check_permission(BUDGET_UPDATE, target_org_id=...)`` — the latter would be
    trivially satisfied here and would hand the authority to the wrong party.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.put(f"/budget/person-cap/{COLLEAGUE_ANCHOR}", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 403, response.text
    assert await stored_caps(session) == [], "a denied request must write nothing"


async def test_t1b_org_admin_cannot_author_a_cap_in_another_tenant(session, seeded):
    """The most obvious case, asserted separately: a person outside their org."""
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.put(f"/budget/person-cap/{FOREIGN_ANCHOR}", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 403, response.text
    assert await stored_caps(session) == []


async def test_t1c_org_admin_cannot_read_another_persons_cap(session, seeded):
    """Reading is denied too, not only writing.

    Reading a partition-free cap discloses a figure that governs the person's
    spend across tenants the org admin has no visibility into — so the read
    carries the same authority question as the write, and gets the same answer.
    """
    await seed_cap(session, COLLEAGUE_ANCHOR, "42.00")

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/person-cap/{COLLEAGUE_ANCHOR}")

    assert response.status_code == 403, response.text
    assert "42.00" not in response.text, "a denial must not leak the cap it refused to show"


# ===========================================================================
# T2 — org admin -> a member of their OWN org: still 403
# ===========================================================================


async def test_t2_org_admin_cannot_author_for_their_own_org_member(session, seeded):
    """ "They're in my org" is not a softer door.

    A person-level cap has no org, so there is no sense in which a member of the
    org admin's tenant makes this cap theirs to set. Written as its own test
    because "same tenant" is the intuitive exception someone would add, and it is
    exactly the one the #4620 ruling rejects: the org admin's own-org membership
    grants them nothing outside that partition, and this row is entirely outside
    every partition.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "10.00"})

    assert response.status_code == 403, response.text
    assert await stored_caps(session) == []


async def test_t2c_forged_admin_role_claim_does_not_grant_authority(session, seeded):
    """A caller cannot promote themselves with a token field.

    ``is_admin`` is derived server-side from a platform-level Cognito claim, and an
    org admin's context has it ``False``. This asserts the denial survives a caller
    who has set every *other* field they control — the authority comes from
    ``is_admin``, which they cannot set, not from ``org_id`` or ``team_id``.
    """
    forged = context_for(ORG_ADMIN_SUB, org_id=OTHER_ORG_ID, team_id="team-anything", department_id="dept-anything")

    async with client_for(session, forged) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "10.00"})

    assert response.status_code == 403, response.text
    assert await stored_caps(session) == []


# ===========================================================================
# T3 — plain member -> another person's cap: 403
# ===========================================================================


async def test_t3_member_cannot_author_another_persons_cap(session, seeded):
    """A colleague at the adjacent desk is denied, for the same reason.

    Covered separately from the org admin because a member fails a *different*
    predicate for the same gate, and one passing does not imply the other.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put(f"/budget/person-cap/{COLLEAGUE_ANCHOR}", json={"budget_amount_usd": "1.00"})

    assert response.status_code == 403, response.text
    assert await stored_caps(session) == []


async def test_t3b_member_cannot_author_their_own_cap_via_the_targeted_path(session, seeded):
    """Even pointing the targeted route at *yourself* is denied.

    Deliberate: the targeted route's gate is a claim about the caller's authority,
    not about the target, so it does not soften when the two coincide. The person's
    own cap has its own route, and keeping the two rules independent is what stops
    "the target is me" from becoming a bypass someone widens later.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "1.00"})

    assert response.status_code == 403, response.text
    assert await stored_caps(session) == []


# ===========================================================================
# T4 — the person authors their OWN cap: 200
# ===========================================================================


async def test_t4_person_authors_their_own_cap(session, seeded):
    """Self-service, the first row of §4.2.

    No authorisation beyond authentication is required, because the person is
    bounding the spend of agents they set in motion and exercising no authority
    over any tenant.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "250.00"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["person_anchor"] == PERSON_ANCHOR
    assert body["cap_usd"] == "250.00"
    assert body["cap_status"] == "capped"
    assert body["period_type"] == "monthly"

    rows = await stored_caps(session)
    assert len(rows) == 1
    assert rows[0].person_anchor == PERSON_ANCHOR
    assert Decimal(rows[0].budget_amount_usd) == Decimal("250.00")


async def test_t4b_person_reads_their_own_cap(session, seeded):
    """The read the authoring UI needs: what limit do I currently have?"""
    await seed_cap(session, PERSON_ANCHOR, "125.50")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cap_usd"] == "125.50"
    assert body["cap_status"] == "capped"
    # The STORED mode is echoed, not a constant. `seed_cap` writes `soft` (a C3-era
    # row), and reading it back as `soft` is the point: those rows stay
    # informational until re-authored (#4630's flag-day rule), and the UI picks its
    # notice off this field — so a read that normalised it to `hard` would tell
    # somebody their spend is being stopped when it is not.
    assert body["enforcement_mode"] == "soft"


async def test_t4c_no_cap_reads_as_uncapped_not_zero(session, seeded):
    """ "No limit authored" is a positive statement, never a ``0.00``.

    Contract rule 2. A zero would render as a person who may spend nothing, which
    is the opposite of the truth, and would make a UI show a limit nobody set.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cap_status"] == "uncapped"
    assert body["cap_usd"] is None
    assert body["enforcement_mode"] is None
    assert body["updated_at"] is None


async def test_t4d_a_persons_cap_is_not_read_from_another_persons_row(session, seeded):
    """The self read is anchored to the caller, with a decoy row present.

    The colleague's row exists and carries an unmistakably different figure, so a
    read that dropped the anchor predicate would return theirs and fail here rather
    than passing by coincidence.
    """
    await seed_cap(session, COLLEAGUE_ANCHOR, "999.99")
    await seed_cap(session, PERSON_ANCHOR, "11.11")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap")

    assert response.json()["cap_usd"] == "11.11"
    assert "999.99" not in response.text


async def test_t4e_each_period_is_authored_independently(session, seeded):
    """daily/weekly/monthly are separate limits for one person."""
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        for period, amount in (("daily", "10.00"), ("weekly", "50.00"), ("monthly", "150.00")):
            response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}?period_type={period}", json={"budget_amount_usd": amount})
            assert response.status_code == 200, response.text
            assert response.json()["period_type"] == period

    rows = await stored_caps(session)
    assert {row.period_type: str(row.budget_amount_usd) for row in rows} == {"daily": "10.00", "weekly": "50.00", "monthly": "150.00"}


# ===========================================================================
# T5 — platform admin authors ANY person's cap: 200
# ===========================================================================


async def test_t5_platform_admin_authors_another_persons_cap(session, seeded):
    """The second row of §4.2: a platform admin already holds cross-org authority.

    This is the one party for whom a partition-free write is coherent, which is
    why the targeted route exists at all.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "500.00"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["person_anchor"] == PERSON_ANCHOR
    assert body["cap_usd"] == "500.00"

    rows = await stored_caps(session)
    assert len(rows) == 1
    assert rows[0].person_anchor == PERSON_ANCHOR
    # The author is recorded as the admin, not the subject: an authority decision
    # taken on somebody else's behalf must be attributable to whoever took it.
    # The CANONICAL id, never the Cognito sub (review fix): the column contract is
    # canonical users.id, and the token id's namespace varies by auth path.
    assert rows[0].authored_by_user_id == PLATFORM_ADMIN_CANONICAL


async def test_t5b_platform_admin_authors_across_tenants(session, seeded):
    """Including for a person in a tenant the admin's own token does not name.

    The whole point of the table: the write is not scoped by the caller's org, and
    a cap for someone in another tenant is an expected operation for this one role.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{FOREIGN_ANCHOR}", json={"budget_amount_usd": "60.00"})

    assert response.status_code == 200, response.text
    assert response.json()["person_anchor"] == FOREIGN_ANCHOR


async def test_t5c_platform_admin_reads_any_persons_cap(session, seeded):
    await seed_cap(session, COLLEAGUE_ANCHOR, "33.00")

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/budget/person-cap/{COLLEAGUE_ANCHOR}")

    assert response.status_code == 200, response.text
    assert response.json()["cap_usd"] == "33.00"


async def test_t5d_platform_admin_read_of_an_unset_cap_is_uncapped(session, seeded):
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/budget/person-cap/{COLLEAGUE_ANCHOR}")

    assert response.status_code == 200, response.text
    assert response.json()["cap_status"] == "uncapped"


# ===========================================================================
# T6 — the stored key is `github:<numeric_id>`, never a `users.id`
# ===========================================================================


async def test_t6_stored_key_is_the_github_anchor_not_a_users_id(session, seeded):
    """The note's key choice, asserted exactly (§3.3, and the issue's validation 4).

    ``users`` carries ``TenantMixin``, so a person onboarded independently into two
    orgs legitimately has TWO ``users.id`` values. A cap keyed on one of them would
    miss their spend in the other org — a cap that exists, shows a number, and
    governs nothing, which is the #4511 inert-cap class one layer up. The GitHub
    numeric id is the same string in every tenant, which is what makes the cap
    cross-org.

    Both the Cognito sub and the canonical id are asserted absent, because either
    one appearing in this column would be the bug.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 200, response.text
    rows = await stored_caps(session)
    assert len(rows) == 1
    stored_key = rows[0].person_anchor

    assert stored_key == f"github:{PERSON_GITHUB_ID}"
    assert stored_key != PERSON_CANONICAL, "the cap must not be keyed on users.id — see design note §3.3"
    assert stored_key != PERSON_SUB, "the cap must not be keyed on the Cognito sub"


async def test_t6b_anchor_carries_the_provider_namespace_qualifier(session, seeded):
    """A bare numeric id would be unqualified and could alias another namespace.

    Same anti-collision reasoning as #4344's ``service:`` prefix: two id namespaces
    under one unique constraint is how one person's cap comes to govern another
    person's spend once a second provider is anchored.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    rows = await stored_caps(session)
    assert rows[0].person_anchor.startswith("github:")
    assert rows[0].person_anchor != PERSON_GITHUB_ID


async def test_t6c_self_and_platform_admin_paths_produce_the_same_key(session, seeded):
    """The two authoring routes must agree on the key, or they author two caps.

    If the self path stored ``github:123`` and the admin path stored ``123``, one
    person would end up with two rows that each look like "their" cap, and which
    one governs would depend on which route last ran. Asserted by having the admin
    re-author over the person's own row and checking the row count stays at one.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        self_body = (await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})).json()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        admin_body = (await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "30.00"})).json()

    assert self_body["person_anchor"] == admin_body["person_anchor"] == PERSON_ANCHOR
    rows = await stored_caps(session)
    assert len(rows) == 1, "the two paths must write the SAME row, not one each"
    assert str(rows[0].budget_amount_usd) == "30.00"


# ===========================================================================
# T7 — `hard` is the only mode any path writes, since C4 (#4630)
#
# C3 pinned these to `soft` because nothing read the table. #4630's
# `_check_person_budget` now does, so an authored cap denies — see
# `test_person_cap_enforcement.py`. What did NOT change is that the mode is not
# client-settable, which is what T7c still guards.
# ===========================================================================


async def test_t7_self_authored_cap_is_enforcing(session, seeded):
    """A self-authored cap is written ``hard`` — it denies (#4630).

    Authoring your own ceiling IS the §5.6 opt-in, which is what makes a denying
    cross-org cap legitimate: the person is the one party present in every org the
    spend happens in, so this is self-restraint rather than an authority inversion.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.json()["enforcement_mode"] == "hard"
    rows = await stored_caps(session)
    assert rows[0].enforcement_mode == "hard"


async def test_t7b_platform_admin_authored_cap_is_also_enforcing(session, seeded):
    """A platform admin's cap enforces too — §4.2's second author.

    They already hold cross-org authority by design, which is why they are the one
    other party permitted to author a partition-free cap at all.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.json()["enforcement_mode"] == "hard"
    rows = await stored_caps(session)
    assert rows[0].enforcement_mode == "hard"


@pytest.mark.parametrize("path", [f"/budget/person-cap/{PERSON_ANCHOR}"])
async def test_t7c_enforcement_mode_is_not_client_settable(session, seeded, path):
    """A request asking for ``soft`` does not get it — on either route.

    The field is absent from the request model, so an extra key is ignored rather
    than honoured. Still asserted after #4630 flipped the written value, and the
    direction of the attempt is deliberately inverted with it: the thing a client
    might now want to smuggle in is a cap that does NOT enforce, which would be a
    limit somebody believes is stopping their spend while nothing does (#4511).
    """
    context = context_for(PLATFORM_ADMIN_SUB, is_admin=True) if path.startswith("/budget") else context_for(PERSON_SUB)

    async with client_for(session, context) as client:
        response = await client.put(path, json={"budget_amount_usd": "20.00", "enforcement_mode": "soft"})

    assert response.status_code == 200, response.text
    assert response.json()["enforcement_mode"] == "hard"
    rows = await stored_caps(session)
    assert [row.enforcement_mode for row in rows] == ["hard"]


async def test_t7d_resaving_a_c3_era_soft_row_upgrades_it_to_enforcing(session, seeded):
    """Re-authoring is the documented flag-day remediation (#4630).

    C3-era rows are deliberately NOT converted on deploy: that UI told the person in
    as many words that "requests are not blocked", and silently turning the number
    they typed into a denial breaks the promise the screen made. Re-saving is the
    person restating the limit against copy that now says it enforces — one click,
    no amount change needed.
    """
    session.add(
        PersonBudgetConfig(
            person_anchor=PERSON_ANCHOR,
            period_type="monthly",
            budget_amount_usd=Decimal("20.00"),
            enforcement_mode="soft",
            authored_by_user_id=PERSON_CANONICAL,
        )
    )
    await session.commit()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.json()["enforcement_mode"] == "hard"
    rows = await stored_caps(session)
    assert len(rows) == 1, "re-authoring updates in place; it must not create a second row"
    assert rows[0].enforcement_mode == "hard"


# ===========================================================================
# T8 — the self path accepts NO target at any position
# ===========================================================================


@pytest.mark.parametrize(
    "params",
    [
        {"person_anchor": COLLEAGUE_ANCHOR},
        {"user_id": COLLEAGUE_CANONICAL},
        {"entity_id": COLLEAGUE_SUB},
    ],
)
async def test_t8_self_path_ignores_any_target_query_parameter(session, seeded, params):
    """Structural scoping: there is no parameter to abuse.

    The anchor is derived from the validated token, so a caller naming somebody
    else is not merely denied — there is nothing to name, and the request is served
    against the caller's own anchor. Same argument ``me_routes.py`` makes for the
    read path, and the reason an org admin cannot reach another person's cap even
    though they can reach this route.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", params=params, json={"budget_amount_usd": "20.00"})

    assert response.status_code == 200, response.text
    assert response.json()["person_anchor"] == PERSON_ANCHOR
    rows = await stored_caps(session)
    assert [row.person_anchor for row in rows] == [PERSON_ANCHOR]


@pytest.mark.parametrize("body_target", [{"person_anchor": COLLEAGUE_ANCHOR}, {"user_id": COLLEAGUE_CANONICAL}])
async def test_t8b_self_path_ignores_any_target_in_the_body(session, seeded, body_target):
    """Nor in the request body — the model has one field and it is an amount."""
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00", **body_target})

    assert response.status_code == 200, response.text
    assert response.json()["person_anchor"] == PERSON_ANCHOR


async def test_t8c_self_route_declares_no_path_or_query_target(session):
    """The route signature itself carries no target parameter.

    Asserted against the declared parameters rather than only through requests: a
    future signature change that ADDED an anchor parameter would still pass the
    request-level tests above (they only check the ones that exist today), but
    would hand every caller of ``/me/*`` the ability to author somebody else's cap.
    """
    self_routes = [route for route in person_cap_router.routes if route.path == "/me/budget/person-cap"]
    assert self_routes, "the self READ route must exist"

    # 2026-09-07 ruling: person limits are admin-governed only. The self surface
    # is GET — the write methods are DELETED, not 403-stubbed, and this pin is
    # what keeps them deleted.
    methods = set()
    for route in self_routes:
        methods |= route.methods
        names = {param.name for param in route.dependant.path_params} | {param.name for param in route.dependant.query_params}
        assert names <= {"period_type"}, f"{route.methods} /me/budget/person-cap must accept no target parameter, found: {sorted(names)}"
    assert "PUT" not in methods and "DELETE" not in methods and "POST" not in methods, (
        f"self-service person-cap WRITES are back on the route table ({sorted(methods)}) — forbidden by the 2026-09-07 ruling"
    )


# ===========================================================================
# T9 — unresolvable / malformed anchors: 422, and nothing is written
# ===========================================================================


async def test_t9_unlinked_anchor_is_422_and_writes_nothing(session, seeded):
    """An anchor no ``user_identities`` row carries is refused, not stored.

    A cap keyed on such an id can never match a settled ``root_user`` ledger row,
    so it would display a limit and govern nothing — the #4511 failure mode, which
    is worse than a rejection because it looks like success.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{UNLINKED_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "unresolvable_person_anchor"
    assert await stored_caps(session) == []


@pytest.mark.parametrize("bad_anchor", ["1234567", "gitlab:1234567", "github:", "github:%201234567"])
async def test_t9b_malformed_anchor_is_422(session, seeded, bad_anchor):
    """Shape violations are refused: unqualified, wrong namespace, empty, padded.

    Each maps to a real way an inert row gets written — an id in another namespace,
    a key no ledger row can carry, or a key that compares unequal to the same
    person's real one.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{bad_anchor}", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 422, response.text
    assert await stored_caps(session) == []


async def test_t9c_authority_is_checked_before_the_anchor_is_resolved(session, seeded):
    """A non-admin gets 403 for an unlinked anchor, not 422.

    Otherwise the 422-vs-403 difference is an existence oracle: an unauthorised
    caller could learn which GitHub ids are linked on this platform by watching
    which status code comes back.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        unlinked = await client.get(f"/budget/person-cap/{UNLINKED_ANCHOR}")
        linked = await client.get(f"/budget/person-cap/{PERSON_ANCHOR}")

    assert unlinked.status_code == linked.status_code == 403
    assert unlinked.json() == linked.json(), "the denial must not vary by whether the target exists"


async def test_t9d_caller_without_a_github_identity_gets_422(session):
    """A person with no linked GitHub account has no cross-org key.

    They get an honest refusal rather than a cap stored under a fabricated key. The
    ``users`` row exists here — only the identity link is missing — so this is the
    second of the two lookups failing, not the first.
    """
    session.add(User(id=PERSON_CANONICAL, cognito_sub=PERSON_SUB, email="p@example.com", org_id=ORG_ID, team_id=TEAM_ID))
    await session.commit()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "unresolvable_person_anchor"
    assert await stored_caps(session) == []


async def test_t9e_caller_with_no_users_row_gets_422(session):
    """No ``users`` row at all: the first lookup fails, still a 422 and no write."""
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 422, response.text
    assert await stored_caps(session) == []


async def test_t9f_caller_matched_by_canonical_id_resolves(session, seeded):
    """A caller whose ``user_id`` is already the canonical id still gets their cap.

    Some callers reach budget code with ``user_id`` rewritten to the canonical
    ``users.id`` (#3989). Matching only on ``cognito_sub`` would deny those callers
    their own limit with a 422 that looks like "you have no GitHub account".
    """
    await seed_cap(session, PERSON_ANCHOR, "20.00")
    async with client_for(session, context_for(PERSON_CANONICAL)) as client:
        response = await client.get("/me/budget/person-cap")

    assert response.status_code == 200, response.text
    assert response.json()["person_anchor"] == PERSON_ANCHOR


@pytest.mark.parametrize("bad_amount", ["0", "0.00", "-5.00", "1.234"])
async def test_t9g_invalid_amounts_are_422(session, seeded, bad_amount):
    """Zero, negative and over-precise amounts are refused.

    Zero in particular: it would be indistinguishable downstream from "no limit
    authored", so removing a limit is a DELETE (T10) rather than a ``PUT`` of 0.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": bad_amount})

    assert response.status_code == 422, response.text
    assert await stored_caps(session) == []


@pytest.mark.parametrize("bad_period", ["run", "chain", "hourly", ""])
async def test_t9h_non_calendar_periods_are_422(session, seeded, bad_period):
    """Run/chain caps are lifetime-scoped and have no calendar window.

    ``get_period_start_end`` raises for them, so an unguarded value would be a 500
    — a server error for what is a bad request. The ``Literal`` answers 422 at the
    HTTP boundary before any handler code runs.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}?period_type={bad_period}", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 422, response.text


# ===========================================================================
# T10 — upsert idempotence; DELETE vs a `0` cap
# ===========================================================================


async def test_t10_re_authoring_replaces_in_place(session, seeded):
    """Re-authoring updates one row rather than creating a second.

    Two rows for one person and period would mean two ceilings on the same dollars
    and no defined winner. The unique constraint would reject the insert, so a
    non-upsert implementation would surface as a 500 on the person's second edit.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        first = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "100.00"})
        second = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "200.00"})

    assert first.status_code == second.status_code == 200, second.text
    assert second.json()["cap_usd"] == "200.00"

    rows = await stored_caps(session)
    assert len(rows) == 1
    assert str(rows[0].budget_amount_usd) == "200.00"


async def test_t10b_re_authoring_keeps_the_row_identity(session, seeded):
    """The row's ``id`` and ``created_at`` survive a re-author.

    ``created_at`` answers "when did this person first set a limit", which a
    delete-and-recreate would silently reset.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "100.00"})
        before = (await stored_caps(session))[0]
        original_id, original_created = before.id, before.created_at

        await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "200.00"})

    after = (await stored_caps(session))[0]
    assert after.id == original_id
    assert after.created_at == original_created


async def test_t10c_re_authoring_records_the_current_author(session, seeded):
    """After a platform admin overrides, the row attributes the limit to them.

    The audit question is who set the limit that is in force NOW, not who set the
    first one ever.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "100.00"})
    assert (await stored_caps(session))[0].authored_by_user_id == PLATFORM_ADMIN_CANONICAL

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "50.00"})
    assert (await stored_caps(session))[0].authored_by_user_id == PLATFORM_ADMIN_CANONICAL


async def test_t10d_delete_removes_the_limit(session, seeded):
    """Removing a limit is a DELETE, and the subsequent read is ``uncapped``."""
    await seed_cap(session, PERSON_ANCHOR, "100.00")

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        deleted = await client.delete(f"/budget/person-cap/{PERSON_ANCHOR}")
        after = await client.get("/me/budget/person-cap")

    assert deleted.status_code == 204, deleted.text
    assert after.json()["cap_status"] == "uncapped"
    assert await stored_caps(session) == []


async def test_t10e_delete_is_idempotent(session, seeded):
    """A delete with no row is still ``204``.

    The outcome the caller asked for ("I have no personal limit") holds either way,
    and a 404 would make a retried delete look like a failure.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        first = await client.delete(f"/budget/person-cap/{PERSON_ANCHOR}")
        second = await client.delete(f"/budget/person-cap/{PERSON_ANCHOR}")

    assert first.status_code == second.status_code == 204


async def test_t10f_delete_only_removes_the_callers_own_row(session, seeded):
    """A colleague's cap survives the caller's delete.

    The decoy proves the delete predicate is anchored: a missing anchor filter
    would empty the table for everybody.
    """
    await seed_cap(session, PERSON_ANCHOR, "100.00")
    await seed_cap(session, COLLEAGUE_ANCHOR, "900.00")

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.delete(f"/budget/person-cap/{PERSON_ANCHOR}")

    rows = await stored_caps(session)
    assert [row.person_anchor for row in rows] == [COLLEAGUE_ANCHOR]


async def test_t10g_delete_only_removes_the_named_period(session, seeded):
    """Deleting the monthly limit leaves the daily one alone."""
    await seed_cap(session, PERSON_ANCHOR, "10.00", period_type="daily")
    await seed_cap(session, PERSON_ANCHOR, "100.00", period_type="monthly")

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.delete(f"/budget/person-cap/{PERSON_ANCHOR}?period_type=monthly")

    rows = await stored_caps(session)
    assert [row.period_type for row in rows] == ["daily"]


async def test_t10h_amounts_round_trip_at_two_decimal_places(session, seeded):
    """Money is a string at the column's precision, not a float.

    A float on the wire loses the precision the ``NUMERIC(10,2)`` column holds —
    contract rule 1, and the defect class migration 030 exists for.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "1234.50"})

    body = response.json()
    assert body["cap_usd"] == "1234.50"
    assert isinstance(body["cap_usd"], str)


# ===========================================================================
# T11 — ledger faults: 503, never a 200 reading as "no limit"
# ===========================================================================


async def test_t11_read_fault_is_503_not_uncapped(session, seeded):
    """An outage must not be reported as "you have no limit".

    Returning the uncapped shape here would tell a person they are unlimited at the
    one moment we cannot know that (FR-1.7). Same rule as the rest of the budget
    read surface.
    """
    app = build_app(session, context_for(PERSON_SUB))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch(
            "src.budget.person_cap_routes.resolve_applicable_person_limits",
            side_effect=OperationalError("SELECT", {}, Exception("db down")),
        ):
            response = await client.get("/me/budget/person-cap")

    assert response.status_code == 503, response.text
    assert "not a report" in response.json()["detail"]


async def test_t11b_write_fault_is_503_not_a_false_success(session, seeded):
    """A failed write must not answer 200.

    A 200 would leave the person believing a limit is in force when nothing was
    stored — the inverse of the read case and equally misleading.
    """
    app = build_app(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch(
            "src.budget.person_cap_routes._upsert_cap",
            side_effect=OperationalError("INSERT", {}, Exception("db down")),
        ):
            response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 503, response.text
    assert await stored_caps(session) == []


# ===========================================================================
# Wiring — an unregistered router is a 404 for every authenticated caller
# ===========================================================================


def test_router_is_registered_in_the_app():
    """Registration in ``UNIT_MODULES`` is what makes the routes reachable."""
    from src.app import UNIT_MODULES

    assert "src.budget.person_cap_routes" in UNIT_MODULES


def test_routes_carry_no_api_prefix():
    """CloudFront strips the first ``/api`` before the origin.

    A router mounting under ``/api/...`` is unreachable through the dashboard
    (#4330). The browser calls ``/api/me/budget/person-cap``; this router must
    serve ``/me/budget/person-cap``.
    """
    for route in person_cap_router.routes:
        assert not route.path.startswith("/api"), f"{route.path} must not carry an /api prefix"


def test_the_only_targeted_route_is_the_platform_admin_one():
    """No second route names a person.

    The unit's authority model rests on there being exactly one target-accepting
    path, gated on ``require_platform_admin``. A new route with a
    ``{person_anchor}`` path parameter would need its own gate, and this test is
    what forces that decision to be explicit rather than inherited by accident.
    """
    targeted = sorted({route.path for route in person_cap_router.routes if "{person_anchor}" in route.path})
    assert targeted == ["/budget/person-cap/{person_anchor}"]

    other_paths = {route.path for route in person_cap_router.routes} - set(targeted)
    # `{scope}` (#4690) names a SCOPE, not a person, so it is not a second targeted
    # path in the sense above — but it accepts a target all the same and carries the
    # same gate, pinned by `test_every_scope_route_requires_platform_admin` below.
    #
    # `/admin/organizations/{org_id}/member-budgets` (#4847) names an ORG, and returns
    # a row per member of it — so it discloses, in bulk, the same partition-free
    # figure the anchor route does one at a time. This pin firing on it is the pin
    # working: it forced the gate to be chosen deliberately, and the choice was the
    # same `require_platform_admin`, pinned by
    # `test_member_budgets_route_requires_platform_admin` below. It is listed here
    # rather than under `targeted` because it accepts no person parameter.
    assert other_paths == {
        "/me/budget/person-cap",
        "/budget/person-default/{scope}",
        "/admin/organizations/{org_id}/member-budgets",
    }


def test_every_scope_route_requires_platform_admin():
    """Every ``{scope}``-accepting route calls ``require_platform_admin`` — #4690.

    The defaults routes are the second family that accepts a target, so they need
    the same structural pin the anchor route has. An ORG-scoped default authored by
    that org's own admin would still govern its members' spend in every OTHER tenant
    they work in, which is the authority inversion §4.2 forbids — so the gate here is
    not "admin-ish", it is specifically platform admin.

    Source-level rather than behavioural because the behavioural 403 tests can only
    cover the handlers that exist today; this one fails the moment a fourth scope
    route is added without the gate.
    """
    import inspect

    from src.budget import person_cap_routes as module

    scope_handlers = [module.get_person_default, module.put_person_default, module.delete_person_default]
    for handler in scope_handlers:
        source = inspect.getsource(handler)
        assert "require_platform_admin(current_user)" in source, f"{handler.__name__} must be platform-admin gated"

    # And no scope route was added that this list forgot.
    scope_paths = {route.path for route in person_cap_router.routes if "{scope}" in route.path}
    assert scope_paths == {"/budget/person-default/{scope}"}
    scope_route_count = sum(len(route.methods - {"HEAD", "OPTIONS"}) for route in person_cap_router.routes if "{scope}" in route.path)
    assert scope_route_count == len(scope_handlers)


async def test_first_time_put_race_loser_updates_instead_of_503(session, seeded, monkeypatch):
    """Two concurrent first-time PUTs must both succeed, not 503 the loser.

    Review fix on #4647: the read-then-insert upsert raced — both requests read
    ``None``, both INSERTed, and the loser's ``uq_person_budget_config``
    IntegrityError fell into ``_INFRASTRUCTURE_FAULTS`` and was reported as a
    backend outage ("nothing has changed") while the winner had stored exactly
    that limit. Simulated by making the pre-insert read miss the row the "winner"
    has already committed, which is precisely what the loser of the race sees.
    """
    from src.budget import person_cap_routes as module

    real_read = module._read_cap_row
    calls = {"n": 0}

    async def racing_read(db, person_anchor, period_type):
        calls["n"] += 1
        if calls["n"] == 1:
            # The loser's read: the winner's row is not visible yet.
            winner = await real_read(db, person_anchor, period_type)
            if winner is None:
                await seed_cap(session, person_anchor, "77.00", period_type=period_type)
            return None
        return await real_read(db, person_anchor, period_type)

    monkeypatch.setattr(module, "_read_cap_row", racing_read)

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 200, response.text
    rows = await stored_caps(session)
    assert len(rows) == 1
    assert str(rows[0].budget_amount_usd) in ("100.00", "100.0000", "100")


async def test_over_range_amount_is_a_422_not_a_db_overflow(session, seeded):
    """An amount past NUMERIC(10,2) must die at validation, not in Postgres.

    Review fix on #4647: without ``le`` the value passed pydantic and overflowed
    the column — a deterministic input error misreported as a retryable 503
    (and invisible to SQLite-backed tests, which ignore NUMERIC precision).
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "99999999999.00"})
    assert response.status_code == 422
    assert await stored_caps(session) == []


async def test_two_github_rows_author_and_enforce_under_the_same_anchor(session, seeded):
    """A person with TWO linked GitHub rows gets ONE deterministic anchor everywhere.

    Review fix on #4661: ``user_identities`` has no unique constraint on
    ``(user_id, provider)``, and authoring and enforcement each picked a row with
    an unordered ``LIMIT 1`` — two independent picks that can disagree, storing a
    cap under ``github:A`` that enforcement looks up as ``github:B`` (an inert
    hard cap, the #4511 class). Both sides now order by ``provider_user_id``;
    this pins that the ROUTE-stored anchor equals the ENFORCEMENT-side
    resolution for the same person.

    #4843 changed nothing about this test's substance — only the rename to
    ``resolve_person_anchor_identity``, which returns ``(provider, identifier)`` so
    the precedence walk can report WHICH namespace it landed in. The two-row state
    seeded below stays legal on purpose: ``(user_id, provider)`` is deliberately not
    plainly unique (three write paths accept a legitimate second account), which is
    why migration 042 ships a PARTIAL unique index on ``is_primary`` rather than the
    plain constraint. The property pinned here is unchanged and is the one that
    matters: both sides resolve the SAME row.
    """
    from src.budget.person_ledger import resolve_person_anchor_identity as _resolve_person_anchor_identity
    from src.shared.models.vault import UserIdentity

    # A second, later-linked GitHub account for the same person. Its id sorts
    # AFTER the seeded one, so an unordered pick could return either.
    session.add(
        UserIdentity(
            user_id=PERSON_CANONICAL,
            org_id=ORG_ID,
            team_id=TEAM_ID,
            provider=IdentityProvider.github.value,
            provider_user_id="99999999",
            provider_username="second-account",
            verification_method="admin_link",
        )
    )
    await session.commit()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "42.00"})
    assert response.status_code == 200

    stored_anchor = (await stored_caps(session))[0].person_anchor
    enforcement_provider, enforcement_anchor_id = await _resolve_person_anchor_identity(session, PERSON_CANONICAL)

    assert stored_anchor == f"{enforcement_provider}:{enforcement_anchor_id}", (
        "authoring and enforcement resolved DIFFERENT anchors for one person — the cap is inert"
    )


# ===========================================================================
# D1 — default person limits at platform/org/team scope, and the CEILING rule
# they impose on self-service. Issue #4690.
#
# The authority tests come first here for the same reason T1-T3 do above: the
# whole question this route family raises is *who may bound a population*. An
# org-scoped default authored by that org's OWN admin would still govern its
# members' spend in every OTHER tenant they work in — the §4.2 authority
# inversion, one scope wider. Only a platform admin may author any rung.
# ===========================================================================


async def stored_defaults(session: AsyncSession) -> list[PersonBudgetDefault]:
    """Every default row, so a test can assert on what was (or was not) written."""
    session.expire_all()
    result = await session.scalars(
        sa.select(PersonBudgetDefault).order_by(
            PersonBudgetDefault.scope_type,
            PersonBudgetDefault.period_type,
        )
    )
    return list(result)


async def seed_default(
    session: AsyncSession,
    *,
    scope_type: str,
    amount: str,
    scope_id_org: str | None = None,
    scope_id_team: str | None = None,
    period_type: str = "monthly",
) -> PersonBudgetDefault:
    row = PersonBudgetDefault(
        scope_type=scope_type,
        scope_id_org=scope_id_org,
        scope_id_team=scope_id_team,
        period_type=period_type,
        budget_amount_usd=Decimal(amount),
        enforcement_mode="hard",
        authored_by_user_id="seed",
    )
    session.add(row)
    await session.commit()
    return row


# ---------------------------------------------------------------------------
# D1a — authority on all three verbs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["platform", f"org:{ORG_ID}", f"team:{ORG_ID}:{TEAM_ID}"])
async def test_d1_org_admin_cannot_author_a_default_at_any_scope(session, seeded, scope):
    """403 on every rung — including the org admin's OWN org and team.

    The inversion in full: this org's members spend in other tenants too, and a
    default authored here would follow them there, invisibly to those tenants'
    admins. ``org:`` and ``team:`` are parameterized alongside ``platform`` precisely
    so "surely their own org is fine" cannot creep in as a special case.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.put(f"/budget/person-default/{scope}", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 403
    assert await stored_defaults(session) == []


@pytest.mark.parametrize("scope", ["platform", f"org:{ORG_ID}"])
async def test_d1b_plain_member_cannot_author_a_default(session, seeded, scope):
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put(f"/budget/person-default/{scope}", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 403
    assert await stored_defaults(session) == []


async def test_d1c_org_admin_cannot_read_a_default(session, seeded):
    """Reads are gated too, not just writes.

    A rung's amount is a governance decision about a population; an org admin who
    can read the platform default learns the ceiling every tenant is held to. And a
    readable-but-unwritable surface invites the UI to render an editor that 403s.
    """
    await seed_default(session, scope_type="platform", amount="100.00")

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get("/budget/person-default/platform", params={"period_type": "monthly"})

    assert response.status_code == 403


async def test_d1d_org_admin_cannot_delete_a_default(session, seeded):
    """DELETE is the most damaging verb here — it silently unbounds a population.

    Nothing errors afterwards: every request simply starts succeeding again, so an
    ungated delete is an outage in the opposite direction and nobody notices until
    the bill.
    """
    await seed_default(session, scope_type="platform", amount="100.00")

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.delete("/budget/person-default/platform", params={"period_type": "monthly"})

    assert response.status_code == 403
    assert len(await stored_defaults(session)) == 1


async def test_d1e_forged_admin_claim_does_not_grant_authority(session, seeded):
    """Authority comes from the membership row, never from the token.

    ``is_admin`` is set on the CONTEXT here — the shape a caller would forge — while
    ``tenant_memberships`` still says ``org_admin``. Mirrors T2c for the anchor route:
    if this passed, every 403 above would be asserting a claim the caller controls.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB, is_admin=False)) as client:
        response = await client.put(
            "/budget/person-default/platform",
            json={"budget_amount_usd": "100.00"},
        )

    assert response.status_code == 403
    assert await stored_defaults(session) == []


# ---------------------------------------------------------------------------
# D1b — the platform admin's CRUD, one rung at a time
# ---------------------------------------------------------------------------


async def test_d2_platform_admin_authors_a_platform_default(session, seeded):
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put("/budget/person-default/platform", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_type"] == "platform"
    assert body["cap_status"] == "capped"
    assert body["cap_usd"] == "100.00"

    rows = await stored_defaults(session)
    assert len(rows) == 1
    assert (rows[0].scope_type, rows[0].scope_id_org, rows[0].scope_id_team) == ("platform", None, None)


async def test_d2b_platform_admin_authors_an_org_default(session, seeded):
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-default/org:{ORG_ID}", json={"budget_amount_usd": "500.00"})

    assert response.status_code == 200, response.text
    rows = await stored_defaults(session)
    assert (rows[0].scope_type, rows[0].scope_id_org, rows[0].scope_id_team) == ("org", ORG_ID, None)


async def test_d2c_platform_admin_authors_a_team_default_with_both_halves(session, seeded):
    """A team default stores its ORG as well as its team.

    ``teams`` carries ``TenantMixin``, so a ``teams.id`` is unique only inside its org.
    A row storing the team alone would govern a same-id team in an unrelated tenant —
    the #4511 wrong-key class in its damaging direction.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-default/team:{ORG_ID}:{TEAM_ID}", json={"budget_amount_usd": "50.00"})

    assert response.status_code == 200, response.text
    rows = await stored_defaults(session)
    assert (rows[0].scope_type, rows[0].scope_id_org, rows[0].scope_id_team) == ("team", ORG_ID, TEAM_ID)


async def test_d2d_a_default_is_always_stored_hard(session, seeded):
    """``enforcement_mode`` is not client-settable and is never ``soft``.

    A default authored to bound a population that silently does not enforce is the
    #4511 inert-cap class at platform scale: the operator believes everybody is
    bounded and nothing stops anyone. Mirrors T7c for the individual route.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(
            "/budget/person-default/platform",
            json={"budget_amount_usd": "100.00", "enforcement_mode": "soft"},
        )

    assert response.status_code == 200, response.text
    assert (await stored_defaults(session))[0].enforcement_mode == "hard"


async def test_d2e_the_author_is_recorded_as_the_canonical_user_id(session, seeded):
    """``authored_by_user_id`` is the canonical ``users.id`` — the #4647 contract.

    A Cognito sub here would make the audit column un-joinable to ``users`` and
    incomparable with ``person_budget_configs.authored_by_user_id``, which is what
    the self route's ``own``-vs-``admin`` split is computed from.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.put("/budget/person-default/platform", json={"budget_amount_usd": "100.00"})

    assert (await stored_defaults(session))[0].authored_by_user_id == PLATFORM_ADMIN_CANONICAL


async def test_d3_reading_an_unset_default_is_uncapped_not_zero(session, seeded):
    """No rule at this rung reads as ``uncapped``, never ``$0``.

    ``$0`` at a governance rung would render as "everybody is blocked" — the most
    alarming possible reading of "nothing is configured", and the shape the
    individual route's T4c pins for the same reason.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get("/budget/person-default/platform", params={"period_type": "monthly"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cap_status"] == "uncapped"
    assert body["cap_usd"] is None


async def test_d3b_each_rung_is_read_independently(session, seeded):
    """A platform default does not leak into the org rung's read.

    This route reports what is stored AT the named scope, not what would apply to
    somebody there — the ladder is enforcement's job. An admin editing the org rung
    must not be shown the platform figure in the field, or their first save silently
    copies it down a rung.
    """
    await seed_default(session, scope_type="platform", amount="100.00")

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        platform = await client.get("/budget/person-default/platform", params={"period_type": "monthly"})
        org = await client.get(f"/budget/person-default/org:{ORG_ID}", params={"period_type": "monthly"})

    assert platform.json()["cap_usd"] == "100.00"
    assert org.json()["cap_status"] == "uncapped"


async def test_d3c_each_period_is_authored_independently(session, seeded):
    """daily/weekly/monthly at one scope are three rules, not one.

    Without ``period_type`` in the key an operator setting a daily ceiling would
    silently destroy their monthly one.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.put("/budget/person-default/platform", params={"period_type": "daily"}, json={"budget_amount_usd": "10.00"})
        await client.put("/budget/person-default/platform", params={"period_type": "monthly"}, json={"budget_amount_usd": "300.00"})

    rows = await stored_defaults(session)
    assert {row.period_type: str(row.budget_amount_usd) for row in rows} == {"daily": "10.00", "monthly": "300.00"}


async def test_d4_re_authoring_replaces_in_place(session, seeded):
    """A second PUT at the same rung UPDATES rather than inserting a duplicate.

    The nullable scope columns make this the trap: a predicate written ``== None``
    matches nothing in SQL, so every re-author of a PLATFORM default would attempt an
    INSERT and trip the unique index. Tested on the platform rung deliberately, where
    both scope columns are NULL.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        first = await client.put("/budget/person-default/platform", json={"budget_amount_usd": "100.00"})
        second = await client.put("/budget/person-default/platform", json={"budget_amount_usd": "40.00"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    rows = await stored_defaults(session)
    assert len(rows) == 1
    assert str(rows[0].budget_amount_usd) == "40.00"


async def test_d4b_delete_removes_only_that_rung_and_period(session, seeded):
    await seed_default(session, scope_type="platform", amount="100.00")
    await seed_default(session, scope_type="platform", amount="10.00", period_type="daily")
    await seed_default(session, scope_type="org", scope_id_org=ORG_ID, amount="500.00")

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.delete("/budget/person-default/platform", params={"period_type": "monthly"})

    assert response.status_code == 204
    remaining = {(row.scope_type, row.period_type) for row in await stored_defaults(session)}
    assert remaining == {("platform", "daily"), ("org", "monthly")}


async def test_d4c_delete_is_idempotent(session, seeded):
    """Deleting an absent rule is a 204, not a 404.

    The caller's intent ("no default here") is satisfied either way, and a 404 would
    make a retried delete look like a failure.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.delete("/budget/person-default/platform", params={"period_type": "monthly"})

    assert response.status_code == 204


@pytest.mark.parametrize(
    "bad_scope",
    [
        "org",  # no org id
        "team",  # no ids at all
        f"team:{ORG_ID}",  # team id missing — would match a same-id team elsewhere
        "department:dept-1",  # an explicit non-goal of #4690
        f"org:{ORG_ID}:extra",
        "",
    ],
)
async def test_d5_malformed_scopes_are_422_and_write_nothing(session, seeded, bad_scope):
    """A scope that does not name exactly one rung is rejected.

    ``team:<org>`` is the dangerous one: silently defaulting the missing half would
    store a rule matching on the team id alone, governing a same-id team in an
    unrelated tenant. ``department:`` is a declared non-goal, and accepting it would
    store a row the ladder never walks — authored, displayed, governing nobody.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-default/{bad_scope}", json={"budget_amount_usd": "100.00"})

    assert response.status_code in (404, 422), response.text
    assert await stored_defaults(session) == []


@pytest.mark.parametrize("bad_period", ["run", "chain", "hourly", ""])
async def test_d5b_non_calendar_periods_are_422(session, seeded, bad_period):
    """``run``/``chain`` have no calendar window and would fault the person layer.

    ``get_period_start_end`` RAISES for them, and the fault lands in enforcement's
    containment wrapper where it is swallowed into an allow — so one junk row would
    silently disable the ladder for everybody it matched (#4328).
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(
            "/budget/person-default/platform",
            params={"period_type": bad_period},
            json={"budget_amount_usd": "100.00"},
        )

    assert response.status_code == 422
    assert await stored_defaults(session) == []


@pytest.mark.parametrize("bad_amount", ["0", "0.00", "-5.00", "1.234", "99999999999.00"])
async def test_d5c_invalid_amounts_are_422(session, seeded, bad_amount):
    """A rung's amount must be a positive 2dp figure inside NUMERIC(10,2).

    ``0`` is rejected rather than stored: a zero ceiling for a whole population is
    indistinguishable from "block everybody", and DELETE is the way to remove a rule.
    The over-range case must die at validation, not overflow the column and be
    misreported as a retryable 503 (the #4647 lesson).
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put("/budget/person-default/platform", json={"budget_amount_usd": bad_amount})

    assert response.status_code == 422
    assert await stored_defaults(session) == []


async def test_d5d_authority_is_checked_before_the_scope_is_parsed(session, seeded):
    """A non-admin sending a malformed scope gets 403, not 422.

    Order matters: 422-before-403 turns the parser into an oracle a non-admin can use
    to probe which rungs exist. Mirrors T9c on the anchor route.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/budget/person-default/not-a-scope", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 403


async def test_d6_read_fault_is_503_not_a_false_not_set(session, seeded):
    """An unreadable defaults table is 503 — never a 200 reading as "no default".

    The #4392 ambiguity at a governance rung: an operator refreshing the page during a
    DB blip would be told no ceiling is configured and would go set one, overwriting
    the rule that is actually live.
    """
    with patch(
        "src.budget.person_cap_routes._read_default_row",
        side_effect=OperationalError("SELECT", {}, Exception("connection reset")),
    ):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/budget/person-default/platform", params={"period_type": "monthly"})

    assert response.status_code == 503
    # And the message must not read as a report that nothing is set.
    assert "not a report" in response.json()["detail"].lower() or "unavailable" in response.json()["detail"].lower()


async def test_d6b_first_time_put_race_loser_updates_instead_of_503(session, seeded, monkeypatch):
    """Two concurrent first-time PUTs at one rung both succeed.

    The same read-then-insert race #4647 fixed on the individual route, which the
    defaults upsert reproduces because it has the same shape. The loser's unique-index
    IntegrityError must retry as an UPDATE rather than surface as a backend outage
    while the winner's row sits committed.
    """
    from src.budget import person_cap_routes as module

    real_read = module._read_default_row
    calls = {"n": 0}

    async def racing_read(db, scope_type, scope_id_org, scope_id_team, period_type):
        calls["n"] += 1
        if calls["n"] == 1:
            winner = await real_read(db, scope_type, scope_id_org, scope_id_team, period_type)
            if winner is None:
                await seed_default(session, scope_type=scope_type, amount="77.00", period_type=period_type)
            return None
        return await real_read(db, scope_type, scope_id_org, scope_id_team, period_type)

    monkeypatch.setattr(module, "_read_default_row", racing_read)

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put("/budget/person-default/platform", json={"budget_amount_usd": "100.00"})

    assert response.status_code == 200, response.text
    rows = await stored_defaults(session)
    assert len(rows) == 1
    assert str(rows[0].budget_amount_usd) == "100.00"


# ---------------------------------------------------------------------------
# D1c — the CEILING rule on self-service. The security property of this issue.
# ---------------------------------------------------------------------------


async def test_d8_platform_admin_may_author_an_individual_row_above_a_default(session, seeded):
    """The escape hatch, and the reason the check lives on the SELF route only.

    A platform admin granting one person a higher allowance than the population
    default is the whole point of having an individual rung above the defaults. The
    admin route deliberately applies no ceiling: they authored the default, so
    re-validating their own rule against itself would leave no way to grant an
    exception short of raising the ceiling for everybody.
    """
    await seed_default(session, scope_type="platform", amount="100.00")

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "5000.00"})

    assert response.status_code == 200, response.text
    assert str((await stored_caps(session))[0].budget_amount_usd) == "5000.00"


# ---------------------------------------------------------------------------
# D1d — the self READ reports which rung governs the caller
# ---------------------------------------------------------------------------


async def test_d9_self_read_reports_a_platform_default_as_the_applicable_limit(session, seeded):
    """No personal row, but a default applies → ``capped``, with ``source``.

    Reporting ``uncapped`` here is the #4690 defect on the read surface: the person is
    told they are unlimited and then stopped by a 402 at that exact figure. The
    ``source`` is what lets the UI say "set by your platform administrator" instead of
    offering an editor that will 422.
    """
    await seed_default(session, scope_type="platform", amount="100.00")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cap_status"] == "capped"
    assert body["cap_usd"] == "100.00"
    assert body["source"] == "platform_default"
    assert "platform default" in body["source_label"]


@pytest.mark.parametrize(
    ("scope_type", "scope_id_org", "scope_id_team", "expected_source"),
    [
        ("platform", None, None, "platform_default"),
        ("org", ORG_ID, None, "org_default"),
        ("team", ORG_ID, TEAM_ID, "team_default"),
    ],
)
async def test_d9b_every_default_rung_reports_its_own_source(session, seeded, scope_type, scope_id_org, scope_id_team, expected_source):
    """Each rung is named distinctly on the wire.

    A single collapsed ``"default"`` value would leave a person unable to tell whether
    to ask their team lead or the platform team — and the org/team labels carry the id
    for exactly that reason.
    """
    await seed_default(session, scope_type=scope_type, scope_id_org=scope_id_org, scope_id_team=scope_id_team, amount="100.00")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})

    assert response.json()["source"] == expected_source


async def test_d9c_a_self_authored_row_reports_source_own(session, seeded):
    """``own`` is what the UI turns an editable field on.

    The distinction from ``admin`` is the difference between "lower this yourself" and
    "ask a platform admin", which is the ceiling rule expressed on the read surface.
    """
    await seed_cap(session, PERSON_ANCHOR, "25.00", authored_by=PERSON_CANONICAL)

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})

    body = response.json()
    assert body["source"] == "own"
    assert body["cap_usd"] == "25.00"


async def test_d9d_an_admin_authored_row_reports_source_admin(session, seeded):
    """A row the person did not write must not be reported as theirs.

    Telling them ``own`` would advertise an editor for a number only a platform admin
    can change — and their attempt to lower it would succeed, quietly discarding the
    admin's grant.
    """
    await seed_cap(session, PERSON_ANCHOR, "5000.00", authored_by=PLATFORM_ADMIN_CANONICAL)

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})

    assert response.json()["source"] == "admin"


async def test_d9e_a_personal_row_is_reported_over_the_default(session, seeded):
    await seed_default(session, scope_type="platform", amount="100.00")
    await seed_cap(session, PERSON_ANCHOR, "25.00", authored_by=PERSON_CANONICAL, enforcement_mode="hard")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})

    body = response.json()
    assert body["cap_usd"] == "25.00"
    assert body["source"] == "own"


async def test_d9f_no_rule_at_any_rung_still_reads_as_not_set(session, seeded):
    """Genuinely unlimited stays ``uncapped`` with a null ``source``.

    ``source`` must not acquire a "nothing" sentinel string — ``uncapped`` already says
    it, and a second way to say the same thing is a second thing for a client to get
    wrong.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})

    body = response.json()
    assert body["cap_status"] == "uncapped"
    assert body["cap_usd"] is None
    assert body["source"] is None


async def test_d9g_a_default_for_another_period_does_not_cap_this_one(session, seeded):
    """The read is per period, exactly as the ladder is.

    A daily default must not be reported when the caller asked about monthly, or the
    UI shows one figure against the wrong window and the person budgets against a
    number nothing enforces.
    """
    await seed_default(session, scope_type="platform", amount="10.00", period_type="daily")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        monthly = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})
        daily = await client.get("/me/budget/person-cap", params={"period_type": "daily"})

    assert monthly.json()["cap_status"] == "uncapped"
    assert daily.json()["cap_usd"] == "10.00"


async def test_d9h_the_self_read_is_still_the_callers_own(session, seeded):
    """No target parameter reaches this route, defaults or not (T8, re-pinned).

    The route now resolves the caller's orgs and teams, which is new input to a
    surface whose entire contract is "self only". A caller who could nominate their
    active org could nominate the org with the most generous default and lift their
    own ceiling.
    """
    await seed_default(session, scope_type="platform", amount="100.00")
    await seed_default(session, scope_type="org", scope_id_org=OTHER_ORG_ID, amount="9000.00")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get(
            "/me/budget/person-cap",
            params={"period_type": "monthly", "org_id": OTHER_ORG_ID, "person_anchor": COLLEAGUE_ANCHOR},
        )

    body = response.json()
    assert body["person_anchor"] == PERSON_ANCHOR
    assert body["cap_usd"] == "100.00"


async def test_d9h_a_soft_legacy_row_does_not_displace_a_hard_default(session, seeded):
    """A C3-era `soft` row must not exempt its holder from a hard default.

    Review fix on #4696: `soft` warns and never denies, so letting it shadow the
    hard rung would leave one person silently unlimited while every peer is
    denied at the default. The GOVERNING limit — the default — is what the read
    surface reports, because this endpoint's contract is "the number that stops
    you", not "the most specific row that exists".
    """
    await seed_default(session, scope_type="platform", amount="100.00")
    await seed_cap(session, PERSON_ANCHOR, "50000.00", authored_by=PERSON_CANONICAL, enforcement_mode="soft")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get("/me/budget/person-cap", params={"period_type": "monthly"})

    body = response.json()
    assert body["cap_usd"] == "100.00"
    assert body["source"] == "platform_default"


async def test_d2h_a_default_aimed_at_a_nonexistent_scope_is_422(session, seeded):
    """A rule that would govern nobody is refused at write time (review fix, #4696).

    Stored, it reads back 'capped', logs 'authored', and matches nothing — the
    #4511 inert-cap class on the governance surface itself.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        wrong_org = await client.put("/budget/person-default/org:no-such-org", json={"budget_amount_usd": "500.00"})
        empty_team = await client.put(f"/budget/person-default/team:{ORG_ID}:no-such-team", json={"budget_amount_usd": "500.00"})

    assert wrong_org.status_code == 422
    assert "govern nobody" in wrong_org.json()["detail"]
    assert empty_team.status_code == 422
    from sqlalchemy import select as _select

    rows = (await session.execute(_select(PersonBudgetDefault))).scalars().all()
    assert rows == []


# ===========================================================================
# The Members panel's spend-against-limit list — Issue #4847 (#4839 · T2b)
#
# Same authority model as the anchor route, for the same reason, and that is the
# first thing asserted: this route returns a partition-free limit for a whole page
# of members, so if an org admin could call it, the #4620 inversion would arrive in
# bulk rather than one person at a time.
# ===========================================================================


def test_member_budgets_route_requires_platform_admin():
    """The structural pin, matching ``test_every_scope_route_requires_platform_admin``.

    Source-level because the behavioural 403s below can only cover today's callers.
    The gate on this route is the load-bearing decision of #4847's backend half —
    the whole reason the panel treats the spend column as a platform-admin
    affordance while the Members tab itself is ``ORG_READ`` — so it gets a check
    that fails if a later edit swaps it for an org-scoped `check_permission`, which
    would read as *stricter* code while actually widening disclosure.
    """
    import inspect

    from src.budget import person_cap_routes as module

    source = inspect.getsource(module.list_member_budgets)
    assert "require_platform_admin(current_user)" in source
    # And it is the FIRST authority statement, before the org is read — so a
    # non-admin cannot use a 404-vs-403 difference to enumerate organizations.
    assert source.index("require_platform_admin") < source.index("select(func.count())")


async def test_member_budgets_denied_to_org_admin(session, seeded):
    """An org admin is refused — the #4620 ruling, applied to the list shape.

    They legitimately administer this org's membership (that is T2b's panel), but
    the limit in each row can come from a partition-free individual row, so serving
    it here would disclose ceilings governing their members' spend in tenants the
    org admin has no membership in. Denied for their OWN org's members, which is the
    case someone would reach for as the exception.
    """
    await seed_cap(session, PERSON_ANCHOR, "75.00")

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    assert response.status_code == 403, response.text
    assert "75.00" not in response.text, "a denial must not leak the limits it refused to list"


async def test_member_budgets_denied_to_plain_member(session, seeded):
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    assert response.status_code == 403, response.text


async def test_member_budgets_reports_spend_against_the_applicable_limit(session, seeded):
    """The panel's column: settled month spend, the governing limit, and its rung.

    The individual row is seeded for one person only, so the OTHER seeded members
    must come back on a different rung (or uncapped) — a route that reported one
    person's limit for everybody would pass a single-row assertion.
    """
    await seed_cap(session, PERSON_ANCHOR, "75.00")
    period_start, _ = get_period_start_end(PeriodType.MONTHLY)
    session.add(
        BudgetUsage(
            org_id=ORG_ID,
            entity_type="root_user",
            entity_id=PERSON_CANONICAL,
            period_start=period_start,
            period_type="monthly",
            total_cost_usd=Decimal("38.500000"),
        )
    )
    await session.commit()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["period_type"] == "monthly"
    assert body["period_start"] == period_start.isoformat()

    rows = {row["user_id"]: row for row in body["items"]}
    person = rows[PERSON_CANONICAL]
    # Spend at the ledger column's 6dp, the limit at the cap column's 2dp — each at
    # its own precision (contract rule 1), not a shared one.
    assert person["spend_usd"] == "38.500000"
    assert person["limit_usd"] == "75.00"
    assert person["limit_status"] == "capped"
    assert person["person_anchor"] == PERSON_ANCHOR
    # `admin`: the row was authored by somebody other than the member (seed_cap's
    # default author), so it is an admin grant. A row the member authored on
    # THEMSELVES reports `own` — the two labels are pinned per rung below.
    assert person["source"] == "admin"
    assert person["source_label"] == "individual limit"

    colleague = rows[COLLEAGUE_CANONICAL]
    assert colleague["spend_usd"] == "0.000000", "no settled row is a true zero, not a fallback"
    assert colleague["limit_usd"] is None
    assert colleague["limit_status"] == "uncapped"
    assert colleague["source"] is None, "nothing governs them; there is no rung to name"


async def test_member_budgets_uncapped_is_not_a_zero_limit(session, seeded):
    """Contract rule 2 on this shape: absent is not ``0.00``.

    A zeroed limit renders as somebody who may spend nothing — the opposite of what
    no rule means — and it is the difference between a usage bar the panel must not
    draw and one showing 0%.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    assert response.status_code == 200, response.text
    for row in response.json()["items"]:
        assert row["limit_usd"] is None
        assert row["limit_status"] == "uncapped"
        assert row["source_label"] is None


async def test_member_budgets_reports_a_default_as_the_governing_limit(session, seeded):
    """A member with no individual row is still governed — by the org default.

    The rung matters to the panel: it labels the number's provenance, and rendering
    a default as if it were set for that one person invites an admin to explain a
    figure nobody authored for them.
    """
    session.add(
        PersonBudgetDefault(
            scope_type="org",
            scope_id_org=ORG_ID,
            period_type="monthly",
            budget_amount_usd=Decimal("50.00"),
            enforcement_mode="hard",
            authored_by_user_id="seed",
        )
    )
    await session.commit()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    assert response.status_code == 200, response.text
    rows = {row["user_id"]: row for row in response.json()["items"]}
    colleague = rows[COLLEAGUE_CANONICAL]
    assert colleague["limit_usd"] == "50.00"
    assert colleague["limit_status"] == "capped"
    assert colleague["source"] == "org_default"
    assert colleague["source_label"] == "org default", "the v4 contract's third-person label, not the ladder's second-person prose"


async def test_member_budgets_is_scoped_to_the_org_in_the_path(session, seeded):
    """Only members of ``{org_id}``, and only that partition's spend.

    The spend half is deliberately org-scoped rather than the person's cross-org
    total: it is the one figure here that belongs to the organization being
    administered. A member of another tenant must not appear at all.
    """
    period_start, _ = get_period_start_end(PeriodType.MONTHLY)
    session.add(
        BudgetUsage(
            org_id=OTHER_ORG_ID,
            entity_type="root_user",
            entity_id=PERSON_CANONICAL,
            period_start=period_start,
            period_type="monthly",
            total_cost_usd=Decimal("999.000000"),
        )
    )
    await session.commit()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    body = response.json()
    assert FOREIGN_CANONICAL not in {row["user_id"] for row in body["items"]}, "a member of another org must not appear"
    person = next(row for row in body["items"] if row["user_id"] == PERSON_CANONICAL)
    assert person["spend_usd"] == "0.000000", "another partition's dollars are not this org's column"


async def test_member_budgets_read_fault_is_a_503_not_a_zero(session, seeded):
    """Rule 5 on this shape: a broken read never renders as "nobody has spent anything".

    A page of ``0.000000`` rows is indistinguishable from a quiet month, so a fault
    that surfaced as a 200 would show an admin a calm panel while the figures behind
    it were unknown.
    """
    with patch(
        "src.budget.person_cap_routes.read_person_partition_spend",
        side_effect=OperationalError("SELECT", {}, Exception("ledger unreachable")),
    ):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    assert response.status_code == 503, response.text


async def test_member_budgets_page_size_is_bounded(session, seeded):
    """The fan-out is per member, so an unbounded page is several hundred reads."""
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets?page_size=500")

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("rung", "expected_source", "expected_label"),
    [
        ("individual_admin", "admin", "individual limit"),
        ("individual_self", "own", "individual limit (self-set)"),
        ("team_default", "team_default", "team default"),
        ("org_default", "org_default", "org default"),
        ("platform_default", "platform_default", "platform default"),
    ],
)
async def test_member_budgets_source_labels_are_third_person(session, seeded, rung, expected_source, expected_label):
    """M1 (PR #4936 review): one pinned label per ``PersonLimitSource`` value.

    The ladder's own ``scope_label`` is second-person prose composed for the person
    the limit governs — forwarding it verbatim showed an admin "a limit set for YOU
    by a platform administrator" under somebody ELSE's spend. This surface maps the
    ``source`` enum to the v4 contract's third-person names instead, including the
    self-authored distinction the data allows (``authored_by_user_id`` matching the
    member's own canonical id).
    """
    if rung == "individual_admin":
        await seed_cap(session, PERSON_ANCHOR, "75.00", authored_by=PLATFORM_ADMIN_CANONICAL)
    elif rung == "individual_self":
        await seed_cap(session, PERSON_ANCHOR, "75.00", authored_by=PERSON_CANONICAL)
    else:
        scope_type = rung.removesuffix("_default")
        session.add(
            PersonBudgetDefault(
                scope_type=scope_type,
                scope_id_org=None if scope_type == "platform" else ORG_ID,
                scope_id_team=TEAM_ID if scope_type == "team" else None,
                period_type="monthly",
                budget_amount_usd=Decimal("50.00"),
                enforcement_mode="hard",
                authored_by_user_id="seed",
            )
        )
        await session.commit()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.get(f"/admin/organizations/{ORG_ID}/member-budgets")

    assert response.status_code == 200, response.text
    person = next(row for row in response.json()["items"] if row["user_id"] == PERSON_CANONICAL)
    assert person["source"] == expected_source
    assert person["source_label"] == expected_label
    # The defect class being pinned: prose addressed to the member shown to a third
    # party. No label on this surface may speak in the second person.
    assert "you" not in person["source_label"].lower()


@pytest.mark.parametrize("kind", ["person-cap", "person-default"])
async def test_cli_person_revision_crud(session, seeded, kind):
    """Real HTTP/DB conditional writes preserve later writers and absent state."""
    path = f"/budget/{kind}/" + (PERSON_ANCHOR if kind == "person-cap" else "platform")
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        created = await client.put(path, params={"expected_revision": "absent"}, json={"budget_amount_usd": "1.00"})
        assert created.status_code == 200, created.text
        revision = (await client.get(path)).json()["updated_at"]
        conflict = await client.put(path, params={"expected_revision": "absent"}, json={"budget_amount_usd": "9.00"})
        assert conflict.status_code == 409
        changed = await client.put(path, params={"expected_revision": revision}, json={"budget_amount_usd": "2.00"})
        assert changed.status_code == 200, changed.text
        assert (await client.delete(path, params={"expected_revision": revision})).status_code == 409
        current = await client.get(path)
        assert current.json()["cap_usd"] == "2.00"
        removed = await client.delete(path, params={"expected_revision": current.json()["updated_at"]})
        assert removed.status_code == 204, removed.text
        assert (await client.get(path)).json()["cap_status"] == "uncapped"


@pytest.mark.parametrize("kind", ["person-cap", "person-default"])
@pytest.mark.parametrize("method", ["put", "delete"])
async def test_cli_revision_detects_commit_between_read_and_write(session, engine, seeded, monkeypatch, kind, method):
    """A separate committed writer wins after review, before conditional DML."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.sql.dml import Delete, Update

    model = PersonBudgetConfig if kind == "person-cap" else PersonBudgetDefault
    path = f"/budget/{kind}/" + (PERSON_ANCHOR if kind == "person-cap" else "platform")
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        assert (await client.put(path, json={"budget_amount_usd": "1.00"})).status_code == 200
        revision = (await client.get(path)).json()["updated_at"]
        original_execute = session.execute
        raced = False

        async def execute(statement, *args, **kwargs):
            nonlocal raced
            if not raced and isinstance(statement, Update | Delete) and statement.table.name == model.__tablename__:
                raced = True
                async with async_sessionmaker(engine, expire_on_commit=False)() as other:
                    await other.execute(
                        sa.update(model).values(budget_amount_usd=Decimal("7.00"), updated_at=datetime.now(UTC) + timedelta(seconds=1))
                    )
                    await other.commit()
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", execute)
        kwargs = dict(params={"expected_revision": revision})
        if method == "put":
            kwargs["json"] = {"budget_amount_usd": "9.00"}
        response = await getattr(client, method)(path, **kwargs)
        assert response.status_code == 409, response.text
        assert raced
        assert (await client.get(path)).json()["cap_usd"] == "7.00"

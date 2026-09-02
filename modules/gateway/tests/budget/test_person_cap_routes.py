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
  T7  `soft` is the only mode any path can write (enforcement is C4 / #4630)
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
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.identity.providers import IdentityProvider
from src.shared.models.base import Base
from src.shared.models.budget import PersonBudgetConfig
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext

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
    """
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


async def seed_cap(session: AsyncSession, anchor: str, amount: str, *, period_type: str = "monthly", authored_by: str = "seed") -> PersonBudgetConfig:
    row = PersonBudgetConfig(
        person_anchor=anchor,
        period_type=period_type,
        budget_amount_usd=Decimal(amount),
        enforcement_mode="soft",
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


async def test_t2b_org_admin_may_still_author_their_own_cap_via_the_self_path(session, seeded):
    """An org admin is not locked out of the feature — only of *other* people's caps.

    The denial above is about authority over somebody else, not about the role. On
    the self path they are simply a person, and self-restraint needs no privilege.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "75.00"})

    assert response.status_code == 200, response.text
    rows = await stored_caps(session)
    assert [row.person_anchor for row in rows] == [f"github:{ORG_ADMIN_GITHUB_ID}"]


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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "250.00"})

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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        for period, amount in (("daily", "10.00"), ("weekly", "50.00"), ("monthly", "150.00")):
            response = await client.put(f"/me/budget/person-cap?period_type={period}", json={"budget_amount_usd": amount})
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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})

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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})

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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        self_body = (await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})).json()

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        admin_body = (await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "30.00"})).json()

    assert self_body["person_anchor"] == admin_body["person_anchor"] == PERSON_ANCHOR
    rows = await stored_caps(session)
    assert len(rows) == 1, "the two paths must write the SAME row, not one each"
    assert str(rows[0].budget_amount_usd) == "30.00"


# ===========================================================================
# T7 — `soft` is the only mode any path can write (enforcement is C4 / #4630)
# ===========================================================================


async def test_t7_self_authored_cap_is_soft(session, seeded):
    """Soft/informational only — the scope boundary this issue states explicitly.

    Enforcement is #4630 (C4) and depends on the §5.7 ruling. A row written
    ``hard`` here would be a cap that no code enforces while every surface claims
    it does.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})

    assert response.json()["enforcement_mode"] == "soft"
    rows = await stored_caps(session)
    assert rows[0].enforcement_mode == "soft"


async def test_t7b_platform_admin_authored_cap_is_also_soft(session, seeded):
    """A platform admin cannot author an enforcing person cap either.

    Their extra authority is over *whose* cap they may set, not over whether it
    enforces — nothing reads this table for enforcement yet, so a ``hard`` row from
    any author would be a false promise.
    """
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        response = await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "20.00"})

    assert response.json()["enforcement_mode"] == "soft"
    rows = await stored_caps(session)
    assert rows[0].enforcement_mode == "soft"


@pytest.mark.parametrize("path", ["/me/budget/person-cap", f"/budget/person-cap/{PERSON_ANCHOR}"])
async def test_t7c_enforcement_mode_is_not_client_settable(session, seeded, path):
    """A request asking for ``hard`` does not get it — on either route.

    The field is absent from the request model, so an extra key is ignored rather
    than honoured. Asserted rather than assumed: this is the one property that
    keeps this unit inside its stated scope, and "the model doesn't have the field"
    is exactly the kind of protection a later convenience change removes.
    """
    context = context_for(PLATFORM_ADMIN_SUB, is_admin=True) if path.startswith("/budget") else context_for(PERSON_SUB)

    async with client_for(session, context) as client:
        response = await client.put(path, json={"budget_amount_usd": "20.00", "enforcement_mode": "hard"})

    assert response.status_code == 200, response.text
    assert response.json()["enforcement_mode"] == "soft"
    rows = await stored_caps(session)
    assert [row.enforcement_mode for row in rows] == ["soft"]


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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", params=params, json={"budget_amount_usd": "20.00"})

    assert response.status_code == 200, response.text
    assert response.json()["person_anchor"] == PERSON_ANCHOR
    rows = await stored_caps(session)
    assert [row.person_anchor for row in rows] == [PERSON_ANCHOR]


@pytest.mark.parametrize("body_target", [{"person_anchor": COLLEAGUE_ANCHOR}, {"user_id": COLLEAGUE_CANONICAL}])
async def test_t8b_self_path_ignores_any_target_in_the_body(session, seeded, body_target):
    """Nor in the request body — the model has one field and it is an amount."""
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00", **body_target})

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
    assert self_routes, "the self route must exist"

    for route in self_routes:
        names = {param.name for param in route.dependant.path_params} | {param.name for param in route.dependant.query_params}
        assert names <= {"period_type"}, f"{route.methods} /me/budget/person-cap must accept no target parameter, found: {sorted(names)}"


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

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "unresolvable_person_anchor"
    assert await stored_caps(session) == []


async def test_t9e_caller_with_no_users_row_gets_422(session):
    """No ``users`` row at all: the first lookup fails, still a 422 and no write."""
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 422, response.text
    assert await stored_caps(session) == []


async def test_t9f_caller_matched_by_canonical_id_resolves(session, seeded):
    """A caller whose ``user_id`` is already the canonical id still gets their cap.

    Some callers reach budget code with ``user_id`` rewritten to the canonical
    ``users.id`` (#3989). Matching only on ``cognito_sub`` would deny those callers
    their own limit with a 422 that looks like "you have no GitHub account".
    """
    async with client_for(session, context_for(PERSON_CANONICAL)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})

    assert response.status_code == 200, response.text
    assert response.json()["person_anchor"] == PERSON_ANCHOR


@pytest.mark.parametrize("bad_amount", ["0", "0.00", "-5.00", "1.234"])
async def test_t9g_invalid_amounts_are_422(session, seeded, bad_amount):
    """Zero, negative and over-precise amounts are refused.

    Zero in particular: it would be indistinguishable downstream from "no limit
    authored", so removing a limit is a DELETE (T10) rather than a ``PUT`` of 0.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": bad_amount})

    assert response.status_code == 422, response.text
    assert await stored_caps(session) == []


@pytest.mark.parametrize("bad_period", ["run", "chain", "hourly", ""])
async def test_t9h_non_calendar_periods_are_422(session, seeded, bad_period):
    """Run/chain caps are lifetime-scoped and have no calendar window.

    ``get_period_start_end`` raises for them, so an unguarded value would be a 500
    — a server error for what is a bad request. The ``Literal`` answers 422 at the
    HTTP boundary before any handler code runs.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put(f"/me/budget/person-cap?period_type={bad_period}", json={"budget_amount_usd": "20.00"})

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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        first = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "100.00"})
        second = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "200.00"})

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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        await client.put("/me/budget/person-cap", json={"budget_amount_usd": "100.00"})
        before = (await stored_caps(session))[0]
        original_id, original_created = before.id, before.created_at

        await client.put("/me/budget/person-cap", json={"budget_amount_usd": "200.00"})

    after = (await stored_caps(session))[0]
    assert after.id == original_id
    assert after.created_at == original_created


async def test_t10c_re_authoring_records_the_current_author(session, seeded):
    """After a platform admin overrides, the row attributes the limit to them.

    The audit question is who set the limit that is in force NOW, not who set the
    first one ever.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        await client.put("/me/budget/person-cap", json={"budget_amount_usd": "100.00"})
    assert (await stored_caps(session))[0].authored_by_user_id == PERSON_CANONICAL

    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        await client.put(f"/budget/person-cap/{PERSON_ANCHOR}", json={"budget_amount_usd": "50.00"})
    assert (await stored_caps(session))[0].authored_by_user_id == PLATFORM_ADMIN_CANONICAL


async def test_t10d_delete_removes_the_limit(session, seeded):
    """Removing a limit is a DELETE, and the subsequent read is ``uncapped``."""
    await seed_cap(session, PERSON_ANCHOR, "100.00")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        deleted = await client.delete("/me/budget/person-cap")
        after = await client.get("/me/budget/person-cap")

    assert deleted.status_code == 204, deleted.text
    assert after.json()["cap_status"] == "uncapped"
    assert await stored_caps(session) == []


async def test_t10e_delete_is_idempotent(session, seeded):
    """A delete with no row is still ``204``.

    The outcome the caller asked for ("I have no personal limit") holds either way,
    and a 404 would make a retried delete look like a failure.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        first = await client.delete("/me/budget/person-cap")
        second = await client.delete("/me/budget/person-cap")

    assert first.status_code == second.status_code == 204


async def test_t10f_delete_only_removes_the_callers_own_row(session, seeded):
    """A colleague's cap survives the caller's delete.

    The decoy proves the delete predicate is anchored: a missing anchor filter
    would empty the table for everybody.
    """
    await seed_cap(session, PERSON_ANCHOR, "100.00")
    await seed_cap(session, COLLEAGUE_ANCHOR, "900.00")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        await client.delete("/me/budget/person-cap")

    rows = await stored_caps(session)
    assert [row.person_anchor for row in rows] == [COLLEAGUE_ANCHOR]


async def test_t10g_delete_only_removes_the_named_period(session, seeded):
    """Deleting the monthly limit leaves the daily one alone."""
    await seed_cap(session, PERSON_ANCHOR, "10.00", period_type="daily")
    await seed_cap(session, PERSON_ANCHOR, "100.00", period_type="monthly")

    async with client_for(session, context_for(PERSON_SUB)) as client:
        await client.delete("/me/budget/person-cap?period_type=monthly")

    rows = await stored_caps(session)
    assert [row.period_type for row in rows] == ["daily"]


async def test_t10h_amounts_round_trip_at_two_decimal_places(session, seeded):
    """Money is a string at the column's precision, not a float.

    A float on the wire loses the precision the ``NUMERIC(10,2)`` column holds —
    contract rule 1, and the defect class migration 030 exists for.
    """
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "1234.50"})

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
            "src.budget.person_cap_routes._read_cap_row",
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
    app = build_app(session, context_for(PERSON_SUB))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch(
            "src.budget.person_cap_routes._upsert_cap",
            side_effect=OperationalError("INSERT", {}, Exception("db down")),
        ):
            response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "20.00"})

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
    assert other_paths == {"/me/budget/person-cap"}


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

    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "100.00"})

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
    async with client_for(session, context_for(PERSON_SUB)) as client:
        response = await client.put("/me/budget/person-cap", json={"budget_amount_usd": "99999999999.00"})
    assert response.status_code == 422
    assert await stored_caps(session) == []

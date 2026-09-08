"""``GET /admin/users`` — the platform-wide member picker's data source (Issue #4827).

The Bedrock-routing panel's person rung was a free-text field asking for an internal
``users.id``. The server correctly refused wrong ids, so nothing was ever mis-routed —
but no operator could produce a *right* one, and the control read as broken. This
endpoint is the list they pick out of, so the properties worth pinning are the ones
whose failure is silent:

1. **A tenant's own admin cannot enumerate every other tenant's members.** This is the
   widest read of the member table in the API. ``ORG_READ`` — what every other member
   listing uses — would have made it a cross-tenant roster leak dressed as a picker.

2. **The id it returns is the canonical ``users.id``.** That is the column
   ``require_scope_exists`` and the routing resolver compare against (#4647). A picker
   handing back a Cognito sub would produce rules that store cleanly and govern nobody
   — the #4511 inert-config class on the surface that decides whose bill pays.

3. **A member with no linked GitHub identity is still listed.** Email-onboarded people
   are a permanent, legitimate population; dropping them would leave a valid rule
   target unselectable, which is exactly the gap this issue closes.

4. **Nobody is listed twice, and ``total`` is the count of people.** ``user_identities``
   is unique per (provider, provider_user_id, org_id), so one user CAN carry two GitHub
   rows. A join-based read would emit them twice, inflate ``total``, and shift every
   page boundary — silently making somebody unreachable through the picker.

``AccessControl`` is real throughout and roles come from ``tenant_memberships``, never
from a claim: a mocked authority check asserts a guarantee it never exercised.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.routes import router as admin_router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext

ORG_ID = "org-4827-acme"
OTHER_ORG_ID = "org-4827-globex"
TEAM_ID = "team-4827-ml"

# Canonical ids deliberately unlike the subs beside them: ``scope_id_user`` is the
# canonical id, so a test that accidentally asserted a sub must fail, not coincide.
ANA_ID = "48270000-0000-4000-8000-000000000001"
ANA_SUB = "sub-4827-ana"

BEN_ID = "48270000-0000-4000-8000-000000000002"
BEN_SUB = "sub-4827-ben"

# The GitHub-less member: invited by email, never linked a GitHub account.
CHEN_ID = "48270000-0000-4000-8000-000000000003"
CHEN_SUB = "sub-4827-chen"

# Another tenant entirely — present so the platform-wide claim is testable.
DANA_ID = "48270000-0000-4000-8000-000000000004"
DANA_SUB = "sub-4827-dana"

ORG_ADMIN_ID = "48270000-0000-4000-8000-000000000005"
ORG_ADMIN_SUB = "sub-4827-orgadmin"

PLATFORM_ADMIN_ID = "48270000-0000-4000-8000-000000000006"
PLATFORM_ADMIN_SUB = "sub-4827-platformadmin"


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
async def seeded(session: AsyncSession) -> None:
    """Two tenants, six people, three GitHub links.

    Emails are ordered so the ``ORDER BY email`` page boundary is predictable, and one
    person (Chen) has no GitHub identity at all so the fallback branch is reachable.
    """
    session.add(Organization(id=ORG_ID, name="Acme Corp"))
    session.add(Organization(id=OTHER_ORG_ID, name="Globex"))

    cast = [
        (ANA_ID, ANA_SUB, "ana@acme.example", "Ana Ortiz", ORG_ID, "member"),
        (BEN_ID, BEN_SUB, "ben@acme.example", "Ben Ruiz", ORG_ID, "member"),
        (CHEN_ID, CHEN_SUB, "chen@acme.example", "Chen Wu", ORG_ID, "member"),
        (DANA_ID, DANA_SUB, "dana@globex.example", "Dana Fox", OTHER_ORG_ID, "member"),
        (ORG_ADMIN_ID, ORG_ADMIN_SUB, "orgadmin@acme.example", "Org Admin", ORG_ID, "org_admin"),
        (PLATFORM_ADMIN_ID, PLATFORM_ADMIN_SUB, "zoe-platform@acme.example", "Zoe Platform", ORG_ID, "platform_admin"),
    ]
    for canonical, sub, email, name, org_id, role in cast:
        session.add(User(id=canonical, cognito_sub=sub, email=email, name=name, org_id=org_id, team_id=TEAM_ID))
        session.add(TenantMembership(user_id=canonical, tenant_id=org_id, role=role, is_active=True))

    # GitHub identities. ``provider_user_id`` is the numeric id GitHub issues;
    # ``provider_username`` is the login an operator actually recognises.
    links = [
        (ANA_ID, ORG_ID, "20402445", "anaortiz"),
        (BEN_ID, ORG_ID, "31513556", "benr-dev"),
        (DANA_ID, OTHER_ORG_ID, "42624667", "danafox"),
    ]
    for user_id, org_id, provider_user_id, username in links:
        session.add(
            UserIdentity(
                org_id=org_id,
                team_id=TEAM_ID,
                user_id=user_id,
                provider="github",
                provider_user_id=provider_user_id,
                provider_username=username,
                verification_method="oauth",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    await session.commit()


def context_for(sub: str, *, org_id: str = ORG_ID, is_admin: bool = False) -> TokenContext:
    """A token context. ``is_admin`` means **platform** admin and nothing else.

    ``auth/dependencies.py`` deliberately excludes ``org_admin`` from that flag
    (#3981), so the org admin below carries ``is_admin=False`` — which is why the
    denial test proves a real gate rather than a coincidence.
    """
    return TokenContext(
        user_id=sub,
        org_id=org_id,
        team_id=TEAM_ID,
        department_id="",
        account_type="human",
        is_admin=is_admin,
        expires_at=date(2099, 1, 1),
    )


def client_for(session: AsyncSession, context: TokenContext) -> AsyncClient:
    """The real admin router with only auth and the DB session overridden.

    ``AccessControl`` is NOT overridden — the route builds it against this session, so
    ``require_platform_admin`` runs for real.
    """
    app = FastAPI()
    app.include_router(admin_router)

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: context
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


class TestOnlyAPlatformAdminMayReadTheRoster:
    """A cross-tenant member roster is platform-admin authority, not ``ORG_READ``."""

    @pytest.mark.asyncio
    async def test_org_admin_is_denied(self, session, seeded):
        """The case dev cannot provide: a real org_admin, denied.

        They legitimately hold their own org's ``ORG_READ``. Gating this endpoint on
        that permission would therefore have handed them every other tenant's member
        list — the reason the check is a claim about the caller instead.
        """
        async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
            response = await client.get("/admin/users")

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_member_is_denied(self, session, seeded):
        async with client_for(session, context_for(ANA_SUB)) as client:
            response = await client.get("/admin/users")

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_denial_lists_nobody(self, session, seeded):
        """A 403 body must not carry the roster it just refused to serve."""
        async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
            response = await client.get("/admin/users")

        for email in ("ana@acme.example", "dana@globex.example"):
            assert email not in response.text


# ---------------------------------------------------------------------------
# What the picker is handed
# ---------------------------------------------------------------------------


class TestThePayloadIsWhatARuleCanBeStoredUnder:
    @pytest.mark.asyncio
    async def test_lists_members_of_every_org(self, session, seeded):
        """Platform-wide, not caller-org-scoped.

        The platform admin's token names Acme. Globex's member must still appear: a
        platform admin may pin any user in any org, and an org-scoped list would hide
        exactly the targets that authority covers.
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?page_size=100")

        assert response.status_code == 200
        body = response.json()
        by_email = {item["email"]: item for item in body["items"]}

        assert "dana@globex.example" in by_email
        assert by_email["dana@globex.example"]["org_id"] == OTHER_ORG_ID
        assert body["total"] == 6

    @pytest.mark.asyncio
    async def test_id_is_the_canonical_users_id_not_the_cognito_sub(self, session, seeded):
        """#4647. The resolver compares ``scope_id_user`` to a canonical id.

        A sub here would produce a rule that reads back correctly in the rules table
        and fires for nobody — and ``require_scope_exists`` matches on ``id`` only, so
        it would be rejected at save time with the operator none the wiser as to why.
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?q=ana")

        item = next(i for i in response.json()["items"] if i["email"] == "ana@acme.example")
        assert item["id"] == ANA_ID
        assert item["id"] != ANA_SUB

    @pytest.mark.asyncio
    async def test_github_username_is_the_linked_login(self, session, seeded):
        """The label an operator recognises comes from ``user_identities``.

        Not ``users.cognito_username``, which is only written on the admin-invite path
        and is NULL for the GitHub-onboarded population (#4687).
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?q=ana")

        item = next(i for i in response.json()["items"] if i["email"] == "ana@acme.example")
        assert item["github_username"] == "anaortiz"

    @pytest.mark.asyncio
    async def test_a_member_with_no_github_link_is_still_listed(self, session, seeded):
        """Email-onboarded members are a valid rule target, so they must be pickable.

        ``require_scope_exists`` accepts any ``users`` row. Omitting the GitHub-less
        population would leave a valid target with no way to select it — the same
        dead-end this issue exists to remove, just narrower.
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?page_size=100")

        item = next(i for i in response.json()["items"] if i["email"] == "chen@acme.example")
        assert item["id"] == CHEN_ID
        assert item["github_username"] is None

    @pytest.mark.asyncio
    async def test_two_github_identities_do_not_duplicate_a_person(self, session, seeded):
        """``user_identities`` is unique per (provider, provider_user_id, org_id).

        So one person can carry two GitHub rows. A LEFT JOIN would emit them twice:
        the picker would offer the same option twice, ``total`` would exceed the number
        of people, and every page boundary after them would shift — quietly making
        somebody unreachable. The correlated subquery is why this passes.
        """
        session.add(
            UserIdentity(
                org_id=ORG_ID,
                team_id=TEAM_ID,
                user_id=ANA_ID,
                provider="github",
                provider_user_id="99999999",
                provider_username="ana-second-account",
                verification_method="oauth",
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?page_size=100")

        body = response.json()
        ids = [item["id"] for item in body["items"]]
        assert ids.count(ANA_ID) == 1
        assert body["total"] == 6


# ---------------------------------------------------------------------------
# Search and pagination
# ---------------------------------------------------------------------------


class TestSearchAndPagination:
    """The picker must not pull the whole member table into a browser (#4827 design)."""

    @pytest.mark.asyncio
    async def test_search_matches_email_case_insensitively(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?q=GLOBEX")

        emails = [item["email"] for item in response.json()["items"]]
        assert emails == ["dana@globex.example"]

    @pytest.mark.asyncio
    async def test_search_matches_display_name(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?q=Chen Wu")

        assert [item["email"] for item in response.json()["items"]] == ["chen@acme.example"]

    @pytest.mark.asyncio
    async def test_search_matches_github_username(self, session, seeded):
        """The operator-facing identifier is searchable, not just the email.

        Requirement 2 on this issue: the GitHub login is how operators recognise
        people. A picker that displays it but cannot find by it sends the operator
        back to scrolling.
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?q=benr-dev")

        items = response.json()["items"]
        assert [item["email"] for item in items] == ["ben@acme.example"]
        assert items[0]["github_username"] == "benr-dev"

    @pytest.mark.asyncio
    async def test_total_counts_matches_not_the_page(self, session, seeded):
        """``total`` is the size of the match set, and ``has_more`` follows from it.

        A ``total`` reporting the page length would make the picker claim it had shown
        everything after one page — the reading that leaves members #51+ invisible.
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            first = await client.get("/admin/users?page=1&page_size=2")
            second = await client.get("/admin/users?page=2&page_size=2")

        assert first.json()["total"] == 6
        assert first.json()["has_more"] is True
        assert len(first.json()["items"]) == 2

        # Ordered by email, so the pages are disjoint and stable across requests.
        assert [i["email"] for i in first.json()["items"]] == ["ana@acme.example", "ben@acme.example"]
        assert [i["email"] for i in second.json()["items"]] == ["chen@acme.example", "dana@globex.example"]

    @pytest.mark.asyncio
    async def test_last_page_reports_no_more(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?page=1&page_size=100")

        assert response.json()["has_more"] is False

    @pytest.mark.asyncio
    async def test_blank_search_is_not_a_filter(self, session, seeded):
        """A cleared search box must reset to everybody, not match the empty string.

        Whitespace is the live case: the browser sends what the operator left behind.
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?q=%20%20&page_size=100")

        assert response.json()["total"] == 6

    @pytest.mark.asyncio
    async def test_a_search_matching_nobody_is_an_empty_list_not_an_error(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?q=nobody-by-that-name")

        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["total"] == 0


# ---------------------------------------------------------------------------
# What the payload deliberately omits
# ---------------------------------------------------------------------------


class TestTheRosterIsNarrow:
    @pytest.mark.asyncio
    async def test_does_not_return_cognito_or_role_fields(self, session, seeded):
        """The widest read in the API returns the least it can.

        A picker needs a recognisable label and the id a rule stores under. Subs,
        Cognito usernames and roles are not needed for that, and a field this endpoint
        does not return cannot leak from it.
        """
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/users?page_size=100")

        assert set(response.json()["items"][0]) == {"id", "org_id", "email", "name", "github_username"}
        assert ANA_SUB not in response.text

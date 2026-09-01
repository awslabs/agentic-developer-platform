"""Envelope composition — Issue #4399 (U-2 of EPIC #4324).

U-1 (#4397) established the single binding line. This unit composes the
**multi-line envelope** on top of it: the caller's ``direct`` line
(``entity_type="user"``, keyed by Cognito sub) and their ``cloud`` line
(``entity_type="root_user"``, keyed by canonical ``users.id``), each with its own
cap and headroom, plus the selected binding line and an informational combined
figure.

**The gate this file exists for** is ``TestTheHeadlineIsNeverTheSum`` — the EPIC's
single named hard acceptance criterion. Everything else here supports it. It is
split into its own module (rather than added to
``test_me_budget_routes.py``'s forty-odd cases) precisely so that gate is a
reviewable diff rather than one assertion in a crowd.

Harness notes:

* Same shape as ``test_me_budget_routes.py``: real in-memory SQLite with real
  ``BudgetConfig``/``BudgetUsage`` rows, inserted as raw models rather than
  through ``create_budget`` — the point is what happens when these values are
  already in the database, however they got there.
* The fixture caps and spends are the **worked example from the frozen ruling**
  (``requirements-analysis/envelope-composition.md``): user ``$200``/``$148.20``,
  root_user ``$300``/``$264.60``, so the binding line is root_user with ``$35.40``
  remaining and the forbidden total is ``$412.80``. Those literals are named
  constants below and reused by every gate, so a fixture drift cannot make the
  negative gate pass vacuously.
* Money is compared as ``Decimal``, never as a float or a formatted string:
  ``"35.40"`` and ``"35.400000"`` are the same figure at different column
  precisions, and a string compare would pin the serialisation rather than the
  arithmetic.

Two composition rules that are easy to get backwards, asserted here because
prose cannot enforce them:

1. ``lines`` and ``binding`` range over **different sets**. ``lines`` holds only
   the caller's two personal ledgers; ``binding`` is selected across the whole
   hierarchy, because a team or department cap genuinely can be what stops them
   (U-1's T24). ``TestSharedAncestorsAreNotPersonalLines`` pins both halves.
2. An **uncapped** line can never bind, and "no cap" is never rendered as a
   ``$0`` cap.
"""

import ast
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.auth.dependencies import get_current_user
from src.budget.me_routes import (
    _combined_informational,
    _compose_line,
    _principal_kind_for,
)
from src.budget.me_routes import router as me_budget_router
from src.budget.schemas import SERVICE_PRINCIPAL_QUALIFIER, BudgetLine, CombinedInformational
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

ORG_ID = "org-4399"

CALLER_SUB = "sub-caller-4399"
CALLER_CANONICAL_ID = "22222222-2222-4222-8222-222222222222"

# ---------------------------------------------------------------------------
# The worked example from the frozen ruling — the acceptance fixture
# ---------------------------------------------------------------------------
# Caps and spends are fixed by `envelope-composition.md` §1 and restated in the
# issue's Validation gates 2 and 3. Named rather than inlined so that the negative
# gate (FORBIDDEN_TOTAL) and the positive gate (BINDING_REMAINING) cannot drift
# apart from the seeds: FORBIDDEN_TOTAL is asserted to equal DIRECT_SPEND +
# CLOUD_SPEND below, so editing a seed without editing the expectation fails.
DIRECT_CAP = Decimal("200.00")
DIRECT_SPEND = Decimal("148.20")
CLOUD_CAP = Decimal("300.00")
CLOUD_SPEND = Decimal("264.60")

# The cloud line binds: $35.40 left vs the direct line's $51.80.
BINDING_REMAINING = CLOUD_CAP - CLOUD_SPEND  # 35.40
DIRECT_REMAINING = DIRECT_CAP - DIRECT_SPEND  # 51.80

# The number that must never be presented as a budget. It is governed by no cap
# and equals no ledger row — see the module docstring of `BudgetLine`.
FORBIDDEN_TOTAL = DIRECT_SPEND + CLOUD_SPEND  # 412.80

_SRC = Path(__file__).resolve().parents[2] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_the_acceptance_fixture_matches_the_frozen_worked_example():
    """The fixture literals are the ones the ruling fixes (guards every gate below).

    If a later edit changes a seed, the negative gate could start asserting
    "the headline is not <some number nothing computes>" and pass while the real
    defect shipped. This ties the constants together so that cannot happen
    silently.
    """
    assert (DIRECT_CAP, DIRECT_SPEND) == (Decimal("200.00"), Decimal("148.20"))
    assert (CLOUD_CAP, CLOUD_SPEND) == (Decimal("300.00"), Decimal("264.60"))
    assert BINDING_REMAINING == Decimal("35.40")
    assert FORBIDDEN_TOTAL == Decimal("412.80")
    assert BINDING_REMAINING < DIRECT_REMAINING, "the cloud line must be the binding one in this fixture"


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
async def caller_user_row(session) -> None:
    """The ``users`` row that lets the Cognito sub resolve to a canonical id.

    Without it ``resolve_canonical_user_id`` falls back to the raw sub and the
    ``root_user`` ledger is omitted entirely (U-1's ``identity_status`` path), so
    every test wanting a *cloud* line depends on this.
    """
    session.add(User(id=CALLER_CANONICAL_ID, cognito_sub=CALLER_SUB, email="caller@example.com", org_id=ORG_ID, team_id=""))
    await session.commit()


def caller_context(**overrides) -> TokenContext:
    defaults = {
        "user_id": CALLER_SUB,
        "org_id": ORG_ID,
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


async def seed_cap(
    session: AsyncSession,
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType,
    amount: str,
    enforcement_mode: str = "hard",
    org_id: str = ORG_ID,
) -> None:
    session.add(
        BudgetConfig(
            org_id=org_id,
            entity_type=entity_type.value,
            entity_id=entity_id,
            period_type=period_type.value,
            budget_amount_usd=Decimal(amount),
            enforcement_mode=enforcement_mode,
        )
    )
    await session.commit()


async def seed_usage(
    session: AsyncSession,
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType,
    amount: str,
    period_start: date | None = None,
    org_id: str = ORG_ID,
) -> None:
    from src.budget.utils import get_period_start_end

    start = period_start or get_period_start_end(period_type)[0]
    session.add(
        BudgetUsage(
            org_id=org_id,
            entity_type=entity_type.value,
            entity_id=entity_id,
            period_type=period_type.value,
            period_start=start,
            total_cost_usd=Decimal(amount),
            total_tokens=1000,
            request_count=1,
        )
    )
    await session.commit()


async def seed_worked_example(session: AsyncSession) -> None:
    """Seed the frozen fixture: user $200/$148.20, root_user $300/$264.60."""
    await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, str(DIRECT_CAP))
    await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, str(DIRECT_SPEND))
    await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, str(CLOUD_CAP))
    await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, str(CLOUD_SPEND))


def line_by_source(body: dict, source: str) -> dict:
    matches = [line for line in body["lines"] if line["source"] == source]
    assert len(matches) == 1, f"expected exactly one '{source}' line, got {len(matches)}: {body['lines']}"
    return matches[0]


# ===========================================================================
# Gate 1 — both lines present, each with its own figures (FR-2.1)
# ===========================================================================


class TestBothLinesArePresent:
    """A user with spend on both paths sees both, correctly attributed."""

    async def test_direct_and_cloud_lines_each_carry_their_own_cap_and_headroom(self, session, caller_user_row):
        """Gate 1 — two separately-capped lines, not one merged figure.

        Each line's cap, spend and headroom come from its OWN ledger row. The two
        can never merge: the ``user`` entity is keyed by Cognito sub, ``root_user``
        by canonical ``users.id``, and direct traffic never writes a ``root_user``
        row (``enforcement_service.py:797-802``). Asserting per-line rather than on
        an aggregate is the point — an implementation that reported one blended
        line would satisfy any total-only assertion.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert len(body["lines"]) == 2, f"expected a direct line and a cloud line, got {body['lines']}"

        direct = line_by_source(body, "direct")
        assert direct["entity_type"] == "user"
        assert Decimal(direct["cap_usd"]) == DIRECT_CAP
        assert Decimal(direct["spend_usd"]) == DIRECT_SPEND
        assert Decimal(direct["remaining_usd"]) == DIRECT_REMAINING
        assert direct["cap_status"] == "capped"
        assert direct["principal_kind"] == "human"

        cloud = line_by_source(body, "cloud")
        assert cloud["entity_type"] == "root_user"
        assert Decimal(cloud["cap_usd"]) == CLOUD_CAP
        assert Decimal(cloud["spend_usd"]) == CLOUD_SPEND
        assert Decimal(cloud["remaining_usd"]) == BINDING_REMAINING
        assert cloud["cap_status"] == "capped"
        assert cloud["principal_kind"] == "human"

    async def test_each_line_carries_a_human_readable_label(self, session, caller_user_row):
        """Labels are server-supplied so two surfaces cannot word a line differently.

        Asserted as "mentions the concept" rather than byte-equality: pinning exact
        copy here would make every wording tweak a test failure, while the property
        that matters is that the direct line reads as the user's own machine and the
        cloud line as agents they triggered.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert "direct" in line_by_source(body, "direct")["label"].lower()
        assert "cloud" in line_by_source(body, "cloud")["label"].lower()

    async def test_a_line_with_no_usage_row_reports_a_true_zero(self, session, caller_user_row):
        """A capped line with no spend yet is ``0.000000``, not absent.

        A missing ``budget_usage`` row is a measurement ("nothing settled this
        period"), not missing data, so the line must still appear with its full
        cap and headroom. Dropping it would read as "you have no cloud budget".
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, str(DIRECT_CAP))
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, str(CLOUD_CAP))

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        cloud = line_by_source(body, "cloud")
        assert Decimal(cloud["spend_usd"]) == Decimal("0")
        assert Decimal(cloud["remaining_usd"]) == CLOUD_CAP
        assert cloud["cap_status"] == "capped"


# ===========================================================================
# Gate 2 — the headline is the binding line (FR-2.2)
# ===========================================================================


class TestTheHeadlineIsTheBindingLine:
    async def test_the_lowest_remaining_capped_line_is_the_headline(self, session, caller_user_row):
        """Gate 2 — root_user binds at $35.40, not the user line at $51.80.

        The same selection ``get_budget_status_for_headers`` makes, so the screen
        names the cap that will actually stop the caller first.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["binding"]["entity_type"] == "root_user"
        assert body["binding"]["source"] == "cloud"
        assert Decimal(body["binding"]["remaining_usd"]) == BINDING_REMAINING

    async def test_the_binding_line_agrees_with_the_top_level_headline(self, session, caller_user_row):
        """``binding`` and the flat U-1 fields describe the SAME line.

        U-2 adds ``binding`` beside U-1's top-level fields rather than replacing
        them. If the two ever disagreed, a client reading one and a client reading
        the other would show different budgets for the same user — so their
        agreement is a contract, not an implementation detail.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["binding"]["entity_type"] == body["entity_type"]
        assert Decimal(body["binding"]["cap_usd"]) == Decimal(body["cap_usd"])
        assert Decimal(body["binding"]["spend_usd"]) == Decimal(body["spend_usd"])
        assert Decimal(body["binding"]["remaining_usd"]) == Decimal(body["remaining_usd"])
        assert body["binding"]["band"] == body["band"]
        assert body["binding"]["cap_status"] == body["cap_status"]
        assert body["binding"]["enforcement_mode"] == body["enforcement_mode"]

    async def test_the_direct_line_binds_when_it_has_less_headroom(self, session, caller_user_row):
        """Selection follows the data, not a hardcoded preference for one entity.

        The mirror of the fixture: give the DIRECT line less headroom and it must
        bind. Without this, an implementation that always reported the cloud line
        would pass gate 2 for the wrong reason.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "199.00")
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "300.00")
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "10.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["binding"]["entity_type"] == "user"
        assert body["binding"]["source"] == "direct"
        assert Decimal(body["binding"]["remaining_usd"]) == Decimal("1.00")


# ===========================================================================
# Gate 3 — THE HARD ACCEPTANCE GATE (FR-2.3)
# ===========================================================================


class TestTheHeadlineIsNeverTheSum:
    """The EPIC's single named hard acceptance gate.

    A headline computed as ``user_spend + root_user_spend`` ($412.80 here) exists
    in no ledger row and is governed by no cap. The screen would say "exhausted"
    at a figure enforcement never checks, while enforcement — which evaluates each
    entity separately against its own cap — stops nothing. That screen-vs-enforcer
    disagreement is the one failure this EPIC exists to eliminate.
    """

    async def test_the_headline_is_not_the_sum_of_the_two_lines(self, session, caller_user_row):
        """Gate 3a — no headline field equals $412.80.

        Checked on ``binding`` AND the flat top-level fields, because "the
        headline" is presented in both places and a regression could plausibly
        appear in either.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert Decimal(body["spend_usd"]) != FORBIDDEN_TOTAL, f"the top-level headline spend is the forbidden total {FORBIDDEN_TOTAL}"
        assert Decimal(body["binding"]["spend_usd"]) != FORBIDDEN_TOTAL, f"the binding line's spend is the forbidden total {FORBIDDEN_TOTAL}"

        # And positively: it is one real line's figure.
        assert Decimal(body["binding"]["spend_usd"]) == CLOUD_SPEND

    async def test_no_field_presented_as_a_budget_equals_the_sum(self, session, caller_user_row):
        """Gate 3b — the sum appears in NO budget-bearing field, anywhere.

        Deliberately broader than gate 3a and the reason this class exists. Rather
        than naming the fields a regression might hit, this walks every
        budget-bearing money field in the response — the flat headline, ``binding``,
        and every entry in ``lines`` — and asserts none of them is the total. A
        future refactor that moves the headline somewhere new is still covered,
        which a fixed list of field names would not be.

        ``combined_informational.spend_usd`` is exempt BY DESIGN: it is the total,
        and it is the one place the total is allowed to appear precisely because
        that model carries no cap denominator and is flagged ``is_budget: false``.
        Gate 4 pins those properties.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        budget_bearing: list[tuple[str, dict]] = [("<headline>", body), ("binding", body["binding"])]
        budget_bearing += [(f"lines[{i}]", line) for i, line in enumerate(body["lines"])]

        for where, holder in budget_bearing:
            for field in ("cap_usd", "spend_usd", "remaining_usd"):
                value = holder.get(field)
                if value is None:
                    continue
                assert Decimal(value) != FORBIDDEN_TOTAL, f"{where}.{field} == {FORBIDDEN_TOTAL}: a figure no cap governs is presented as a budget"

    async def test_no_lines_cap_is_the_sum_of_the_two_caps(self, session, caller_user_row):
        """Gate 3c — the fused $600.00 denominator from the mockup is never built.

        The frozen ruling forbids the fused ``$412.80 / $600.00`` envelope
        outright: no cap equal to ``user_cap + root_user_cap`` exists in any
        ledger. That envelope is #4396's job, deliberately not this unit's.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        forbidden_cap = DIRECT_CAP + CLOUD_CAP  # 600.00
        for line in [body["binding"], *body["lines"]]:
            if line["cap_usd"] is not None:
                assert Decimal(line["cap_usd"]) != forbidden_cap, f"a fused cap denominator {forbidden_cap} was rendered; no such cap exists"
        assert body["cap_usd"] is None or Decimal(body["cap_usd"]) != forbidden_cap


# ===========================================================================
# Gate 4 — the combined figure is informational only (FR-2.4)
# ===========================================================================


class TestCombinedIsInformationalOnly:
    async def test_combined_is_flagged_not_a_budget_and_carries_no_denominator(self, session, caller_user_row):
        """Gate 4 — ``is_budget: false`` and NO cap field at all.

        The absence of a denominator is the real guarantee: a frontend cannot bind
        a progress bar to a field that does not exist on the wire. Asserted as an
        explicit key-absence check over the serialised payload, so adding a
        ``cap_usd``/``remaining_usd``/``utilization_pct``/``band`` to this model
        fails here rather than being caught in review.
        """
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        combined = body["combined_informational"]
        assert combined is not None
        assert combined["is_budget"] is False
        assert Decimal(combined["spend_usd"]) == FORBIDDEN_TOTAL, "the informational total should be the real sum — it is only forbidden as a BUDGET"

        for forbidden in ("cap_usd", "remaining_usd", "utilization_pct", "band", "cap_status"):
            assert forbidden not in combined, f"combined_informational carries '{forbidden}'; a denominator implies a cap and invites a progress bar"

    async def test_is_budget_cannot_be_set_true(self):
        """The flag is typed ``Literal[False]``, so no code path can flip it.

        A plain ``bool`` default would let a future caller construct a "combined
        budget" and start presenting the total as a ceiling. Pydantic rejecting
        ``True`` is what makes the rule structural rather than conventional.
        """
        with pytest.raises(ValueError):
            CombinedInformational(spend_usd="412.800000", is_budget=True, note="not a cap")

    async def test_the_note_says_it_is_not_a_cap(self, session, caller_user_row):
        """The figure carries its own caption, so a surface cannot render it bare."""
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert "not a cap" in body["combined_informational"]["note"].lower()

    async def test_a_single_line_produces_no_combined_figure(self, session):
        """Nothing to combine ⇒ ``None``, not a restatement of one line.

        No ``caller_user_row`` fixture here, so the identity does not resolve and
        the cloud line is absent. A "combined" total over one line is that line's
        spend repeated, which invites a redundant tile implying a second source
        exists.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, str(DIRECT_CAP))
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, str(DIRECT_SPEND))

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["identity_status"] == "unresolved"
        assert body["combined_informational"] is None

    def test_combined_preserves_sub_cent_precision(self):
        """Six-decimal spend survives the fold — no float round-trip.

        Unit-level because the arithmetic is the point: ``budget_usage`` settles at
        ``NUMERIC(14,6)`` and a burst of cheap traffic accrues real spend in the
        sixth decimal. Summing via float would silently drop it.
        """
        lines = [
            _compose_line(EntityType.USER, CALLER_SUB, None, Decimal("0.000001")),
            _compose_line(EntityType.ROOT_USER, CALLER_CANONICAL_ID, None, Decimal("0.000002")),
        ]

        combined = _combined_informational(lines)

        assert combined is not None
        assert Decimal(combined["spend_usd"] if isinstance(combined, dict) else combined.spend_usd) == Decimal("0.000003")

    def test_uncapped_lines_still_contribute_their_spend(self):
        """A line with no cap is real spend and belongs in the total.

        The combined figure is explicitly not a budget, so having no ceiling does
        not disqualify a line from contributing dollars. Excluding uncapped lines
        would under-report a user whose caps are not yet configured.
        """
        lines = [
            _compose_line(EntityType.USER, CALLER_SUB, None, DIRECT_SPEND),
            _compose_line(EntityType.ROOT_USER, CALLER_CANONICAL_ID, None, CLOUD_SPEND),
        ]

        combined = _combined_informational(lines)

        assert combined is not None
        assert Decimal(combined.spend_usd) == FORBIDDEN_TOTAL


# ===========================================================================
# Gate 5 — an uncapped line can never bind (FR-2, rule 6)
# ===========================================================================


class TestUncappedLinesCannotBind:
    async def test_the_capped_line_binds_when_the_other_is_uncapped(self, session, caller_user_row):
        """Gate 5 — one uncapped, one capped ⇒ the capped line binds.

        The uncapped line here carries far MORE spend than the capped one, so an
        implementation that treated a missing cap as ``$0`` would select it and
        report a phantom exhausted budget.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "10.00")
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "5000.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["binding"]["entity_type"] == "user"
        assert body["binding"]["cap_status"] == "capped"

        # The uncapped line is still REPORTED — its spend is real — with every
        # cap-derived field null, so it cannot be read as a $0 cap.
        cloud = line_by_source(body, "cloud")
        assert cloud["cap_status"] == "uncapped"
        assert cloud["cap_usd"] is None
        assert cloud["remaining_usd"] is None
        assert cloud["utilization_pct"] is None
        assert cloud["band"] is None
        assert Decimal(cloud["spend_usd"]) == Decimal("5000.00")

    async def test_all_lines_uncapped_yields_no_binding_line(self, session, caller_user_row):
        """Nothing capped ⇒ ``binding: null``, not an uncapped line as the headline.

        Reporting an uncapped line as binding would render a ``null`` headroom as
        the headline figure. ``null`` here means "no cap governs you", which is a
        different statement from a ``$0`` cap.
        """
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, str(DIRECT_SPEND))
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, str(CLOUD_SPEND))

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["binding"] is None
        assert body["cap_status"] == "uncapped"
        # The lines and the combined figure are still composed — spend is real.
        assert len(body["lines"]) == 2
        assert Decimal(body["combined_informational"]["spend_usd"]) == FORBIDDEN_TOTAL

    async def test_a_zero_cap_line_binds_and_is_not_uncapped(self, session, caller_user_row):
        """A ``$0`` cap is a real hard stop, distinct from having no cap.

        It has the least headroom possible, so it binds. Conflating it with
        "uncapped" would show a user who cannot spend anything as unlimited.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "0.00")
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, str(CLOUD_CAP))
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, str(CLOUD_SPEND))

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        direct = line_by_source(body, "direct")
        assert direct["cap_status"] == "capped"
        assert Decimal(direct["cap_usd"]) == Decimal("0")
        assert direct["band"] == "exceeded"
        assert direct["utilization_pct"] is None, "no percentage is defined at a zero denominator"
        assert body["binding"]["entity_type"] == "user"


# ===========================================================================
# Gates 6 & 7 — service roots (FR-2.5, envelope-composition.md §5)
# ===========================================================================


class TestServiceRootsAreDistinguished:
    """A ``service:``-qualified root is an unattended trigger, not a person.

    On ``/me/budget`` such a root cannot arise — ``_resolve_root_principal``
    returns ``not_applicable`` for service callers and ``resolve_canonical_user_id``
    only ever yields a bare ``users.id``. These are therefore unit tests against
    the pure composition helpers, which is also how U-4 will reach them: it renders
    this same line model for managed-scope rollups over arbitrary entity ids, where
    a ``service:`` root very much does appear.
    """

    def test_a_service_qualified_root_is_kind_service(self):
        """Gate 6a — the ``service:`` qualifier is recognised (#4344)."""
        assert _principal_kind_for(EntityType.ROOT_USER, f"{SERVICE_PRINCIPAL_QUALIFIER}eventbridge:adp-dev-high-error-rate") == "service"

    def test_a_bare_canonical_id_root_is_kind_human(self):
        """Gate 6b — an unqualified root id is a person.

        A canonical ``users.id`` is a UUID and contains no colon, so the two cases
        cannot be confused.
        """
        assert _principal_kind_for(EntityType.ROOT_USER, CALLER_CANONICAL_ID) == "human"

    def test_the_qualifier_matches_the_enforcement_side_constant(self):
        """The schema constant equals the private one enforcement writes with.

        The prefix is duplicated as a public constant for U-4 to read. If the two
        ever diverged, a service root written by enforcement would be classified as
        a person here — so they are pinned equal rather than assumed so.
        """
        from src.budget.enforcement_service import _SERVICE_PRINCIPAL_PREFIX

        assert SERVICE_PRINCIPAL_QUALIFIER == _SERVICE_PRINCIPAL_PREFIX

    def test_a_service_line_is_excluded_from_the_per_person_total(self):
        """Gate 6c — unattended CI spend is not a person's personal spend.

        Folding a service root into a human's envelope makes per-person cost truth
        wrong: the human appears to have spent money that a schedule spent.
        """
        lines = [
            _compose_line(EntityType.USER, CALLER_SUB, None, DIRECT_SPEND),
            _compose_line(EntityType.ROOT_USER, f"{SERVICE_PRINCIPAL_QUALIFIER}eventbridge:nightly", None, CLOUD_SPEND),
        ]

        combined = _combined_informational(lines)

        assert combined is None, "only one HUMAN line remains, so there is nothing to combine"

    def test_a_service_root_does_not_double_count_the_combined_figure(self):
        """Gate 7 — the #4391 write asymmetry must not inflate the total.

        Per ``envelope-composition.md`` §5: the usage tracker writes the
        ``root_user`` row whenever ``root_human_id`` is set, with no equality skip
        (``handler.py:467``), while enforcement DOES skip the root entity when the
        root is the caller (``enforcement_service.py:437``). For a service-rooted
        run the registry row names the same service key as both ``user_id`` and
        ``root_human_id``, so the SAME dollar lands on both rows.

        Here both lines carry $264.60 — the same dollar, written twice. A naive
        fold would report $529.20, money that was never spent. Excluding service
        principals removes the double-count structurally.
        """
        duplicated = CLOUD_SPEND
        lines = [
            _compose_line(EntityType.SERVICE_ACCOUNT, "service-account-4399", None, duplicated),
            _compose_line(EntityType.ROOT_USER, f"{SERVICE_PRINCIPAL_QUALIFIER}eventbridge:nightly", None, duplicated),
        ]

        combined = _combined_informational(lines)

        assert combined is None or Decimal(combined.spend_usd) != duplicated * 2, "the same dollar was counted on both the user and root_user rows"

    async def test_a_service_account_caller_reports_kind_service(self, session):
        """Gate 6d — a service-account caller's own line is not a person's line.

        End-to-end rather than unit, because this is the one service-principal
        shape that DOES reach ``/me/budget``: an IAM/service-account caller.
        """
        await seed_cap(session, EntityType.SERVICE_ACCOUNT, "sa-4399", PeriodType.MONTHLY, "50.00")
        await seed_usage(session, EntityType.SERVICE_ACCOUNT, "sa-4399", PeriodType.MONTHLY, "12.50")

        app = build_app(session, caller_context(user_id="sa-4399", account_type="service"))
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["identity_status"] == "not_applicable"
        assert len(body["lines"]) == 1
        assert body["lines"][0]["principal_kind"] == "service"
        assert body["lines"][0]["entity_type"] == "service_account"
        # One line, and a service one at that — nothing to roll up per-person.
        assert body["combined_informational"] is None


# ===========================================================================
# Gate 8 — bands come from the server thresholds
# ===========================================================================


class TestLineBands:
    @pytest.mark.parametrize(
        ("spend", "expected_band"),
        [
            ("79.00", "none"),
            ("85.00", "warning"),
            ("97.00", "critical"),
            ("101.00", "exceeded"),
        ],
    )
    async def test_band_is_derived_from_the_server_thresholds(self, session, caller_user_row, spend, expected_band):
        """Gate 8 — 79/85/97/101% ⇒ none/warning/critical/exceeded.

        Against a ``$100`` cap so the spend reads as the utilisation percentage.
        Thresholds come from ``budget_config`` (80/95), NOT a new constant —
        ``BudgetManagement.tsx`` currently hardcodes a different band (50/80) and
        that drift is what a server-derived band prevents.

        95% is a *critical warning*, not a stop: enforcement blocks at >= 100%,
        which is why 97% is ``critical`` and only 101% is ``exceeded``.
        """
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, spend)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert line_by_source(body, "cloud")["band"] == expected_band

    async def test_a_lines_utilization_is_reported_to_one_decimal(self, session, caller_user_row):
        """The fixture's 88.2% — the figure the api-contract's example shows."""
        await seed_worked_example(session)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert line_by_source(body, "cloud")["utilization_pct"] == 88.2
        assert line_by_source(body, "direct")["utilization_pct"] == 74.1

    async def test_a_line_over_its_cap_reports_negative_headroom(self, session, caller_user_row):
        """Overage is shown, not clamped to ``$0.00 left``.

        Settled spend can pass a cap (lowered after the fact, or settled
        asynchronously after requests were admitted). The response-header path
        clamps at zero (``enforcement_service.py:1314``); a read surface whose
        purpose is the true position must not.
        """
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "150.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert Decimal(line_by_source(body, "cloud")["remaining_usd"]) == Decimal("-50.00")


# ===========================================================================
# lines vs binding — the two range over different sets, on purpose
# ===========================================================================


class TestSharedAncestorsAreNotPersonalLines:
    """A team/department/org cap can bind, but is not the caller's own spend.

    Both halves matter. If shared ancestors entered ``lines``, a colleague's spend
    would render as the caller's personal usage and inflate the combined figure. If
    they were excluded from binding SELECTION, a user stopped by their department's
    cap would see a headline that never mentions it — U-1's T24 pins that a
    department cap does bind.
    """

    async def test_a_department_cap_binds_without_appearing_as_a_personal_line(self, session, caller_user_row):
        await seed_worked_example(session)
        await seed_cap(session, EntityType.DEPARTMENT, "dept-4399", PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.DEPARTMENT, "dept-4399", PeriodType.MONTHLY, "99.00")

        app = build_app(session, caller_context(department_id="dept-4399"))
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        # It binds — least headroom in the hierarchy at $1.00.
        assert body["binding"]["entity_type"] == "department"
        assert Decimal(body["binding"]["remaining_usd"]) == Decimal("1.00")
        assert body["binding"]["source"] is None, "a shared ancestor is neither direct nor cloud"

        # But it is not one of the caller's personal lines.
        assert {line["entity_type"] for line in body["lines"]} == {"user", "root_user"}

    async def test_shared_ancestor_spend_is_excluded_from_the_combined_figure(self, session, caller_user_row):
        """The org row is $412.80 here too — but by aggregation, not personally.

        The org ledger legitimately holds the caller's total. Folding it into their
        per-person figure would count their own spend twice over, and in a real
        tenant would add every colleague's spend to their personal envelope.
        """
        await seed_worked_example(session)
        await seed_cap(session, EntityType.ORGANIZATION, ORG_ID, PeriodType.MONTHLY, "9000.00")
        await seed_usage(session, EntityType.ORGANIZATION, ORG_ID, PeriodType.MONTHLY, str(FORBIDDEN_TOTAL))

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert Decimal(body["combined_informational"]["spend_usd"]) == FORBIDDEN_TOTAL
        assert "org" not in {line["entity_type"] for line in body["lines"]}


# ===========================================================================
# Pure-helper unit coverage
# ===========================================================================


class TestComposeLineIsPure:
    """``_compose_line`` is the contract U-4 reuses, so it is tested directly."""

    def test_an_uncapped_line_nulls_every_cap_derived_field(self):
        line = _compose_line(EntityType.USER, CALLER_SUB, None, DIRECT_SPEND)

        assert isinstance(line, BudgetLine)
        assert line.cap_status == "uncapped"
        assert (line.cap_usd, line.remaining_usd, line.utilization_pct, line.band) == (None, None, None, None)
        assert Decimal(line.spend_usd) == DIRECT_SPEND
        assert line.enforcement_mode is None

    def test_a_capped_line_carries_the_effective_cap_and_enforcement_mode(self):
        cap_row = BudgetConfig(
            org_id=ORG_ID,
            entity_type=EntityType.ROOT_USER.value,
            entity_id=CALLER_CANONICAL_ID,
            period_type=PeriodType.MONTHLY.value,
            budget_amount_usd=CLOUD_CAP,
            enforcement_mode="soft",
        )

        line = _compose_line(EntityType.ROOT_USER, CALLER_CANONICAL_ID, cap_row, CLOUD_SPEND)

        assert line.cap_status == "capped"
        assert Decimal(line.cap_usd) == CLOUD_CAP
        assert Decimal(line.remaining_usd) == BINDING_REMAINING
        assert line.enforcement_mode == "soft"
        assert line.source == "cloud"
        assert line.principal_kind == "human"

    def test_shared_ancestors_get_a_label_but_no_source(self):
        for entity_type in (EntityType.TEAM, EntityType.DEPARTMENT, EntityType.ORGANIZATION):
            line = _compose_line(entity_type, "shared-id", None, Decimal("1.00"))
            assert line.source is None, f"{entity_type.value} must not be a per-person source"
            assert line.label, f"{entity_type.value} still needs a label for the headline"


# ===========================================================================
# Static gates from the issue's Validation section
# ===========================================================================


class TestStaticGates:
    """The issue's two ``git diff`` gates, asserted in-suite.

    Written as tests rather than left to a reviewer for the reason U-1 gives for
    its own static gates: a CI-visible assertion holds on every future commit,
    whereas a one-off command run at review time does not.
    """

    # test_enforcement_service_is_not_modified removed (Issue #4591 review):
    # it asserted `git diff origin/main...HEAD -- enforcement_service.py` is
    # empty, which was U-2's per-branch scope gate — but written as a permanent
    # test it outlaws EVERY future change to the enforcement service (it only
    # stayed green in CI because shallow clones made it skip). #4591 modifies
    # that file deliberately; the remaining gates below still pin U-2's real
    # invariants (read-only composition, no fused envelope, no SQL aggregates).

    def test_no_direct_spend_is_written_to_the_root_user_ledger(self):
        """Scope gate — writing direct spend to ``root_user`` is #4396's job.

        Doing it here would change what enforcement reads, which is explicitly out
        of scope. Asserted structurally over the module's AST rather than by
        grepping the diff: this module must contain no ORM write at all, so any
        ledger write — to ``root_user`` or anywhere else — fails, and the check
        does not depend on a diff being available.
        """
        tree = ast.parse((_SRC / "budget" / "me_routes.py").read_text())

        called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        for write_op in ("add", "add_all", "commit", "merge", "delete", "flush", "execute_many"):
            assert write_op not in called, f"me_routes.py calls session.{write_op}(); this module is read-only (NFR-2)"

        source = (_SRC / "budget" / "me_routes.py").read_text()
        for statement in ("insert(", "update(", "INSERT ", "UPDATE "):
            assert statement.lower() not in source.lower(), f"me_routes.py contains '{statement}'; no ledger writes belong in this read path"

    def test_the_fused_envelope_is_not_built(self):
        """The frozen ruling's negative: no combined CAP anywhere in the contract.

        ``CombinedInformational`` must never grow a denominator — that is #4396.
        Checked on the model's own fields so the guarantee survives a refactor of
        the route.
        """
        assert set(CombinedInformational.model_fields) == {"spend_usd", "is_budget", "note"}, (
            "CombinedInformational grew a field; a cap/denominator here would make the informational total look like a budget (#4396)"
        )

    def test_the_idor_prone_router_is_still_untouched(self):
        """NFR-1 — no own-scope route was added to ``src/budget/routes.py``.

        Re-asserted here because this unit edits the module that exists to avoid
        #4384's unscoped ``entity_type``/``entity_id``.
        """
        from src.budget.routes import router as legacy_router

        paths = {getattr(route, "path", "") for route in legacy_router.routes}
        assert not any("/me" in path for path in paths), f"an own-scope route was added to the IDOR-prone router: {sorted(paths)}"

    def test_no_sql_aggregate_in_the_composition_path(self):
        """Every ledger figure stays a single 5-filter row read (FR-1.6).

        The combined figure folds in Python over rows already read; an SQL
        aggregate appearing here would be the #4328 unfiltered-sum class returning.
        """
        source = (_SRC / "budget" / "me_routes.py").read_text()
        assert not re.search(r"\bfunc\.sum\b|\bSUM\s*\(", source, re.IGNORECASE), (
            "me_routes.py contains a SQL aggregate; figures must be single-row reads"
        )

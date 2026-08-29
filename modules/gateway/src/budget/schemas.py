"""Budget module Pydantic response schemas — Issue #4397 (U-1 of EPIC #4324).

This module is the **contract of record** for the budget read surface. U-2…U-5
either consume these models or derive their fixtures from them, so a change here
is a change to every later unit — treat the field rules below as load-bearing
rather than descriptive.

Created on the `src/activity/schemas.py` precedent: contract rules live in the
module docstring next to the models they constrain, so a client cannot read the
shape without reading the rules.

Contract rules (each one is a test in `tests/budget/test_me_budget_routes.py`):

1. **Money is a JSON string, never a float.** Costs settle at
   ``NUMERIC(14,6)`` (``budget_usage.total_cost_usd``) and caps at
   ``NUMERIC(10,2)`` (``budget_configs.budget_amount_usd``). A float round-trip
   loses sub-cent precision, which is exactly the class of defect that made a
   burst of haiku traffic accrue real spend against a $0.00 accumulator
   (migration 030). Each field is serialised at **its own column's** precision:
   caps at 2dp, spend/headroom at 6dp. Widening a cap to 6dp would invent
   digits the column cannot hold; narrowing spend to 2dp would drop digits it
   does hold.

2. **"No cap configured" is not "a cap of $0".** ``cap_status="uncapped"``
   means no ``budget_configs`` row exists for any entity in the caller's
   hierarchy, and ``cap_usd``/``remaining_usd``/``utilization_pct`` are all
   ``None``. A real ``$0`` row is ``cap_status="capped"`` with
   ``cap_usd="0.00"``. Conflating them shows an uncapped user as exhausted, or
   a $0-capped user as unlimited (FR-1.5).

3. **``cap_usd`` is the effective cap, after the platform-ceiling clamp** — not
   the raw ``budget_configs`` row. See ``budget_period_cap_usd`` in
   ``src/budget/config.py``. Rendering the raw row advertises headroom
   enforcement may not honour (FR-1.4).

4. **``remaining_usd`` may be negative.** Settled spend can exceed a cap (a cap
   lowered after the fact, or spend settled by the async tracker after the
   requests were admitted). Clamping to zero — which the response-header path
   does, ``enforcement_service.py:1314`` — would hide the overage behind a
   flat "$0.00 left" and is wrong for a read surface whose whole purpose is to
   show the true position.

5. **A backend failure is never a zero.** The route raises ``503`` rather than
   returning this model with zeroes, so no field here can encode "the database
   was unreachable" (FR-1.7). ``get_budget_status_for_headers`` returns ``{}``
   for both "no budget" and "DB error" and is deliberately not reused.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

# The warning band a utilisation figure falls in. Derived server-side from
# `budget_config.budget_warning_threshold_percent` / `_critical_threshold_percent`
# (80.0 / 95.0) so the frontend cannot drift its own thresholds — today
# `BudgetManagement.tsx` hardcodes a *different* band (50/80), which is the drift
# this type exists to prevent (FR-5.3).
#
# 95% is a *critical warning*, NOT a stop. Enforcement blocks at >= 100%.
BudgetBand = Literal["none", "warning", "critical", "exceeded"]

# Whether a cap exists at all. See contract rule 2 — the two states are
# deliberately not collapsible into "cap_usd is None".
CapStatus = Literal["capped", "uncapped"]

# Whether the caller's canonical `users.id` could be resolved from their Cognito
# sub. See `MyBudgetResponse.identity_status` for why this is on the wire.
IdentityStatus = Literal["resolved", "unresolved", "not_applicable"]


# Serialisation precision per column, so a caller reading these strings gets
# exactly the digits the database holds and no more. See contract rule 1.
CAP_PLACES = Decimal("0.01")  # budget_configs.budget_amount_usd  NUMERIC(10,2)
SPEND_PLACES = Decimal("0.000001")  # budget_usage.total_cost_usd  NUMERIC(14,6)


def format_money(amount: Decimal, places: Decimal) -> str:
    """Render a Decimal as a fixed-precision JSON string.

    Args:
        amount: The value to render.
        places: ``CAP_PLACES`` or ``SPEND_PLACES`` — the precision of the column
            the value came from.

    Returns:
        A plain decimal string (e.g. ``"500.00"``, ``"264.600000"``). Never
        scientific notation: ``quantize`` on a fixed exponent keeps
        ``Decimal("0E-6")`` from serialising as ``"0E-6"``, which would break
        every client that parses this with a plain decimal reader.
    """
    return f"{amount.quantize(places):f}"


class BudgetPeriod(BaseModel):
    """The calendar window a set of figures describes.

    Always present, and always the window that was actually queried — the
    ``period_start`` here is the same value used in the 5-filter
    ``budget_usage`` predicate, so a client can reproduce the query.

    Only calendar periods appear here. ``run``/``chain`` caps are
    lifetime-scoped and have no calendar window at all
    (``budget/utils.py:get_period_start_end`` raises for them), which is why the
    route rejects those period types with a ``422`` rather than inventing bounds.
    """

    period_type: Literal["daily", "weekly", "monthly"]
    period_start: date = Field(description="First day of the period, inclusive. The `period_start` used in the budget_usage lookup.")
    period_end: date = Field(description="Last day of the period, inclusive.")
    resets_in_days: int = Field(
        description=("Whole days from today until `period_end`. `0` means today IS the last day of the period, so the counter resets tomorrow."),
    )


class MyBudgetResponse(BaseModel):
    """The signed-in caller's own cap, settled spend and headroom for one period.

    This is the **single-line** form: it reports the one *binding* line — the
    lowest-remaining capped entity across the caller's hierarchy, which is the
    line that will actually stop them first. U-2 (#4324) adds the multi-line
    envelope (`binding` + `lines` + `combined_informational`) on top of this
    shape; it does not replace it.

    Why the binding line rather than a sum: enforcement evaluates each entity in
    the hierarchy **separately**, each against its own cap
    (``_check_entity_budget``). A summed headline would be a number that exists
    in no ledger row and is enforced by no cap, so the screen would say
    "exhausted" while enforcement stopped nothing — the precise screen-vs-enforcer
    disagreement EPIC #4324 exists to eliminate.
    """

    period: BudgetPeriod

    entity_type: str = Field(
        description=(
            "Which entity in the caller's hierarchy these figures describe — the "
            "binding (lowest-remaining capped) line, or the caller's own line "
            "when nothing is capped. Present because `cap_usd` is not "
            "interpretable without knowing which ledger it came from, and "
            "because the spend-parity check is defined per entity+period."
        )
    )

    cap_usd: str | None = Field(
        description=("The EFFECTIVE cap after the platform-ceiling clamp, at 2dp. `None` when `cap_status` is `uncapped` — see contract rule 2/3."),
    )
    spend_usd: str = Field(
        description=(
            "Settled spend for this entity and period at 6dp, read with the full "
            "5-filter `(org_id, entity_type, entity_id, period_type, "
            "period_start)` predicate — the same figure enforcement's "
            "`_check_entity_budget` compares against. Always present: with no "
            "usage row it is a true `0.000000`, which is a measurement, not a "
            "fallback. Settles asynchronously, so very recent spend may not be "
            "included yet."
        ),
    )
    remaining_usd: str | None = Field(
        description=(
            "Headroom (`cap - spend`) at 6dp. May be NEGATIVE when settled spend has passed the cap — see contract rule 4. `None` when uncapped."
        ),
    )
    utilization_pct: float | None = Field(
        description=(
            "Spend as a percentage of cap, to 1dp. `None` when uncapped, and "
            "also `None` for a `$0` cap, where no percentage is defined — "
            "reporting `0.0` there would read as 'plenty of room' when in fact "
            "no request with any cost can pass."
        ),
    )
    band: BudgetBand | None = Field(
        description=("Warning band from the server-side 80/95 thresholds. `None` when uncapped. A `$0` cap is always `exceeded`."),
    )
    cap_status: CapStatus = Field(description="`capped` if a budget row governs the caller, `uncapped` if none does. See contract rule 2.")

    enforcement_mode: str | None = Field(
        description=(
            "How the binding cap behaves when exceeded, straight off the "
            "`budget_configs` row enforcement itself reads: `hard` blocks the "
            "request, `soft` warns and allows. `None` when uncapped. This is the "
            "field a client must consult before telling a user their spend will "
            "be stopped."
        ),
    )

    identity_status: IdentityStatus = Field(
        description=(
            "Whether the caller's canonical `users.id` resolved. `unresolved` "
            "means their cloud-agent (`root_user`) ledger could NOT be looked up "
            "and is therefore ABSENT from these figures — it must not be read as "
            "'no cloud spend'. `not_applicable` for service-account callers, "
            "which have no canonical user row by design."
        ),
    )

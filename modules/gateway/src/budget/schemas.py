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

Rules 6-8 arrived with the multi-line envelope (U-2, #4399) and are stated on
``BudgetLine`` / ``CombinedInformational`` next to the fields they constrain:

6. **An uncapped line can never be the binding line** — see ``BudgetLine``.
7. **``principal_kind`` is structural**: ``service:``-rooted lines are excluded
   from per-person rollups — see ``BudgetLine``.
8. **The headline is never the sum of the lines**, and the combined figure carries
   no cap denominator — see ``CombinedInformational`` and ``MyBudgetResponse``.
   This is the hard acceptance gate of U-2.
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

# Which of the caller's two spend paths a line describes (Issue #4399, FR-2.1).
# `direct` is traffic they originated themselves (`entity_type="user"`, keyed by
# Cognito sub); `cloud` is the agent chains they triggered (`root_user`, keyed by
# canonical `users.id`). `None` for shared ancestors — a team/department/org line
# is neither, and labelling one "direct" would attribute a colleague's spend to
# the caller's own machine.
BudgetSource = Literal["direct", "cloud"]

# Whether a root principal is a person or an unattended trigger (FR-2.5).
# Derived from the `service:` id qualifier that `_qualify_root_principal_id`
# (`enforcement_service.py:93`) writes, NOT from a new sentinel — the qualifier
# is applied once where `attributed_user_id` is published, so the enforcement key
# and the settled ledger key are the same string by construction (#4344).
PrincipalKind = Literal["human", "service"]

# The `service:` namespace qualifier on a ROOT_USER entity id (#4344). A canonical
# `users.id` is a generated UUID and contains no colon, so no bare human id can
# ever equal a qualified value — which is what makes prefix matching a sound test
# of principal kind rather than a guess.
#
# Deliberately duplicated as a public constant rather than imported from
# `enforcement_service._SERVICE_PRINCIPAL_PREFIX`: that name is private, and this
# module is the contract of record that U-4 reads. Pinned equal to it by a test.
SERVICE_PRINCIPAL_QUALIFIER = "service:"


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


class BudgetLine(BaseModel):
    """One separately-capped line in the caller's envelope — Issue #4399 (U-2).

    A line is **one ledger row's worth of truth**: a single
    ``(org_id, entity_type, entity_id, period_type, period_start)`` read, its own
    cap, and the headroom that follows. Two lines are never added together to
    produce anything a client may present as a budget (FR-2.3) — that is the
    single hard gate of this unit.

    **Why the caller's direct and cloud lines cannot merge into one** (do not
    "fix" this): the ``user`` entity is keyed by Cognito **sub**, ``root_user`` by
    canonical **``users.id``**, and ``attributed_user_id`` is published only from a
    resolved run binding (``enforcement_service.py:797-802``) — so direct
    interactive traffic, which has no run binding, never writes a ``root_user``
    row. There is no ledger row and no cap anywhere in the data model equal to
    "everything this person set in motion". A fused envelope is therefore governed
    by no cap and is tracked separately as #4396; see
    ``requirements-analysis/envelope-composition.md`` for the full trace.

    Field rules beyond ``MyBudgetResponse``'s (each one is a test in
    ``tests/budget/test_envelope_composition.py``):

    6. **An uncapped line can never be the binding line.** With no cap it has no
       headroom to compare, so it cannot be the thing that stops anyone.
       Selecting one would render a ``null`` headroom as the headline, or let an
       unlimited line mask a genuinely constrained one.
    7. **``principal_kind`` is structural, not cosmetic.** A ``service:``-rooted
       line is an unattended trigger (CI, EventBridge, an alarm), not a person, so
       it is excluded from per-person rollups (FR-2.5). Rendering one as a human
       makes per-person cost truth wrong.
    """

    entity_type: str = Field(
        description=("Which ledger this line was read from — `user`, `root_user`, `team`, `department`, `org` or `service_account`.")
    )
    label: str = Field(
        description="Human-readable line name, e.g. `Direct usage (my machine)`. Server-supplied so two surfaces cannot word it differently."
    )
    source: BudgetSource | None = Field(
        description=(
            "`direct` for the caller's own traffic, `cloud` for chains they "
            "triggered, `null` for a shared ancestor (team/department/org), which "
            "is neither. See `BudgetSource`."
        ),
    )
    principal_kind: PrincipalKind = Field(
        description=(
            "`human` or `service`, from the `service:` id qualifier (#4344). A `service` line is excluded from per-person rollups — see field rule 7."
        ),
    )

    cap_usd: str | None = Field(description="The EFFECTIVE cap after the platform-ceiling clamp, at 2dp. `None` when `cap_status` is `uncapped`.")
    spend_usd: str = Field(
        description=(
            "Settled spend for this entity and period at 6dp, from the full "
            "5-filter predicate. Always present; with no usage row it is a true "
            "`0.000000`."
        )
    )
    remaining_usd: str | None = Field(
        description="Headroom (`cap - spend`) at 6dp. May be NEGATIVE when settled spend has passed the cap. `None` when uncapped."
    )
    utilization_pct: float | None = Field(
        description="Spend as a percentage of cap, to 1dp. `None` when uncapped, and `None` for a `$0` cap where no percentage is defined."
    )
    band: BudgetBand | None = Field(
        description="Warning band from the server-side 80/95 thresholds. `None` when uncapped. A `$0` cap is always `exceeded`."
    )
    cap_status: CapStatus = Field(
        description="`capped` if a budget row governs this line, `uncapped` if none does. Never collapsible into `cap_usd is None`."
    )
    enforcement_mode: str | None = Field(
        description=(
            "How this cap behaves when exceeded, straight off the `budget_configs` row: `hard` blocks, `soft` warns and allows. `None` when uncapped."
        ),
    )


class CombinedInformational(BaseModel):
    """The direct+cloud dollar total — **informational only, never a budget**.

    This model's **shape** is the guarantee, not its docstring. It carries a
    ``spend_usd`` and nothing else numeric: there is deliberately **no**
    ``cap_usd``, no ``remaining_usd``, no ``utilization_pct`` and no ``band``
    field *anywhere on it*. A frontend cannot bind a progress bar to a
    denominator that does not exist on the wire, so the "no `x / y` bar" rule
    (FR-2.4) is enforced by the type rather than by reviewer vigilance.

    ``is_budget`` is a ``Literal[False]`` for the same reason — it is unsettable,
    so no future code path can flip it true and start presenting this figure as a
    ceiling. **No cap governs this number and no ledger row contains it.**

    Two exclusions are baked into how the total is summed, both from
    ``envelope-composition.md``:

    * **Shared ancestors are excluded.** Team, department and org lines are not
      the caller's personal spend; folding them in would count colleagues' spend
      as the caller's.
    * **``service:``-rooted lines are excluded** (§5). The usage tracker gates the
      ``root_user`` write on ``if root_human_id:`` with no equality skip
      (``handler.py:467``), whereas enforcement *does* skip the root entity when
      the root is the caller (``enforcement_service.py:437``). For a service-rooted
      run the same dollar therefore lands on **both** the ``user`` and
      ``root_user`` rows, so summing across entity types would double-count it.
      Excluding service roots removes the double-count at the same time as it
      keeps unattended CI spend out of a person's envelope (FR-2.5).
    """

    spend_usd: str = Field(
        description=(
            "Sum of the caller's own capped/uncapped per-person lines (direct + "
            "cloud) at 6dp. NOT a budget and NOT enforced: no cap governs this "
            "figure and no ledger row equals it. Excludes shared ancestors and "
            "`service:`-rooted lines."
        )
    )
    is_budget: Literal[False] = Field(
        default=False,
        description=("Always `false`, and typed so it cannot be anything else. Present so a client cannot mistake this total for a cap (FR-2.4)."),
    )
    note: str = Field(description="Plain-language restatement of `is_budget` for surfaces that render the figure with a caption.")


class MyBudgetResponse(BaseModel):
    """The signed-in caller's own cap, settled spend and headroom for one period.

    The top-level cap/spend/headroom fields are the **headline**, and the headline
    is the *binding* line — the lowest-remaining capped entity across the caller's
    hierarchy, which is the line that will actually stop them first. U-2 (#4399)
    adds the multi-line envelope (``binding`` + ``lines`` +
    ``combined_informational``) **on top of** this shape rather than replacing it,
    so every U-1 client keeps working.

    **The headline is NEVER the sum of the lines** (FR-2.3) — the one hard
    acceptance gate of U-2. Enforcement evaluates each entity in the hierarchy
    separately, each against its own cap (``_check_entity_budget``). A summed
    headline would be a number that exists in no ledger row and is enforced by no
    cap, so the screen would say "exhausted" while enforcement stopped nothing —
    the precise screen-vs-enforcer disagreement EPIC #4324 exists to eliminate.
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

    # -----------------------------------------------------------------------
    # The multi-line envelope — Issue #4399 (U-2)
    # -----------------------------------------------------------------------

    binding: BudgetLine | None = Field(
        default=None,
        description=(
            "The line that will stop the caller first — the lowest-remaining "
            "CAPPED line across their hierarchy, the same selection "
            "`get_budget_status_for_headers` makes. This is the headline, and it "
            "carries the same figures as the top-level `cap_usd`/`spend_usd`/"
            "`remaining_usd` fields. `None` when nothing in the hierarchy is "
            "capped, since an uncapped line cannot bind — NOT an error, and not "
            "the same as a `$0` cap."
        ),
    )

    lines: list[BudgetLine] = Field(
        default_factory=list,
        description=(
            "The caller's per-person lines, most specific first: their `direct` "
            "line and — when their identity resolved — their `cloud` line. Each "
            "carries its OWN cap, spend, headroom, utilisation and band, because "
            "enforcement checks each separately (FR-2.1). Shared ancestors "
            "(team/department/org) are NOT listed here even though they can bind, "
            "since they are not the caller's personal spend; when one of them "
            "binds it appears as `binding` and the client should render it as the "
            "headline alongside these lines."
        ),
    )

    combined_informational: CombinedInformational | None = Field(
        default=None,
        description=(
            "The direct+cloud dollar total, INFORMATIONAL ONLY — no cap governs "
            "it and no ledger row equals it. Carries no denominator field by "
            "design, so no progress bar can be bound to it (FR-2.4). `None` when "
            "there is nothing to combine (fewer than two per-person lines). The "
            "fused per-person ENVELOPE, with a real cap, is #4396 and is "
            "deliberately not built here."
        ),
    )

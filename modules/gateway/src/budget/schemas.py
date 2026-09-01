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

Rule 9 arrived with the run drill-down (U-3, #4400) and is stated on
``CostFigure`` next to the fields it constrains:

9. **A run's cost is three-valued, never a bare number.** A lineage row with no
   matching ``usage_logs`` row is ``unknown``, never ``0`` — see ``CostFigure``.
   Cost back-fill is asynchronous, so that is the common case, not the edge case.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
        description="Human-readable line name, e.g. `Direct use (my machine)`. Server-supplied so two surfaces cannot word it differently."
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


class Freshness(BaseModel):
    """How complete the settled figures alongside this are — Issue #4477 (NFR-5).

    Every spend figure in this response is a **settled** total, and settlement is
    asynchronous: the gateway writes a ``usage_logs`` row the moment a request
    finishes, but the price on that row and the ``budget_usage`` accumulator this
    endpoint reads are both written later by the budget-usage-tracker Lambda
    (``bridge_cost_to_usage_logs``). For the minutes in between, a caller's real
    spend is genuinely higher than ``spend_usd`` says.

    NFR-5 requires that gap be surfaced rather than smoothed over: "recent spend
    may be incomplete, so the UI carries a freshness affordance rather than
    implying real-time truth". Someone who reads an understated figure as final
    keeps working under a cap they have already passed, which is the same
    screen-vs-reality disagreement EPIC #4324 exists to eliminate — just displaced
    in time rather than in scope.

    **Why an object rather than a bare boolean on the response.** The contract
    (``requirements-analysis/api-contract.md``) specifies an object, and
    ``BudgetEnvelopeResponse`` in ``frontend/src/types/budget.ts`` (#4402) declares
    one. Flattening it to ``freshness: true`` would make the frontend read
    ``undefined``, the affordance would silently never render, and tests written
    against mocks would still pass — the #3675 closed-loop failure. It is also the
    extension point: any further completeness caveat about these figures belongs
    here as a sibling field, not as another top-level boolean.
    """

    cost_backfill_lag: bool = Field(
        description=(
            "`true` when at least one of the caller's recent requests has been "
            "logged but not yet priced, so `spend_usd` is a LOWER BOUND and their "
            "real spend is higher. `false` means every recent request has settled "
            "— a positive statement that the figures are complete, not merely an "
            "absence of evidence. Never a permanent `true`: the probe behind it is "
            "bounded to a recent window, so a stale unpriced row cannot pin the "
            "affordance on until users learn to ignore it."
        )
    )


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

    # -----------------------------------------------------------------------
    # Settlement completeness — Issue #4477 (NFR-5)
    # -----------------------------------------------------------------------

    freshness: Freshness = Field(
        description=(
            "Whether asynchronous cost back-fill may have left the spend figures "
            "above incomplete — see `Freshness`. ALWAYS present and never `null`, "
            "unlike `binding`/`combined_informational`, so a client may read "
            "`freshness.cost_backfill_lag` unconditionally without a null guard. "
            "Deliberately NOT defaulted: a construction site that forgets it must "
            "fail loudly at composition rather than serve a fabricated 'settled' "
            "claim, which is the one wrong answer this field can give."
        ),
    )


# ---------------------------------------------------------------------------
# Run drill-down — Issue #4400 (U-3)
# ---------------------------------------------------------------------------
#
# The envelope above answers "how much have I spent". This answers "what spent
# it": the individual agent runs that contributed, each with its own cost.
#
# The models below carry ONE idea that the envelope models do not have to: a
# per-run cost may be genuinely **unknown**. Settled period spend is always a
# number (a missing `budget_usage` row is a true zero for that period — nothing
# has settled yet). A per-run figure is different: the run demonstrably exists,
# it demonstrably did work, and the ledger simply has no row for it yet, because
# cost back-fill is asynchronous. Rendering that as `$0.00` says the work was
# free. So cost gets a status, and `unknown` is structurally prevented from
# carrying an amount.


# The three-valued cost status. Pinned equal to `CostStatus` in
# `src/orchestration/cost.py` by a test, and duplicated here deliberately rather
# than imported: that module's enums live behind `OrchestrationFlow`/
# `OrchestrationNode` model imports, and this file is the contract of record that
# U-5 reads — the same reasoning as `SERVICE_PRINCIPAL_QUALIFIER` above.
#
# `none_incurred` and `unknown` both total zero dollars and mean opposite things,
# which is exactly why the status is on the wire instead of inferred from the
# amount (`frontend/src/utils/cost.ts`).
CostStatusValue = Literal["known", "none_incurred", "unknown"]

# Why a figure is `unknown`. Required — a bare "unknown" with no explanation
# reads as a UI bug, and `describeUnknownReason` (`frontend/src/utils/cost.ts`)
# maps each of these to a sentence.
#
# A SUPERSET of `UnknownReason` in `src/orchestration/cost.py`, pinned as one by a
# test: every reason that module can produce must be expressible here, because
# both describe the same three-valued contract to the same client. The extra
# member is `cost_store_unavailable` — the cross-store read this endpoint performs
# can fail as a whole (DDB lineage resolved, Postgres cost did not), and reporting
# that as `no_usage_rows` would assert something about the ledger that was never
# observed. `describeUnknownReason` degrades unmapped reasons to "Cost data
# unavailable for this item.", which is exactly right for it.
#
# The second extra is `lineage_unavailable`: the runs themselves could not be
# enumerated (the caller's canonical identity did not resolve), so there is no set
# of runs to have a cost. Reporting that as a `$0.00` subtotal would be the
# EPIC's headline failure — a screen saying "you have spent nothing" when the
# truth is "we could not look".
UnknownReasonValue = Literal[
    "no_usage_rows",
    "not_started",
    "not_costable",
    "non_gateway_path",
    "cost_store_unavailable",
    "lineage_unavailable",
]

# Stamped onto every cost figure. A total that silently excludes CodeBuild, EKS,
# NAT and storage reads as "what this cost", and someone will make a budget
# decision on it. Pinned equal to `COST_SCOPE_LABEL` in
# `src/orchestration/cost.py` and `frontend/src/utils/cost.ts` by a test.
COST_SCOPE_LABEL = "agent run costs only; excludes build/infra"

# Which of the caller's two spend paths a run is attributed through — the same
# distinction `BudgetSource` draws for envelope lines, reported per run so the
# drill-down can be read against the line it belongs to.
RunAttribution = Literal["direct", "cloud"]


class CostFigure(BaseModel):
    """A three-valued cost — **the status is authoritative over the number**.

    ``unknown`` is not a formatting concern; it is a different claim. The two
    zero-dollar states mean opposite things:

    * ``none_incurred`` — usage rows exist and they total zero. A *measured*
      zero, and ``$0.00`` is the honest rendering.
    * ``unknown`` — **no usage rows at all**. We do not know what this cost. It
      is emphatically not a claim that the work was free.

    Distinguishing them requires the **row count**, not the sum: ``SUM`` over
    zero rows is ``0``, indistinguishable from a real zero
    (``src/orchestration/cost.py``). ``get_cost_by_run_ids`` omits run ids with
    no rows from its result dict entirely, and that absence is the signal.

    **``unknown`` cannot carry an amount, and cannot omit its reason.** Both are
    enforced by a validator rather than left to callers, because an ``unknown``
    carrying ``0`` is precisely the shape that becomes ``$0.00`` three layers
    away — and this model is what U-5 renders. ``formatCostFigure``
    (``frontend/src/utils/cost.ts``) defends the same seam on the client; having
    the invariant on both sides is deliberate, since the API and the SPA deploy
    independently.

    ``partial`` applies to a figure that AGGREGATES others (the subtotal). A
    total missing an unmeasured contribution is a **lower bound**, and presenting
    it as exact is how decisions get made on wrong numbers. It is the
    aggregate-level counterpart of ``unknown``, mirroring ``AggregateCost.partial``
    in ``src/orchestration/cost.py`` and consumed by
    ``costTooltip(..., {partial: true})``.
    """

    status: CostStatusValue = Field(
        description=(
            "`known` (rows exist, total > 0), `none_incurred` (rows exist, total "
            "== 0 — a VERIFIED zero), or `unknown` (no rows — we do not know, and "
            "this is NOT a claim the work was free). Authoritative over "
            "`amount_usd`."
        )
    )
    amount_usd: str | None = Field(
        default=None,
        description=(
            "The amount at 6dp as a STRING, preserving `usage_logs.cost_usd`'s "
            "`NUMERIC(10,6)` precision — most individual agent calls are sub-cent, "
            "so a float round-trip or a 2dp render loses the figure entirely. "
            'ALWAYS `null` when `status` is `unknown`; never `"0"`.'
        ),
    )
    reason: UnknownReasonValue | None = Field(
        default=None,
        description=(
            "Why the figure is `unknown`. REQUIRED whenever it is, and `null` "
            "otherwise. `no_usage_rows` is the common one and means cost back-fill "
            "has not caught up — not that the run was free."
        ),
    )
    scope: str = Field(
        default=COST_SCOPE_LABEL,
        description="What the figure covers. Agent-run Bedrock spend only: it excludes CodeBuild, EKS compute, NAT and storage.",
    )
    partial: bool = Field(
        default=False,
        description=(
            "Only meaningful on an AGGREGATE figure (the subtotal). `true` means "
            "at least one contributing run is `unknown`, so the amount is a LOWER "
            "BOUND and must not be presented as an exact total."
        ),
    )

    @model_validator(mode="after")
    def _unknown_carries_no_amount(self) -> "CostFigure":
        """Keep absence from ever serialising as a number.

        Enforced here rather than trusted to call sites: this model is the wire
        shape U-5 renders, and an ``unknown`` carrying ``0`` is the one shape that
        silently becomes ``$0.00``. Same invariant as ``NodeCost.__post_init__``
        (``src/orchestration/cost.py``).
        """
        if self.status == "unknown":
            if self.amount_usd is not None:
                raise ValueError("an unknown cost must not carry an amount — that is how absence becomes $0.00")
            if self.reason is None:
                raise ValueError("an unknown cost must carry a reason; bare 'unknown' reads as a bug")
        elif self.amount_usd is None:
            raise ValueError(f"a {self.status} cost must carry an amount")
        return self


class BudgetRunItem(BaseModel):
    """One agent run that contributed to the caller's spend.

    Identity fields come from the DynamoDB lineage row; ``cost`` comes from
    Postgres ``usage_logs``. The two stores are joined **in Python, on run id**,
    never in SQL — see the route.

    ``run_id`` is the DynamoDB ``event_id`` (the agent-worker's ``message_id``),
    which is also ``usage_logs.agent_run_id``. It is deliberately NOT the
    DynamoDB attribute *named* ``run_id``, which is the KEDA job/pod name and
    matches no usage row at all — joining on that plausible-sounding field
    returns zero rows and reports every run as free
    (``src/orchestration/cost.py``, ``assert_join_key_is_event_id``).
    """

    run_id: str = Field(
        description=(
            "The run's id — the DynamoDB `event_id`, which is the join key into "
            "`usage_logs.agent_run_id`. NOT the KEDA job name that the DynamoDB "
            "`run_id` attribute holds."
        )
    )
    correlation_id: str | None = Field(
        default=None,
        description="The chain this run belongs to. Runs sharing one are one fan-out; `null` for a run with no chain context.",
    )
    persona: str | None = Field(default=None, description="Which agent persona ran, e.g. `developer`. `null` when the lineage row records none.")
    started_at: str | None = Field(default=None, description="ISO-8601 instant the run arrived (`arrived_at` on the lineage row).")
    status: str | None = Field(
        default=None,
        description="The run's last known status, e.g. `in_progress`, `complete`, `budget_stopped`. Rendered from the shared status config.",
    )
    cost: CostFigure = Field(
        description=(
            "Three-valued cost for this run. `unknown` when no `usage_logs` row "
            "exists yet — the common case for a recent run, since back-fill is "
            "asynchronous. Never `$0.00` for a missing row."
        )
    )
    attribution: RunAttribution = Field(
        description=(
            "Which of the caller's lines this run counts against: `direct` for "
            "their own traffic, `cloud` for a chain they triggered. Matches "
            "`BudgetLine.source` so a run can be read against the line it "
            "belongs to."
        )
    )


class MyBudgetRunsResponse(BaseModel):
    """The runs that contributed to the caller's spend in one period — U-3 (#4400).

    Scoped to the caller by construction: the lineage query is partitioned on
    their own canonical id, and the endpoint accepts **no** ``user_id`` or
    ``entity_id`` param, so there is no parameter to abuse (FR-3.3).

    **``subtotal`` and ``total_run_count`` describe THIS PAGE, not the period.**
    The issue bounds the request to one lineage query plus one batched cost
    lookup, both limited by page size, so a period-wide total is not available
    without reading every page — and a page figure silently labelled as a period
    total is the kind of wrong number this EPIC exists to eliminate. The
    period-wide settled total is what ``GET /me/budget`` reports.
    """

    items: list[BudgetRunItem] = Field(description="The runs on this page, newest first.")
    subtotal: CostFigure = Field(
        description=(
            "Total cost of the runs ON THIS PAGE — not the period. `partial` is "
            "`true` when any run on the page has `unknown` cost, in which case "
            "this is a LOWER BOUND. `unknown` when every run on the page is "
            "unknown, and `none_incurred` for an empty page (nothing on it cost "
            "anything, which is a measurement about the page)."
        )
    )
    total_run_count: int = Field(description="Number of runs on this page (`len(items)`). Not a period-wide count — see the class docstring.")
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque cursor for the next page; `null` means no more pages. A "
            "non-null cursor with few or zero items is normal — DynamoDB applies "
            "filters after the page read — so keep following it until it is null."
        ),
    )
    period: BudgetPeriod = Field(description="The calendar window these runs were selected from — the same window `GET /me/budget` reports.")
    identity_status: IdentityStatus = Field(
        description=(
            "Whether the caller's canonical `users.id` resolved. `unresolved` "
            "means chain-attributed runs could NOT be looked up and are therefore "
            "ABSENT from `items` — it must not be read as 'no cloud runs'."
        )
    )


# ---------------------------------------------------------------------------
# Managed (operator) scope — Issue #4401 (U-4)
# ---------------------------------------------------------------------------
#
# Everything above describes the SIGNED-IN CALLER. The models below describe a
# TARGET the caller named, which is the entire difference: U-4 is the only unit in
# the EPIC that accepts an entity other than the caller, so it is the only one
# carrying cross-tenant risk.
#
# The shapes deliberately REUSE `BudgetLine`, `CostFigure` and `BudgetPeriod`
# rather than restating them. An operator's view and a user's own view of the same
# entity must be the same numbers rendered the same way — a second set of
# money/cap/band fields for the operator surface is exactly how the two drift
# apart (the read/write asymmetry class of #4322).
#
# What is NOT on these models, and must not be added: any field describing the
# target beyond the id the caller already supplied — no name, no email, no member
# count, no "exists" flag. A denial reveals nothing (see the router), so a SUCCESS
# must not be the thing that reveals it either; a response shape carrying a
# display name would make a 200-vs-403 comparison an enumeration oracle by
# another route.


class ScopeRollupRow(BaseModel):
    """One member's contribution inside a container target — Issue #4401 (U-4).

    Emitted only for CONTAINER targets (team, department, org). A ``user`` or
    ``root_user`` target is a single principal and has no members to roll up, so
    it carries no rows at all rather than one row describing itself.

    **``principal_kind`` is required, and that is the point of this model.** The
    mockup's member table renders these rows as people; a ``service:``-rooted
    entry is CI, EventBridge or an alarm, not a person (FR-2.5). Rendering
    ``ci-bot`` as a colleague makes per-person cost truth wrong — and it is per
    person cost truth that a team lead opens this screen to get. The field is
    non-optional so a row cannot be constructed without answering the question;
    it is derived from the ``service:`` id qualifier (#4344), never from a
    display name, because a human can be *called* anything.

    The figures are a ``BudgetLine`` — the same model, from the same composition
    helpers, as the caller's own envelope lines. A rollup row is one ledger row's
    worth of truth exactly as an envelope line is.
    """

    entity_type: str = Field(description="Which ledger this member's row was read from — `user` or `root_user`.")
    entity_id: str = Field(
        description=(
            "The member's id, as it is keyed in the ledger: a Cognito `sub` for a "
            "`user` row, a canonical `users.id` (possibly `service:`-qualified) "
            "for a `root_user` row. Echoed rather than resolved to a name — see "
            "the module comment on why no display metadata is on this shape."
        )
    )
    principal_kind: PrincipalKind = Field(
        description=(
            "`human` or `service`, from the `service:` id qualifier (#4344). "
            "REQUIRED: a `service` row is an unattended trigger, not a person, and "
            "rendering one as a colleague is the FR-2.5 defect this field exists "
            "to prevent."
        )
    )
    line: BudgetLine = Field(
        description=("This member's cap, settled spend, headroom, utilisation and band — the SAME `BudgetLine` model the caller's own envelope uses.")
    )


class ManagedScopeBudgetResponse(BaseModel):
    """A target entity's cap, settled spend and headroom, for an operator — U-4.

    Same line model as ``MyBudgetResponse`` (U-2), read through the same helpers,
    so an operator's view of a user and that user's own view of themselves cannot
    disagree. What is added is a *target* and, for containers, per-member
    ``rollup``; what is deliberately NOT added is any second rendering of money.

    ``binding`` is the line that will stop the target first — the lowest-remaining
    CAPPED line for the entity. For a single principal there is one candidate
    line; the field is still present and still selected rather than summed,
    because the "headline is never a sum" rule (FR-2.3) is a property of the
    contract, not of how many lines happen to exist.
    """

    period: BudgetPeriod

    entity_type: str = Field(description="The target's entity type, echoed from the request path after allow-list validation.")
    entity_id: str = Field(description="The target's id, echoed from the request path. Never a resolved display name — see the module comment.")

    line: BudgetLine = Field(description="The target entity's own cap/spend/headroom, composed by the SAME helper as the caller's own lines.")
    binding: BudgetLine | None = Field(
        default=None,
        description=(
            "The lowest-remaining CAPPED line for this target, or `null` when it "
            "is uncapped — an uncapped line cannot bind (contract rule 6). "
            "Selected, never summed (FR-2.3)."
        ),
    )

    rollup: list[ScopeRollupRow] = Field(
        default_factory=list,
        description=(
            "Per-member contributions, for CONTAINER targets (team/department/org) "
            "only. Empty for a `user`/`root_user` target, which is a single "
            "principal with no members. Each row carries `principal_kind` so a "
            "service account is not rendered as a person (FR-2.5)."
        ),
    )


class ManagedScopeRunsResponse(BaseModel):
    """The runs that contributed to a TARGET's spend in one period — U-4.

    U-3's run list, rendered for an operator-named target instead of the caller.
    Identical shape and identical three-valued cost rules, because an operator
    reading a run's cost and the run's owner reading it must see the same figure.

    ``subtotal`` and ``total_run_count`` describe **this page**, not the period —
    the same bound U-3 carries, for the same reason: the request is one lineage
    query plus one batched cost lookup, so a period-wide total is not available
    without reading every page, and a page figure labelled as a period total is
    the class of wrong number this EPIC exists to eliminate.
    """

    items: list[BudgetRunItem] = Field(description="The target's runs on this page, newest first.")
    subtotal: CostFigure = Field(
        description=(
            "Total cost of the runs ON THIS PAGE — not the period. `partial` is "
            "`true` when any run on the page is `unknown`, making this a LOWER "
            "BOUND. `unknown` when every run on the page is unknown."
        )
    )
    total_run_count: int = Field(description="Number of runs on this page (`len(items)`). Not a period-wide count.")
    next_cursor: str | None = Field(default=None, description="Opaque cursor for the next page; `null` means no more pages.")
    period: BudgetPeriod = Field(description="The calendar window these runs were selected from.")

    entity_type: str = Field(description="The target's entity type, echoed from the request path after allow-list validation.")
    entity_id: str = Field(description="The target's id, echoed from the request path.")

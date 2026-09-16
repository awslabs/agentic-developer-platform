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

# The wire enum is the LADDER's own type, imported rather than restated (#4690).
# `person_ledger` is a deliberate leaf — models and shared schemas only — so this
# adds no cycle, and it means the set of values this contract advertises cannot
# drift from the set the resolver can actually return. A hand-copied Literal here
# would go stale the first time a rung is added (department scope is explicitly
# left room for).
from .person_ledger import PersonLimitSource

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


# ---------------------------------------------------------------------------
# Cross-org person view — Issue #4626 (C1 of #4620)
# ---------------------------------------------------------------------------
#
# Everything above describes ONE partition: the caller's active/attributed tenant.
# That is what enforcement reads and it stays exactly as it was. The two models
# below answer the different question #4620 was filed for — "how much have MY
# agents spent, everywhere they run" — for a person whose runs execute outside the
# tenant their session is in.
#
# The mechanics, from `docs/design-notes/4620-cross-org-person-budgets.md`:
# `budget_usage` is uniquely keyed `(org_id, entity_type, entity_id, …)`, the
# tracker writes every `root_user` row into the run's *attributed* tenant (#4132),
# and the `root_user` key is a canonical `users.id` that the webhook resolver looks
# up org-free. So the same person's spend is spread across partitions under keys
# that already match — the read is a widened `org_id` predicate, not an
# identity-stitching project (note §0.1).
#
# Two things make that widening safe rather than a tenant leak, and both are
# structural rather than a reviewer's care:
#
#   * The partition list is derived SERVER-SIDE from `tenant_memberships` (plus the
#     shadow-user `users.org_id` fallback), never from a request parameter. This
#     model appears only on the `/me` router, which accepts no scope parameter at
#     all (note §7.3).
#   * Only settled dollar TOTALS cross. No run detail, and never to anyone but the
#     person themselves — explicitly NOT to their home-org admin, whose reach stops
#     at their own partition (note §7.2).


class PerOrgLine(BaseModel):
    """One tenant's settled spend for the caller — both ledgers — for this period.

    **Widened by #4396.** This line used to carry cloud (``root_user``) spend only,
    because the person figure it fed was cloud-only. The operator ruling of
    2026-09-05 made the person's limit govern their TOTAL spend, so the line now
    reports ``direct_spend_usd`` beside ``cloud_spend_usd``: the components of the
    fused total, kept separate so a reader can add them up and audit the headline.

    **The two figures cannot double-count each other.** They come from rows that are
    disjoint by ``entity_type`` and by key namespace — ``user`` rows are keyed by
    Cognito sub, ``root_user`` rows by canonical ``users.id`` — and ``budget_usage``
    is uniquely keyed including ``entity_type``. Summing one of each sums each dollar
    once (the #4322 discipline satisfied, not bypassed).

    ``cap_usd`` is the cloud-agent cap **that tenant** authored for this person, if
    any. It is reported per line rather than folded into a single number because each
    one is independently authoritative: an org's own ``root_user`` cap governs spend
    inside that org and nothing else, and that layer is unchanged by #4620 and by
    #4396 (note §3.1). A ``null`` cap on a line with real spend is the
    mis-partitioned-cap signature the issue is about — the spend is accruing where no
    ceiling was authored.
    """

    org_id: str = Field(description="The tenant this line's ledger rows live in. One of the caller's own member tenants, derived server-side.")
    org_name: str = Field(
        description=(
            "Display name from `organizations.name`, falling back to `org_id` when "
            "the row is unreadable. Server-supplied so two surfaces cannot word one "
            "tenant differently."
        )
    )
    cloud_spend_usd: str = Field(
        description=(
            "Settled `root_user` spend in this tenant for this period at 6dp — the "
            "agent runs the caller triggered here. Read with the same full 5-filter "
            "predicate as every other figure here. A true `0.000000` when no usage "
            "row exists — a measurement, not a fallback."
        )
    )
    direct_spend_usd: str = Field(
        description=(
            "Settled `user` spend in this tenant for this period at 6dp — the "
            "caller's own direct, interactive use (#4396). Keyed by Cognito sub, so "
            "it is a DIFFERENT row from `cloud_spend_usd` and adding the two counts "
            "each dollar once. A true `0.000000` when no usage row exists. Governed "
            "by this tenant's own `user` cap, not by `cap_usd` below."
        )
    )
    cap_usd: str | None = Field(
        description=(
            "The cloud-agent (`root_user`) cap THIS tenant authored for the caller, "
            "at 2dp, or `null` when it authored none. Not clamped across tenants: "
            "each org's cap governs only spend executing inside it. It governs "
            "`cloud_spend_usd` only — `direct_spend_usd` answers to this tenant's "
            "`user` cap, which `lines`/`binding` render for the active partition."
        )
    )
    is_active_partition: bool = Field(
        description=(
            "`true` for the tenant the caller's session is attributed to — the one "
            "partition `lines`/`binding` above describe. Present so a client can "
            "show 'this workspace' without re-deriving it from the token."
        )
    )


class PersonEnvelope(BaseModel):
    """The caller's ONE number: everything they spent, everywhere — **and it is enforced**.

    Issue #4626, note §7.1, **fused and made load-bearing by #4396**.

    **What changed, and why it matters to anyone reading a figure from here.** This
    started as an informational cloud-agent-only total: no cap existed to compare it
    against, so it deliberately carried no denominator. Both halves of that premise
    are gone. The operator ruling of 2026-09-05 (thread on #4669/#4685) is that **a
    person's limit governs their TOTAL spend — direct use plus cloud agents, across
    all GitHub orgs — and they see ONE number tracked against it.** So:

    * ``spend_usd`` is now ``direct + cloud``, summed across every member partition.
    * ``BudgetEnforcementService._check_person_budget`` enforces against **this exact
      figure**, via the same ``_read_person_partition_spend`` that composes it. The
      displayed number IS the enforced number — not two derivations that agree today.

    **There is still no cap field here, and the reason has inverted rather than
    lapsed.** Before, a denominator would have advertised a ceiling nothing enforced.
    Now the ceiling is real but it lives on ``GET /me/budget/person-cap``, which is
    the one authoring-and-reading surface for it; restating it here would put one
    ceiling on two surfaces that can disagree. ``person_cap_routes.py`` refuses to
    return spend for the mirror-image reason. One figure, one home, each.

    **How the fused total avoids the #4322 double-count.** The two ledgers are
    disjoint by construction, not by a filter applied after the fact:

    * ``entity_type="user"`` rows are keyed by **Cognito sub** (direct traffic);
      ``entity_type="root_user"`` rows by **canonical ``users.id``** (chains the
      person triggered). ``budget_usage`` is uniquely keyed including
      ``entity_type``, so these are different rows holding different dollars.
    * A direct request writes only the ``user`` row — the tracker's ``!= user_id``
      gate suppresses the ``root_user`` row exactly when the root IS the caller.
    * A hosted run's ``user`` row is keyed by the shared **worker** identity, never
      by this person's sub, so it cannot enter the direct half.
    * ``org``/``team``/``department`` rows are never read here at all: those are
      shared ancestors, not this person's spend.

    **``service:``-qualified principals are excluded** (#4344): the key list is built
    from ``users`` primary keys and their subs, neither of which can carry the
    qualifier, so an unattended CI trigger is kept out of a human's personal envelope
    structurally rather than by a filter that could be dropped.

    Like every figure on this surface it is a **lower bound**:
    ``freshness.cost_backfill_lag`` applies to it, which is also why the enforcement
    layer's overshoot bound exists (``docs/budget-ratelimit.md``).
    """

    anchor: str = Field(
        description=(
            "The cross-org identity this total was fused on — `github:<numeric id>` "
            "when a GitHub identity is linked, else `users:<canonical id>`. The "
            "GitHub numeric id is the anchor because one person can hold a "
            "DIFFERENT `users.id` per tenant (note §3.3), so summing by canonical "
            "id alone under-reports for exactly the multi-org population this "
            "figure exists for."
        )
    )
    spend_usd: str = Field(
        description=(
            "The caller's ONE total at 6dp: the exact sum of every "
            "`per_org[].direct_spend_usd` AND `per_org[].cloud_spend_usd` across all "
            "their member tenants (#4396). This is the figure the personal spending "
            "limit is ENFORCED against — the cap itself is on "
            "`GET /me/budget/person-cap`. A LOWER BOUND like every figure here: "
            "`freshness.cost_backfill_lag` applies to this total too, which is why "
            "enforcement documents an overshoot bound."
        )
    )
    cloud_spend_usd: str = Field(
        description=(
            "The cloud-agent (`root_user`) component of `spend_usd` at 6dp — the "
            "agent runs the caller triggered, everywhere. Reported so the fused total "
            "is auditable: this plus `direct_spend_usd` equals `spend_usd` exactly."
        )
    )
    direct_spend_usd: str = Field(
        description=(
            "The direct-use (`user`) component of `spend_usd` at 6dp — the caller's "
            "own interactive traffic, everywhere. A different row namespace from "
            "`cloud_spend_usd`, so the two sum without re-counting a dollar."
        )
    )
    partition_count: int = Field(
        description=(
            "How many tenants contributed to `spend_usd`. Present so a client can "
            "say 'across N workspaces' rather than implying the figure is "
            "single-tenant, and so a caller can tell 'one partition, genuinely $0' "
            "from 'several partitions, genuinely $0'."
        )
    )
    is_budget: Literal[False] = Field(
        default=False,
        description=(
            "Always `false`, and typed so it cannot be anything else. It means THIS "
            "OBJECT carries no denominator — not that the figure is ungoverned. Since "
            "#4396 a personal limit IS enforced against `spend_usd`; the cap is served "
            "by `GET /me/budget/person-cap` and a client renders the two together. The "
            "field stays `false` so no bar can be bound to a ceiling this payload does "
            "not contain, which is what keeps the two surfaces from disagreeing."
        ),
    )
    note: str = Field(
        description=("Plain-language statement of what the figure covers and what governs it, for surfaces that render it with a caption.")
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
    # Cross-org person view — Issue #4626 (C1 of #4620)
    # -----------------------------------------------------------------------

    per_org: list[PerOrgLine] = Field(
        default_factory=list,
        description=(
            "The caller's settled cloud-agent spend PER member tenant, active "
            "partition first. Everything above this field describes ONE partition "
            "(the attributed tenant enforcement reads); this describes all of them, "
            "because a person whose runs execute outside their session's tenant sees "
            "`$0` above while real dollars accrue elsewhere (#4620). Empty when the "
            "caller's canonical identity did not resolve — never a fabricated "
            "single-tenant `$0` line."
        ),
    )

    person_envelope: PersonEnvelope | None = Field(
        default=None,
        description=(
            "The cross-org sum of `per_org[].cloud_spend_usd`, INFORMATIONAL ONLY — "
            "no cap governs it and no ledger row equals it, so it carries no "
            "denominator field by design (see `PersonEnvelope`). `None` when there "
            "are no per-org lines to sum, i.e. when the caller's identity did not "
            "resolve. Present even for a single partition, unlike "
            "`combined_informational`: 'this is your total everywhere' is a distinct, "
            "useful claim when the count is one, and a client that hid it would show "
            "nothing to the person whose spend has not yet crossed a boundary."
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


class MisPartitionedCapRow(BaseModel):
    """One ``root_user`` cap that has never accrued in its own partition — #4627.

    Design note ``4620-cross-org-person-budgets.md`` §8.2. The row describes a cap
    the operator authored, in the partition they authored it in, that no settled
    ``budget_usage`` row has ever matched. That is the mis-partitioned signature:
    the cap's key and the accrual's key are the *same string*, and only ``org_id``
    differs (§2), so the cap displays, accrues nothing, and stops nothing.

    **Every field describes the caller's OWN partition, except one boolean.** §7.2
    of the note draws the disclosure line precisely: a foreign org's dollar totals
    are that org's cost data and must not reach an admin of an unrelated tenant on
    the basis that a person is shared. So there is no foreign ``org_id`` here, no
    foreign spend figure and no foreign run detail — only
    ``accrues_elsewhere``, which says *whether* a matching accrual exists somewhere
    the caller cannot see. A bare boolean is not a total; it is the minimum needed
    to tell "authored in the wrong partition" from "authored correctly and quiet",
    and without it the report is a list of maybes nobody can act on.

    ``accrues_elsewhere=false`` is deliberately still reported rather than
    filtered out. §8.2: "a dormant cap is not proof of misconfiguration (a person
    may simply not have run yet), which is exactly why this reports rather than
    migrates." Suppressing those rows would make the report *look* authoritative
    while hiding the cap that is about to become mis-partitioned the moment its
    owner runs somewhere else.
    """

    org_id: str = Field(description="The partition the cap was authored in — always the caller's own resolved partition, never a foreign one.")
    entity_id: str = Field(
        description=(
            "The cap's key: a canonical `users.id`, possibly `service:`-qualified "
            "(#4344). Echoed as stored, because it is the string that has to match "
            "an accrual for the cap to ever bind."
        )
    )
    display_name: str | None = Field(
        default=None,
        description=(
            "The person's name or email, resolved org-scoped from `users`. `null` "
            "for a `service:`-qualified principal, which has no `users` row by "
            "design — the caller renders the raw id, the honest label for an "
            "automation."
        ),
    )
    principal_kind: PrincipalKind = Field(
        description=(
            "`human` or `service`, from the `service:` id qualifier (#4344). A "
            "`service`-rooted cap is an unattended trigger, not a person, and a "
            "report that renders one as a colleague makes the operator chase the "
            "wrong fix."
        )
    )
    period_type: str = Field(description="The cap's period type, echoed from the row. Part of the predicate that found no matching accrual.")
    cap_usd: str = Field(
        description=("The cap as authored, at 2dp (contract rule 1). The RAW row, not the platform-clamped effective cap — see the module comment.")
    )
    enforcement_mode: str = Field(
        description=("`soft` or `hard`, echoed from the row. A `hard` cap that can never match is the more alarming case of the two.")
    )
    accrues_elsewhere: bool = Field(
        description=(
            "`true` when a settled `root_user` accrual for the SAME person exists "
            "in a partition other than this one — the cap can never match, and "
            "this is the operator's confirmed defect. `false` means no accrual "
            "exists anywhere yet: the cap is merely dormant, which §8.2 says is "
            "not proof of misconfiguration. A boolean, never a figure: a foreign "
            "org's dollars do not cross this surface (§7.2)."
        )
    )


class MisPartitionedCapReport(BaseModel):
    """The mis-partitioned ``root_user`` cap report — #4627, note §8.2.

    **Read-only and detection-only.** Nothing here mutates a cap, and no endpoint
    on this router does either: §8.1 rules that existing caps stay exactly where
    they are, because moving one silently changes what stops a workload. The
    remedy is the operator's, in two steps (§8.3); this report is what tells them
    which caps need it.

    ``rows`` is ordered with ``accrues_elsewhere=true`` first, so the confirmed
    defects are what an operator sees without scrolling, then by ``entity_id`` and
    ``period_type`` for a stable order across calls.
    """

    org_id: str = Field(description="The partition reported on, resolved SERVER-SIDE from `tenant_memberships`. Never read from a request parameter.")
    rows: list[MisPartitionedCapRow] = Field(
        default_factory=list,
        description=(
            "The `root_user` caps in this partition with no matching settled "
            "accrual, confirmed cross-partition cases first. An empty list means "
            "every `root_user` cap here has accrued at least once — it never means "
            "the check could not run, which is a `503`."
        ),
    )
    total_row_count: int = Field(
        description=("`len(rows)`. The report is not paginated: `root_user` caps are per-person and one partition's set is small.")
    )
    accrues_elsewhere_count: int = Field(
        description=(
            "How many rows are CONFIRMED mis-partitioned (`accrues_elsewhere=true`). The number an operator acts on; the rest are dormant caps."
        )
    )


# ---------------------------------------------------------------------------
# The person-level cap — Issue #4629 (#4620 · C3)
# ---------------------------------------------------------------------------
#
# Design note `docs/design-notes/4620-cross-org-person-budgets.md` §4.
#
# Everything above is keyed on a tenant. These two models are the first budget
# shapes in this module that are NOT: a person-level cap is stored partition-free
# in `person_budget_configs`, keyed on the person's cross-org anchor, because a
# person's runs execute in whichever tenant the work is in (#4620).
#
# Money is still a JSON string at the cap column's own precision (contract rule 1),
# and "no cap authored" is still not "a cap of $0" (rule 2) — expressed here as a
# `null` `cap` object rather than a zeroed one.
#
# What is deliberately NOT on these models:
#
# * **No spend, headroom, utilisation or band.** This unit is storage and authoring
#   only. Rendering the cap against a cross-org denominator is C1 (#4626), which
#   adds `person_envelope` to `GET /me/budget`; putting a second, separately-derived
#   spend figure here is how the two surfaces come to disagree about the same
#   dollars (the #4322 read/write asymmetry class).
# * **No `enforcement_mode` on the REQUEST.** The person layer is informational in
#   this unit and `soft` is the only value the API writes. `hard` requires the §5.7
#   ruling and lands with C4 (#4630). Accepting the field now would let a client
#   author a cap that claims to stop spend while nothing reads it.


class PersonCapRequest(BaseModel):
    """Author (or re-author) a person's own platform-wide spend limit — C3.

    One field, on purpose. ``period_type`` is in the path, the anchor is derived
    server-side (or, for a platform admin, taken from the path and validated), and
    ``enforcement_mode`` is not client-settable in this unit — see the section
    comment above. A request body that cannot name a target cannot author a cap for
    somebody else, which is the same structural-scoping argument ``me_routes.py``
    makes for the read path.

    ``gt=0`` matches ``BudgetCreateRequest`` (``src/shared/schemas/budget.py``): a
    limit of exactly zero would be indistinguishable in every downstream reader
    from "no limit authored", and a negative one has no meaning at all. Someone
    who wants no ceiling deletes the row.
    """

    budget_amount_usd: Decimal = Field(
        gt=0,
        le=Decimal("99999999.99"),
        decimal_places=2,
        description=(
            "The person's total spend ceiling for one calendar period, across every "
            "organization. 2dp, matching the `NUMERIC(10,2)` column; `le` is that "
            "column's maximum — without it an over-range amount passes validation "
            "and dies in Postgres as a numeric-field overflow, which the routes' "
            "fault mapping would misreport as a retryable 503 backend failure "
            "(and SQLite-backed tests would never catch, since SQLite ignores "
            "NUMERIC precision). Must be > 0 — removing a limit is a DELETE, not "
            "a `0`."
        ),
    )


class PersonCapResponse(BaseModel):
    """A person's platform-wide limit, or the explicit absence of one — C3.

    ``cap_usd`` is ``None`` exactly when no row exists, and the caller is told so
    by ``cap_status`` rather than having to infer it from a zero (contract rule 2,
    applied to this table).

    ``enforcement_mode`` is on the wire even though it is not settable, because a
    client MUST be able to tell an informational limit from an enforcing one. When
    C4 (#4630) makes `hard` reachable, the same field carries it and no client
    needs a new one — and until then, a surface reading this cannot claim spend
    will be stopped.
    """

    person_anchor: str = Field(
        description=(
            "The cross-org person key this limit is stored against, "
            "`github:<numeric_user_id>`. Deliberately NOT a `users.id`: a person "
            "onboarded into two orgs has two of those, so a cap keyed on one would "
            "miss their spend in the other."
        )
    )
    period_type: Literal["daily", "weekly", "monthly"] = Field(
        description="The calendar period the limit applies to. Run/chain caps are not calendar periods and have no person-level equivalent."
    )
    cap_usd: str | None = Field(
        description="The authored limit at 2dp, or `null` when none is authored. `null` is NOT `0.00` — see `cap_status`.",
    )
    cap_status: CapStatus = Field(
        description="`capped` when a limit row exists for this person and period, `uncapped` when none does.",
    )
    enforcement_mode: str | None = Field(
        description=(
            "`hard` — every write since C4 (#4630): the limit DENIES attributed "
            "agent requests across every organization once the settled cross-org "
            "total passes it. `soft` — a row authored before C4, still "
            "informational until re-saved (nothing is denied). `null` when "
            "uncapped. A client MUST render `soft` as not-enforcing and `hard` as "
            "enforcing; claiming either the other way is the screen/behavior "
            "disagreement #4620 exists to close."
        ),
    )
    updated_at: str | None = Field(
        description="ISO-8601 instant the limit was last authored, or `null` when uncapped.",
    )
    source: PersonLimitSource | None = Field(
        default=None,
        description=(
            "WHERE the reported limit comes from (#4690), and therefore who can "
            "change it. `own` — an individual row this person authored; they may "
            "lower it freely. `admin` — an individual row a platform admin "
            "authored for them. `team_default` / `org_default` / `platform_default` "
            "— no individual row exists and a DEFAULT rule governs them; the "
            "number is a ceiling they may set themselves BELOW but not above. "
            "`null` only when `cap_status` is `uncapped`, i.e. no rule of any kind "
            "applies. A client that renders a default as if it were the person's "
            "own limit invites them to raise it and collect a 422."
        ),
    )
    source_label: str | None = Field(
        default=None,
        description=(
            "The same provenance in prose, ready to show a person — e.g. `your own "
            "limit`, `platform default`, `org default for acme-corp`. Composed "
            "server-side so this string, the 402 denial text and the 422 ceiling "
            "rejection cannot describe the same rule differently. `null` when "
            "uncapped."
        ),
    )


# ---------------------------------------------------------------------------
# DEFAULT person limits at platform/org/team scope — Issue #4690 (D1)
# ---------------------------------------------------------------------------
#
# `PersonCapRequest`/`PersonCapResponse` above are about ONE person's row. These two
# are about a RULE — "$1,000/month each, unless we say otherwise" — that governs
# every current and future member of a scope. The distinction is why they are
# separate models rather than an optional `scope` field on the pair above: the
# target of a default is a scope, not a person, and a model that could express
# either would put an anchor and a scope in one field.
#
# The scope itself is NOT in the body. It is in the path (`platform`, `org:<id>`,
# `team:<org>:<team>`), so the request model has nothing to name — the same
# structural-scoping argument the self-service cap routes make.


class PersonDefaultRequest(BaseModel):
    """Author (or re-author) the default person limit for one scope and period — #4690.

    One field, matching ``PersonCapRequest`` exactly: the scope is in the path, the
    period is a query parameter, and ``enforcement_mode`` is not client-settable (a
    default that silently does not enforce is the #4511 inert-cap class at platform
    scale, so ``hard`` is the only value written).

    The constraints are ``PersonCapRequest``'s, deliberately identical because the
    two columns are identical ``NUMERIC(10,2)``: a default a client could express but
    an individual row could not would be a ceiling nobody could comply with.
    """

    budget_amount_usd: Decimal = Field(
        gt=0,
        le=Decimal("99999999.99"),
        decimal_places=2,
        description=(
            "The default total spend ceiling, per person, for one calendar period "
            "across every organization. Applies to everybody in the scope who has "
            "no individual limit and no tighter-scoped default. 2dp, matching the "
            "`NUMERIC(10,2)` column; `le` is that column's maximum (see "
            "`PersonCapRequest` for why an unbounded value would surface as a "
            "misleading 503). Must be > 0 — removing a default is a DELETE, not a "
            "`0`, which would be a real ceiling of zero dollars applied to "
            "everybody in the scope."
        ),
    )


class PersonDefaultResponse(BaseModel):
    """A scope's default person limit, or the explicit absence of one — #4690.

    Same two contract rules as every other cap shape here: money is a string at the
    column's precision (rule 1), and "no default authored" is a distinct
    ``cap_status`` rather than a ``0.00`` (rule 2) — a zeroed default would read as
    "nobody in this scope may spend anything", which is the opposite of what an
    absent rule means.

    No spend and no member count: this is the authoring surface for a rule. How many
    people it currently governs, and what they have spent, is the admin/person UI
    (#4691) reading the existing cross-org figures — deriving a second copy here is
    the #4322 class.
    """

    scope_type: Literal["platform", "org", "team"] = Field(
        description=(
            "Which rung this default sits on. The ladder is individual row > team default > org default > platform default, tightest scope first."
        )
    )
    scope_id_org: str | None = Field(
        description="The organization this default is scoped to, or `null` on the platform rung.",
    )
    scope_id_team: str | None = Field(
        description=(
            "The team this default is scoped to, `null` on every rung but `team`. "
            "Always accompanied by `scope_id_org`, because a team id is unique only "
            "inside its own organization."
        ),
    )
    period_type: Literal["daily", "weekly", "monthly"] = Field(
        description="The calendar period the default applies to. Run/chain caps are not calendar periods and have no person-level equivalent."
    )
    cap_usd: str | None = Field(
        description="The authored default at 2dp, or `null` when this scope and period have none. `null` is NOT `0.00` — see `cap_status`.",
    )
    cap_status: CapStatus = Field(
        description="`capped` when a default rule exists for this scope and period, `uncapped` when none does.",
    )
    enforcement_mode: str | None = Field(
        description=(
            "`hard` for every default (#4690): the rule DENIES once a governed "
            "person's settled cross-org total passes it. `null` when uncapped. "
            "Unlike an individual row there is no `soft` case — no generation of "
            "these rows was ever promised to be informational."
        ),
    )
    updated_at: str | None = Field(
        description="ISO-8601 instant the default was last authored, or `null` when none is authored.",
    )


# ---------------------------------------------------------------------------
# Member spend-against-limit, list-shaped — Issue #4847 (T2b)
# ---------------------------------------------------------------------------
#
# The admin console's Members panel renders one row per person: this month's spend,
# the limit that applies to them, and which rung of the ladder that limit came from.
# Every other surface answering that question answers it for ONE person
# (`/budget/person-cap/{anchor}`) or for the caller (`/me/budget/person-cap`), so a
# panel listing thirty members had no read to make but thirty of them.
#
# Deliberately READ-ONLY and deliberately composed from the SHARED resolvers
# (`resolve_applicable_person_limits`, `read_person_partition_spend`) rather than from
# queries of its own. That is the standing rule in `person_ledger.py`: the displayed
# number IS the enforced number, and a second summation is how a dashboard and a 402
# come to disagree about whether somebody is over their limit. There is no new budget
# logic here — only an existing figure, per member, in one response.


class MemberBudgetResponse(BaseModel):
    """One member's month spend against the limit that governs them — #4847.

    Both money fields are strings at their column's precision (contract rule 1), and
    each is rendered at the precision of the column it came from, not a shared one:
    ``spend_usd`` is ``NUMERIC(14,6)`` and ``limit_usd`` is ``NUMERIC(10,2)``.

    ``limit_usd`` is ``None`` exactly when nothing governs this person for the period
    — no individual row and no default at any rung — and ``limit_status`` says so
    (contract rule 2). A ``0.00`` here would render as somebody who may spend
    nothing, which is the opposite of what an absent rule means, and it is the
    difference between a usage bar the panel must not draw and one showing 0%.
    """

    user_id: str = Field(
        description=(
            "The canonical `users.id`. The column the membership routes and the "
            "person-scoped rule resolvers both key on, so a client can join this row "
            "to a membership row without re-deriving an identity."
        )
    )
    person_anchor: str | None = Field(
        description=(
            "The cross-org person key the individual limit would be stored against, "
            "or `null` for a member with no linked identity to anchor one to. `null` "
            "is a legitimate permanent state (email/invite onboarding), never an "
            "error: such a member can hold no individual row, but a DEFAULT still "
            "governs them — skipping the ladder's top rung is not skipping the ladder."
        )
    )
    spend_usd: str = Field(
        description=(
            "Settled spend for this period in THIS organization, at 6dp. Scoped to "
            "the org in the path, not the person's cross-org total: this is an "
            "org-admin-facing panel and the figure is read per partition. A true "
            "`0.000000` when the person has no settled row — a measurement, not a "
            "fallback."
        )
    )
    limit_usd: str | None = Field(
        description=(
            "The limit that GOVERNS this person for the period, at 2dp, or `null` when nothing does. `null` is NOT `0.00` — see `limit_status`."
        ),
    )
    limit_status: CapStatus = Field(
        description="`capped` when any rule governs this person for the period, `uncapped` when none does at any rung.",
    )
    source: PersonLimitSource | None = Field(
        description=(
            "Which rung supplied `limit_usd` — the same values, with the same "
            "meanings, as `PersonCapResponse.source`. `null` only when "
            "`limit_status` is `uncapped`. Reported so the panel can label the "
            "number's provenance (`individual limit` / `team default` / `org "
            "default`) instead of implying every figure was set for that one person."
        ),
    )
    source_label: str | None = Field(
        description=(
            "The same provenance in prose, composed server-side so this panel, the "
            "402 text and the person's own screen name the rung identically. "
            "`null` when uncapped."
        ),
    )


class MemberBudgetListResponse(BaseModel):
    """Month spend and applicable limit for a page of an organization's members — #4847."""

    items: list[MemberBudgetResponse]
    total: int
    page: int
    page_size: int
    has_more: bool
    period_type: Literal["daily", "weekly", "monthly"] = Field(
        description="The calendar period every row was resolved for, echoed back so a client cannot mislabel the column it renders.",
    )
    period_start: str = Field(
        description="ISO-8601 date the period began — the `period_start` the spend rows were read at, so the figure is reproducible.",
    )

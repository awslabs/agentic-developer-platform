"""Which capacity is eligible, whether its price is fresh, and what needs approval.

Issue #5061 (U19), EPIC #4910. R18, the placement half.

## The three questions this module keeps separate

The story requires that placement "retain eligible-capacity filtering (GPU/CPU/RAM,
image, locality/network, quota, deadline and cost), freshness/recheck at allocation,
explicit stale-price handling and approval for forbidden location/settings/spend
changes". Those are three different questions and this module refuses to merge them:

1. **Is this candidate eligible at all?** `eligible_candidates` — a hard filter. An
   ineligible candidate is not a more expensive option, it is not an option.
2. **Is the price I am about to allocate against still current?** `recheck_freshness`
   — asked *at allocation*, not at ranking, because the gap between the two is where a
   price goes stale.
3. **Does this change need a human?** `requires_approval` — location, settings and
   spend increases are not the adapter's to make.

Merging any two of them produces the failure the separation prevents. Merging (1) and
(3) lets a forbidden region change look like an eligibility miss and get silently
routed around. Merging (2) into ranking means the freshness check happens against the
whole candidate list at ranking time and is stale again by allocation.

## Why cost ordering is imported, not reimplemented

`spike.harness.select_options` already encodes the baseline's cheapest-first ordering,
including upstream's `COST_TIE_EPSILON` pairwise comparator and the rule that a zero
spot price means "unknown", not "free". This module calls it.

It does **not** extend it. `spike/tests/test_parity_harness.py` pins that function's
parameter list with `inspect.signature` to exactly `["pricing", "prefer_spot"]`, and
that pin is correct: `select_options` is a claim about *captured baseline behavior*, so
a new ADP rule added as a parameter would make the harness assert behavior the baseline
does not have. That is precisely the defect its docstring records having already been
fixed once — the earlier revision that applied an `/enabled_clouds` filter to every
candidate, which would have failed a faithful adapter and passed a diverging one.

So eligibility composes *in front of* the baseline ordering: filter to eligible rows
here, then hand those rows to `select_options`. The baseline ordering stays a statement
about the baseline; the ADP eligibility rules stay a recorded migration decision.

## Why a stale price is a refusal and not a warning

`PlacementDecision.allocatable` is False when the quote that won is stale. It would be
easy to allocate anyway and log — the price is probably close, and the workload is
waiting. The reason not to is that a stale price is the input to two later claims: the
relocation record's cost provenance, and C's reservation. Allocating against a price
nobody rechecked means both inherit a number no observation supports, which is the
`consolidator.go:419-425` pattern `accounting.py` documents — advancing past an
unconfirmed step until a status field asserts something no evidence backs.

## What this module does not do

* **No spend total, no budget, no reservation.** C owns the ledger. `PriceQuote` holds
  an hourly rate a provider quoted, and `RelocationRecord` retains what was quoted at
  each attempt. Neither sums anything into a spend figure. Same rule `accounting.py`
  states for A and the same one `observation.BudgetUsage` states for enforcement.
* **No provider call.** Quotes arrive as arguments. There is no client here, so there
  is no live pricing path and no credential.
* **No approval store.** `requires_approval` returns *that* approval is needed and for
  what; it does not grant, record or check one. Per the story, "A owns no replacement
  Jobs, approval store or budget ledger".
* **No live migration.** `relocate` records an old attempt and a new one. The story is
  explicit that relocation "is not silently called live migration", so the type is
  named `RelocationRecord` and it carries both attempts rather than replacing one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from spike.harness import COST_TIE_EPSILON, select_options
from superplane_contracts import ContractViolation

# How old a quote may be at allocation time before it must be rechecked.
#
# Five minutes, matching the freshness window `auth.verify_submission` uses for an
# observation, and for the same reason: it is long enough that a normal
# provision->allocate sequence does not re-quote needlessly, and short enough that a
# price is still meaningfully current. It is a default, not a policy — every entry
# point takes `max_age` so a caller with a tighter requirement can say so.
DEFAULT_QUOTE_MAX_AGE = timedelta(minutes=5)


class EligibilityFailure(str, Enum):
    """Why a candidate is not eligible.

    ``str``-valued for the same reason as the contracts' enums: the wire form is a
    stable string rather than an ordinal that shifts when a member is inserted.

    These are *categories*, and a decision records every one that applies rather
    than the first. A caller told only "ineligible" cannot tell an under-specified
    request from an exhausted region, and those have opposite remedies.
    """

    GPU_TYPE = "gpu_type"
    """Wrong accelerator model, or none offered."""

    GPU_COUNT = "gpu_count"
    """Fewer accelerators than requested."""

    CPU = "cpu"
    """Fewer vCPUs than requested."""

    MEMORY = "memory"
    """Less RAM than requested."""

    IMAGE = "image"
    """The required node image is not offered here."""

    LOCALITY = "locality"
    """Outside the permitted regions."""

    NETWORK = "network"
    """Does not offer the required network capability (e.g. the workspace VPC peering
    or the interconnect a multi-node job needs)."""

    QUOTA = "quota"
    """The provider quota remaining is below the request."""

    DEADLINE = "deadline"
    """Cannot be provisioned before the deadline."""

    COST = "cost"
    """Above the caller's cost ceiling."""

    UNAVAILABLE = "unavailable"
    """The provider reports no capacity."""


class ApprovalRequired(str, Enum):
    """A change class the adapter may not make on its own authority."""

    LOCATION = "location"
    """Moves the workload to a different region or cloud."""

    SETTINGS = "settings"
    """Changes a resource setting the request pinned — image, network, spot/on-demand."""

    SPEND = "spend"
    """Costs more per hour than the approved rate."""


class PricingMode(str, Enum):
    """The rate a placement decision selected and later accounting must retain."""

    ON_DEMAND = "on_demand"
    SPOT = "spot"


@dataclass(frozen=True)
class CapacityRequest:
    """What the workload needs, and what it is not allowed to change to get it.

    Frozen, like every type in `superplane_contracts`, and validated in
    `__post_init__` raising `ContractViolation` — the same convention, so a caller
    catching contract violations at its input boundary catches these too.

    ``permitted_regions`` empty means "no locality restriction", matching
    `filter_by_configured_clouds`'s treatment of an empty cloud tuple. That is
    deliberate consistency with the baseline rather than a separate convention.
    """

    workspace: str
    gpu_type: str
    gpu_count: int
    min_vcpus: int = 0
    min_memory_gb: int = 0
    required_image: str = ""
    required_network: str = ""
    permitted_regions: frozenset[str] = field(default_factory=frozenset)
    permitted_clouds: frozenset[str] = field(default_factory=frozenset)
    max_hourly_cost: float | None = None
    deadline: datetime | None = None
    allow_spot: bool = True

    def __post_init__(self) -> None:
        for name in ("workspace", "gpu_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"CapacityRequest.{name} is required")
        if not isinstance(self.gpu_count, int) or self.gpu_count < 1:
            raise ContractViolation(
                "CapacityRequest.gpu_count must be a positive integer"
            )
        for name in ("min_vcpus", "min_memory_gb"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ContractViolation(
                    f"CapacityRequest.{name} must be a non-negative integer"
                )
        if self.max_hourly_cost is not None and self.max_hourly_cost <= 0:
            raise ContractViolation(
                "CapacityRequest.max_hourly_cost must be positive when set"
            )
        # Same timezone rule the contracts apply everywhere: a naive datetime is a
        # deadline in an unstated zone, which compares wrongly rather than loudly.
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ContractViolation("CapacityRequest.deadline must be timezone-aware")


@dataclass(frozen=True)
class PriceQuote:
    """One provider's offer, and when it was quoted.

    ``quoted_at`` is not optional and must be aware. A quote whose age cannot be
    computed cannot be rechecked, and an unrecheckable price is exactly what
    `recheck_freshness` exists to refuse — so allowing it in would create a candidate
    that silently bypasses the freshness gate.

    ``provisioning_eta`` is how long this provider says it needs. It feeds the
    deadline check; without it a deadline could only be checked after provisioning
    started, which is too late to pick a different provider.
    """

    cloud: str
    region: str
    instance_type: str
    gpu_type: str
    gpu_count: int
    hourly_cost: float
    quoted_at: datetime
    spot_cost: float = 0.0
    vcpus: int = 0
    memory_gb: int = 0
    images: frozenset[str] = field(default_factory=frozenset)
    networks: frozenset[str] = field(default_factory=frozenset)
    quota_remaining: int | None = None
    provisioning_eta: timedelta | None = None
    available: bool = True

    def __post_init__(self) -> None:
        for name in ("cloud", "region", "instance_type", "gpu_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"PriceQuote.{name} is required")
        if not isinstance(self.gpu_count, int) or self.gpu_count < 1:
            raise ContractViolation("PriceQuote.gpu_count must be a positive integer")
        if self.hourly_cost <= 0:
            raise ContractViolation("PriceQuote.hourly_cost must be positive")
        # A negative spot price is not a cheaper price, it is a bad reading.
        if self.spot_cost < 0:
            raise ContractViolation("PriceQuote.spot_cost cannot be negative")
        for name in ("vcpus", "memory_gb"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ContractViolation(
                    f"PriceQuote.{name} must be a non-negative integer"
                )
        if self.quoted_at.tzinfo is None:
            raise ContractViolation("PriceQuote.quoted_at must be timezone-aware")

    @property
    def hardware_provenance(self) -> tuple[tuple[str, str], ...]:
        """What hardware this quote is for, as ordered pairs.

        The story requires relocation to record "hardware provenance". This is that
        record: cloud, region and instance type identify the machine class a cost was
        incurred on, so a later reader can tell whether two attempts' costs are even
        comparable. Ordered pairs rather than a dict so the record stays hashable and
        comparable in a frozen dataclass.
        """
        return (
            ("cloud", self.cloud),
            ("region", self.region),
            ("instance_type", self.instance_type),
            ("gpu_type", self.gpu_type),
            ("gpu_count", str(self.gpu_count)),
        )

    def age(self, now: datetime) -> timedelta:
        """How old this quote is at ``now``."""
        if now.tzinfo is None:
            raise ContractViolation("now must be timezone-aware")
        return now - self.quoted_at

    def is_fresh(
        self, now: datetime, max_age: timedelta = DEFAULT_QUOTE_MAX_AGE
    ) -> bool:
        """Whether this quote is still current enough to allocate against.

        A quote from the future is not fresh. Clock skew between the pricing source
        and this process is a reason to re-quote, not a reason to trust a negative
        age — treating it as fresh would make skew the easiest way past this gate.
        """
        if max_age <= timedelta(0):
            raise ContractViolation("max_age must be positive")
        age = self.age(now)
        return timedelta(0) <= age <= max_age

    def as_pricing_row(self) -> dict[str, object]:
        """Render as the row shape `spike.harness.select_options` consumes.

        The key names are the baseline's, not this module's: `select_options` reads
        `hourly_cost`, `spot_cost`, `available` and `cloud`, and
        `filter_by_configured_clouds` subscripts `cloud` directly. Building the row
        here keeps that coupling in one place, so a change to the harness's row shape
        breaks one function rather than every call site.
        """
        return {
            "cloud": self.cloud,
            "region": self.region,
            "gpu_type": self.gpu_type,
            "gpu_count": self.gpu_count,
            "instance_type": self.instance_type,
            "hourly_cost": self.hourly_cost,
            "spot_cost": self.spot_cost,
            "available": self.available,
        }


def _outside_permitted_location(request: CapacityRequest, quote: PriceQuote) -> bool:
    """Whether this quote sits outside the request's permitted region *or* cloud.

    One predicate rather than two branches, shared by `_eligibility_failures` and
    `requires_approval`, because the two must agree: a location the eligibility filter
    excludes and a location the approval gate flags are the same question asked at two
    points, and two copies of the rule can disagree after an edit to one.

    An empty list on either axis means "no restriction on that axis", matching
    `filter_by_configured_clouds`'s treatment of an empty cloud tuple.
    """
    if request.permitted_regions and quote.region not in request.permitted_regions:
        return True
    return bool(
        request.permitted_clouds and quote.cloud not in request.permitted_clouds
    )


def _eligibility_failures(
    request: CapacityRequest,
    quote: PriceQuote,
    now: datetime,
) -> tuple[EligibilityFailure, ...]:
    """Every reason this quote fails the request, in declaration order.

    Every reason, not the first: see `EligibilityFailure`'s docstring. Order follows
    the enum so two candidates' failures are comparable.
    """
    failures: list[EligibilityFailure] = []

    if quote.gpu_type != request.gpu_type:
        failures.append(EligibilityFailure.GPU_TYPE)
    if quote.gpu_count < request.gpu_count:
        failures.append(EligibilityFailure.GPU_COUNT)
    # A zero reading is "not reported", not "zero vCPUs" — the baseline's pricing
    # rows carry no CPU/RAM columns at all, so a migration that treated missing as
    # zero would make every baseline row ineligible the moment a request named a
    # CPU floor. Only a positive reading below the floor is a failure.
    if request.min_vcpus and 0 < quote.vcpus < request.min_vcpus:
        failures.append(EligibilityFailure.CPU)
    if request.min_memory_gb and 0 < quote.memory_gb < request.min_memory_gb:
        failures.append(EligibilityFailure.MEMORY)
    # An image or network requirement, by contrast, is *not* satisfiable by silence.
    # "This provider did not tell us which images it offers" is not evidence that it
    # offers the one the workload needs, and guessing wrong means a node that
    # provisions and then cannot run the workload.
    if request.required_image and request.required_image not in quote.images:
        failures.append(EligibilityFailure.IMAGE)
    # Cloud restriction is the same question as locality from the caller's side, so it
    # reports as LOCALITY rather than inventing a fourth placement axis, and the two
    # are one condition so a quote outside both lists records LOCALITY once. This is
    # the request-side equivalent of `filter_by_configured_clouds`.
    if _outside_permitted_location(request, quote):
        failures.append(EligibilityFailure.LOCALITY)
    if request.required_network and request.required_network not in quote.networks:
        failures.append(EligibilityFailure.NETWORK)
    if quote.quota_remaining is not None and quote.quota_remaining < request.gpu_count:
        failures.append(EligibilityFailure.QUOTA)
    if request.deadline is not None:
        # No ETA means the provider did not say how long it needs. Against a deadline
        # that is a failure, not a pass: an unbounded provisioning time cannot be shown
        # to fit, and this is the direction that fails safely.
        misses_deadline = (
            quote.provisioning_eta is None
            or now + quote.provisioning_eta > request.deadline
        )
        if misses_deadline:
            failures.append(EligibilityFailure.DEADLINE)
    if request.max_hourly_cost is not None:
        # Compare against the price the workload would actually be billed at, which
        # is the spot price only when spot is permitted AND the reading is a real
        # one. `select_options` treats a zero spot cost as unknown rather than free;
        # the ceiling check has to agree, or a row with no spot reading would look
        # like it costs nothing.
        effective = quote.hourly_cost
        if request.allow_spot and 0.0 < quote.spot_cost < quote.hourly_cost:
            effective = quote.spot_cost
        if effective > request.max_hourly_cost:
            failures.append(EligibilityFailure.COST)
    if not quote.available:
        failures.append(EligibilityFailure.UNAVAILABLE)

    return tuple(failures)


def _pricing_mode(request: CapacityRequest, quote: PriceQuote) -> PricingMode:
    """The mode `select_options` uses for this request and quote."""
    if request.allow_spot and 0.0 < quote.spot_cost < quote.hourly_cost:
        return PricingMode.SPOT
    return PricingMode.ON_DEMAND


def _effective_hourly_cost(quote: PriceQuote, mode: PricingMode) -> float:
    """Return the one rate selected for approval and retained accounting."""
    return quote.spot_cost if mode is PricingMode.SPOT else quote.hourly_cost


def eligible_candidates(
    request: CapacityRequest,
    quotes: tuple[PriceQuote, ...],
    now: datetime,
) -> tuple[tuple[PriceQuote, ...], dict[str, tuple[EligibilityFailure, ...]]]:
    """Split quotes into the eligible ones and why the rest were excluded.

    Returns both halves because the rejections are the useful half when nothing is
    eligible. A function returning only survivors turns "no H100 anywhere in your two
    permitted regions" and "your CPU floor excluded every candidate" into the same
    empty list, and those need different actions from the caller.

    Rejection keys are ``f"{cloud}/{region}/{instance_type}"`` — the identity a
    reader can act on. Two quotes for the same instance type in the same region
    collapse to one key; that is intentional, since they are the same offer at
    different prices and the ordering step decides between them.
    """
    if now.tzinfo is None:
        raise ContractViolation("now must be timezone-aware")

    eligible: list[PriceQuote] = []
    rejected: dict[str, tuple[EligibilityFailure, ...]] = {}
    for quote in quotes:
        failures = _eligibility_failures(request, quote, now)
        if failures:
            key = f"{quote.cloud}/{quote.region}/{quote.instance_type}"
            rejected[key] = failures
        else:
            eligible.append(quote)
    return tuple(eligible), rejected


def recheck_freshness(
    quotes: tuple[PriceQuote, ...],
    now: datetime,
    max_age: timedelta = DEFAULT_QUOTE_MAX_AGE,
) -> tuple[tuple[PriceQuote, ...], tuple[PriceQuote, ...]]:
    """Partition quotes into (fresh, stale) at allocation time.

    Separate from `eligible_candidates` on purpose. Eligibility is a property of the
    hardware and the request and does not change between ranking and allocation;
    freshness is a property of *when you looked* and does change. Checking them
    together means the freshness answer is as old as the eligibility answer.

    Stale quotes are returned rather than dropped so the caller can re-quote exactly
    those providers, and so `place` can say a decision was blocked by staleness
    rather than by having no candidates.
    """
    fresh: list[PriceQuote] = []
    stale: list[PriceQuote] = []
    for quote in quotes:
        (fresh if quote.is_fresh(now, max_age) else stale).append(quote)
    return tuple(fresh), tuple(stale)


def requires_approval(
    request: CapacityRequest,
    quote: PriceQuote,
    *,
    approved_hourly_cost: float | None = None,
    incumbent: PriceQuote | None = None,
    pricing_mode: PricingMode | None = None,
) -> tuple[ApprovalRequired, ...]:
    """Which change classes in this placement need an approval the adapter lacks.

    Note the asymmetry with `eligible_candidates`: a *permitted* region list makes an
    out-of-region quote ineligible, and it never reaches here. This function is for
    the case where a placement is technically eligible but changes something the
    caller pinned — the region/cloud it was previously running in, a setting it
    pinned, or a rate above what was approved.

    The story requires "approval for forbidden location/settings/spend changes". This
    reports the requirement; it does not grant it. A owns no approval store.
    """
    needed: list[ApprovalRequired] = []

    if _outside_permitted_location(request, quote) or (
        incumbent is not None
        and (incumbent.cloud, incumbent.region) != (quote.cloud, quote.region)
    ):
        needed.append(ApprovalRequired.LOCATION)

    settings_changed = (
        (request.required_image and request.required_image not in quote.images)
        or (request.required_network and request.required_network not in quote.networks)
        # Spot when the request forbade it is a settings change, not a cheaper price:
        # a preemptible node has different failure behavior, which is the caller's
        # decision to make.
    )
    if settings_changed:
        needed.append(ApprovalRequired.SETTINGS)

    # Only an *increase* needs approval. Spending less than approved is not a change
    # anyone needs to authorize, and gating it would make every price drop block.
    expected_mode = _pricing_mode(request, quote)
    if pricing_mode is not None and pricing_mode is not expected_mode:
        raise ContractViolation(
            "pricing_mode must match the mode selected for this request and quote"
        )
    mode = pricing_mode or expected_mode
    effective_cost = _effective_hourly_cost(quote, mode)
    if (
        approved_hourly_cost is not None
        and effective_cost - approved_hourly_cost >= COST_TIE_EPSILON
    ):
        needed.append(ApprovalRequired.SPEND)

    return tuple(needed)


@dataclass(frozen=True)
class PlacementDecision:
    """The chosen placement, or a refusal that says what was in the way.

    ``allocatable`` is a property, not a field, for the same reason
    `ParityResult.live_verified` is: a field can be set to True by a caller who wants
    the answer to be True, and this one gates a provider allocation. It is derivable
    from the recorded facts, so it is derived.
    """

    request: CapacityRequest
    selected: PriceQuote | None
    ranked: tuple[PriceQuote, ...] = ()
    rejected: dict[str, tuple[EligibilityFailure, ...]] = field(default_factory=dict)
    stale: tuple[PriceQuote, ...] = ()
    approvals_required: tuple[ApprovalRequired, ...] = ()
    pricing_mode: PricingMode | None = None
    effective_hourly_cost: float | None = None
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.decided_at is not None and self.decided_at.tzinfo is None:
            raise ContractViolation(
                "PlacementDecision.decided_at must be timezone-aware"
            )
        # A selection has to be one of the ranked candidates. Otherwise the decision
        # could name a quote that never passed eligibility or freshness, which is the
        # whole property this type exists to carry.
        if self.selected is not None and self.selected not in self.ranked:
            raise ContractViolation(
                "PlacementDecision.selected must be one of the ranked candidates"
            )
        if self.selected is None:
            if self.pricing_mode is not None or self.effective_hourly_cost is not None:
                raise ContractViolation(
                    "an unselected placement cannot record a pricing mode or rate"
                )
        elif self.pricing_mode is None or self.effective_hourly_cost is None:
            raise ContractViolation(
                "a selected placement must record its pricing mode and effective rate"
            )
        elif self.pricing_mode is not _pricing_mode(
            self.request, self.selected
        ) or self.effective_hourly_cost != _effective_hourly_cost(
            self.selected, self.pricing_mode
        ):
            raise ContractViolation(
                "placement pricing mode and effective rate must match the selected quote"
            )

    @property
    def allocatable(self) -> bool:
        """Whether the caller may allocate against this decision.

        Both conditions are required. A selection with an outstanding approval is not
        allocatable — that is what "approval for forbidden location/settings/spend
        changes" means operationally, and a decision that reported the approval while
        remaining allocatable would be a comment rather than a gate.
        """
        return self.selected is not None and not self.approvals_required

    @property
    def refusal_reason(self) -> str:
        """Why this decision is not allocatable, in one line, or ``""`` if it is.

        Ordered most-actionable first: an outstanding approval is a human step, a
        stale price is a re-quote, and no eligible candidate is a request change.
        """
        if self.allocatable:
            return ""
        if self.approvals_required:
            classes = ", ".join(sorted(item.value for item in self.approvals_required))
            return f"approval required before allocating: {classes}"
        if self.stale and not self.ranked:
            return (
                f"every eligible quote is stale ({len(self.stale)}); "
                "re-quote before allocating"
            )
        if not self.ranked:
            return "no eligible capacity for this request"
        return "no placement selected"


def place(
    request: CapacityRequest,
    quotes: tuple[PriceQuote, ...],
    now: datetime,
    *,
    max_age: timedelta = DEFAULT_QUOTE_MAX_AGE,
    approved_hourly_cost: float | None = None,
    incumbent: PriceQuote | None = None,
) -> PlacementDecision:
    """Choose a placement: eligibility, then freshness, then baseline cost ordering.

    The order is the point.

    1. **Eligibility** first, because ranking ineligible candidates wastes the
       ordering and, worse, can surface one as the winner.
    2. **Freshness** second, on the eligible set only, at the moment of allocation.
       Re-quoting candidates that were never eligible costs provider calls for
       nothing.
    3. **Cost ordering** last, delegated to `spike.harness.select_options` so the
       ordering stays the baseline's — including the epsilon tie-break and the
       zero-spot-is-unknown rule — rather than a second implementation that drifts.

    A stale winner does not silently fall through to the next-cheapest quote. Stale
    quotes are removed before ranking, so the winner is the cheapest *fresh* eligible
    quote and the stale ones are reported for re-quoting. Ranking them and then
    refusing the winner would mean a placement's cost depended on which quotes
    happened to be old.
    """
    eligible, rejected = eligible_candidates(request, quotes, now)
    fresh, stale = recheck_freshness(eligible, now, max_age)

    # Hand the harness the baseline's row shape and map the ordering back onto the
    # quotes. Keyed on the row identity rather than list position because
    # `select_options` filters unavailable rows, so positions do not correspond.
    rows = tuple(quote.as_pricing_row() for quote in fresh)
    by_identity = {id(row): quote for row, quote in zip(rows, fresh)}
    ordered_rows = select_options(rows, prefer_spot=request.allow_spot)
    ranked = [by_identity[id(row)] for row in ordered_rows]

    selected = ranked[0] if ranked else None
    approvals: tuple[ApprovalRequired, ...] = ()
    selected_mode: PricingMode | None = None
    effective_cost: float | None = None
    if selected is not None:
        selected_mode = _pricing_mode(request, selected)
        effective_cost = _effective_hourly_cost(selected, selected_mode)
        approvals = requires_approval(
            request,
            selected,
            approved_hourly_cost=approved_hourly_cost,
            incumbent=incumbent,
            pricing_mode=selected_mode,
        )

    return PlacementDecision(
        request=request,
        selected=selected,
        ranked=tuple(ranked),
        rejected=rejected,
        stale=stale,
        approvals_required=approvals,
        pricing_mode=selected_mode,
        effective_hourly_cost=effective_cost,
        decided_at=now,
    )


@dataclass(frozen=True)
class RelocationRecord:
    """An interrupted attempt and its replacement, with both costs retained.

    The story is explicit on two points and this type is shaped by both.

    **"Relocation records old/new attempts, costs and hardware provenance."** Both
    quotes are held, not just the new one, and each carries its own
    `hardware_provenance`. The interrupted attempt's cost is retained even though the
    attempt failed, because that cost was really incurred — a spot node that ran for
    forty minutes before preemption was billed for forty minutes. Dropping it is the
    "declare zero" failure `accounting.py` refuses: zero is the expensive claim to
    get wrong, so zero is the one that needs evidence.

    **"It is not silently called live migration."** There is no `migrated` field and
    no method suggesting the workload moved without interruption. `interrupted_at`
    and `relocated_at` are two instants with a gap between them, and the gap is
    visible in the type.
    """

    workload_id: str
    workspace: str
    interrupted: PriceQuote
    replacement: PriceQuote
    interrupted_at: datetime
    relocated_at: datetime
    interrupted_runtime: timedelta
    reason: str
    interrupted_pricing_mode: PricingMode = PricingMode.ON_DEMAND

    def __post_init__(self) -> None:
        for name in ("workload_id", "workspace", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"RelocationRecord.{name} is required")
        for name in ("interrupted_at", "relocated_at"):
            if getattr(self, name).tzinfo is None:
                raise ContractViolation(
                    f"RelocationRecord.{name} must be timezone-aware"
                )
        if self.relocated_at < self.interrupted_at:
            raise ContractViolation(
                "RelocationRecord.relocated_at cannot precede the interruption"
            )
        if self.interrupted_runtime < timedelta(0):
            raise ContractViolation(
                "RelocationRecord.interrupted_runtime cannot be negative"
            )
        if not isinstance(self.interrupted_pricing_mode, PricingMode):
            raise ContractViolation(
                "RelocationRecord.interrupted_pricing_mode must be a PricingMode"
            )

    @property
    def is_live_migration(self) -> bool:
        """Always False. Relocation relaunches; it does not migrate a running node.

        Present as an explicit False rather than absent, because "does the record
        claim a live migration?" is a question a reviewer and a test should be able
        to ask directly and get an answer to.
        """
        return False

    @property
    def interrupted_cost(self) -> float:
        """What the interrupted attempt cost, at the rate it was billed.

        Uses the interrupted quote's own rate, not the replacement's: the
        interruption happened on that hardware at that price, and the two attempts
        may not even be on comparable machines — which is what
        `hardware_provenance` is retained to show.
        """
        hours = self.interrupted_runtime.total_seconds() / 3600.0
        rate = _effective_hourly_cost(self.interrupted, self.interrupted_pricing_mode)
        return round(rate * hours, 10)

    @property
    def hardware_changed(self) -> bool:
        """Whether the replacement is a different machine class from the original."""
        return (
            self.interrupted.hardware_provenance != self.replacement.hardware_provenance
        )


def relocate(
    workload_id: str,
    request: CapacityRequest,
    interrupted: PriceQuote,
    interrupted_at: datetime,
    interrupted_runtime: timedelta,
    quotes: tuple[PriceQuote, ...],
    now: datetime,
    reason: str,
    *,
    interrupted_pricing_mode: PricingMode,
    max_age: timedelta = DEFAULT_QUOTE_MAX_AGE,
    approved_hourly_cost: float | None = None,
) -> tuple[PlacementDecision, RelocationRecord | None]:
    """Place a replacement for an interrupted attempt and record both.

    Returns the decision *and* the record, with the record ``None`` when no
    replacement was allocatable. Both halves matter: a caller that got no
    replacement still needs the decision to know whether to re-quote (stale), seek
    an approval, or change the request.

    The record is only built for an allocatable decision. A record naming a
    replacement that was never allocatable would assert a relocation that did not
    happen — and since the record is the cost and provenance evidence, that would put
    a fictional attempt into the accounting.
    """
    decision = place(
        request,
        quotes,
        now,
        max_age=max_age,
        approved_hourly_cost=approved_hourly_cost,
        incumbent=interrupted,
    )
    if not decision.allocatable or decision.selected is None:
        return decision, None

    record = RelocationRecord(
        workload_id=workload_id,
        workspace=request.workspace,
        interrupted=interrupted,
        replacement=decision.selected,
        interrupted_at=interrupted_at,
        relocated_at=now,
        interrupted_runtime=interrupted_runtime,
        reason=reason,
        interrupted_pricing_mode=interrupted_pricing_mode,
    )
    return decision, record

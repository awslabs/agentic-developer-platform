"""No premature release or accounting clearance while exposure is unresolved.

Issue #5049 (U11), EPIC #4910. R15 acceptances 4 and 7.

## The two claims this refuses to let A make cheaply

"Released" and "cost zero" are both claims about the world, and both are
currently derivable from local state alone. R15 acceptance 7 says that while
resources or cost exposure remain unresolved, the allocation is not marked
released and the reservation is not returned as unused, and that incurred cost is
accrued conservatively rather than declared zero.

The upstream shape of this failure is `consolidator.go:419-425`:

```go
if err := c.deleteK8sNode(ctx, spNode.Status.K8sNodeName); err != nil {
    logger.Error(err, "failed to delete K8s node, continuing anyway")
}
...
// Step 6: Update phase to Terminated.
```

The delete failed, and the phase becomes `Terminated` regardless. Nothing lied;
the code simply advanced past an unconfirmed step, and the resulting status field
now says the node is gone. Anything downstream reading that field — a cost
attribution, a reservation return, an operator's dashboard — inherits a
conclusion no observation supports.

## Why the ledger is not A's, and what A does instead

C owns the reservation ledger. A does not write it, and this module contains no
balance, no reservation arithmetic and no spend total, because a second place
computing cost is a second answer that can disagree with the real one — the same
reason U8's `BudgetUsage` carries no enforcement field.

What A owns is **provider truth**: what a provider re-check established, and
therefore whether a clearance is *permitted*. `ReleaseAssessment` is that, and it
is deliberately a refusal rather than a computation. C asks "may I return this
reservation as unused?" and A answers from provider observations only.

## Conservative accrual, expressed as a category rather than a number

`CostExposure.UNRESOLVED` is the load-bearing value. A cannot know the dollar
figure for a resource it could not observe — that is C's, from provider billing —
but it can refuse to let the exposure be recorded as `NONE`. So the exposure is a
three-way category, and the one thing no unresolved path can produce is `NONE`.

`NONE` requires provider-established absence for every resource in the
allocation. That is the asymmetry the criterion asks for: zero is the expensive
claim to get wrong, so zero is the one that needs evidence.

## Retained, never erased

Acceptance 4 requires unresolved allocations to be retained and reported rather
than erased, and forbids claiming cleanup after credential loss. Both fall out of
the same rule here: a credential failure produces an `UNKNOWN` provider
observation (see `reconciliation.ProviderObservation`), an `UNKNOWN` observation
cannot reach `RELEASED`, and `unresolved_resources` names what is still
outstanding so the report says which resources, not merely that some exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .health import ContractViolation
from .reconciliation import ProviderObservation, ProviderPresence


class CostExposure(str, Enum):
    """Whether an allocation can still be costing money.

    Three values rather than a number, because A reports provider truth and C
    owns the ledger. The ordering that matters is that `NONE` is the only value
    asserting no further cost, and it is unreachable without provider evidence.
    """

    # Provider-established absence for every resource. Nothing can still bill.
    NONE = "none"

    # A resource is confirmed present, so cost continues to accrue.
    ACTIVE = "active"

    # The provider could not be consulted for at least one resource. Cost is
    # accrued conservatively: unknown, explicitly not zero.
    UNRESOLVED = "unresolved"


class ReleaseState(str, Enum):
    """How far a release has actually got, as opposed to how far it was driven."""

    # A provider re-check found nothing remaining for the allocation.
    RELEASED = "released"

    # The provider still holds resources for this allocation.
    RETAINED = "retained"

    # The provider could not be consulted. Neither released nor known-retained;
    # the allocation stays on the books and is reported.
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class AllocationResources:
    """Expected resource identifiers from the authoritative allocation record.

    B's authorized cleanup driver supplies this inventory from the allocation
    records persisted upstream by U11c, independently of the provider's response. A neither
    constructs the inventory from observations nor owns its persistence. Provider
    integration must include compute, storage and network resources in this set.
    """

    allocation_id: str
    resource_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.allocation_id, str) or not self.allocation_id.strip():
            raise ContractViolation("allocation inventory requires an allocation id")
        if (
            not isinstance(self.resource_ids, frozenset)
            or not self.resource_ids
            or any(
                not isinstance(name, str) or not name.strip()
                for name in self.resource_ids
            )
        ):
            raise ContractViolation(
                "allocation inventory requires a non-empty frozen resource set"
            )


@dataclass(frozen=True)
class ReleaseAssessment:
    """What provider observations establish about a release, and what they permit.

    `assess_release` derives this from provider observations and independently
    supplied allocation membership. Construction checks consistency, not the
    provenance of those inputs; live provider proof remains a separate gate.
    """

    state: ReleaseState
    exposure: CostExposure
    allocation_id: str
    unresolved_resources: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.allocation_id, str) or not self.allocation_id.strip():
            raise ContractViolation("release assessment requires an allocation id")
        if self.state is ReleaseState.RELEASED:
            if self.exposure is not CostExposure.NONE:
                raise ContractViolation(
                    "a RELEASED assessment cannot carry continuing cost exposure"
                )
            if self.unresolved_resources:
                raise ContractViolation(
                    "a RELEASED assessment cannot name unresolved resources"
                )
        if self.state is ReleaseState.UNRESOLVED and not self.unresolved_resources:
            # An unresolved release that names nothing is unactionable: an
            # operator cannot go and look at "something".
            raise ContractViolation(
                "an UNRESOLVED assessment must name the resources it could not "
                "establish, so they can be retained and reported rather than erased"
            )
        if (
            self.exposure is CostExposure.NONE
            and self.state is not ReleaseState.RELEASED
        ):
            raise ContractViolation(
                "zero cost exposure requires a RELEASED state established by a "
                "provider re-check"
            )

    @property
    def may_mark_released(self) -> bool:
        """True only when a provider re-check established absence.

        The single gate for "mark this allocation released". A caller that asks
        this cannot reproduce `consolidator.go`'s advance-past-a-failed-delete,
        because no unconfirmed path reaches `RELEASED`.
        """
        return self.state is ReleaseState.RELEASED

    @property
    def may_return_reservation_unused(self) -> bool:
        """True only when nothing can still be billing against the allocation.

        Separate from `may_mark_released` because they are separate claims that
        happen to coincide here, and a future exposure category (a resource
        deleted but still within a committed-spend window, say) would need to
        deny this while permitting the release. Collapsing them into one flag now
        would hide that distinction at the point it starts to matter.
        """
        return self.exposure is CostExposure.NONE


def assess_release(
    observations: dict[str, ProviderObservation],
    *,
    allocation: AllocationResources,
) -> ReleaseAssessment:
    """Compare provider evidence with independently supplied allocation membership.

    The required inventory comes from the authorized allocation record, never
    from observed keys. Missing, foreign, mismatched and UNKNOWN observations
    retain unresolved exposure. An empty provider response cannot clear a
    non-empty allocation. A reports the assessment; C owns accounting changes.
    """
    if not isinstance(allocation, AllocationResources):
        raise ContractViolation("release requires an allocation inventory")
    if not isinstance(observations, dict):
        raise ContractViolation("release requires an observation mapping")

    if any(not isinstance(name, str) or not name.strip() for name in observations):
        raise ContractViolation("observation keys must be resource identifiers")
    if any(
        not isinstance(observed, ProviderObservation)
        or not isinstance(observed.presence, ProviderPresence)
        for observed in observations.values()
    ):
        raise ContractViolation("release requires typed provider observations")

    expected = allocation.resource_ids
    observed_ids = set(observations)
    unknown = sorted(
        (expected - observed_ids)
        | (observed_ids - expected)
        | {
            name
            for name, observed in observations.items()
            if observed.presence is ProviderPresence.UNKNOWN
            or observed.queried_by != name
        }
    )
    present = sorted(
        name
        for name, observed in observations.items()
        if observed.presence is ProviderPresence.PRESENT and name not in unknown
    )

    if unknown:
        return ReleaseAssessment(
            allocation_id=allocation.allocation_id,
            state=ReleaseState.UNRESOLVED,
            exposure=CostExposure.UNRESOLVED,
            unresolved_resources=tuple(unknown + present),
            reason=(
                f"provider evidence was unavailable or mismatched for {len(unknown)} "
                "resource(s); the allocation is retained and reported, and "
                "incurred cost is accrued as unresolved rather than zero"
            ),
        )

    if present:
        return ReleaseAssessment(
            allocation_id=allocation.allocation_id,
            state=ReleaseState.RETAINED,
            exposure=CostExposure.ACTIVE,
            unresolved_resources=tuple(present),
            reason=(
                f"the provider still holds {len(present)} resource(s) for this "
                "allocation; it is not released and cost continues to accrue"
            ),
        )

    return ReleaseAssessment(
        allocation_id=allocation.allocation_id,
        state=ReleaseState.RELEASED,
        exposure=CostExposure.NONE,
        reason=(
            f"a provider re-check of {len(observations)} resource(s) found none "
            "remaining"
        ),
    )

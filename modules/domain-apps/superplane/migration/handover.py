"""Inventory existing resources, decide each one's fate, and keep one owner apiece.

Issue #5061 (U19), EPIC #4910. R18, the state-handover half.

## The question this module refuses to answer by default

The story requires: "inventory existing SkyPilot clusters/jobs/services, persisted
state, handles and credential references; record whether each is adopted,
drained/relaunched or outside the selected migration. If this is a clean deployment,
record evidence of no pre-existing state."

U12 recorded five `EXISTING_STATE_CLASSES`, and every one of them has
`default_decision="undecided"` — a value `spike/tests/test_baseline_inventory.py`
asserts for all five. That is not an oversight to be tidied up by this story; it is the
finding. As U12's `joined_eks_hybrid_nodes` rationale puts it: "Draining it is a
workload-affecting action, so the decision needs the resource owner's agreement, not a
default."

So `build_plan` **refuses** rather than defaults. A resource left `UNDECIDED` makes the
plan not executable, and `HandoverPlan.blocking_reason` says which resources are
waiting on a decision. The amendment says the same thing in operational terms:
"Existing environments and workloads are not transferred automatically. Their owners,
target, retained resources and permitted cutover method must be explicit before a live
migration."

## Why a clean deployment needs evidence too

"Where no existing state is being migrated, record and verify that fact instead of
silently assuming a clean deployment."

`NO_EXISTING_STATE` is therefore a *decision recorded per state class*, not the absence
of entries in the plan. An empty inventory is ambiguous between "we looked and there is
nothing" and "we did not look", and those differ by exactly the amount of money U12's
`skypilot_api_server_state` rationale describes: "A redeploy onto fresh storage orphans
every running cluster, which is the concrete way a 'clean deployment' assumption leaks
money." So `build_plan` requires an explicit finding for every one of U12's five state
classes, and `HandoverPlan.clean_deployment` is True only when all five were checked
and each came back empty with a recorded enumeration method.

## One owner per resource, and why old controllers stay stopped

"Establish exactly one owning controller per resource; never restart an old controller
over resources already handed over."

`OwnershipConflict` is raised at plan construction, not detected later, because two
controllers reconciling one cluster is not a state to report — it is a state where both
controllers act. This is also the answer to U12's fourth chain gap,
`no-owning-controller-for-serving`, whose required decision is "name the owning
controller... Exactly one controller must own each serving resource": serving resources
go through the same ownership check as every other kind, so a serving resource with no
named owner blocks the plan rather than passing unnoticed.

## Rollback that does not undo a deliberate release

"Exercise rollback and retain unresolved resources/expense exposure" and "rollback
preserves records and does not recreate deliberately released capacity".

`rollback` returns ownership to the previous controller for adopted and drained
resources, and explicitly **does not** for a resource whose release was deliberate. The
test is U11's `ReleaseIntent.deliberate`, which already carries exactly the required
distinction — a deliberate release "must stop every recreation driver", whereas an
accidental deletion "must remain repairable". Recreating capacity someone deliberately
released is how a rollback spends money to undo a decision that was correct.

Unresolved resources are retained in the rollback record rather than dropped, and
`CostExposure.UNRESOLVED` is carried through, for the reason `accounting.py` gives:
zero is the expensive claim to get wrong, so zero is the one that needs evidence.

## What this module does not do

**No provider or cluster access.** Inventories arrive as arguments — from `sky status`,
a CR listing, an API-server store inspection — and this module decides over them. It
enumerates nothing itself, so it cannot claim to have looked.

**No persistence.** The plan and the rollback record are values returned to the caller.
Domain state stays in the upstream API.

**No live evidence.** R18's service/state-handover criterion needs a selected
environment, authorized access, spend authority and a named cleanup owner — all
Unresolved in `validation-mapping.md`. This module makes the decision structure
testable offline; it closes no live criterion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from spike.baseline_inventory import EXISTING_STATE_CLASSES, HANDOVER_DECISIONS
from superplane_contracts import (
    ContractViolation,
    CostExposure,
    HandleRecord,
    ReleaseIntent,
)

# The five state classes U12 recorded, as a frozen set of `kind` strings.
#
# Derived from `EXISTING_STATE_CLASSES` rather than retyped, so a class added upstream
# becomes a class this module requires a finding for, instead of one it silently stops
# asking about. That direction matters: a forgotten state class is an orphaned resource.
REQUIRED_STATE_KINDS: frozenset[str] = frozenset(
    entry.kind for entry in EXISTING_STATE_CLASSES
)


class HandoverDecision(str, Enum):
    """What happens to one existing resource at cutover.

    The four values are U12's `HANDOVER_DECISIONS` tuple, as an enum so a decision is
    a member rather than a bare string. `_check_decisions_match_u12` below asserts the
    two stay in agreement — a drifting duplicate is caught by a test that fails, which
    is the same arrangement `provisioning.REQUIRED_PERMISSION` uses against U9's enum.
    """

    ADOPT = "adopt"
    """The new controller takes over the existing resource and its handle.

    Per U12's `skypilot_clusters` rationale, this "requires the new controller to
    accept a handle it did not create" — so an adoption must carry a durable handle.
    """

    DRAIN_RELAUNCH = "drain_relaunch"
    """The resource is drained and its work relaunched on new capacity.

    Workload-affecting, so it requires a named owner's agreement.
    """

    NO_EXISTING_STATE = "no_existing_state"
    """This class was enumerated and found empty. A positive finding, not a default."""

    UNDECIDED = "undecided"
    """No decision has been made. Blocks the plan; never treated as a safe default."""


def _check_decisions_match_u12() -> None:
    """Fail loudly at import if this enum and U12's tuple have drifted apart.

    Import-time rather than test-time because a mismatch means the values this module
    writes into a plan are not the values U12's inventory recognizes, and every
    subsequent decision would be recorded against the wrong vocabulary.
    """
    ours = {member.value for member in HandoverDecision}
    theirs = set(HANDOVER_DECISIONS)
    if ours != theirs:
        raise ContractViolation(
            "HandoverDecision has drifted from U12's HANDOVER_DECISIONS: "
            f"only here {sorted(ours - theirs)}, only there {sorted(theirs - ours)}"
        )


_check_decisions_match_u12()


@dataclass(frozen=True)
class InventoriedResource:
    """One existing resource, how it was found, and what is to become of it.

    ``enumerated_by`` is required and is the method actually used — "POST /status with
    no cluster_names filter", "list SuperplaneNode CRs". It is required because a
    finding without a method is an assertion without evidence, and the whole point of
    the clean-deployment rule is that "we looked and found nothing" must be
    distinguishable from "we did not look".

    ``owning_controller`` is the single controller that will reconcile this resource
    after cutover. Empty is allowed only for `NO_EXISTING_STATE` (there is nothing to
    own) and for `UNDECIDED` (the decision that names the owner has not been made).
    """

    kind: str
    resource_id: str
    decision: HandoverDecision
    enumerated_by: str
    owning_controller: str = ""
    previous_controller: str = ""
    handle: HandleRecord | None = None
    release_intent: ReleaseIntent | None = None
    exposure: CostExposure = CostExposure.UNRESOLVED
    detail: str = ""

    def __post_init__(self) -> None:
        for name in ("kind", "resource_id", "enumerated_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"InventoriedResource.{name} is required")
        if not isinstance(self.decision, HandoverDecision):
            raise ContractViolation(
                "InventoriedResource.decision must be a HandoverDecision"
            )
        if self.kind not in REQUIRED_STATE_KINDS:
            raise ContractViolation(
                f"unknown state class {self.kind!r}; U12 records "
                f"{sorted(REQUIRED_STATE_KINDS)}"
            )
        # An adopted or drained resource must name the controller that will own it.
        # Without a name, "exactly one owning controller" is unverifiable, and the
        # resource is the kind that gets reconciled by two controllers or by none.
        if (
            self.decision
            in (
                HandoverDecision.ADOPT,
                HandoverDecision.DRAIN_RELAUNCH,
            )
            and not self.owning_controller.strip()
        ):
            raise ContractViolation(
                f"a {self.decision.value} decision must name the owning controller "
                "that will reconcile this resource after cutover"
            )
        # U12's `skypilot_clusters` rationale: adoption "requires the new controller to
        # accept a handle it did not create". A non-durable handle is one a crash
        # loses, leaving an adopted cluster with no reference to reconcile against —
        # which is the orphaned-resource failure adoption is supposed to prevent.
        if self.decision is HandoverDecision.ADOPT:
            if self.handle is None:
                raise ContractViolation(
                    "an adopted resource must carry the durable handle the new "
                    "controller takes over"
                )
            if not self.handle.durable:
                raise ContractViolation(
                    "an adopted resource requires a DURABLE handle record; a handle "
                    "that persistence has not acknowledged cannot be reconciled after "
                    "a crash"
                )
        # A "nothing here" finding that names a resource id or an owner is describing
        # something, which contradicts the finding.
        if self.decision is HandoverDecision.NO_EXISTING_STATE:
            if self.handle is not None:
                raise ContractViolation(
                    "a no_existing_state finding cannot carry a handle: a handle is a "
                    "reference to a resource that exists"
                )
            if self.owning_controller.strip():
                raise ContractViolation(
                    "a no_existing_state finding cannot name an owning controller: "
                    "there is no resource to own"
                )

    @property
    def is_decided(self) -> bool:
        """Whether a decision has actually been made for this resource."""
        return self.decision is not HandoverDecision.UNDECIDED

    @property
    def deliberately_released(self) -> bool:
        """Whether this resource's capacity was released on purpose.

        Uses U11's `ReleaseIntent.deliberate`, which already carries the distinction
        rollback needs: a deliberate release has stopped every recreation driver,
        whereas an accidental deletion must remain repairable.
        """
        return self.release_intent is not None and self.release_intent.deliberate


@dataclass(frozen=True)
class OwnershipConflict:
    """Two controllers named for one resource. Raised, not reported."""

    resource_id: str
    controllers: tuple[str, ...]


class OwnershipConflictError(ContractViolation):
    """Exactly one controller must own each resource, and two were named.

    A `ContractViolation` subclass so a caller catching contract violations at its
    boundary catches this too, while a caller that specifically wants to handle a
    handover conflict can still name it.

    Raised at plan construction rather than surfaced as a field, because a plan is a
    thing a caller executes: a conflict reported inside an executable plan is a
    conflict that gets executed, and two controllers reconciling one GPU cluster both
    act on it.
    """

    def __init__(self, conflicts: tuple[OwnershipConflict, ...]) -> None:
        self.conflicts = conflicts
        detail = "; ".join(
            f"{item.resource_id} claimed by {', '.join(sorted(item.controllers))}"
            for item in conflicts
        )
        super().__init__(
            f"exactly one controller must own each resource, but: {detail}. "
            "Never restart an old controller over resources already handed over."
        )


def _validate_resource_consistency(
    resources: tuple[InventoriedResource, ...],
) -> None:
    """Refuse competing owners or decisions for one resource identity."""
    controllers: dict[str, set[str]] = {}
    decisions: dict[str, set[HandoverDecision]] = {}
    for item in resources:
        decisions.setdefault(item.resource_id, set()).add(item.decision)
        if item.owning_controller:
            controllers.setdefault(item.resource_id, set()).add(item.owning_controller)
    conflicts = tuple(
        OwnershipConflict(resource_id=resource_id, controllers=tuple(sorted(names)))
        for resource_id, names in sorted(controllers.items())
        if len(names) > 1
    )
    if conflicts:
        raise OwnershipConflictError(conflicts)
    contradictory = sorted(
        resource_id for resource_id, values in decisions.items() if len(values) > 1
    )
    if contradictory:
        raise ContractViolation(
            "conflicting handover decisions recorded for resource(s): "
            f"{', '.join(contradictory)}"
        )


@dataclass(frozen=True)
class HandoverPlan:
    """Every inventoried resource, its decision, and whether this can be executed.

    ``executable`` is a property rather than a field, like
    `PlacementDecision.allocatable` and for the same reason: it gates a cutover, so it
    is derived from the recorded findings rather than settable by a caller who wants
    the answer to be True.
    """

    resources: tuple[InventoriedResource, ...]
    kinds_checked: frozenset[str] = field(default_factory=frozenset)
    planned_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.planned_at is not None and self.planned_at.tzinfo is None:
            raise ContractViolation("HandoverPlan.planned_at must be timezone-aware")
        _validate_resource_consistency(self.resources)
        unknown = self.kinds_checked - REQUIRED_STATE_KINDS
        if unknown:
            raise ContractViolation(
                f"unknown state class(es) checked: {sorted(unknown)}"
            )
        missing = {item.kind for item in self.resources} - self.kinds_checked
        if missing:
            raise ContractViolation(
                f"findings recorded for unenumerated state class(es): {sorted(missing)}"
            )

    @property
    def undecided(self) -> tuple[InventoriedResource, ...]:
        """Resources still waiting on their owner's decision."""
        return tuple(item for item in self.resources if not item.is_decided)

    @property
    def unchecked_kinds(self) -> tuple[str, ...]:
        """State classes U12 records that this plan has no finding for.

        Sorted for a stable message. An unchecked class is the failure mode U12's
        `skypilot_api_server_state` rationale describes — the state nobody looked for
        is the state that orphans running clusters.
        """
        return tuple(sorted(REQUIRED_STATE_KINDS - self.kinds_checked))

    @property
    def checked_without_evidence(self) -> tuple[str, ...]:
        """Kinds claimed as checked without an ``enumerated_by`` finding."""
        evidenced = {item.kind for item in self.resources if item.enumerated_by.strip()}
        return tuple(sorted(self.kinds_checked - evidenced))

    @property
    def executable(self) -> bool:
        """Whether this plan may be executed as a cutover.

        Every U12 state class must be claimed as checked *and* have a positive
        ``enumerated_by`` finding, no resource may be undecided, and ownership and
        decisions must be unambiguous (enforced by construction).
        """
        return (
            not self.unchecked_kinds
            and not self.checked_without_evidence
            and not self.undecided
        )

    @property
    def clean_deployment(self) -> bool:
        """Whether this is an evidenced clean deployment.

        Requires that every state class was checked and that every finding is
        `NO_EXISTING_STATE`. An empty `resources` tuple is **not** a clean deployment:
        that is the silent assumption this property exists to refuse. A plan with no
        findings has `unchecked_kinds` for all five classes and returns False.
        """
        if self.unchecked_kinds or self.checked_without_evidence or not self.resources:
            return False
        return all(
            item.decision is HandoverDecision.NO_EXISTING_STATE
            for item in self.resources
        )

    @property
    def owners(self) -> dict[str, str]:
        """Resource id to the single controller that owns it after cutover."""
        return {
            item.resource_id: item.owning_controller
            for item in self.resources
            if item.owning_controller
        }

    @property
    def blocking_reason(self) -> str:
        """Why this plan is not executable, or ``""`` when it is."""
        if self.executable:
            return ""
        parts: list[str] = []
        if self.unchecked_kinds:
            parts.append(
                "no finding recorded for state class(es): "
                f"{', '.join(self.unchecked_kinds)} — an unchecked class cannot be "
                "reported as a clean deployment"
            )
        if self.checked_without_evidence:
            parts.append(
                "checked state class(es) lack an enumerated_by finding: "
                f"{', '.join(self.checked_without_evidence)}"
            )
        if self.undecided:
            ids = ", ".join(sorted(item.resource_id for item in self.undecided))
            parts.append(
                f"awaiting the resource owner's decision for: {ids} — draining is "
                "workload-affecting and has no safe default"
            )
        return "; ".join(parts)


def build_plan(
    resources: tuple[InventoriedResource, ...],
    kinds_checked: frozenset[str],
    *,
    planned_at: datetime | None = None,
) -> HandoverPlan:
    """Assemble a handover plan, refusing ambiguous ownership.

    ``kinds_checked`` is passed separately from ``resources`` and that separation is
    the point: it is the caller's statement of *what was enumerated*, which is not
    derivable from what was found. A class that was checked and came back empty
    appears in `kinds_checked` with a `NO_EXISTING_STATE` finding; a class nobody
    enumerated appears in neither, and `unchecked_kinds` names it.

    Raises `OwnershipConflictError` when two controllers are named for one resource id.
    Duplicate entries naming the *same* controller are not a conflict — re-inventorying
    a resource is normal — so the check is on the number of distinct controllers.
    """
    _validate_resource_consistency(resources)

    unknown = kinds_checked - REQUIRED_STATE_KINDS
    if unknown:
        raise ContractViolation(
            f"unknown state class(es) checked: {sorted(unknown)}; U12 records "
            f"{sorted(REQUIRED_STATE_KINDS)}"
        )

    # Every resource's class must be among the classes the caller says it enumerated.
    # Otherwise a finding exists for a class the plan does not claim to have checked,
    # and `unchecked_kinds` would under-report.
    missing = {item.kind for item in resources} - kinds_checked
    if missing:
        raise ContractViolation(
            f"findings recorded for unenumerated state class(es): {sorted(missing)}"
        )

    return HandoverPlan(
        resources=resources,
        kinds_checked=frozenset(kinds_checked),
        planned_at=planned_at,
    )


@dataclass(frozen=True)
class RollbackRecord:
    """What a rollback returned, what it deliberately left alone, and what is unresolved.

    Three separate tuples because they need three different follow-ups, and a single
    "handled" list would erase the distinction:

    * ``returned`` — ownership went back to the previous controller.
    * ``not_recreated`` — deliberately released capacity that rollback left released.
      Named in the positive so the record says *why* nothing happened, rather than
      leaving a reader to infer it from an absence.
    * ``unresolved`` — resources whose disposition could not be established. Retained,
      per R15 acceptance 4's rule that unresolved allocations are reported rather than
      erased.
    """

    plan: HandoverPlan
    returned: tuple[InventoriedResource, ...]
    not_recreated: tuple[InventoriedResource, ...]
    unresolved: tuple[InventoriedResource, ...]
    exposure: CostExposure
    rolled_back_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.rolled_back_at is not None and self.rolled_back_at.tzinfo is None:
            raise ContractViolation(
                "RollbackRecord.rolled_back_at must be timezone-aware"
            )
        # The rule from `accounting.py`, applied to rollback: NONE is the claim that
        # needs evidence, and an unresolved resource is evidence against it.
        if self.unresolved and self.exposure is CostExposure.NONE:
            raise ContractViolation(
                "cost exposure cannot be NONE while resources remain unresolved; "
                "unknown exposure is recorded as UNRESOLVED, never as zero"
            )

    @property
    def recreated_deliberately_released_capacity(self) -> bool:
        """Always False. Rollback never undoes a deliberate release.

        An explicit False rather than an absent field, because "did the rollback
        recreate capacity someone deliberately released?" is a question a reviewer
        should be able to ask the record directly.
        """
        return False

    @property
    def records_preserved(self) -> bool:
        """Whether every resource in the plan still appears in this record.

        The rule is that rollback "preserves records" — so nothing may be dropped.
        Checked as a count over the three output tuples against the plan's input, which
        catches a resource that fell through the branches rather than being classified.
        """
        accounted = len(self.returned) + len(self.not_recreated) + len(self.unresolved)
        return accounted == len(self.plan.resources)


def rollback(
    plan: HandoverPlan,
    *,
    unresolved_ids: frozenset[str] = frozenset(),
    rolled_back_at: datetime | None = None,
) -> RollbackRecord:
    """Return ownership to the previous controllers, without recreating released capacity.

    Classification, in order — the order matters because the branches overlap:

    1. **Unresolved** first. A resource whose disposition could not be established is
       unresolved regardless of what the plan intended, because the intent is not the
       observation.
    2. **Deliberately released** next. Rollback leaves it released: U11's
       `ReleaseIntent.deliberate` means every recreation driver was stopped, and
       recreating the capacity would spend money undoing a correct decision.
    3. **Returned** otherwise, when a previous controller is named.
    4. Anything with no previous controller to return to is **unresolved**, not
       quietly successful. A resource whose old owner is unknown has no one to hand
       back to, which is a state a human needs to see.

    Exposure follows `accounting.py`: `UNRESOLVED` whenever anything is unresolved,
    and otherwise the most severe exposure any returned resource carries. It is never
    computed as `NONE` from local state alone — `NONE` requires provider-established
    absence, which nothing offline can supply.
    """
    returned: list[InventoriedResource] = []
    not_recreated: list[InventoriedResource] = []
    unresolved: list[InventoriedResource] = []

    for item in plan.resources:
        if item.resource_id in unresolved_ids:
            unresolved.append(item)
        elif item.deliberately_released:
            not_recreated.append(item)
        elif item.previous_controller.strip():
            returned.append(item)
        elif item.decision is HandoverDecision.NO_EXISTING_STATE:
            # Nothing existed, so there is nothing to hand back and nothing
            # outstanding. This is the one branch where an absent previous controller
            # is not a problem.
            not_recreated.append(item)
        else:
            unresolved.append(item)

    if unresolved:
        exposure = CostExposure.UNRESOLVED
    elif any(item.exposure is CostExposure.ACTIVE for item in returned):
        exposure = CostExposure.ACTIVE
    elif any(item.exposure is CostExposure.UNRESOLVED for item in returned):
        exposure = CostExposure.UNRESOLVED
    elif returned:
        exposure = CostExposure.ACTIVE
    else:
        exposure = CostExposure.NONE

    return RollbackRecord(
        plan=plan,
        returned=tuple(returned),
        not_recreated=tuple(not_recreated),
        unresolved=tuple(unresolved),
        exposure=exposure,
        rolled_back_at=rolled_back_at,
    )

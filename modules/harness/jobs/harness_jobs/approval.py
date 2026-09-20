"""What an approval *is*, and what makes one currently valid.

Issue #5526 (w6-03), EPIC #4910, Wave 6. Consumes the versioned contract published by
#5524 (w6-01), which records at `INTEGRATION-CONTRACT.md:174` that current approval is
**not a port there**: *"#5526 (w6-03) owns approval binding and recheck at decision and
admission"*, and that §3 specifies the ordering it must fit into.

## The one sentence this module exists for

**A stored approval is not permission to spend, and neither is a stored operation.**

`harness_jobs` already guarantees that an admitted operation is durable, single-effect
and tenant-scoped (#5525). None of those properties asks whether the operation was ever
allowed to exist. A caller holding a well-formed request got admission on the strength
of being well-formed. This module is the gate in front of that.

## Why the approval binds to a digest rather than to an operation id

An approval that named an `operation_id` would be an approval of *whatever that id now
refers to*. The id is minted at admission, so a binding in that direction cannot exist
before the thing it gates, and a binding created afterwards approves a row that already
happened.

So the binding is over the **request**, through `payload_digest` -- the same digest
`store.py` stores and re-verifies, which is already what makes a changed retry
detectable there. Reusing it is deliberate: a second digest function over the same bytes
is a second answer that can disagree, and the disagreement would surface as "the store
thinks this is the same request and the approval gate does not", which is the failure
this wave exists to prevent.

Concretely, `ApprovalBinding.for_request()` is the only constructor, it takes the
`ResolvedPrincipal` and the `OperationRequest`, and it computes the digest itself. A
caller cannot hand in a digest, because a caller-supplied digest is a caller-supplied
claim about what was approved.

## The envelope is an aggregate, and it is a ceiling

`SpendEnvelope` carries what the approver actually saw: how much resource, for how long,
and the cost ceiling. It is an **aggregate** rather than a per-resource list because the
approver agreed to a total -- and a per-item list is satisfiable by an operation that
respects every item and exceeds their sum.

`covers()` is the comparison, and it is one-directional on purpose: an operation may ask
for less than was approved, never more. Equality is not required because a retry that
narrows its request is still within what a human agreed to.

## Why `HitlResult` is duplicated here rather than imported

`contracts/hitl-ticket/v1/models.py` is the repository's written, CI-executed answer
vocabulary (#4178), and it is the right one: four results rather than two, because
"nobody could be asked" is materially different from "a human said no". This module
uses **the same four wire strings**.

It does not import them. That file is a pydantic model under `contracts/`, and this
package declares *no runtime dependencies at all* -- not even a driver -- so that the
API server (asyncpg) and any other consumer can install it without a dependency
conflict (`pyproject.toml:11`). Importing pydantic here would make a shared admission
gate uninstallable in exactly the places it is supposed to be shared.

This is the same trade `identity.py` already makes for `REQUIRED_PERMISSION` and
`OperationState`, with the same mitigation: duplication without a test is drift waiting
to happen, so `tests/test_approval_contract_agreement.py` drives its assertions from the
HITL contract's own members and fails if either side moves.

## Fail-closed, including on silence

`ALLOWED_ONCE` is the only value that permits. Every other result, *and the absence of
a result*, denies. Absence is the case a vocabulary cannot represent -- the HITL
contract states it as a consumer obligation (its README invariant 1) -- so it is
represented here as `ApprovalRecord | None`, and `None` is a denial rather than a
default. #5524 §2 states the same rule for its ports: *"None is a denial, never a
default. An unanswered question is not permission."*
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from .identity import (
    REQUIRED_PERMISSION,
    ContractViolation,
    OperationRequest,
    ResolvedPrincipal,
    payload_digest,
)

# The permission an approver must hold to approve spending, as distinct from the
# permission the *operation* demands (`REQUIRED_PERMISSION`, `workspace:provision`).
#
# String form of `superplane_auth.policy.Permission.ADMINISTER`. Deliberately not
# PROVISION: the domain's `_IMPLIED` table (`policy.py:178-190`) is explicit that
# "PROVISION does not confer SPEND and SPEND does not confer PROVISION, because 'may
# deploy a model' and 'may hand out a kubeconfig' are different blast radii". A
# requester holding PROVISION can therefore ask; approving is a strictly higher
# authority, and ADMINISTER is the only one the domain's table closes over both.
#
# Choosing PROVISION here would make every principal that can request an operation also
# able to approve one, which is self-issued authority with extra steps -- the thing
# `requires_distinct_approver` below refuses case-by-case, made structurally impossible
# instead. Agreement with the domain spelling is asserted by
# `tests/test_approval_contract_agreement.py`.
APPROVAL_PERMISSION = "workspace:administer"

# The four wire strings of `contracts/hitl-ticket/v1`. Same values, same spellings; see
# the module docstring for why they are duplicated rather than imported.
#
# `(str, Enum)` rather than `StrEnum` to match `OperationState` in `identity.py`, whose
# own comment gives the reason: these values are stored in a database column and
# compared across a boundary, so the two enums in this package should be obviously the
# same shape. Note this differs from the HITL contract's own `StrEnum` choice -- that
# module renders the value through f-strings into hand-rolled payloads, which this one
# never does. `ruff` UP042 is already waived package-wide for exactly this reason.


class ApprovalResult(str, Enum):
    """The closed answer vocabulary. Only one member permits."""

    ALLOWED_ONCE = "allowed-once"
    """Approval, scoped to exactly this ticket. The ONLY value meaning proceed."""

    REJECTED = "rejected"
    """A human, whose identity is recorded, said no."""

    CANCELLED = "cancelled"
    """The asking side withdrew the request before it was answered."""

    UNAVAILABLE = "unavailable"
    """Nobody could be asked, or the ask failed in transport. NOT a denial."""


#: Every result other than `ALLOWED_ONCE`. Mirrors the HITL contract's
#: `NON_PERMISSIVE_RESULTS`. Absence of a result is also non-permissive and cannot
#: appear in a set of results -- see the module docstring.
NON_PERMISSIVE_RESULTS: frozenset[ApprovalResult] = frozenset(
    r for r in ApprovalResult if r is not ApprovalResult.ALLOWED_ONCE
)


class ApprovalRefused(PermissionError):
    """A well-formed approval that does not authorize this admission.

    A `PermissionError` subclass for the same reason `OperationRefused` is one
    (`identity.py:611`): a caller handling authorization failures uniformly catches
    this too. Distinct from `OperationRefused` because the refusal is about the
    *approval*, and an operator reading a log needs to know which of the two gates
    said no -- they have different remedies (obtain an approval; obtain a permission).
    """


@dataclass(frozen=True)
class SpendEnvelope:
    """The aggregate resource, runtime and cost ceiling an approver agreed to.

    Frozen: an envelope that could be widened after approval is not a ceiling. Every
    field is a maximum, and `covers()` is the only comparison -- there is deliberately
    no arithmetic here, because C owns the ledger
    (`accounting.py:31-34`: "this module contains no balance, no reservation arithmetic
    and no spend total, because a second place computing cost is a second answer that
    can disagree with the real one"). An envelope is a bound to check against, not a
    balance to maintain.

    `max_cost_micros` is an integer of millionths rather than a float: a float cost
    ceiling compares unequal to itself across a JSON round trip, and the comparison is
    the whole point of the field.
    """

    max_resource_units: int
    max_runtime_seconds: int
    max_cost_micros: int

    def __post_init__(self) -> None:
        for name in ("max_resource_units", "max_runtime_seconds", "max_cost_micros"):
            value = getattr(self, name)
            # `bool` is an `int` subclass, and `True` would otherwise pass as a ceiling
            # of 1. Refused because a boolean in a numeric ceiling is a type confusion
            # whose symptom is a budget of one unit rather than an error.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractViolation(f"{name} must be an integer")
            if value < 0:
                raise ContractViolation(f"{name} must not be negative")

    def covers(self, requested: SpendEnvelope) -> bool:
        """Whether `requested` fits entirely inside this approved envelope.

        One-directional: asking for less than was approved is fine, asking for more is
        refused. Every dimension must fit -- an operation cheap in cost but exceeding
        the approved runtime is outside what the approver agreed to, and a check that
        took the best of the three dimensions would admit it.
        """
        if not isinstance(requested, SpendEnvelope):
            raise ContractViolation("covers() requires a SpendEnvelope")
        return (
            requested.max_resource_units <= self.max_resource_units
            and requested.max_runtime_seconds <= self.max_runtime_seconds
            and requested.max_cost_micros <= self.max_cost_micros
        )


@dataclass(frozen=True)
class ApprovalBinding:
    """The immutable facts an approval is bound to.

    "Immutable plan/workspace/inputs" from this story's design 1, expressed as a type
    whose only constructor derives every field from a resolved principal and a request.
    There is no constructor taking a digest, because a caller-supplied digest is a
    caller-supplied claim about what was approved.

    `plan_digest` covers the action, the idempotency key, the contract version and every
    parameter (`identity.payload_digest`) -- so "changed inputs" is detected by the
    digest differing, not by comparing fields this type would have to enumerate and
    keep in step with the request shape.
    """

    org_id: str
    workspace_id: str
    plan_digest: str
    requester: str

    @classmethod
    def for_request(
        cls, principal: ResolvedPrincipal, request: OperationRequest
    ) -> ApprovalBinding:
        """Bind an approval to exactly what this principal is asking for.

        The tenant comes from the principal and the plan from the request, which is the
        same split `OperationBinding.issue` makes for the same reason: one place to read
        to confirm the tenant was never caller-supplied.
        """
        if not isinstance(principal, ResolvedPrincipal):
            raise ContractViolation("binding requires a ResolvedPrincipal")
        if not isinstance(request, OperationRequest):
            raise ContractViolation("binding requires an OperationRequest")
        return cls(
            org_id=principal.org_id,
            workspace_id=principal.workspace_id,
            plan_digest=payload_digest(request),
            requester=principal.subject,
        )


@dataclass(frozen=True)
class ApproverStatus:
    """Current, freshly-read facts about one approver.

    This type is the reason the recheck is a *recheck*. It is supplied by the caller at
    each decision point, and nothing here caches it: a cached status is authority
    inherited from the past, which is precisely what
    `WorkspaceAuthorizationModel.authorize_operation` refuses to allow
    (`policy.py:626-645`: "R6's 'rechecked at the operation' is a repeat of this call,
    not a single check whose result is cached on a session").

    `revoked` is separate from "absent from the mapping" because the two arrive
    differently: a membership lookup that returns nothing and a membership record
    explicitly marked revoked are different reads, and an implementation that modelled
    revocation as absence would silently treat a failed lookup as a revocation.
    """

    subject: str
    is_member: bool
    permissions: frozenset[str] = field(default_factory=frozenset)
    revoked: bool = False

    @property
    def may_approve(self) -> bool:
        """Whether this approver currently carries approval authority."""
        return (
            self.is_member
            and not self.revoked
            and APPROVAL_PERMISSION in self.permissions
        )


@dataclass(frozen=True)
class ApprovalRecord:
    """A server-held record that a human approved one specific request.

    Held by the server, never accepted from a request body -- the same property
    `OperationAuthorization` states for itself (`policy.py:551-560`: "A fabricated
    envelope arriving with the request is not one of these, which is the whole point of
    requiring the object rather than a claim in the payload").

    `approvers` is the set a *policy* selected, recorded so the recheck knows whose
    authority to re-examine. Plural because a policy may require more than one, and
    `decided_by` names which of them actually answered.
    """

    approval_id: str
    binding: ApprovalBinding
    envelope: SpendEnvelope
    result: ApprovalResult
    approvers: frozenset[str]
    decided_by: str
    decided_at: datetime
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.result, ApprovalResult):
            raise ContractViolation("result must be an ApprovalResult")
        if not isinstance(self.binding, ApprovalBinding):
            raise ContractViolation("binding must be an ApprovalBinding")
        if not isinstance(self.envelope, SpendEnvelope):
            raise ContractViolation("envelope must be a SpendEnvelope")
        if not isinstance(self.approval_id, str) or not self.approval_id:
            raise ContractViolation("approval_id is required")
        for name in ("decided_at", "expires_at"):
            value = getattr(self, name)
            # Timezone-aware only, matching the HITL contract's `_require_timezone` and
            # `handles.py:183-191`. A naive datetime compared against an aware `now()`
            # raises at the comparison -- inside the expiry check, which is the one
            # place an exception would be read as "could not determine" rather than
            # "expired".
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ContractViolation(f"{name} must be timezone-aware")
        if not isinstance(self.approvers, frozenset) or not self.approvers:
            raise ContractViolation("approvers must be a non-empty frozenset")
        if self.decided_by not in self.approvers:
            # An answer from someone the policy did not select is not an answer to this
            # ticket. Refused at construction so no such record can exist to be
            # rechecked -- the HITL contract makes the same check on its named mode.
            raise ContractViolation(
                "decided_by must be one of the policy-selected approvers"
            )

    @property
    def is_permissive(self) -> bool:
        """Whether the recorded answer is the one value that means proceed."""
        return self.result is ApprovalResult.ALLOWED_ONCE


@dataclass(frozen=True)
class ApprovalDecision:
    """The outcome of a recheck: permitted, or refused with a caller-safe reason.

    Returned rather than raised by `evaluate_approval` so a caller can report a denial
    without exception handling, and so the *reason* is a value that can be logged and
    asserted on. `admission.py` converts a refusal into `ApprovalRefused` at the point
    it must stop.
    """

    permitted: bool
    reason: str

    def __post_init__(self) -> None:
        if self.permitted and self.reason:
            # A permitted decision carrying a reason invites a caller to log the reason
            # and conclude a denial. Permitted decisions have nothing to explain.
            raise ContractViolation("a permitted decision must not carry a reason")
        if not self.permitted and not self.reason:
            raise ContractViolation("a refusal must carry a reason")


def evaluate_approval(
    record: ApprovalRecord | None,
    *,
    principal: ResolvedPrincipal,
    request: OperationRequest,
    requested_envelope: SpendEnvelope,
    approver_statuses: dict[str, ApproverStatus],
    now: datetime,
) -> ApprovalDecision:
    """Decide whether `record` currently authorizes this exact request.

    Called at **both** decision and admission (this story's design 1). The same
    function at both points on purpose: two functions would be two chances for the
    admission-time check to be the weaker one, and the admission-time check is the one
    that matters, because it is the last thing between a stored approval and spending.

    The checks are ordered so that each is only asked once the previous one makes it
    meaningful, and no check can be reached with an unvalidated premise:

    1. **Existence.** `None` is a denial, not a default (#5524 §2).
    2. **The answer.** Anything other than `ALLOWED_ONCE` denies.
    3. **Revocation of the approval itself.**
    4. **Expiry**, against the caller's `now`.
    5. **The binding** -- tenant and plan. A changed plan means this approval is about
       a different request.
    6. **Self-approval.**
    7. **Current approver authority**, re-read now.
    8. **The envelope.**

    Returns a decision; raises only `ContractViolation` for malformed inputs, which is
    a programming error rather than a denial and must not be confused with one.
    """
    if not isinstance(principal, ResolvedPrincipal):
        raise ContractViolation("evaluate_approval requires a ResolvedPrincipal")
    if not isinstance(request, OperationRequest):
        raise ContractViolation("evaluate_approval requires an OperationRequest")
    if not isinstance(requested_envelope, SpendEnvelope):
        raise ContractViolation("evaluate_approval requires a SpendEnvelope")
    if not isinstance(now, datetime) or now.tzinfo is None:
        # Aware, so the expiry comparison below cannot raise. A naive `now` would fail
        # inside step 4 and surface as an error rather than as a denial.
        raise ContractViolation("now must be timezone-aware")
    if not isinstance(approver_statuses, dict):
        raise ContractViolation("approver_statuses must be a mapping")

    # (1) Absence. The case the answer vocabulary cannot represent.
    if record is None:
        return ApprovalDecision(
            permitted=False,
            reason="no approval record for this request; absence is not permission",
        )
    if not isinstance(record, ApprovalRecord):
        raise ContractViolation("record must be an ApprovalRecord or None")

    # (2) The recorded answer. `is_permissive` is the single place the permissive value
    # is named, so a new member added to `ApprovalResult` denies by default.
    if not record.is_permissive:
        return ApprovalDecision(
            permitted=False,
            reason=f"approval result is {record.result.value!r}, which does not permit",
        )

    # (3) Revocation of the approval. Checked before expiry because a revoked approval
    # that has not yet expired is still revoked, and reporting "expired" for it would
    # send an operator to the wrong remedy.
    if record.revoked:
        return ApprovalDecision(permitted=False, reason="approval has been revoked")

    # (4) Expiry. `<=` so an approval expiring exactly now does not permit: the
    # boundary belongs to the side that denies.
    if record.expires_at <= now:
        return ApprovalDecision(
            permitted=False,
            reason="approval expired before admission",
        )

    # (5) The binding. Recomputed from the principal and request in hand rather than
    # compared field-by-field, so a request shape that grows a field is covered without
    # this function changing.
    current = ApprovalBinding.for_request(principal, request)
    if record.binding.org_id != current.org_id:
        return ApprovalDecision(
            permitted=False, reason="approval was granted for a different organization"
        )
    if record.binding.workspace_id != current.workspace_id:
        return ApprovalDecision(
            permitted=False, reason="approval was granted for a different workspace"
        )
    if record.binding.plan_digest != current.plan_digest:
        # The "same key, bigger machine" case. Honouring it is how a retry becomes a
        # budget increase (#5524 §3.2).
        return ApprovalDecision(
            permitted=False,
            reason="approval was granted for a different plan; inputs have changed",
        )
    if record.binding.requester != current.requester:
        return ApprovalDecision(
            permitted=False,
            reason="approval was granted to a different requester",
        )

    # (6) Self-approval, delegated to the published predicate so the rule has exactly
    # one implementation. Note this is reached only after (5) established
    # `principal.subject == record.binding.requester`, so the predicate's two clauses
    # are the same comparison here -- both are kept in the predicate because it is also
    # callable on its own, where that equality has not been established.
    if not requires_distinct_approver(principal, record):
        return ApprovalDecision(
            permitted=False,
            reason="an approval issued by its own requester is not authority",
        )

    # (7) Current approver authority. Every policy-selected approver is re-read, not
    # only the one who answered: a policy that selected two and got one answer has not
    # been satisfied, and checking only `decided_by` would miss that.
    for subject in sorted(record.approvers):
        status = approver_statuses.get(subject)
        if status is None:
            # An unread status is not a pass. Same rule as (1), one level down.
            return ApprovalDecision(
                permitted=False,
                reason=(
                    "current authority for a selected approver could not be "
                    "established; an unverified approver does not authorize"
                ),
            )
        if not isinstance(status, ApproverStatus):
            raise ContractViolation("approver_statuses values must be ApproverStatus")
        if status.subject != subject:
            raise ContractViolation(
                "approver_statuses keys must match their ApproverStatus.subject"
            )
        if subject == record.decided_by and not status.may_approve:
            # Membership loss, permission loss or revocation since the decision.
            return ApprovalDecision(
                permitted=False,
                reason=(
                    "the approver no longer holds current authority to approve "
                    "this operation"
                ),
            )

    # (8) The envelope. Last, because it is the only check that is about the *size* of
    # what was asked rather than about whether the approval applies at all.
    if not record.envelope.covers(requested_envelope):
        return ApprovalDecision(
            permitted=False,
            reason=(
                "the requested resource, runtime or cost exceeds the approved envelope"
            ),
        )

    return ApprovalDecision(permitted=True, reason="")


def requires_distinct_approver(
    principal: ResolvedPrincipal, record: ApprovalRecord
) -> bool:
    """Whether this record's approver is someone other than its requester.

    Published as its own predicate because "no self-issued authority" is a named
    acceptance concern for this story, and a reviewer should be able to find the rule in
    one place rather than as two branches inside `evaluate_approval`. The gate calls the
    branches; this exists so the property is directly testable and directly readable.
    """
    if not isinstance(record, ApprovalRecord):
        raise ContractViolation("requires_distinct_approver requires an ApprovalRecord")
    return (
        record.decided_by != record.binding.requester
        and record.decided_by != principal.subject
    )


def utc_now() -> datetime:
    """Timezone-aware current time.

    Here rather than inlined at call sites so that every `now` this module compares
    against is aware by construction, and so a test can pass a fixed instant to
    `evaluate_approval` without this function being involved at all. Nothing in this
    module calls it: the gate takes `now` as a parameter precisely so expiry is
    testable without patching a clock.
    """
    return datetime.now(UTC)


# Re-exported so a caller checking the operation's own permission and the approver's
# does not have to import from two modules and risk mismatching the pair.
__all__ = [
    "APPROVAL_PERMISSION",
    "NON_PERMISSIVE_RESULTS",
    "REQUIRED_PERMISSION",
    "ApprovalBinding",
    "ApprovalDecision",
    "ApprovalRecord",
    "ApprovalRefused",
    "ApprovalResult",
    "ApproverStatus",
    "SpendEnvelope",
    "evaluate_approval",
    "requires_distinct_approver",
    "utc_now",
]

"""Execution/action vocabulary and the store's typed inputs and results.

Issue #5142 (ENGINE-K1, parent #5122). This module is the *contract*: the enums
that name what delivery work can be doing, and the immutable dataclasses that
`execution_store.py` accepts and returns. It is deliberately free of database and
network access so that the sibling issues — the runner (#5143) and the read model
(#5145) — can import it and integrate against a checked-in shape rather than
against a guess.

## Why a separate ledger at all

A worker carries "where am I up to" only in process memory. When the pod is
evicted, the container is killed, or the run simply ends before the work is done,
nothing outside that process records what already happened. A later process
cannot answer whether the branch was pushed, whether the pull request was opened,
or whether an external call that was started ever completed. The two outcomes are
both bad and both observed: work that has stalled while looking finished, and work
that is repeated so a side effect happens twice.

These records are that missing durable state. An **execution** is the identity of
"this node, on this cycle, is being carried out", and an **action** is one
externally-visible step it takes.

## What this is NOT

`src/agentauth/execution.py` already owns a protected per-run record used for run
authentication, keyed on its own run identifiers. This is a different thing with a
different purpose: a delivery ledger keyed on graph nodes. The two are not merged
and neither reads the other's ids.

## Vocabulary is pinned here exactly once

`NodeState` is imported from `state.py`, never redeclared — the same requirement
that `models.py` documents (a second copy of a vocabulary is a requirement
violation, because the two copies drift and then disagree about what a stored
string means). The phase/status/block/action vocabularies below are the *new*
ones this issue introduces, and they are declared here and nowhere else;
`execution_store.py`, the migration and the tests all read them from this module.

The relationship between the two is a projection, not a duplication: `NodeState`
is the *graph's* answer to "has this node passed?", which includes the human gate
and terminal outcomes the engine may not set for itself. `ExecutionPhase` is the
*ledger's* answer to "what part of carrying it out are we in?". A node sitting in
`NodeState.RUNNING` can be in any of several execution phases, and nothing here
widens what the graph permits.

## Why every refusal is a typed value

A store method that returned `"stale"` or `"blocked"` as a bare string would push
string comparison into every consumer, and a typo in one consumer would read as
"not stale" — failing open on exactly the check that exists to fail closed. So the
store returns an `ExecutionOutcome` carrying an `OutcomeKind` enum member, and a
blocked result carries a structured `BlockRecord` rather than prose. Consumers
branch on enum identity.

## Why the dataclasses are frozen

A caller must not be able to mutate a result after the store produced it — a
mutated `revision` on a returned record would be presented to the next
compare-and-set as though the store had issued it. `frozen=True` makes that a
`FrozenInstanceError` at the point of the mistake instead of a silent
lost-update. The same applies to the inputs: an `ActionIntent` handed to two
calls must describe the same step in both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# Imported, never redeclared. See the module docstring.
from .state import NodeState

__all__ = [
    "ActionIntent",
    "ActionRecord",
    "ActionStatus",
    "BlockCode",
    "BlockRecord",
    "ExecutionIdentity",
    "ExecutionOutcome",
    "ExecutionPhase",
    "ExecutionRecord",
    "ExecutionStatus",
    "ExecutionStoreError",
    "NodeState",
    "Observation",
    "ObservedOutcome",
    "OutcomeKind",
    "PhaseAdvance",
    "TERMINAL_EXECUTION_STATUSES",
    "UNRESOLVED_ACTION_STATUSES",
]


class ExecutionPhase(StrEnum):
    """Which part of carrying out a node's delivery work we are in.

    Ordered by normal progression, but the ordering is documentation rather than
    a state machine — the legal moves belong to the graph (`state.py`) and to the
    phase handlers a sibling issue owns. What this enum guarantees is that a
    *stored* phase means one thing to every reader.

    `ADMITTED` exists separately from `PREPARING` for the same reason
    `work_claims.py` distinguishes an admitted claim from a bound run: there is a
    real window in which ownership is established and no work has begun, and
    collapsing it would make a crash in that window indistinguishable from a
    crash mid-preparation.
    """

    ADMITTED = "admitted"  # Ledger identity exists; no work started yet
    PREPARING = "preparing"  # Gathering context/credentials for the first action
    DELIVERING = "delivering"  # Producing the change itself
    SUBMITTING = "submitting"  # Publishing the change (branch/PR) to the provider
    AWAITING_REVIEW = "awaiting_review"  # Published; waiting on review/check evidence
    REPAIRING = "repairing"  # Responding to review or a failed check
    MERGE_READY = "merge_ready"  # Reviewed current head; merge is a separate effect
    DEPLOYMENT_PENDING = "deployment_pending"  # Code merged; deployment remains separately observable
    AWAITING_RUNTIME_VERIFICATION = "awaiting_runtime_verification"  # Workflow completed; runtime acceptance still required
    EVALUATION_PENDING = "evaluation_pending"  # Runtime verified; graph evaluation still required
    SETTLING = "settling"  # Evidence observed; recording the terminal result
    CONCLUDED = "concluded"  # Nothing further for this execution to do


class ExecutionStatus(StrEnum):
    """Whether this execution is runnable right now, and if not, why not.

    Separate from `ExecutionPhase` because they answer different questions and a
    single column conflating them cannot express the common case: *delivering, but
    currently waiting on an external outcome we did not observe*. A runner asks
    this enum "may I pick this up?" and the phase "what would I be doing?".

    `AWAITING_EXTERNAL` is the honest state for an action whose result is unknown.
    It is not `RUNNABLE` (picking it up could repeat a side effect that already
    happened) and it is not `BLOCKED` (nothing needs a human). Recording an
    unknown outcome as either of those is the specific mistake this member
    prevents: the issue requires that an unknown action outcome remain uncertain.
    """

    RUNNABLE = "runnable"  # Due for pickup at or after next_check_at
    AWAITING_EXTERNAL = "awaiting_external"  # An action's outcome is not yet known
    BLOCKED = "blocked"  # Cannot proceed without the input named in BlockRecord
    CONCLUDED = "concluded"  # Finished; no further pickup
    SUPERSEDED = "superseded"  # Replaced by a newer cycle of the same node


# Statuses a runner must never pick up. Derived as data rather than re-listed at
# each call site, so adding a member cannot leave one reader behind.
TERMINAL_EXECUTION_STATUSES: frozenset[ExecutionStatus] = frozenset({ExecutionStatus.CONCLUDED, ExecutionStatus.SUPERSEDED})


class BlockCode(StrEnum):
    """Why an execution cannot proceed, in a form an operator can route.

    Machine-readable and stable: these values appear in a stored column, in
    operator diagnostics and (later) in the read model, so they are part of the
    contract rather than log text. Each names something a *different* party
    resolves, which is why they are not collapsed into one "blocked" value.

    Note what is absent. There is no `INTERNAL_ERROR` catch-all: a block must name
    a resolvable condition and an owner, and an unclassified failure is an attempt
    failure (see `ATTEMPTS_EXHAUSTED`), not a block with no owner.
    """

    HUMAN_GATE_REQUIRED = "human_gate_required"  # An existing gate node awaits a human
    HUMAN_INPUT_REQUIRED = "human_input_required"  # A question only the requester can answer
    HUMAN_REFUSED = "human_refused"  # A human declined; existing recovery path owns it
    DEPENDENCY_UNSATISFIED = "dependency_unsatisfied"  # A predecessor node has not passed
    AUTHORITY_UNVERIFIABLE = "authority_unverifiable"  # Policy/claim binding could not be confirmed
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"  # Scoped credential absent; no broad fallback
    BUDGET_EXHAUSTED = "budget_exhausted"  # Existing policy limit reached
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"  # Attempt bound reached; recovery is authorized-only
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # External provider is failing; retryable
    OWNERSHIP_LOST = "ownership_lost"  # The claim generation moved on; this run is stale


class ActionStatus(StrEnum):
    """Where one externally-visible step stands.

    `PREPARED` and `DISPATCHED` are distinct because the gap between them is the
    dangerous one: a record written before the external call, and a crash inside
    it, leaves a row that says "we were about to do this" — which is precisely
    what lets a later process ask the provider rather than blindly retrying.

    `UNKNOWN` is a first-class terminal-ish state, not an error. An action whose
    outcome was never observed must stay unknown; recording it as `SUCCEEDED`
    would let work advance on evidence nobody saw, and recording it as `FAILED`
    would invite a retry that duplicates a side effect that may have landed.
    """

    PREPARED = "prepared"  # Intent recorded; external call not yet made
    DISPATCHED = "dispatched"  # External call made; result not yet observed
    SUCCEEDED = "succeeded"  # Observed to have completed
    FAILED = "failed"  # Observed to have failed
    UNKNOWN = "unknown"  # Outcome was never observed and cannot be assumed


# Action statuses that do not license advancing past the action. `UNKNOWN` is here
# deliberately: it is settled as a *record* but unresolved as *evidence*.
UNRESOLVED_ACTION_STATUSES: frozenset[ActionStatus] = frozenset({ActionStatus.PREPARED, ActionStatus.DISPATCHED, ActionStatus.UNKNOWN})


class ObservedOutcome(StrEnum):
    """What an observer reports about an action it looked at.

    Narrower than `ActionStatus` on purpose: an observer may only report what it
    saw. It cannot set `PREPARED` (that is the store's own bookkeeping), and its
    `INDETERMINATE` maps to `ActionStatus.UNKNOWN` rather than to a guess.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"  # Looked, could not tell → stays UNKNOWN


class OutcomeKind(StrEnum):
    """The four answers a store transition can give.

    Typed rather than stringly so a consumer branches on enum identity. Each is
    acted on differently, which is why none of them collapse:

    - `APPLIED`: the write happened; the returned record is current.
    - `STALE`: the caller's revision is not the row's; *another writer moved it*.
      The caller must re-read, never re-apply.
    - `CONFLICT`: the caller's authority binding does not match the stored row
      (wrong tenant, superseded claim generation, different accepted plan
      version). Not a retry — the caller has no standing to write here.
    - `BLOCKED`: the write happened *and* recorded a block; the accompanying
      `BlockRecord` names the owner and the required input.
    """

    APPLIED = "applied"
    STALE = "stale"
    CONFLICT = "conflict"
    BLOCKED = "blocked"


class ExecutionStoreError(RuntimeError):
    """The request was malformed, or named something that does not exist.

    Distinct from a `STALE`/`CONFLICT` outcome the same way `WorkClaimError` is
    distinct from a `CONFLICT` receipt in `work_claims.py`: an outcome is an
    *answer* about the ledger, this is "the question could not be asked". Callers
    fail closed on it rather than treating it as an absent record.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExecutionIdentity:
    """Which work, under whose authority. Presented on every store call.

    This is the "current authoritative binding" the store re-verifies *inside* the
    transaction. Passing it on every call — rather than trusting the row, or
    trusting a check the caller did earlier — is what makes the guard hold under
    concurrency: between a caller's read and its write, the claim generation can
    advance and the accepted plan can be superseded, and only a check inside the
    writing transaction sees that.

    Each field names a contract owned elsewhere and reused here, never
    re-implemented:

    - `org_id` is the tenant partition every orchestration table carries.
    - `node_id` is a node in the existing orchestration graph.
    - `cycle` distinguishes a re-delivery of the same node from the original. It
      is part of the execution's uniqueness key, so a repair cycle gets its own
      durable identity instead of overwriting the history of the first attempt.
    - `accepted_plan_version` is the accepted-plan version integer that
      `policy_admission.load_in_force_policy` resolves (`AdmissionInputs.
      plan_version`). Stored so a record states which accepted plan authorized
      it; a write presenting a different version is a `CONFLICT`, because the plan
      it was authorized against is no longer the plan in force.
    - `claim_id` / `claim_generation` come from the `ClaimReceipt` that
      `work_claims.claim_work` issued (#5127). The generation is the fence: a run
      holding generation N when the claim has moved to N+1 is stale by
      construction. This store keeps no second claim record and never decides
      ownership itself — it only refuses to write for a generation that is no
      longer current.
    """

    org_id: str
    node_id: str
    cycle: int
    accepted_plan_version: int
    claim_id: str
    claim_generation: int

    def __post_init__(self) -> None:
        # Validated at construction rather than at the database, because a blank
        # tenant or a zero generation reaching SQL becomes either an integrity
        # error with no context or — worse, for org_id — a row that no tenant
        # query will ever return but which occupies the uniqueness key.
        if not str(self.org_id or "").strip():
            raise ExecutionStoreError("invalid_identity", "An execution identity must name its tenant (org_id).")
        if not str(self.node_id or "").strip():
            raise ExecutionStoreError("invalid_identity", "An execution identity must name its graph node (node_id).")
        if isinstance(self.cycle, bool) or not isinstance(self.cycle, int) or self.cycle < 1:
            raise ExecutionStoreError("invalid_identity", "cycle must be a positive integer; cycles start at 1.")
        if isinstance(self.accepted_plan_version, bool) or not isinstance(self.accepted_plan_version, int) or self.accepted_plan_version < 0:
            # 0 is legal and meaningful: `load_in_force_policy` reports
            # plan_version=0 when no accepted plan exists, which is the legacy
            # path that must stay usable. Negative is not a version.
            raise ExecutionStoreError("invalid_identity", "accepted_plan_version must be a non-negative integer.")
        if not str(self.claim_id or "").strip():
            raise ExecutionStoreError("invalid_identity", "An execution identity must name the work claim it runs under.")
        if isinstance(self.claim_generation, bool) or not isinstance(self.claim_generation, int) or self.claim_generation < 1:
            raise ExecutionStoreError("invalid_identity", "claim_generation must be a positive integer; generations start at 1.")


@dataclass(frozen=True)
class ActionIntent:
    """One externally-visible step, described before it is attempted.

    `operation_key` is the idempotency key and the whole point of this type. It is
    caller-supplied and must be *derived from the work*, not generated per
    attempt: `"open_pr:node-7:cycle-1"` is correct, a fresh UUID is not. The store
    enforces uniqueness on `(org_id, execution_id, operation_key)`, so a retry
    after a crash presents the same key and receives the original record instead
    of opening a second pull request. A per-attempt key would satisfy the type and
    defeat the protection, which is why it is documented here rather than only in
    the store.

    `artifact_ref` holds a *reference* — an S3 key, a PR node id, a comment id —
    and never a credential, a token or a complete transcript. These rows are read
    by operators and surfaced in diagnostics, so a secret written here would be a
    disclosure with no revocation path.
    """

    operation_key: str
    kind: str
    # A reference to the input or output artifact, if any. Reference only.
    artifact_ref: str | None = None
    # Small, non-sensitive detail for operators: which repo, which PR number.
    # Deliberately not a free-form payload dump — see artifact_ref's note.
    detail: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.operation_key or "").strip():
            # Without a key there is no idempotency at all: every retry would
            # insert a new action and duplicate its side effect. Refused rather
            # than defaulted to a generated value, which would look like it
            # worked.
            raise ExecutionStoreError("invalid_action", "An action intent must carry an operation_key for idempotency.")
        if not str(self.kind or "").strip():
            raise ExecutionStoreError("invalid_action", "An action intent must name its kind.")


@dataclass(frozen=True)
class Observation:
    """What an observer saw about one prepared action.

    Separate from `ActionIntent` because observing is a different authority from
    intending: the intent is written by the process about to act, the observation
    by whatever later process managed to look. Keeping them apart is what allows a
    *different* process to settle an action the original run never reported on —
    the recovery case this whole ledger exists for.

    `receipt_ref` is the provider's own identifier for the effect (a PR node id, a
    comment id, a check-run id). It is what makes the observation falsifiable
    later: an operator can go and look.
    """

    operation_key: str
    outcome: ObservedOutcome
    receipt_ref: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not str(self.operation_key or "").strip():
            raise ExecutionStoreError("invalid_observation", "An observation must name the operation_key it observed.")


@dataclass(frozen=True)
class BlockRecord:
    """Why work stopped, who resolves it, and what they must supply.

    Every field is here because an operator reading only this record must be able
    to act. A block that says `blocked: true` and nothing else sends someone to
    the logs, and logs expire.

    `progressed_at` is the last time this execution made real progress, carried on
    the block so "stuck for two minutes" and "stuck since Tuesday" are
    distinguishable without reconstructing a timeline.

    `remaining_gates` lists the human decision points still outstanding. It is
    informational here: the gates themselves stay with the existing
    `controls.py`/graph state, and nothing in this module approves or bypasses
    one. `BlockCode.ATTEMPTS_EXHAUSTED` and `BlockCode.HUMAN_REFUSED` likewise
    record a condition and route to the existing authorized recovery path; this
    store creates no new way out of either.
    """

    code: BlockCode
    owner: str  # Who resolves it: an operator role, a requesting user, a service
    required_input: str  # What must be supplied, in plain words
    remaining_gates: tuple[str, ...] = ()
    progressed_at: datetime | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not str(self.owner or "").strip():
            raise ExecutionStoreError("invalid_block", "A block must name who resolves it; an unowned block is a stall.")
        if not str(self.required_input or "").strip():
            raise ExecutionStoreError("invalid_block", "A block must state what input is required to clear it.")


@dataclass(frozen=True)
class PhaseAdvance:
    """The requested next state of an execution, as one atomic unit.

    `phase`, `status` and `next_check_at` travel together because writing any of
    them without the others produces a durable inconsistency. The specific failure
    the atomicity prevents: advancing the phase while failing to record the next
    check time leaves an execution that has moved on and will never be picked up
    again — a stall that looks, in every dashboard, exactly like progress.

    `expected_revision` is the compare-and-set fence. The caller presents the
    revision it read; the store applies the write only if the row still carries
    it, and otherwise answers `STALE` without writing. There is no
    "retry with the current revision" convenience here on purpose — re-reading is
    the caller's job, because whatever moved the row may have changed what the
    caller wants to do.
    """

    phase: ExecutionPhase
    status: ExecutionStatus
    expected_revision: int
    # When a runner should next consider this execution. Required for a RUNNABLE
    # or AWAITING_EXTERNAL advance (otherwise the work is invisible to pickup) and
    # meaningless for a terminal one; the store enforces that pairing rather than
    # trusting each caller to remember it.
    next_check_at: datetime | None = None
    # Increments the attempt counter. Set when this advance begins a new attempt,
    # so the attempt bound is counted by the store rather than by callers who
    # would each count differently.
    consume_attempt: bool = False
    deadline_at: datetime | None = None
    progress_note: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.expected_revision, bool) or not isinstance(self.expected_revision, int) or self.expected_revision < 1:
            raise ExecutionStoreError("invalid_advance", "expected_revision must be a positive integer read from the record.")


@dataclass(frozen=True)
class ActionRecord:
    """A stored action, as the store returns it. Read-only to consumers."""

    id: str
    org_id: str
    execution_id: str
    operation_key: str
    kind: str
    status: ActionStatus
    attempt: int
    artifact_ref: str | None
    receipt_ref: str | None
    detail: dict[str, str]
    created_at: datetime | None
    observed_at: datetime | None

    @property
    def resolved(self) -> bool:
        """True only when the outcome was actually observed as success or failure.

        `UNKNOWN` is excluded: it is a settled record of an *unsettled* fact. A
        consumer that treated it as resolved would advance on evidence nobody saw.
        """
        return self.status not in UNRESOLVED_ACTION_STATUSES


@dataclass(frozen=True)
class ExecutionRecord:
    """A stored execution, as the store returns it. Read-only to consumers.

    This is also the read model #5145 renders: it carries everything an operator
    needs about one piece of delivery work — where it is, whether it is runnable,
    why not if not, when it was last making progress — without a join and without
    exposing anything sensitive. The receipt reference columns are the hooks
    #5143 and #5144 need for pending external outcomes, notifications and
    handoffs; they hold references only.
    """

    id: str
    org_id: str
    flow_id: str
    node_id: str
    cycle: int
    phase: ExecutionPhase
    status: ExecutionStatus
    revision: int
    accepted_plan_version: int
    claim_id: str
    claim_generation: int
    attempts: int
    next_check_at: datetime | None = None
    deadline_at: datetime | None = None
    progressed_at: datetime | None = None
    progress_note: str | None = None
    block: BlockRecord | None = None
    pending_action_key: str | None = None
    notification_receipt_ref: str | None = None
    handoff_receipt_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def runnable(self) -> bool:
        """Whether a runner may pick this up at all, ignoring the clock.

        Time-based due-ness is the runner's query (#5143), not this property:
        combining them here would make a record's own report of itself depend on
        when it was asked.
        """
        return self.status is ExecutionStatus.RUNNABLE


@dataclass(frozen=True)
class ExecutionOutcome:
    """The result of a store transition: what happened, and the current record.

    `record` is populated on every kind, including refusals, and that is the
    useful part: a caller that lost a compare-and-set receives the *current* row
    it lost to, so it can decide what to do without a second round trip. On
    `CONFLICT` the record is omitted when the caller had no standing to read it
    (a mismatched tenant), because returning it would leak across the boundary the
    conflict exists to enforce.
    """

    kind: OutcomeKind
    record: ExecutionRecord | None = None
    action: ActionRecord | None = None
    # Stable machine-readable detail for refusals, so an operator can tell which
    # fail-closed arm fired: "stale_revision", "claim_generation_superseded",
    # "accepted_plan_version_mismatch", "tenant_mismatch".
    reason: str | None = None

    @property
    def applied(self) -> bool:
        """True only for APPLIED. A BLOCKED write persisted, but did not advance."""
        return self.kind is OutcomeKind.APPLIED

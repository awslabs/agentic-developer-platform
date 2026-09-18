"""Orchestration graph store: flows, nodes, edges, accepted plans, decisions.

Issue #4196 (EPIC #4191, intent #4120). Durable storage for the plan that was
accepted at a gate and for where the loop currently stands against it.

This is the schema that makes "engine-enforced trust" structural rather than
behavioural: once a loop proposal is accepted, the accepted plan is recorded by
the engine and all later progress is tracked against **what was accepted** — not
against prose, and not rewritable by agents.

Shape decisions that are load-bearing, not stylistic:

- **The story is the graph floor.** `orchestration_nodes` holds executable nodes
  only — story / eval / gate. Runs attach via the ledger, never as nodes.
  Containers (wave / EPIC / flow) are **derived state**, computed by rolling up
  their member nodes, not rows to be kept in sync. A container row would be a
  second source of truth for a value already implied by its children, and the
  two would drift.

- **The accepted plan is versioned, not mutable.** Amendment writes a new
  version row and supersedes the prior one. Nothing rewrites an accepted plan in
  place, so "what was accepted at the gate" stays answerable after an amendment.

- **Decisions are append-only** and carry three separate attribution fields:
  `actor_id`, `actor_role` *at decision time*, and `actor_kind` (human vs
  service) as its own non-nullable discriminator. The discriminator is required
  because the existing `tenant_access_requests.decided_by` column mixes real
  Cognito subs (`admin/onboarding/handler.py:1041`) with synthetic values like
  `system:org-member-match` (`handler.py:391`) in one `String(255)` — the two are
  indistinguishable after the fact, so "was this approved by a human?" is not
  answerable from that column. Here it is a column, not a string convention.
  `actor_role` is snapshotted because roles change; attribution must reflect the
  authority held when the decision was made, not whatever the actor holds today.

- **Attribution does NOT go to `audit_logs`.** That table has no alembic DDL at
  all — `admin/models.py::AuditLog` declares it, and it only ever exists under
  the local dev `create_all` flag. `008_magic_link.py` creates
  `security_audit_logs`, a different table. Persisting attribution to
  `audit_logs` would pass every local test and raise `UndefinedTable` on every
  deployed database.

Tenant isolation: every table carries `org_id` via `TenantMixin`, and every
query in `repository.py` filters on it. There is no cross-tenant read path.

The node-state vocabulary (`NodeState`, `ActorKind`, `LEGAL_TRANSITIONS`) is
imported from `state.py` and deliberately NOT redefined here — R-N2a makes a
second copy of the vocabulary a requirement violation, not a style preference.
The same rule governs the execution/action vocabulary added for issue #5142:
`ExecutionPhase`, `ExecutionStatus`, `BlockCode` and `ActionStatus` are declared
once in `execution_state.py`, and the `OrchestrationExecution` /
`OrchestrationAction` columns below store their values as strings without
restating the members.

JSON column note: `plan_document` is declared with a dialect variant so it is real
`JSONB` on Postgres (GIN-indexable later) and plain `JSON` on SQLite, where the
tests run. A bare `JSON` would render as `JSON` on Postgres as well and quietly
forfeit JSONB; the migration declares the identical variant so the two agree.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, Session, mapped_column

from src.shared.models.base import Base, TenantMixin, new_uuid, utcnow

# Re-exported for callers that need the vocabulary alongside the tables. Imported
# from state.py rather than redeclared — see module docstring.
from .state import ActorKind, NodeState

# Must stay identical to JSON_DOC in alembic/versions/029_orchestration_graph.py.
JSON_DOC = JSON().with_variant(postgresql.JSONB(), "postgresql")

__all__ = [
    "ActorKind",
    "DecisionKind",
    "NodeKind",
    "NodeState",
    "OrchestrationAcceptedPlan",
    "OrchestrationAction",
    "OrchestrationDecision",
    "OrchestrationEdge",
    "OrchestrationExecution",
    "OrchestrationFlow",
    "OrchestrationNode",
    "OrchestrationPullRequestBinding",
    "OrchestrationWorkClaim",
    "BindingRole",
    "BindingState",
    "ClaimState",
    "AppendOnlyViolationError",
]


class NodeKind(StrEnum):
    """The three executable node kinds — the graph floor.

    Container levels (wave / epic / flow) are deliberately absent: they are
    derived by rolling up member nodes, not stored as nodes. A `WAVE` member of
    this enum would invite exactly the container-rows-as-nodes mistake the
    schema is shaped to prevent.
    """

    STORY = "story"  # A unit of delivery work
    EVAL = "eval"  # An automated evaluation of preceding work
    GATE = "gate"  # A human decision point


class DecisionKind(StrEnum):
    """What a decision record attributes.

    `TRANSITION_REJECTED` is first-class rather than an error log: under RULING 5
    recorded rejections are the primary detector for off-plan agent activity, so
    they must be queryable alongside the decisions they were rejected against.
    """

    PLAN_ACCEPTED = "plan_accepted"  # A loop proposal was accepted at a gate
    PLAN_AMENDED = "plan_amended"  # A new accepted-plan version superseded one
    GATE_APPROVED = "gate_approved"  # A gate node was approved
    GATE_PRESENTED = "gate_presented"  # Dependencies satisfied; human answer required
    NODE_RESUMED = "node_resumed"
    NODE_DISPATCHED = "node_dispatched"  # Stable attempt/run binding
    RESULT_CHECKED = "result_checked"
    RESULT_OBSERVED = "result_observed"  # Evidence observed for the current attempt
    PR_BINDING_CHANGED = "pr_binding_changed"  # Append-only binding revision/provenance
    GATE_REJECTED = "gate_rejected"  # A gate node was refused
    TRANSITION_REJECTED = "transition_rejected"  # An illegal transition attempt
    HALT_OVERRIDDEN = "halt_overridden"  # A human cleared a halt (R-Q9c)
    # Issue #4211. Two members, not one, because a stall and a halt need different
    # operator responses: a stall is "go find out why this node is wedged", a halt
    # is "this defect is not converging, stop paying for it". Recording which
    # occurred is what makes the outcome diagnosable rather than inferred from a
    # state that would mean both. `kind` is String(32), so neither needs DDL.
    NODE_STALLED = "node_stalled"  # Ran past the stall threshold (R-O4a)
    NODE_HALTED = "node_halted"  # Defect-cycle bound exhausted (R-Q9c)
    # Issue #4214. An agent's *advisory* diagnosis of a stalled or halted node —
    # a proposal, never a verdict, and never a state change. The name says
    # `_PROPOSED` because the record's authority is the whole question: a row
    # named `NODE_DIAGNOSED` would read as a settled finding, and a human who
    # reads an agent's guess as a conclusion turns the gate this EPIC exists to
    # protect into a rubber stamp. Rows of this kind always carry
    # `to_state = NULL`: a diagnosis proposes nowhere for the node to go, so it
    # is structurally incapable of expressing a promotion. `kind` is String(32),
    # so this needs no DDL.
    NODE_DIAGNOSIS_PROPOSED = "node_diagnosis_proposed"
    # Issue #4527. A human asked, in words, for the plan to be re-planned. Like a
    # diagnosis this row carries `to_state = NULL`, and for the same structural
    # reason: a replan request proposes *nowhere* for any node to go, so the record
    # is incapable of expressing a promotion. v1 records the request and notifies;
    # the authoring loop that turns it into an amended plan is a separate story, so
    # a row of this kind is deliberately the whole of the effect. `kind` is
    # String(32), so this needs no DDL.
    REPLAN_REQUESTED = "replan_requested"
    # Issue #4528. An authoring agent *registered* a compiled proposal as a draft.
    # Deliberately NOT in `genesis.APPROVAL_DECISION_KINDS`: that frozenset is what
    # roots an engine dispatch, so a kind absent from it cannot arm execution no
    # matter how many ticks run. That absence is the whole reason this is a
    # separate member rather than reusing `PLAN_ACCEPTED` — registration must land
    # a graph a human can *see* without landing an approval nobody made. `kind` is
    # String(32), so this needs no DDL.
    PLAN_DRAFTED = "plan_drafted"
    AGENT_DISPATCHED = "agent_dispatched"  # Committed delegated dispatch receipt, never human approval
    WAVE_MATERIALIZED = "wave_materialized"  # Bound delivery issue references; never an approval
    WAVE_COORDINATOR_DISPATCHED = "wave_coordinator_dispatched"


class ClaimState(StrEnum):
    """Whether a work claim currently authorizes a run.

    Two members, not a boolean, because "nobody owns this issue right now" and
    "someone owns it" are read at admission while the *history* of the row has to
    survive: the claim row is reused across generations rather than deleted, so a
    released claim keeps its release reason and its generation for the next
    admission to build on. Stored as `String(16)`, so a future member needs no DDL.
    """

    HELD = "held"  # An owner holds this issue; a competing claim is refused
    RELEASED = "released"  # No current owner; the next claim advances generation


class BindingRole(StrEnum):
    """What a bound pull request *is* to its story.

    Two members because the reviewer-artifact PR is the single most dangerous
    false positive in this whole path. A reviewer run pushes its transcript to the
    same issue's branch family, so "a merged PR referencing this issue" is
    satisfied by a PR that contains no implementation at all. Completing a story
    on one would mark delivery done on the strength of a review log.

    Only `IMPLEMENTATION` can complete a story — enforced in `pr_bindings.py`, not
    by convention. Stored as `String(32)`, so a further member needs no DDL.
    """

    IMPLEMENTATION = "implementation"  # Carries the story's delivered code
    REVIEWER_ARTIFACT = "reviewer_artifact"  # Review output only; completes nothing


class BindingState(StrEnum):
    """Whether this binding is the one reconciliation may read.

    `SUPERSEDED` exists rather than deleting the row for the reason the work-claim
    row survives release: an authorized replacement must retain provenance, and a
    deleted row cannot explain who replaced what. It is also the fence — a
    superseded binding is permanently incapable of completing the new scope, which
    is what stops an old PR from finishing work it never did.
    """

    ACTIVE = "active"  # The current binding for its (node, attempt)
    SUPERSEDED = "superseded"  # Replaced by an authorized later binding


class AppendOnlyViolationError(RuntimeError):
    """Raised when something attempts to UPDATE an append-only decision row.

    The repository layer exposes no update path, but "no method" only prevents
    the accidental case. An attacker or a careless caller holding the session can
    mutate a loaded ORM instance and commit it. This exception is what makes the
    append-only guarantee hold at the boundary that actually writes.
    """


class OrchestrationFlow(Base, TenantMixin):
    """One row per delivery flow — the top container.

    The flow ran once and fans out to EPICs. It is a row (unlike wave/EPIC)
    because it is the entity an operator names, addresses and scopes queries by;
    it has identity independent of its children.
    """

    __tablename__ = "orchestration_flows"
    __table_args__ = (
        Index("ix_orchestration_flows_org_id_created_at", "org_id", "created_at"),
        # Issue #4898: a flow's slug is the first segment of every node's graph
        # address (`{slug}/{epic}/{wave}/{node}`), and the cost readback groups
        # `usage_logs` by `(org_id, graph_address)`. Two same-slug flows in one
        # tenant would therefore merge their model spend into a single total with
        # nothing in the result revealing that two flows were summed. Enforced by
        # the database because the resolve-then-create path is a read-then-write
        # race that concurrent registrations can both pass; see
        # `OrchestrationRepository.create_flow`. Scoped to `org_id`, so two
        # tenants keep independent flows of the same name.
        Index("uq_orchestration_flows_org_slug", "org_id", "slug", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # The flow segment of the graph address (`flow/epic/wave/node`). Stable and
    # human-meaningful, so the address survives id churn.
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # The originating intent issue (e.g. "4120"). Nullable: a flow may be created
    # before its intent issue exists, and hand-run flows have none.
    intent_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- The design loop's story (#4885) -----------------------------------
    # Both captured once at registration, and both NULLABLE with NULL meaning
    # "we do not know" — never backfilled. For every flow registered before
    # #4885 that is the honest value, and it is what makes the card able to show
    # nothing rather than assert a design history that never happened.
    #
    # Plain-language use case, taken from the intent issue's mandatory
    # plain-terms opening — human-written prose that already exists, never a
    # model-generated summary. Capped at `DESCRIPTION_MAX_LEN` at write time by
    # `proposal.LoopProposal`; `Text` here rather than `String(500)` because the
    # cap is a product decision that may move, and moving it should not need DDL.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The inception record: which of the five AIDLC gates ran, which were skipped
    # by scope, and which is open. Shape is validated by `proposal.DesignHistory`
    # at write time, not by the database — the canonical stage names live in
    # `rules/personas/aidlc.md`, and a CHECK constraint over them would need a
    # migration every time that list changed.
    design_history: Mapped[dict | None] = mapped_column(JSON_DOC, nullable=True)

    state: Mapped[str] = mapped_column(String(32), nullable=False, default=NodeState.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=utcnow)


class OrchestrationNode(Base, TenantMixin):
    """An executable node: story, eval, or gate. The graph floor.

    Carries its own `NodeState` plus the graph-address components
    (`flow/epic/wave/node`) that the cost rollup and the graph view both key off.
    The address components are stored denormalised rather than joined out of a
    container table precisely because containers are not rows.
    """

    __tablename__ = "orchestration_nodes"
    __table_args__ = (
        # The graph address is unique per flow — it is an address, so a duplicate
        # means two nodes answer to the same name and the rollup double-counts.
        Index("uq_orchestration_nodes_address", "flow_id", "epic_ref", "wave_ref", "node_ref", unique=True),
        Index("ix_orchestration_nodes_org_id_state", "org_id", "state"),
        Index("ix_orchestration_nodes_flow_id_state", "flow_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    flow_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False, index=True)

    # --- Graph address components: `flow/epic/wave/node` -------------------
    # epic_ref / wave_ref are the DERIVED containers' addresses. They are strings
    # here, not FKs, because no container table exists to point at.
    epic_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    wave_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    node_ref: Mapped[str] = mapped_column(String(64), nullable=False)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=NodeState.PENDING.value)
    title: Mapped[str] = mapped_column(String(512), nullable=False)

    # --- Container refs ---------------------------------------------------
    # The GitHub issue this node was materialised as. Nullable: eval and gate
    # nodes frequently have no issue of their own.
    issue_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Attempt counter — a resume/retry (R-O4b) increments it rather than
    # rewriting history.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=utcnow)


class OrchestrationEdge(Base, TenantMixin):
    """A dependency edge between two nodes.

    Edges are what make look-ahead ("what unblocks when this passes?") and
    parallel-branch rendering possible; a node-only graph can express neither.
    """

    __tablename__ = "orchestration_edges"
    __table_args__ = (
        # A duplicate edge is not additional information, and it would make any
        # fan-in/fan-out count wrong.
        Index("uq_orchestration_edges_pair", "flow_id", "from_node_id", "to_node_id", unique=True),
        Index("ix_orchestration_edges_to_node_id", "to_node_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    flow_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False, index=True)
    from_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    to_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class OrchestrationAcceptedPlan(Base, TenantMixin):
    """The immutable accepted-plan record.

    Versioned so that amendment **supersedes** rather than mutates: accepting an
    amended plan writes version N+1 and marks N superseded. The plan that was in
    force at any past gate therefore stays readable, which is the whole point —
    an accepted plan that can be edited proves nothing about what was accepted.

    `plan_document` is the accepted plan verbatim. It is stored rather than
    referenced so that acceptance does not depend on an external document
    (a branch artifact, an issue body) staying unedited.
    """

    __tablename__ = "orchestration_accepted_plans"
    __table_args__ = (
        # One row per (flow, version). Two rows claiming the same version would
        # make "the accepted plan" ambiguous at exactly the wrong moment.
        Index("uq_orchestration_accepted_plans_version", "flow_id", "version", unique=True),
        Index("ix_orchestration_accepted_plans_org_id_flow_id", "org_id", "flow_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    flow_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The accepted plan, verbatim.
    plan_document: Mapped[dict] = mapped_column(JSON_DOC, nullable=False)
    # Content hash of the accepted document, so tampering is detectable without
    # re-reading and re-comparing the whole blob.
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The decision that accepted this version. Nullable only because the plan row
    # and its decision row are written in the same transaction and one must be
    # inserted first.
    accepted_by_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Set when a later version supersedes this one. NULL = currently in force,
    # which is what makes "the accepted plan" a single indexed lookup.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class OrchestrationDecision(Base, TenantMixin):
    """Append-only attribution record. Never updated, never deleted.

    Three separate attribution columns, for the reason spelled out in the module
    docstring: `actor_id` (who), `actor_role` (what authority they held **at
    decision time**), and `actor_kind` (human or service — its own column, not a
    naming convention inside `actor_id`).

    Append-only is enforced three ways: the repository exposes no update method,
    the `before_update` hook below raises when a caller mutates a loaded instance
    directly, and the `do_orm_execute` hook rejects a bulk `update()` statement
    aimed at this table. All three are needed because each covers a different
    caller: no-method stops the accidental case, `before_update` stops the unit-of
    -work path, and only the statement-level hook sees a bulk UPDATE — which never
    loads an instance and therefore never fires `before_update` at all
    (issue #4213).
    """

    __tablename__ = "orchestration_decisions"
    __table_args__ = (
        Index("ix_orchestration_decisions_org_id_created_at", "org_id", "created_at"),
        Index("ix_orchestration_decisions_flow_id_kind", "flow_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    flow_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False, index=True)
    # The node this decision was about. NULL for flow-level decisions such as
    # accepting the initial plan, which precede any node.
    node_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=True, index=True)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Attribution: three columns, deliberately not one string -----------
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Snapshotted at decision time. Roles change; attribution must not.
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    # The human-vs-service discriminator. NOT NULL with no server_default: a
    # default would let an unattributed write land looking attributed, which is
    # the exact failure this column exists to prevent.
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # Free-text justification, carried verbatim from `TransitionResult.reason`.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Populated exactly when this records a rejected transition, so
    # deviation-visibility always has something to render.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # State pair for a transition decision; NULL for non-transition decisions.
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


@event.listens_for(OrchestrationDecision, "before_update", propagate=True)
def _forbid_decision_update(_mapper, _connection, target: OrchestrationDecision) -> None:
    """Block every UPDATE against `orchestration_decisions`.

    Gate attribution that can be rewritten makes the audit trail worthless, so
    this is enforced at the ORM flush boundary rather than left to convention.
    A caller that needs to correct a decision appends a new one.
    """
    raise AppendOnlyViolationError(
        f"orchestration_decisions is append-only; UPDATE attempted on decision id={target.id!r}. "
        "Append a new decision record instead of mutating this one."
    )


@event.listens_for(Session, "do_orm_execute")
def _forbid_decision_bulk_update(orm_execute_state) -> None:
    """Block bulk `update()` statements targeting `orchestration_decisions`.

    Issue #4213. `before_update` above is a *mapper* event: it fires per instance
    during the unit-of-work flush, so it never sees
    ``session.execute(update(OrchestrationDecision)...)`` — that statement is
    emitted straight to the database without loading anything. So the guarantee the
    EPIC rests on ("gate attribution cannot be rewritten") had a hole exactly the
    width of one bulk UPDATE, which is also the cheapest way to rewrite many rows
    at once.

    Registered on `Session` rather than on the mapper because only the ORM-execute
    event carries the statement. Delete is deliberately NOT blocked here: nothing
    in the codebase deletes decisions, and the FK from decisions to flows is
    ``ondelete="CASCADE"`` — blocking cascade-driven deletes would make dropping a
    flow raise instead of tearing down its rows.
    """
    if not orm_execute_state.is_update:
        return

    entity = orm_execute_state.bind_mapper
    if entity is not None and issubclass(entity.class_, OrchestrationDecision):
        raise AppendOnlyViolationError(
            "orchestration_decisions is append-only; a bulk UPDATE was attempted against it. "
            "Append a new decision record instead of rewriting existing ones."
        )


class OrchestrationWorkClaim(Base, TenantMixin):
    """The single durable execution owner of one issue (issue #5127).

    One row per `(org_id, provider_repository_id, issue_number)`, reused for the
    lifetime of that issue rather than inserted per run. Admission is a row-locked
    read plus a compare-and-set against `generation`, which is what makes "exactly
    one mutating run at a time" hold across *both* launch paths — the engine tick
    and direct dispatch — instead of each path racing on its own bookkeeping.

    **Why the provider repository id and not the repo name.** Every existing
    dispatch path keys on the mutable `owner/name` string, so a rename or transfer
    silently re-points every name-keyed row while the underlying repository — and
    any run already working on it — is unchanged. A claim keyed on the name would
    therefore be bypassable by a rename, which is the one thing an ownership
    record must not be. `provider_repository_id` is GitHub's immutable numeric
    repository id, `BigInteger` because it is a 64-bit provider integer and
    `String` would let `"123"` and `123` become two owners of one repository.

    **Why the row survives release.** Deleting on release would lose the release
    reason and reset the generation, and a reset generation makes a stale worker's
    token look current again. `state`/`generation` carry the lifecycle instead: a
    released row keeps its history and the next admission advances the generation,
    so anything issued under an older generation is permanently distinguishable.

    **What this row cannot do.** `lease_expires_at` records when the owner's lease
    lapses; it never authorizes a takeover on its own. Lease expiry means "we have
    lost contact", not "the worker exited" — and a database row cannot revoke a
    GitHub token that has already been issued. Handover therefore requires the
    liveness service to report a positive `exited` verdict plus a recorded
    decision; see `work_claims.py`, which is the only module that mutates this
    table.
    """

    __tablename__ = "orchestration_work_claims"
    __table_args__ = (
        # THE invariant: one owner per issue per tenant. Enforced by the database
        # rather than by the admission code, so two concurrent transactions that
        # both pass their application-level checks still cannot both insert.
        Index(
            "uq_orchestration_work_claims_binding",
            "org_id",
            "provider_repository_id",
            "issue_number",
            unique=True,
        ),
        # Admission reads by binding; operators read by tenant and recency.
        Index("ix_orchestration_work_claims_org_id_state", "org_id", "state"),
        # Duplicate-event lookup: a replayed event must find its original receipt
        # instead of being admitted a second time.
        Index("ix_orchestration_work_claims_claim_event_id", "org_id", "claim_event_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # --- The binding: which issue this row owns. Immutable once inserted. ---
    provider_repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- The owner: the flow/lane, not the individual run. ---
    # Developer, reviewer and repair runs execute sequentially under ONE owner, so
    # the owner is the lane and `active_run_id` is whichever run currently holds
    # it. Keying the claim on the run instead would refuse the reviewer that is
    # supposed to follow the developer.
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_ref: Mapped[str] = mapped_column(String(255), nullable=False)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default=ClaimState.HELD.value)

    # Monotonic, advanced on every fresh admission and never reset. A run holding
    # generation N when the row has moved to N+1 is stale by construction, which is
    # what `bind_run` checks instead of trusting the run's own claim to be current.
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # The run currently executing under this claim. NULL between `claim_work` and
    # `bind_run` (admitted, not yet started) and after release.
    active_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The event that produced the current generation. A replay of the same event
    # returns this generation's receipt rather than being admitted again, so
    # at-least-once delivery on either path cannot become two runs.
    claim_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the lease lapses. Evidence of lost contact only — never of an exit, and
    # never sufficient for takeover on its own.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Why the last release happened, kept on the row so a released claim explains
    # itself without joining the decision log.
    release_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=utcnow)


class OrchestrationPullRequestBinding(Base, TenantMixin):
    """Which pull request implements which story (issue #5301).

    ## What this row is for

    Before it existed, the engine knew a story had been dispatched and knew a
    worker had exited, but had no durable record of **which pull request carried
    the work**. Completion therefore had to be inferred from the GitHub issue's
    closure timeline (`results.GitHubEvidenceSource.merged_story`), which only
    exists when the PR body used a closing keyword. A body saying `Issue #5049`
    creates no closing event, so the story waited on evidence that would never
    arrive while its implementation PR sat merged. This table is the missing
    association, and reconciliation reads it directly.

    GitHub issue closure remains a *projection* of delivery, never its sole
    authority.

    ## Why the provider's immutable ids and not `owner/name` + number

    `provider_repository_id` and `provider_pr_node_id` are GitHub's own immutable
    identifiers. The same reasoning as `OrchestrationWorkClaim`: every name-keyed
    row is silently re-pointed by a repository rename or transfer, and a binding
    that a rename can redirect is not an association. `provider_pr_node_id` is the
    GraphQL node id, which survives even the number being re-used across a
    transfer. `repo` and `pr_number` are carried alongside for display and for
    composing provider queries — they are the *mutable* names, and nothing
    authorizes off them.

    ## What makes a binding trustworthy

    Not the caller's claim. `node_id` / `attempt` are resolved server-side from the
    `NODE_DISPATCHED` decision belonging to the registering run, so a caller can
    only ever bind a PR to the story its own run was dispatched for. Title text,
    branch naming and a plain `Issue #...` mention are discovery hints with no
    authority anywhere in this path.

    `head_sha` is the head the binding was registered against, and it is what makes
    review/check evidence *falsifiable*: a new commit changes the head, and a
    binding whose head has moved cannot inherit the eligibility its previous head
    earned. Reconciliation compares provider truth against this column rather than
    trusting that a recorded approval still describes the code.

    ## What this row cannot do

    It authorizes nothing. It records an association; completion still requires
    provider-verified merge, green required checks and a non-author approving
    review, none of which the registering agent can fabricate. That is why a run
    may register its own binding under nothing more than its run credential
    (`src/agentauth/pr_binding_routes.py`) rather than needing an admin permission
    — which would have to be granted to `MEMBER` and so to every ordinary user in
    every tenant.
    """

    __tablename__ = "orchestration_pr_bindings"
    __table_args__ = (
        # One binding per PR per tenant. THE idempotency invariant: a duplicated
        # registration event, a retried request or a restarted tick must converge on
        # one row rather than inserting a second binding for the same pull request.
        # Enforced by the database because two concurrent registrations can both
        # pass an application-level "is this already bound?" read.
        Index(
            "uq_orchestration_pr_bindings_pr",
            "org_id",
            "provider_repository_id",
            "provider_pr_node_id",
            unique=True,
        ),
        # Reconciliation's read path: the active binding for a node.
        Index("ix_orchestration_pr_bindings_node_id", "org_id", "node_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # --- What story this implements. Server-resolved, never caller-asserted. ---
    flow_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False)
    # The attempt this binding was registered under. A retry increments the node's
    # attempt counter, so this is what distinguishes "the PR for the current work"
    # from "the PR for a superseded attempt" without deleting either.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    # The run that registered it, for provenance and duplicate-event convergence.
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- Immutable provider identity. See the class docstring. ---
    provider_repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_pr_node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Mutable display/query names. Nothing authorizes off these.
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # The head this binding describes. A change invalidates prior eligibility.
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)

    # Every mutation appends its full snapshot to orchestration_decisions. The
    # revision fences a provider read against concurrent repair or replacement.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    accepted_scope: Mapped[str | None] = mapped_column(Text, nullable=True)

    role: Mapped[str] = mapped_column(String(32), nullable=False, default=BindingRole.IMPLEMENTATION.value)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=BindingState.ACTIVE.value)

    # --- Registration provenance: who established this association. ---
    # Two columns for the same reason `OrchestrationDecision` carries three: a
    # recovery performed by a human operator and a self-registration by a worker
    # must be distinguishable after the fact, and `actor_id` alone cannot do it.
    registered_by: Mapped[str] = mapped_column(String(255), nullable=False)
    registered_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Set on an attributed recovery of historical unbound work, NULL for an
    # ordinary self-registration. Non-NULL is what marks a row as human-established
    # rather than worker-observed, so a backfill can never be mistaken for a
    # registration the delivering run made itself.
    recovery_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Why this binding stopped being current, kept on the row so a superseded
    # binding explains itself without joining the decision log.
    superseded_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=utcnow)


class OrchestrationExecution(Base, TenantMixin):
    """The durable identity of one node's delivery work (issue #5142).

    ## What this row answers

    A worker keeps "where am I up to" in process memory only. When its pod is
    evicted or the run ends before the work is finished, nothing outside the
    process records what already happened — so a later process cannot tell whether
    the branch was pushed, the pull request was opened, or an external call that
    was started ever completed. This row is that missing state: one durable record
    per `(org_id, node_id, cycle)` saying which phase the work reached, whether it
    is runnable, why not if not, and when it should next be looked at.

    ## Distinct from the run-authentication record

    `src/agentauth/execution.py` owns a protected per-run record used to
    authenticate a run, keyed on its own run identifiers. This is a *delivery
    ledger* keyed on graph nodes; the two have different purposes and different
    ids and neither reads the other's. They are deliberately not merged — a
    ledger an operator reads and a credential-bearing auth record have different
    exposure, and one table would give them the same.

    ## Why `cycle` is in the uniqueness key

    A repair or re-delivery of the same node is new work with its own attempts,
    deadlines and actions. Keyed on `node_id` alone, the second cycle would
    overwrite the first — destroying the record of what the first cycle already
    did externally, which is exactly the evidence a recovering process needs. A
    new cycle therefore gets its own row and the previous one is marked
    `SUPERSEDED` rather than deleted.

    ## Why the authority columns are stored rather than looked up

    `accepted_plan_version`, `claim_id` and `claim_generation` record the authority
    this execution was admitted under. They are *stored* so that a write can be
    checked against them inside its own transaction: between a caller's read and
    its write the claim generation can advance (#5127) and the accepted plan can be
    superseded (#5128), and only a comparison inside the writing transaction
    observes that. A lookup at write time, by contrast, would be a second read
    racing the same way. Nothing here decides ownership or policy — those stay with
    `work_claims.py` and `policy_admission.py`; this row only refuses to be written
    by a caller whose binding no longer matches.

    ## Why `revision` exists

    Compare-and-set. A caller presents the revision it read and the store applies
    the write only if the row still carries it. Without it, two processes that both
    read an execution mid-flight would both write, and the later write would
    silently erase the earlier one's progress — a lost update on the record whose
    entire job is to survive process loss.

    ## The atomicity this row's columns require

    `phase`/`status` and `next_check_at` must move together. A commit that advanced
    the phase without recording the next check time would leave work that has moved
    on and will never be picked up again: a permanent stall that looks like
    progress in every view. The store writes them in one transaction for that
    reason, and never makes an external call inside it (a hung request holding this
    row's lock would block every other writer on the same execution).

    ## What the reference columns may hold

    References only — an S3 key, a PR node id, a comment id, a notification
    receipt id. Never a credential, never a token, never a complete transcript.
    These rows are read by operators and surfaced in diagnostics, so a secret
    written here would be a disclosure with no revocation path.
    `pending_action_key`, `notification_receipt_ref` and `handoff_receipt_ref` are
    the initial storage the runner (#5143) and the handoff work (#5144) need; they
    are deliberately plain references and no phase-handler result is invented here.
    """

    __tablename__ = "orchestration_executions"
    __table_args__ = (
        # THE identity invariant: one execution per node per cycle per tenant. Two
        # concurrent starts can both pass an application-level "is there one
        # already?" read, so the database is what refuses the second. Without this
        # index the whole ledger would be advisory.
        Index(
            "uq_orchestration_executions_cycle",
            "org_id",
            "node_id",
            "cycle",
            unique=True,
        ),
        # The due-work read path: "what is runnable now?" A runner (#5143) asks
        # this on every pass, so it must not be a full scan of the tenant's
        # history. Status precedes time because status is the more selective
        # predicate once concluded rows accumulate.
        Index(
            "ix_orchestration_executions_due",
            "org_id",
            "status",
            "next_check_at",
        ),
        # Operator/read-model path: every execution for one flow.
        Index("ix_orchestration_executions_flow_id", "org_id", "flow_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # --- What work this is. Immutable once inserted. ---
    # Tenant-safe foreign keys: both parents carry org_id, and every query filters
    # on org_id as well, so a cross-tenant id cannot resolve to a readable row.
    flow_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False)
    # Which delivery cycle of that node. Starts at 1; a repair cycle is a new row.
    cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- Where the work stands. See ExecutionPhase/ExecutionStatus in
    # execution_state.py, which is where this vocabulary is declared. String
    # columns rather than native enums so a new member needs no DDL, matching the
    # existing orchestration tables.
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- The compare-and-set fence. Advanced by exactly one on every applied
    # write, never reset. A caller presenting a revision the row has passed is
    # stale by construction.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- Authority this execution was admitted under. Re-verified inside each
    # writing transaction; see the class docstring.
    # 0 is legal and meaningful: policy_admission reports plan_version=0 when no
    # accepted plan exists, which is the legacy path that must stay usable.
    accepted_plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_id: Mapped[str] = mapped_column(String(36), nullable=False)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- Attempts and scheduling ---
    # Counted by the store rather than by callers, who would each count
    # differently and so disagree about when the existing attempt bound is hit.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When a runner should next consider this execution. NULL only for a terminal
    # status — a non-terminal row with no next check is invisible work.
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Progress and block metadata ---
    # Last time real progress happened, so "stuck for a minute" and "stuck since
    # Tuesday" are distinguishable without reconstructing a timeline from logs.
    progressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A block states its code, who resolves it and what they must supply. All three
    # are needed for an operator to route it from this row alone; a bare "blocked"
    # flag sends someone to logs that expire.
    block_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    block_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    block_required_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human decision points still outstanding, recorded for operator context. The
    # gates themselves stay with the existing controls.py/graph state; nothing on
    # this row approves or bypasses one.
    block_remaining_gates: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Sanitized references. See the class docstring: references only. ---
    # The action whose external outcome is still unknown, if any. This is what
    # makes AWAITING_EXTERNAL actionable: a recovering process knows which step to
    # go and ask the provider about instead of blindly retrying it.
    pending_action_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notification_receipt_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    handoff_receipt_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=utcnow)


class OrchestrationAction(Base, TenantMixin):
    """One externally-visible step an execution takes (issue #5142).

    ## Why actions are rows and not log lines

    An action is something the outside world can see: a branch pushed, a pull
    request opened, a comment posted. If the process dies after making the call
    but before recording it, a retry repeats the side effect — two pull requests
    for one story, two comments on one issue. So the intent is written *before* the
    call and the outcome *after*, and the row between the two is what tells a later
    process "this may already have happened; go and look".

    ## `operation_key` is the idempotency contract

    Uniqueness is `(org_id, execution_id, operation_key)`, and the key is supplied
    by the caller. It must be derived from the work — `"open_pr:node-7:cycle-1"` —
    and never generated per attempt: a fresh UUID each time satisfies the column
    and destroys the protection, because every retry would insert a new row and
    duplicate its effect. Preparing the same key twice returns the original record.

    The uniqueness is a database index rather than an application check for the
    usual reason: two concurrent preparations can both read "no action yet" before
    either writes, and only the index refuses the second.

    ## Why an unobserved outcome stays `unknown`

    `status` may hold `unknown` (see `ActionStatus` in `execution_state.py`) and
    that is a real answer, not a missing one. Recording an unobserved action as
    succeeded advances delivery on evidence nobody saw; recording it as failed
    invites a retry that duplicates an effect which may well have landed. Both are
    worse than carrying the uncertainty, so the uncertainty is storable.

    ## What the reference columns may hold

    `artifact_ref` and `receipt_ref` hold provider or storage identifiers — a PR
    node id, a comment id, an S3 key. `receipt_ref` is what makes an observation
    falsifiable later: an operator can go and look at the thing it names. Neither
    column ever holds a credential or a complete transcript, for the same
    disclosure reason as the execution row.
    """

    __tablename__ = "orchestration_actions"
    __table_args__ = (
        # THE idempotency invariant: one action per operation key per execution per
        # tenant. This is what makes a crash-and-retry safe, so it is enforced by
        # the database and not by a read-then-write in the store.
        Index(
            "uq_orchestration_actions_operation",
            "org_id",
            "execution_id",
            "operation_key",
            unique=True,
        ),
        # Recovery's read path: the unresolved actions of one execution.
        Index(
            "ix_orchestration_actions_execution_status",
            "org_id",
            "execution_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    execution_id: Mapped[str] = mapped_column(String(36), ForeignKey("orchestration_executions.id", ondelete="CASCADE"), nullable=False)

    # The caller-supplied idempotency key. See the class docstring.
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # What kind of step this is, e.g. "open_pr". A string rather than an enum
    # because the set of steps belongs to the phase handlers a sibling issue owns;
    # pinning it here would make adding a handler a schema change.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    # Which attempt of the execution prepared this action, so the actions of a
    # superseded attempt stay distinguishable from the current one's without
    # deleting either.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Sanitized references. See the class docstring: references only. ---
    artifact_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receipt_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Small non-sensitive operator detail (which repo, which PR number), stored as
    # JSONB on Postgres and JSON on SQLite via the same variant the rest of this
    # module uses. Not a payload dump.
    detail: Mapped[dict | None] = mapped_column(JSON_DOC, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # When the outcome was observed. NULL while prepared/dispatched, and NULL for
    # an action left `unknown` — the absence of an observation time is itself the
    # record that nobody managed to look.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=utcnow)

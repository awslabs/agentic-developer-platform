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

JSON column note: `plan_document` is declared with a dialect variant so it is real
`JSONB` on Postgres (GIN-indexable later) and plain `JSON` on SQLite, where the
tests run. A bare `JSON` would render as `JSON` on Postgres as well and quietly
forfeit JSONB; the migration declares the identical variant so the two agree.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, event
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
    "OrchestrationDecision",
    "OrchestrationEdge",
    "OrchestrationFlow",
    "OrchestrationNode",
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
    __table_args__ = (Index("ix_orchestration_flows_org_id_created_at", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # The flow segment of the graph address (`flow/epic/wave/node`). Stable and
    # human-meaningful, so the address survives id churn.
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # The originating intent issue (e.g. "4120"). Nullable: a flow may be created
    # before its intent issue exists, and hand-run flows have none.
    intent_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
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

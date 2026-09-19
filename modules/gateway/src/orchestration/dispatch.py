"""Dispatch a ready node: the point the engine acts.

Issue #4204 (EPIC #4191, intent #4120).

The tick moves a node to `ready` and stops there. This module is the next step
and the first one with an outside effect: it takes a `ready` node, moves it to
`running`, and records the run the work will be attributed to. Everything before
this was bookkeeping; this is where the engine spends money.

**Authority is decided here, against a server-resolved identity (R-O5d).** Not
from the envelope `persona`, not from `AGENT_TYPE`, not from a caller ARN. That
is not a stylistic preference about where to put a check — IAM *cannot* express
per-persona authority in this platform, because every persona's pod presents an
identical ARN (one shared role, one service account, one registry entry). An
ARN-derived authority check would be a check that always passes, which is worse
than no check because it looks like one. The only authority input is the
:class:`EngineGenesis` resolved from a real gate-approval row by
`genesis.resolve_engine_genesis`, and this module cannot construct one.

**Dispatch is idempotent (R-NF2).** The same `ready` node dispatched twice
produces **one** run. The mechanism is the same conditional UPDATE the tick uses
— `UPDATE ... WHERE id = :id AND state = 'ready'` — so the second attempt matches
zero rows and returns a "lost race" outcome rather than a second run. This is not
belt-and-braces around a lock; it *is* the mechanism, and it holds under
overlapping ticks and retries because those are normal operation here, not
exceptional. Duplicate dispatch means duplicate agent runs, duplicate Bedrock
cost and duplicate PRs, so "at most once" has to be structural.

**Illegal dispatch is rejected AND recorded (AC-14 / AC-15).** Both criteria fall
out of the vocabulary rather than needing their own branches here: a node whose
predecessor eval is red is not in `ready` (the tick never released it), and
advancing a node out of `awaiting_gate` is a human-only edge, so a SERVICE actor
attempting it is refused by `transition()`. What this module adds is that the
refusal is **persisted** as a `TRANSITION_REJECTED` decision row. Under RULING 5
recorded rejections are a primary detector for off-plan activity, so a rejection
that only logged would lose the evidence — which is why `transition()` returns
rejections instead of raising.

**Every state change goes through `transition()`.** There is exactly one write
seam (`_dispatch_transition`), mirroring `tick.py`'s `apply_guarded_transition`,
and `test_dispatch.py` asserts at the AST level that no function here writes node
state without consulting `transition()` first. A future refactor that adds a
second write path fails the build rather than silently bypassing the guard.

Tenant isolation: the node is re-resolved under `org_id` inside this module
rather than trusted from the caller's object, and the run row is stamped with
that same org. A node id from another tenant resolves to nothing.

Scope note: this records the *dispatch* — the node moves to `running` and a run
row is written with the graph address the work will report cost against. Handing
the work to the agent-worker fleet is a transport concern that crosses into
webhook-ingress; see `DispatchOutcome.run` for what a caller passes on, and the
module docstring in `genesis.py` for why the human root travels as a
`decision_id` reference rather than a resolved identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .genesis import EngineGenesis
from .models import DecisionKind, OrchestrationDecision, OrchestrationNode
from .state import ActorKind, NodeState, transition

logger = logging.getLogger("bedrockgateway.orchestration.dispatch")

__all__ = [
    "DispatchOutcome",
    "DispatchStatus",
    "DispatchedRun",
    "dispatch_node",
]


# The actor the engine dispatches as. SERVICE, never HUMAN — the dispatch is
# performed by the engine even though its *authority* traces to a human approval.
# Recording it as HUMAN would make the decisions table claim a person pressed a
# button they did not press, and would unlock the human-only recovery edges in
# `state.py` (clearing a halt, walking a node out of a gate) to the engine.
#
# This is the distinction the genesis ruling turns on: the engine acts with a
# human's *authorisation*, not with a human's *identity*.
_DISPATCH_ACTOR = ActorKind.SERVICE

_DISPATCH_ACTOR_ID = "system:orchestration-dispatch"
_DISPATCH_ACTOR_ROLE = "engine"


class DispatchStatus(StrEnum):
    """The outcome of one dispatch attempt.

    Four members rather than a bool, because the three non-success cases need
    different handling by the caller and collapsing them loses the distinction
    that matters. `REJECTED` is a recorded deviation to surface; `ALREADY_RUNNING`
    is normal overlap to ignore; `NOT_FOUND` is a tenant-scoped miss.
    """

    DISPATCHED = "dispatched"  # The node moved to running; a run was recorded
    REJECTED = "rejected"  # transition() refused the edge; recorded as a decision
    ALREADY_RUNNING = "already_running"  # Lost race — another dispatch got there first
    NOT_FOUND = "not_found"  # No such node in this org


@dataclass(frozen=True)
class DispatchedRun:
    """The run a successful dispatch created.

    `graph_address` is the join key between this dispatch and everything that
    later reports against it: `usage_logs.graph_address` is what the cost rollup
    groups by, and it is what deviation detection reconciles observed runs
    against. Composed here, once, so the dispatch and the ledger cannot disagree
    about a node's address.
    """

    node_id: str
    org_id: str
    flow_id: str
    graph_address: str
    # The decision the dispatch's human authority derives from. Carried so the
    # audit trail answers "who authorised this run?" with a row id, not prose.
    root_decision_id: str
    root_human_id: str


@dataclass(frozen=True)
class DispatchOutcome:
    """What one `dispatch_node` call did.

    `run` is populated exactly when `status is DISPATCHED`, enforced in
    `__post_init__`: a caller that reads `.run` without checking `.status` would
    otherwise treat a rejected dispatch as a successful one, and the whole point
    of a rejection is that no run exists.
    """

    status: DispatchStatus
    node_id: str
    run: DispatchedRun | None = None
    # Non-empty exactly when the dispatch did not happen, so a caller always has
    # something to render for a deviation or a refusal.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is DispatchStatus.DISPATCHED:
            if self.run is None:
                raise ValueError("a DISPATCHED outcome must carry the run it created")
        elif self.run is not None:
            raise ValueError(f"a {self.status.value} outcome must not carry a run — no run was created")

    @property
    def dispatched(self) -> bool:
        return self.status is DispatchStatus.DISPATCHED


def graph_address(node: OrchestrationNode, *, flow_slug: str) -> str:
    """The node's graph address, `flow/epic/wave/node`.

    One definition, used by both the dispatch write and the deviation read. The
    cost story composes the identical string (`cost.py::get_flow_cost`); a second
    spelling here would make dispatched runs invisible to a rollup that computes
    the address the other way.
    """
    return f"{flow_slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"


@dataclass(frozen=True)
class GraphAttribution:
    """A model call's verified graph identity, for `usage_logs.graph_address`.

    Issue #4898. This is the write side of the cost story: `cost.py` groups
    `usage_logs` by `graph_address`, and until something persisted the column
    every flow reported `unknown` / `no_usage_rows` — honestly, but uselessly.

    **Only `agentauth.engine.validate_engine_authority` may construct one**, and
    only on the branch where it has just re-read live SQL and proved: the flow is
    runnable, its plan approval is not superseded, the assigned node is RUNNING on
    exactly `node_attempt`, and (for a child dispatch) the parent's dispatch
    receipt is committed. The value is therefore a *report of a completed
    authorization*, never an input to one — nothing here is consulted to decide
    whether a request is allowed.

    Frozen, and `address` is composed by `graph_address` above rather than
    re-spelled, so the ledger write and the cost read cannot disagree.

    What deliberately gets NO attribution (each stays NULL, which reads as
    "unavailable" rather than as zero spend):

      - A flow-level or wave coordinator. It owns no single graph node, and
        charging it to one of its children would invent a number. Those branches
        of `validate_engine_authority` return None.
      - Ordinary human, CLI and chat traffic, which has no protected assignment
        at all — so NULL by construction, not by a policy some caller could
        ignore. This is also what keeps migration 031's partial index
        (`WHERE graph_address IS NOT NULL`) small.

    `run_id` is carried so the usage writer can refuse to stamp an address onto a
    row whose run identity disagrees with the invocation this assignment was
    proved for, rather than persisting a graph/run mismatch.
    """

    org_id: str
    flow_id: str
    node_id: str
    node_attempt: int
    address: str
    run_id: str


async def _dispatch_transition(
    session: AsyncSession,
    *,
    node_id: str,
    org_id: str,
    flow_id: str,
    observed_state: str,
    reason: str,
) -> tuple[int, bool, str | None]:
    """The one place this module changes a node's state.

    Two guards, in order, both required — the same shape as
    `tick.py::apply_guarded_transition`, and deliberately so: one audited seam per
    module beats a clever shared abstraction that hides which module wrote what.

    1. `transition()` decides whether `ready -> running` is legal for a SERVICE
       actor. A refusal is **recorded** as a decision row (R-N2b) before
       returning, because a dropped rejection is lost evidence of off-plan
       activity.
    2. The UPDATE is conditional on `state = :observed_state`, so a concurrent
       dispatch that already moved this node leaves us matching 0 rows (R-NF2).

    Returns `(rows_affected, allowed, rejection_reason)`. `(0, False, why)` is an
    authority rejection and `(0, True, None)` is a lost race; the caller must tell
    them apart because one is a recorded deviation and the other is normal.
    """
    result = transition(observed_state, NodeState.RUNNING, actor_kind=_DISPATCH_ACTOR, reason=reason)

    if not result.allowed:
        # Recorded, not swallowed. The decisions table is append-only, so this is
        # a durable record of an attempt the engine was not permitted to make —
        # which is exactly what AC-14 and AC-15 ask to be able to read back.
        session.add(
            OrchestrationDecision(
                org_id=org_id,
                flow_id=flow_id,
                node_id=node_id,
                kind=DecisionKind.TRANSITION_REJECTED.value,
                actor_id=_DISPATCH_ACTOR_ID,
                actor_role=_DISPATCH_ACTOR_ROLE,
                actor_kind=_DISPATCH_ACTOR.value,
                reason=reason,
                rejection_reason=result.rejection_reason,
                from_state=str(result.from_state),
                to_state=str(result.to_state),
            )
        )
        await session.flush()
        logger.warning(
            "orchestration dispatch: rejected for node %s (%s -> %s): %s",
            node_id,
            result.from_state,
            result.to_state,
            result.rejection_reason,
        )
        return 0, False, result.rejection_reason

    # Conditional on the observed prior state — this is the idempotency guard.
    stmt = (
        update(OrchestrationNode)
        .where(
            OrchestrationNode.id == node_id,
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.state == observed_state,
        )
        .values(state=result.new_state.value, attempts=OrchestrationNode.attempts + 1, updated_at=utcnow())
    )
    rows = (await session.execute(stmt)).rowcount or 0
    await session.flush()
    return rows, True, None


async def dispatch_node(
    session: AsyncSession,
    node: OrchestrationNode | str,
    genesis: EngineGenesis,
) -> DispatchOutcome:
    """Dispatch one node, if the engine is permitted to.

    Args:
        session: Caller-owned session. Nothing is committed here, so the state
            change and its decision row land atomically or not at all.
        node: The node to dispatch, or its id. Either way it is **re-resolved**
            under `genesis.org_id` before anything is written — a caller-supplied
            ORM instance describes what the caller read, which is not necessarily
            what is in the database now, and its `org_id` is the caller's claim
            rather than a verified fact.
        genesis: The dispatch's human root, resolved from a gate-approval decision
            row by `genesis.resolve_engine_genesis`. This is the **only** source
            of authority. There is no parameter for a persona, an agent type or a
            caller ARN, because R-O5d forbids reading authority from any of them
            and a parameter that exists will eventually be trusted.

    Returns:
        A :class:`DispatchOutcome`. Only `DISPATCHED` created a run.
    """
    node_id = node if isinstance(node, str) else node.id
    org_id = genesis.org_id

    # Re-resolve under the genesis org. Two things are being established: that the
    # node exists in this tenant, and what state it is ACTUALLY in right now. The
    # org filter is in SQL, so a node id from another tenant is NOT_FOUND rather
    # than a cross-tenant dispatch.
    resolved = (
        await session.execute(
            select(OrchestrationNode).where(
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.id == node_id,
            )
        )
    ).scalar_one_or_none()

    if resolved is None:
        logger.warning(
            "orchestration dispatch: node %s not found in org %s — refusing",
            node_id,
            org_id,
        )
        return DispatchOutcome(
            status=DispatchStatus.NOT_FOUND,
            node_id=node_id,
            reason=f"node {node_id!r} does not exist in org {org_id!r}",
        )

    # The flow is resolved (not taken from the genesis row) because the node's own
    # flow is what its address is built from, and the slug is what the ledger will
    # be grouped by. Reading it here keeps the address definition in one place.
    from .models import OrchestrationFlow  # local: avoids widening the module's import surface

    flow_slug = (
        await session.execute(
            select(OrchestrationFlow.slug).where(
                OrchestrationFlow.org_id == org_id,
                OrchestrationFlow.id == resolved.flow_id,
            )
        )
    ).scalar_one_or_none()

    if flow_slug is None:
        # A node whose flow is missing has no address, so nothing could attribute
        # its cost or reconcile its run. Refusing beats dispatching unaddressable
        # work.
        logger.error(
            "orchestration dispatch: node %s references missing flow %s in org %s — refusing",
            node_id,
            resolved.flow_id,
            org_id,
        )
        return DispatchOutcome(
            status=DispatchStatus.NOT_FOUND,
            node_id=node_id,
            reason=f"node {node_id!r} references a flow that does not exist in this org",
        )

    reason = f"engine dispatch authorised by decision {genesis.decision_id} (approver {genesis.root_human_id})"

    rows, allowed, rejection_reason = await _dispatch_transition(
        session,
        node_id=resolved.id,
        org_id=org_id,
        flow_id=resolved.flow_id,
        observed_state=resolved.state,
        reason=reason,
    )

    if not allowed:
        # Already recorded as a decision row inside the seam. AC-14 (red
        # predecessor: the node is not in `ready`) and AC-15 (gate skip:
        # `awaiting_gate -> running` is not a legal edge at all) both land here.
        return DispatchOutcome(
            status=DispatchStatus.REJECTED,
            node_id=resolved.id,
            reason=rejection_reason,
        )

    if rows != 1:
        # transition() allowed it but no row matched: another dispatch moved this
        # node between our read and our write. The correct outcome is exactly one
        # run in total, so this attempt creates none (R-NF2).
        logger.info(
            "orchestration dispatch: node %s already dispatched by a concurrent attempt; no second run",
            resolved.id,
        )
        return DispatchOutcome(
            status=DispatchStatus.ALREADY_RUNNING,
            node_id=resolved.id,
            reason="node was already dispatched by a concurrent attempt",
        )

    address = graph_address(resolved, flow_slug=flow_slug)

    logger.info(
        "orchestration dispatch: dispatched node %s address=%s root_decision=%s org=%s",
        resolved.id,
        address,
        genesis.decision_id,
        org_id,
    )

    return DispatchOutcome(
        status=DispatchStatus.DISPATCHED,
        node_id=resolved.id,
        run=DispatchedRun(
            node_id=resolved.id,
            org_id=org_id,
            flow_id=resolved.flow_id,
            graph_address=address,
            root_decision_id=genesis.decision_id,
            root_human_id=genesis.root_human_id,
        ),
    )

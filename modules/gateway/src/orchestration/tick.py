"""The engine tick: read durable state, effect the transitions that became legal.

Issue #4203 (EPIC #4191, intent #4120). This is the heartbeat that makes the
graph move. One invocation reads the nodes that are `pending`, works out which of
them have had all their predecessors satisfied, moves those to `ready`, and
exits. Nothing is held in memory between invocations — all continuity lives in
Postgres, which is what makes the tick safe to kill, retry and overlap.

`run_tick` takes a session rather than opening one, so the whole of the engine's
decision logic is testable against SQLite with no AWS involved. The Lambda
entrypoint that supplies the session lives in `tick_handler.py`.

Scope: the tick performs **no dispatch**. Execution nodes stop at `ready`.
Gates advance through the existing legal edges to `awaiting_gate` atomically,
without starting a worker. Only the human approval boundary answers them.

Three invariants are load-bearing rather than stylistic:

**Every state change goes through `transition()` (R-N2a/R-N2b).** `transition()`
is the authority guard — it decides whether an edge is legal for a SERVICE actor
at all. The conditional UPDATE below is only the persistence mechanism, and it is
unreachable unless `transition()` has already allowed the move. All persistence
is funnelled through the single `apply_guarded_transition` seam so there is one
place to audit; `tests/orchestration/test_tick.py` asserts at the AST level that
no function here writes state without consulting `transition()` first, so a
future refactor that adds a second write path fails the build rather than
silently bypassing the guard.

**Concurrency safety is structural (R-O4e).** Each write is
`UPDATE ... WHERE id = :id AND state = :observed_state`. Two overlapping ticks
both read `pending`, both attempt the move, and exactly one matches a row; the
loser updates 0 rows and no-ops. This needs no advisory lock and no leader
election, which matters because overlapping schedules and retries are normal
operation, not an exceptional case. A lost race is counted (`lost_races`) rather
than being indistinguishable from success.

**Failure surfaces (R-NF3).** A node that raises is logged at error level,
counted, and forces `success=False` on the report. The tick never returns success
having quietly done nothing — that silent stall is the exact failure this EPIC
exists to end, and the fail-soft guard writes at `spawn_persona.py:377-378` are
the anti-pattern being avoided.

Tenant isolation: the tick is a service actor and legitimately spans orgs, but
every read is filtered by the `org_id` of the node being considered, and counts
are broken out per org (`per_org`) so no org's numbers are aggregated into
another's view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationEdge, OrchestrationNode
from .state import ActorKind, NodeState, transition

logger = logging.getLogger("bedrockgateway.orchestration.tick")

# Rows fetched per keyset page. Small enough that one page is a cheap query,
# large enough that a normal-sized flow finishes in one or two pages.
_PAGE_SIZE = 500

# Hard ceiling on candidate nodes examined per invocation, following the
# `_ITEM_BACKSTOP` precedent at `activity/stats_service.py:48`. Without it one
# very large flow's tick runs long and starves every other flow's forward motion
# (R-NF7). Hitting it is not silent: `truncated` is set on the report and logged
# at warning level, so "we ran out of budget" never reads as "there was nothing
# left to do".
_ITEM_BACKSTOP = 10_000

# The actor the tick transitions as. It is SERVICE, never HUMAN, which is what
# makes the human-only recovery edges in `state.py` unreachable from here: the
# engine cannot clear its own halt (R-Q9c) or walk a node out of a gate (AC-15).
_TICK_ACTOR = ActorKind.SERVICE

# The synthetic actor id recorded on decisions the tick makes. `actor_kind` is a
# real column, so this string is descriptive only and is never what
# "was this a human?" is answered from.
_TICK_ACTOR_ID = "system:orchestration-tick"
_TICK_ACTOR_ROLE = "engine"


# A predecessor counts as satisfied only when it reached PASSED.
#
# Spelled out rather than derived from TERMINAL_STATES, because "terminal" and
# "satisfied" are different questions and conflating them would silently release
# work that should stay blocked. Every exclusion is deliberate:
#   - REJECTED_AT_GATE: `state.py` is explicit that successors stay pending.
#   - FAILED / HALTED:  reached the end of the line without succeeding.
#   - SUPERSEDED:       replaced by another attempt; that attempt's PASSED is
#                       what should release the successor, not this row.
# Non-terminal states (PENDING/READY/RUNNING/AWAITING_GATE) are simply not done.
SATISFIED_STATES: frozenset[NodeState] = frozenset({NodeState.PASSED})


@dataclass
class TickReport:
    """What one invocation did. Every field exists to be emitted as a metric.

    `success` is False if anything went wrong, even when transitions were also
    effected — a partially failed tick is not a successful one (R-NF3).
    """

    nodes_examined: int = 0
    transitions_effected: int = 0
    transitions_rejected: int = 0
    errors: int = 0
    # Guarded writes that matched 0 rows because a concurrent tick got there
    # first. Expected under overlapping schedules; counted so it stays visible.
    lost_races: int = 0
    pages_read: int = 0
    # True when the backstop cut the scan short — there was more work to do.
    truncated: bool = False
    # Per-org counts so metrics can be dimensioned without cross-org aggregation.
    per_org: dict[str, dict[str, int]] = field(default_factory=dict)
    # node_id -> the predecessor node_ids that are holding it back. Answers
    # R-O2b's "what would make this node ready?" without a second query.
    blocked: dict[str, list[str]] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """False if any node failed. Callers must not treat errors as success."""
        return self.errors == 0

    def _org(self, org_id: str) -> dict[str, int]:
        return self.per_org.setdefault(
            org_id,
            {"nodes_examined": 0, "transitions_effected": 0, "transitions_rejected": 0, "errors": 0, "lost_races": 0},
        )

    def record(self, org_id: str, key: str, amount: int = 1) -> None:
        """Increment a counter both in total and for one org."""
        setattr(self, key, getattr(self, key) + amount)
        self._org(org_id)[key] += amount


@dataclass(frozen=True)
class _Candidate:
    """A `pending` node as observed, with the state we observed it in.

    `observed_state` is carried explicitly rather than re-read at write time:
    guarding the UPDATE on a freshly re-read value would reintroduce exactly the
    read-then-write race the guard exists to close.
    """

    node_id: str
    org_id: str
    flow_id: str
    observed_state: str
    kind: str = NodeKind.STORY.value


async def _fetch_candidate_page(session: AsyncSession, *, after_id: str | None, limit: int) -> list[_Candidate]:
    """One keyset page of `pending` nodes, ordered by id.

    Keyset (`id > :after_id`) rather than OFFSET: pagination stays correct even
    though rows are being updated underneath it, and it stays index-served as the
    table grows.

    Selecting `pending` nodes directly is the tighter form of "flows with at
    least one non-terminal node" — a flow with no pending node has nothing for
    this tick to do, and `ix_orchestration_nodes_org_id_state` serves this
    predicate rather than requiring a scan of every flow.
    """
    stmt = select(
        OrchestrationNode.id,
        OrchestrationNode.org_id,
        OrchestrationNode.flow_id,
        OrchestrationNode.state,
        OrchestrationNode.kind,
    ).where(
        or_(
            OrchestrationNode.state == NodeState.PENDING.value,
            and_(OrchestrationNode.kind == NodeKind.GATE.value, OrchestrationNode.state == NodeState.READY.value),
        )
    )

    if after_id is not None:
        stmt = stmt.where(OrchestrationNode.id > after_id)

    # Keep dependency reads coherent with an amendment's topology replacement.
    stmt = stmt.order_by(OrchestrationNode.id).limit(limit).with_for_update(skip_locked=True)

    rows = (await session.execute(stmt)).all()
    return [_Candidate(node_id=row[0], org_id=row[1], flow_id=row[2], observed_state=row[3], kind=row[4]) for row in rows]


async def _predecessor_states(session: AsyncSession, *, org_id: str, node_id: str) -> list[tuple[str, str]]:
    """`(predecessor_node_id, state)` for every edge pointing at `node_id`.

    Both the edge and the node it resolves to are filtered on `org_id`: an edge
    is only trusted to describe a dependency inside its own tenant, so a
    cross-tenant edge row cannot influence whether this node becomes ready.
    """
    stmt = (
        select(OrchestrationEdge.from_node_id, OrchestrationNode.state)
        .join(OrchestrationNode, OrchestrationNode.id == OrchestrationEdge.from_node_id)
        .where(
            OrchestrationEdge.org_id == org_id,
            OrchestrationEdge.to_node_id == node_id,
            OrchestrationNode.org_id == org_id,
        )
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


def _unsatisfied(predecessors: list[tuple[str, str]]) -> list[str]:
    """Which predecessors are not yet satisfied.

    An unrecognised state string counts as unsatisfied rather than raising: a
    node carrying a value outside the vocabulary must not be treated as done, and
    blocking is the safe reading. `transition()` is where undeclared literals are
    rejected on write.
    """
    blocking: list[str] = []
    for predecessor_id, raw_state in predecessors:
        try:
            state = NodeState(raw_state)
        except ValueError:
            logger.error(
                "orchestration tick: predecessor %s carries undeclared state %r; treating as unsatisfied",
                predecessor_id,
                raw_state,
            )
            blocking.append(predecessor_id)
            continue
        if state not in SATISFIED_STATES:
            blocking.append(predecessor_id)
    return blocking


async def apply_guarded_transition(
    session: AsyncSession,
    *,
    node_id: str,
    org_id: str,
    flow_id: str,
    observed_state: str,
    to_state: NodeState,
    reason: str,
) -> tuple[int, bool]:
    """The one place this module changes a node's state.

    Two distinct guards, in order, and both are required:

    1. `transition()` decides whether the edge is legal for a SERVICE actor. A
       rejection is **recorded** as a decision row (R-N2b) rather than dropped,
       because recorded rejections are the primary detector for off-plan
       activity.
    2. The UPDATE is conditional on `state = :observed_state`, so a concurrent
       tick that already moved this node leaves us matching 0 rows (R-O4e).

    Returns `(rows_affected, allowed)`. `(0, False)` is an authority rejection and
    `(0, True)` is a lost race — the caller must be able to tell them apart,
    because one is a recorded deviation and the other is normal overlap.
    """
    result = transition(observed_state, to_state, actor_kind=_TICK_ACTOR, reason=reason)

    if not result.allowed:
        # Recorded, not swallowed. The decisions table is append-only, so this is
        # a durable record of an attempt the engine was not permitted to make.
        session.add(
            OrchestrationDecision(
                org_id=org_id,
                flow_id=flow_id,
                node_id=node_id,
                kind=DecisionKind.TRANSITION_REJECTED.value,
                actor_id=_TICK_ACTOR_ID,
                actor_role=_TICK_ACTOR_ROLE,
                actor_kind=_TICK_ACTOR.value,
                reason=reason,
                rejection_reason=result.rejection_reason,
                from_state=str(result.from_state),
                to_state=str(result.to_state),
            )
        )
        await session.flush()
        logger.warning(
            "orchestration tick: transition rejected for node %s (%s -> %s): %s",
            node_id,
            result.from_state,
            result.to_state,
            result.rejection_reason,
        )
        return 0, False

    # Conditional on the observed prior state — this is the concurrency guard.
    stmt = (
        update(OrchestrationNode)
        .where(
            OrchestrationNode.id == node_id,
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.state == observed_state,
        )
        .values(state=result.new_state.value, updated_at=utcnow())
    )
    rows = (await session.execute(stmt)).rowcount or 0
    if rows and to_state == NodeState.AWAITING_GATE:
        session.add(
            OrchestrationDecision(
                org_id=org_id,
                flow_id=flow_id,
                node_id=node_id,
                kind=DecisionKind.GATE_PRESENTED.value,
                actor_id=_TICK_ACTOR_ID,
                actor_role=_TICK_ACTOR_ROLE,
                actor_kind=_TICK_ACTOR.value,
                reason=reason,
                from_state=observed_state,
                to_state=to_state.value,
            )
        )
    await session.flush()
    return rows, True


async def _advance_node(session: AsyncSession, candidate: _Candidate, report: TickReport) -> None:
    """Decide and effect one candidate's move. Records why it did not move."""
    predecessors = await _predecessor_states(session, org_id=candidate.org_id, node_id=candidate.node_id)
    blocking = _unsatisfied(predecessors)

    if blocking:
        # Not an error and not a rejection — simply not ready yet. Retained so
        # "what would make this ready?" is answerable (R-O2b).
        report.blocked[candidate.node_id] = blocking
        return

    if candidate.kind == NodeKind.EVAL.value:
        from datetime import UTC, datetime

        from .evaluation_plan import accepted_evaluation, managed_evaluation, predecessor_deployments
        from .review_cycle import CycleBlockedError

        evaluation = await session.get(OrchestrationNode, candidate.node_id)
        if evaluation is not None and evaluation.org_id == candidate.org_id:
            try:
                if await managed_evaluation(session, evaluation):
                    accepted = await accepted_evaluation(session, evaluation)
                    if accepted is None:
                        report.blocked[candidate.node_id] = ["evaluation_specification_missing"]
                        return
                    deployed = await predecessor_deployments(session, evaluation, accepted[0].version, now=datetime.now(UTC))
                    if not deployed:
                        report.blocked[candidate.node_id] = ["verified_deployment_required"]
                        return
            except (CycleBlockedError, ValueError):
                report.blocked[candidate.node_id] = ["deployment_authority_unverifiable"]
                return

    targets = [NodeState.READY] if candidate.observed_state == NodeState.PENDING.value else []
    if candidate.kind == NodeKind.GATE.value:
        # Present a decision using existing legal edges, without a worker or an
        # attempt charge. The savepoint makes the intermediate RUNNING invisible.
        # Neither this path nor any service actor can answer the gate.
        targets.extend([NodeState.RUNNING, NodeState.AWAITING_GATE])
    observed = candidate.observed_state
    async with session.begin_nested():
        for target in targets:
            rows, allowed = await apply_guarded_transition(
                session,
                node_id=candidate.node_id,
                org_id=candidate.org_id,
                flow_id=candidate.flow_id,
                observed_state=observed,
                to_state=target,
                reason=f"all {len(predecessors)} predecessor(s) satisfied; "
                + ("human decision required" if candidate.kind == NodeKind.GATE.value else "ready for execution"),
            )
            if not allowed or rows != 1:
                if observed != candidate.observed_state:
                    raise RuntimeError("gate presentation did not complete; rolling back its intermediate states")
                break
            observed = target.value

    if not allowed:
        # The authority guard refused the edge. Already recorded as a decision
        # row inside the seam; counted here so it reaches the metrics.
        report.record(candidate.org_id, "transitions_rejected")
    elif rows == 1:
        report.record(candidate.org_id, "transitions_effected")
    else:
        # transition() allowed it but no row matched: a concurrent tick moved
        # this node between our read and our write. The correct outcome is a
        # no-op, but it is counted so overlap stays observable.
        report.record(candidate.org_id, "lost_races")
        logger.info(
            "orchestration tick: node %s already advanced by a concurrent tick; no-op",
            candidate.node_id,
        )


async def release_satisfied_successors(session: AsyncSession, node: OrchestrationNode) -> TickReport:
    """Reuse the normal dependency and gate rules inside an acceptance transaction."""
    rows = list(
        (
            await session.scalars(
                select(OrchestrationNode)
                .join(
                    OrchestrationEdge,
                    OrchestrationEdge.to_node_id == OrchestrationNode.id,
                )
                .where(
                    OrchestrationEdge.org_id == node.org_id,
                    OrchestrationEdge.from_node_id == node.id,
                    OrchestrationNode.org_id == node.org_id,
                    OrchestrationNode.flow_id == node.flow_id,
                    OrchestrationNode.state.in_([NodeState.PENDING.value, NodeState.READY.value]),
                )
                .order_by(OrchestrationNode.id)
                .limit(129)
            )
        ).all()
    )
    if len(rows) > 128:
        raise ValueError("evaluation_successor_limit")
    report = TickReport()
    for successor in rows:
        if successor.state == NodeState.READY.value and successor.kind != NodeKind.GATE.value:
            continue
        await _advance_node(session, _Candidate(successor.id, successor.org_id, successor.flow_id, successor.state, successor.kind), report)
    return report


async def run_tick(session: AsyncSession) -> TickReport:
    """One tick. Read pending nodes, release the ones whose predecessors are done.

    Pure over the session: no AWS, no request context, no commit. The caller owns
    the transaction, so the handler can commit once at the end and a failure
    leaves nothing half-applied.

    Never raises for a per-node failure — it records the error and continues, so
    one bad node cannot stall every other flow. The failure is still reported:
    `report.success` is False and the error count is non-zero (R-NF3). An error
    fetching a whole page is also caught and surfaced the same way.
    """
    report = TickReport()
    after_id: str | None = None

    while report.nodes_examined < _ITEM_BACKSTOP:
        remaining = _ITEM_BACKSTOP - report.nodes_examined
        try:
            page = await _fetch_candidate_page(session, after_id=after_id, limit=min(_PAGE_SIZE, remaining))
        except Exception:
            # A page we cannot read is a real failure, not an empty page.
            # Returning success here would be the silent stall (R-NF3).
            logger.exception("orchestration tick: failed to fetch candidate page after id=%s", after_id)
            report.errors += 1
            break

        if not page:
            break

        report.pages_read += 1

        for candidate in page:
            report.record(candidate.org_id, "nodes_examined")
            try:
                await _advance_node(session, candidate, report)
            except Exception:
                # Per-node containment: log, count, force non-success, keep going.
                logger.exception(
                    "orchestration tick: failed to advance node %s (org %s)",
                    candidate.node_id,
                    candidate.org_id,
                )
                report.record(candidate.org_id, "errors")

        after_id = page[-1].node_id

        if len(page) < _PAGE_SIZE:
            break
    else:
        # Loop exited on the backstop rather than exhausting candidates.
        report.truncated = True
        logger.warning(
            "orchestration tick: stopped at the %d-node backstop; more candidates remain",
            _ITEM_BACKSTOP,
        )

    return report

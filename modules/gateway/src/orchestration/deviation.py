"""Deviation detection: find work the engine did not dispatch.

Issue #4204 (EPIC #4191, intent #4120), requirement **R-O5f**.

This is not a reporting nicety. Capability confinement is deliberately deferred
(RULING 5, residual **RES-1**), so agents keep the ability to act off-graph —
close issues, edit bodies, apply dispatch labels, call the trigger endpoint. That
makes the honest v1 guarantee *"promotion state cannot be forged, and off-graph
activity is visible"*, not *"agents cannot act"*. This module is what makes the
second clause true. Without it the first clause protects a record that no longer
describes reality: the graph would say four stories ran while six actually did,
and the two extra would be invisible rather than merely unauthorised.

**Detection, not prevention.** Nothing here blocks anything, and that is the
design, not a limitation being worked around. Per **R-O5g** the controls that
*look* like enforcement must not be cited as enforcement: the loop guards bound
depth and alternation but never authority (and are fail-soft, and their state is
pod-writable); IAM cannot scope per-persona because every persona shares one role;
and the `adp-dispatch` marker is applied automatically by a wrapper the pod
controls, so it proves *a pod did it*, not *a pod was authorised to*. A reviewer
asked "what stops this?" should be told: nothing yet — this is what *notices* it.

**Runs are observed from the usage ledger, not from DynamoDB.** `usage_logs`
carries `graph_address` (migration 031) and one row per model call, so "which
addresses saw activity?" is a single grouped query with no run enumeration in
front of it. The DynamoDB webhook-events rows expire at 30 days, so an
enumerate-then-filter reconciliation would silently stop seeing older deviations
— the same 30-day cliff the cost story removed for the same reason (`cost.py`).
A detector that goes blind with age is worse than none, because its silence reads
as "no deviations".

**Both directions are load-bearing (AC-13).** A run with no dispatching node must
be flagged, *and* a dispatched run must not be. False negatives lose the
compensating control outright. False positives are nearly as bad in practice:
a detector that cries wolf on legitimate dispatches gets muted, and a muted
detector is a false negative with extra steps. The tests assert both.

**What counts as "dispatched"** is a node the engine actually moved — i.e. one
that reached `running` or beyond, not merely one that exists on the graph. A node
sitting in `pending` has an address, and treating its presence as authorisation
would make every off-graph run against a planned-but-not-yet-started node
invisible. That is the single most likely way an agent's off-plan work would slip
through, so the check is on state, not on existence.

Tenant isolation: every query filters `org_id`, and observed addresses are matched
only against nodes in the same org. A deviation is never reported to, or computed
from, another tenant's rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.usage import UsageLog

from .models import DecisionKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from .state import ActorKind, NodeState

logger = logging.getLogger("bedrockgateway.orchestration.deviation")

__all__ = [
    "DISPATCHED_STATES",
    "Deviation",
    "DeviationKind",
    "DeviationReport",
    "detect_deviations",
    "record_deviations",
]


# A node counts as "the engine dispatched this" only once it has actually moved.
#
# Spelled out rather than derived from "not pending", because the exclusions are
# the point. PENDING and READY are excluded: both are addresses the engine has
# NOT yet acted on, so accepting them as dispatch-evidence would let any run
# against a planned node pass unnoticed — the most likely shape of real off-plan
# work, since an agent working ahead of the plan works on nodes that exist.
#
# The post-run states are all included: a node that reached `passed`, `failed`,
# `halted`, `rejected_at_gate`, `awaiting_gate` or `superseded` was dispatched at
# some point, and its ledger rows are legitimately attributable.
DISPATCHED_STATES: frozenset[NodeState] = frozenset(
    {
        NodeState.RUNNING,
        NodeState.AWAITING_GATE,
        NodeState.PASSED,
        NodeState.REJECTED_AT_GATE,
        NodeState.FAILED,
        NodeState.HALTED,
        NodeState.SUPERSEDED,
    }
)

# Recorded on deviation decision rows. `actor_kind` is SERVICE because the
# *detector* is the engine — this says nothing about who caused the deviation,
# which is precisely what is unknown and why the row exists to be investigated.
_DETECTOR_ACTOR_ID = "system:orchestration-deviation"
_DETECTOR_ACTOR_ROLE = "engine"


class DeviationKind(StrEnum):
    """Why an observed run is considered off-graph.

    Two members, because the two cases have genuinely different diagnoses and a
    single "unknown run" bucket would hide that. `NO_SUCH_NODE` is work against an
    address with no node at all — most likely a fabricated or stale address.
    `NODE_NOT_DISPATCHED` is work against a real node the engine never started —
    the "agent worked ahead of the plan" shape.
    """

    NO_SUCH_NODE = "no_such_node"  # No node in this org answers to the address
    NODE_NOT_DISPATCHED = "node_not_dispatched"  # Node exists but was never dispatched


@dataclass(frozen=True)
class Deviation:
    """One observed run that the engine did not dispatch."""

    graph_address: str
    org_id: str
    kind: DeviationKind
    # How many ledger rows were seen at this address. Carried because scale is
    # diagnostic: one call is plausibly a stray, thousands is a running agent.
    call_count: int
    # The node's state when the node exists but was not dispatched. None for
    # NO_SUCH_NODE, where there is no node to have a state.
    observed_node_state: str | None = None
    node_id: str | None = None
    flow_id: str | None = None

    @property
    def detail(self) -> str:
        """A renderable one-line explanation (R-O5f: deviations are *rendered*)."""
        if self.kind is DeviationKind.NO_SUCH_NODE:
            return f"{self.call_count} model call(s) at address {self.graph_address!r}, which matches no node in this org"
        return (
            f"{self.call_count} model call(s) at address {self.graph_address!r}, "
            f"whose node is in state {self.observed_node_state!r} — the engine never dispatched it"
        )


@dataclass(frozen=True)
class DeviationReport:
    """The result of one reconciliation pass.

    `addresses_observed` and `addresses_reconciled` are both reported so a caller
    can tell "no deviations because everything matched" from "no deviations
    because nothing was observed". Those look identical in a bare empty list, and
    conflating them is how a broken detector reads as a clean bill of health.
    """

    org_id: str
    deviations: tuple[Deviation, ...] = ()
    addresses_observed: int = 0
    addresses_reconciled: int = 0

    @property
    def has_deviations(self) -> bool:
        return bool(self.deviations)


async def _observed_addresses(session: AsyncSession, *, org_id: str, flow_slug: str | None) -> list[tuple[str, int]]:
    """`(graph_address, call_count)` for every address with ledger activity.

    One grouped query, no run enumeration — see the module docstring on why
    enumerating DynamoDB rows first would make this detector go blind at 30 days.

    Rows with a NULL `graph_address` are excluded rather than treated as
    deviations: a null means "this call is not attributable to a graph node",
    which is the truth for every pre-feature row and every non-gateway Bedrock
    path. Flagging them would drown the real signal in historical noise.
    """
    stmt = select(
        UsageLog.graph_address,
        func.count(UsageLog.id).label("call_count"),
    ).where(
        UsageLog.org_id == org_id,
        UsageLog.graph_address.is_not(None),
    )

    if flow_slug is not None:
        # Scope to one flow's subtree. Exact match OR descendant, with LIKE
        # metacharacters escaped — an address containing `%` would otherwise widen
        # the match past its own subtree, and the trailing `/` is what stops
        # `flow-1` from matching `flow-10`.
        escaped = flow_slug.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where((UsageLog.graph_address == flow_slug) | (UsageLog.graph_address.like(f"{escaped}/%", escape="\\")))

    stmt = stmt.group_by(UsageLog.graph_address)
    return [(row[0], int(row[1] or 0)) for row in (await session.execute(stmt)).all()]


async def _dispatched_index(session: AsyncSession, *, org_id: str) -> dict[str, tuple[str, str, str]]:
    """Map every node's graph address to `(node_id, flow_id, state)`.

    Addresses are composed the same way `dispatch.graph_address` and
    `cost.get_flow_cost` compose them (`flow_slug/epic/wave/node`). The join to
    the flow is what supplies the slug; both sides are filtered on `org_id`, so a
    cross-tenant flow row cannot lend its slug to another tenant's node.
    """
    stmt = (
        select(
            OrchestrationFlow.slug,
            OrchestrationNode.epic_ref,
            OrchestrationNode.wave_ref,
            OrchestrationNode.node_ref,
            OrchestrationNode.id,
            OrchestrationNode.flow_id,
            OrchestrationNode.state,
        )
        .join(OrchestrationFlow, OrchestrationFlow.id == OrchestrationNode.flow_id)
        .where(
            OrchestrationNode.org_id == org_id,
            OrchestrationFlow.org_id == org_id,
        )
    )

    index: dict[str, tuple[str, str, str]] = {}
    for slug, epic_ref, wave_ref, node_ref, node_id, flow_id, state in (await session.execute(stmt)).all():
        index[f"{slug}/{epic_ref}/{wave_ref}/{node_ref}"] = (node_id, flow_id, state)
    return index


async def detect_deviations(
    session: AsyncSession,
    *,
    org_id: str,
    flow_slug: str | None = None,
) -> DeviationReport:
    """Reconcile observed runs against nodes the engine dispatched (AC-13).

    Read-only. Recording is a separate, explicit step (:func:`record_deviations`)
    so that a caller can surface deviations without writing decision rows on every
    render — a detector that writes on read would append a duplicate row every
    time somebody opened the page.

    Args:
        session: Caller-owned session.
        org_id: Tenant to reconcile. Every query filters on it.
        flow_slug: Optionally narrow to one flow's subtree. `None` reconciles the
            whole org, which is what a scheduled sweep wants.

    Returns:
        A :class:`DeviationReport`. An empty `deviations` tuple with a non-zero
        `addresses_observed` means everything reconciled; with a zero it means
        nothing was observed at all.
    """
    observed = await _observed_addresses(session, org_id=org_id, flow_slug=flow_slug)
    if not observed:
        return DeviationReport(org_id=org_id, addresses_observed=0, addresses_reconciled=0)

    index = await _dispatched_index(session, org_id=org_id)

    deviations: list[Deviation] = []
    reconciled = 0

    for address, call_count in observed:
        entry = index.get(address)

        if entry is None:
            deviations.append(
                Deviation(
                    graph_address=address,
                    org_id=org_id,
                    kind=DeviationKind.NO_SUCH_NODE,
                    call_count=call_count,
                )
            )
            continue

        node_id, flow_id, raw_state = entry

        try:
            state = NodeState(raw_state)
        except ValueError:
            # A node carrying a state outside the vocabulary cannot be shown to
            # have been dispatched, and "cannot be shown" is a deviation here
            # rather than a pass. The alternative — treating an unreadable state
            # as authorisation — is the fail-open reading.
            logger.error(
                "orchestration deviation: node %s carries undeclared state %r; treating its runs as undispatched",
                node_id,
                raw_state,
            )
            deviations.append(
                Deviation(
                    graph_address=address,
                    org_id=org_id,
                    kind=DeviationKind.NODE_NOT_DISPATCHED,
                    call_count=call_count,
                    observed_node_state=raw_state,
                    node_id=node_id,
                    flow_id=flow_id,
                )
            )
            continue

        if state in DISPATCHED_STATES:
            # The engine dispatched this node; its ledger rows are attributable.
            # This is the no-false-positives half of AC-13.
            reconciled += 1
            continue

        deviations.append(
            Deviation(
                graph_address=address,
                org_id=org_id,
                kind=DeviationKind.NODE_NOT_DISPATCHED,
                call_count=call_count,
                observed_node_state=state.value,
                node_id=node_id,
                flow_id=flow_id,
            )
        )

    if deviations:
        logger.warning(
            "orchestration deviation: %d off-graph address(es) observed in org %s (%d reconciled)",
            len(deviations),
            org_id,
            reconciled,
        )

    return DeviationReport(
        org_id=org_id,
        deviations=tuple(deviations),
        addresses_observed=len(observed),
        addresses_reconciled=reconciled,
    )


async def record_deviations(session: AsyncSession, report: DeviationReport) -> int:
    """Append a decision row per deviation, so the record is durable.

    Written as `TRANSITION_REJECTED` — the kind the store story made first-class
    for exactly this purpose ("under RULING 5 recorded rejections are the primary
    detector for off-plan agent activity, so they must be queryable alongside the
    decisions they were rejected against"). A deviation is the same class of
    evidence: something happened that the engine did not authorise.

    Only deviations carrying a `flow_id` can be recorded, because
    `orchestration_decisions.flow_id` is a non-nullable FK. A `NO_SUCH_NODE`
    deviation has no flow by definition — there is no node and so no flow to
    attach it to — and is therefore returned by :func:`detect_deviations` and
    logged, but not persisted here. That gap is real and is named rather than
    hidden: closing it needs a flow-independent deviations table, which is a
    schema change and this story ships no migration.

    Returns:
        The number of decision rows appended.
    """
    appended = 0

    for deviation in report.deviations:
        if deviation.flow_id is None:
            logger.warning(
                "orchestration deviation: %s is not persistable (no flow to attach it to); logged only",
                deviation.detail,
            )
            continue

        session.add(
            OrchestrationDecision(
                org_id=deviation.org_id,
                flow_id=deviation.flow_id,
                node_id=deviation.node_id,
                kind=DecisionKind.TRANSITION_REJECTED.value,
                actor_id=_DETECTOR_ACTOR_ID,
                actor_role=_DETECTOR_ACTOR_ROLE,
                actor_kind=ActorKind.SERVICE.value,
                reason=f"deviation detected: {deviation.kind.value}",
                rejection_reason=deviation.detail,
                from_state=deviation.observed_node_state,
                to_state=None,
            )
        )
        appended += 1

    if appended:
        await session.flush()

    return appended

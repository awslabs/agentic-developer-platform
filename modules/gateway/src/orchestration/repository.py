"""Tenant-scoped data access for the orchestration graph store.

Issue #4196. Every method takes `org_id` as a keyword-only argument and filters
on it; there is no method that reads or writes across tenants, and no method that
updates an `orchestration_decisions` row.

The absent decisions-update method is the point, not an omission: combined with
the `before_update` hook in `models.py`, an attempt to rewrite gate attribution
raises rather than succeeding.

Shape follows `shared/services/credential_resolver.py` — constructor-injected
`AsyncSession`, keyword-only arguments, SQLAlchemy 2.0 `select()` +
`session.execute()`. Reads and writes flush but do not commit: the caller owns
the transaction, so a decision and the plan version it accepted can land
atomically.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)


class OrchestrationRepository:
    """Tenant-scoped reads and appends over the orchestration graph tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- flows --------------------------------------------------------------

    async def create_flow(self, *, org_id: str, slug: str, title: str, intent_ref: str | None = None) -> OrchestrationFlow:
        flow = OrchestrationFlow(org_id=org_id, slug=slug, title=title, intent_ref=intent_ref)
        self._session.add(flow)
        await self._session.flush()
        return flow

    async def get_flow(self, *, org_id: str, flow_id: str) -> OrchestrationFlow | None:
        stmt = select(OrchestrationFlow).where(
            OrchestrationFlow.org_id == org_id,
            OrchestrationFlow.id == flow_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_flows(self, *, org_id: str) -> list[OrchestrationFlow]:
        stmt = select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id).order_by(OrchestrationFlow.created_at.desc())
        return list((await self._session.execute(stmt)).scalars().all())

    # -- nodes --------------------------------------------------------------

    async def add_node(
        self,
        *,
        org_id: str,
        flow_id: str,
        epic_ref: str,
        wave_ref: str,
        node_ref: str,
        kind: str,
        title: str,
        issue_ref: str | None = None,
        state: str | None = None,
    ) -> OrchestrationNode:
        # `state=None` means "use the column default" (pending), which is what
        # every compile has always done. It is an argument at all only for issue
        # #4528's acceptance gate, which must be *born* in `awaiting_gate`: a node
        # created pending and then moved would be a second writer of node state
        # outside `transition()`, and `transition()` has no edge into
        # `awaiting_gate` from `pending` to offer it.
        node = OrchestrationNode(
            org_id=org_id,
            flow_id=flow_id,
            epic_ref=epic_ref,
            wave_ref=wave_ref,
            node_ref=node_ref,
            kind=kind,
            title=title,
            issue_ref=issue_ref,
            **({"state": state} if state is not None else {}),
        )
        self._session.add(node)
        await self._session.flush()
        return node

    async def get_node(self, *, org_id: str, node_id: str) -> OrchestrationNode | None:
        stmt = select(OrchestrationNode).where(
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.id == node_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_nodes(self, *, org_id: str, flow_id: str) -> list[OrchestrationNode]:
        stmt = (
            select(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.flow_id == flow_id,
            )
            .order_by(OrchestrationNode.wave_ref, OrchestrationNode.node_ref)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- edges --------------------------------------------------------------

    async def add_edge(self, *, org_id: str, flow_id: str, from_node_id: str, to_node_id: str) -> OrchestrationEdge:
        edge = OrchestrationEdge(org_id=org_id, flow_id=flow_id, from_node_id=from_node_id, to_node_id=to_node_id)
        self._session.add(edge)
        await self._session.flush()
        return edge

    async def list_edges(self, *, org_id: str, flow_id: str) -> list[OrchestrationEdge]:
        stmt = select(OrchestrationEdge).where(
            OrchestrationEdge.org_id == org_id,
            OrchestrationEdge.flow_id == flow_id,
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- accepted plans -----------------------------------------------------

    async def record_accepted_plan(
        self,
        *,
        org_id: str,
        flow_id: str,
        plan_document: dict,
        plan_hash: str,
        accepted_by_decision_id: str | None = None,
    ) -> OrchestrationAcceptedPlan:
        """Append the next accepted-plan version, superseding the one in force.

        Amendment supersedes rather than mutates, so the version in force at any
        past gate stays readable. Version numbers are allocated from the current
        maximum for the flow; the unique index on `(flow_id, version)` is what
        makes a concurrent double-accept fail loudly instead of silently
        producing two "version 2" plans.
        """
        current = await self.get_accepted_plan(org_id=org_id, flow_id=flow_id)
        if current is not None:
            current.superseded_at = utcnow()

        stmt = select(OrchestrationAcceptedPlan.version).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
        )
        versions = list((await self._session.execute(stmt)).scalars().all())
        next_version = (max(versions) + 1) if versions else 1

        plan = OrchestrationAcceptedPlan(
            org_id=org_id,
            flow_id=flow_id,
            version=next_version,
            plan_document=plan_document,
            plan_hash=plan_hash,
            accepted_by_decision_id=accepted_by_decision_id,
        )
        self._session.add(plan)
        await self._session.flush()
        return plan

    async def get_accepted_plan(self, *, org_id: str, flow_id: str) -> OrchestrationAcceptedPlan | None:
        """The accepted plan currently in force (the one not yet superseded)."""
        stmt = select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_plan_versions(self, *, org_id: str, flow_id: str) -> list[OrchestrationAcceptedPlan]:
        stmt = (
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == flow_id,
            )
            .order_by(OrchestrationAcceptedPlan.version)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- decisions (append-only: no update method, by design) ----------------

    async def append_decision(
        self,
        *,
        org_id: str,
        flow_id: str,
        kind: str,
        actor_id: str,
        actor_role: str,
        actor_kind: str,
        node_id: str | None = None,
        reason: str | None = None,
        rejection_reason: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
    ) -> OrchestrationDecision:
        """Append one decision record. There is deliberately no counterpart
        `update_decision` / `delete_decision` — see the class docstring."""
        decision = OrchestrationDecision(
            org_id=org_id,
            flow_id=flow_id,
            node_id=node_id,
            kind=kind,
            actor_id=actor_id,
            actor_role=actor_role,
            actor_kind=actor_kind,
            reason=reason,
            rejection_reason=rejection_reason,
            from_state=from_state,
            to_state=to_state,
        )
        self._session.add(decision)
        await self._session.flush()
        return decision

    async def list_decisions(self, *, org_id: str, flow_id: str) -> list[OrchestrationDecision]:
        stmt = (
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.flow_id == flow_id,
            )
            .order_by(OrchestrationDecision.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())

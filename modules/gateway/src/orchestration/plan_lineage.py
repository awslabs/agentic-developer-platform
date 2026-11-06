"""Verify narrowly scoped amendments without rewriting worker assignment identities.

Human-accepted dependency amendments can bridge a worker's execution identity;
each hop preserves non-edge fields and protected prerequisites. Independently
accepted limit receipts have a separate, narrower policy-authority hop.
Ordinary replacement/append plans still invalidate old execution authority.
"""

import json
from dataclasses import dataclass, field
from types import MappingProxyType

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import load_only

from .continuation import digest
from .display_state import NODE_STATE_OVERRIDES_EXECUTION
from .models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .proposal import split_address

CONTRACT = "paused-wave-dependencies/v1"
MAX_HOPS = 100


@dataclass(frozen=True)
class _LineageLookups:
    plans_by_id: dict
    plans_by_version: dict
    receipts: dict
    nodes: dict
    flow_slugs: dict
    # These lookups live only inside one read-only display snapshot. Admission
    # never uses them; the ORM documents are not changed during that snapshot.
    _document_hashes: dict = field(default_factory=dict)
    _graph_indexes: dict = field(default_factory=dict)

    def accepted_hash(self, plan):
        from .shared_budget import accepted_document_hash

        if plan.id not in self._document_hashes:
            self._document_hashes[plan.id] = accepted_document_hash(plan.plan_document)
        return self._document_hashes[plan.id]

    def graph_index(self, plan):
        if plan.id not in self._graph_indexes:
            nodes, incoming = {}, {}
            for item in plan.plan_document.get("nodes", []):
                # Keep duplicates: the existing validator requires exactly one
                # matching node, rather than silently choosing the last one.
                address = item.get("address")
                if isinstance(address, str):
                    nodes.setdefault(address, []).append(item)
            for source, target in edge_set(plan.plan_document):
                incoming.setdefault(target, set()).add(source)
            self._graph_indexes[plan.id] = (nodes, incoming)
        return self._graph_indexes[plan.id]


class PlanLineageResolver:
    """Request-owned display facts; never use these to authorize a write."""

    def __init__(self):
        self._scopes = {}

    async def preserved_pairs(self, session, *, org_id, flow_ids=None):
        scope = (org_id, None if flow_ids is None else tuple(sorted(set(flow_ids))))
        if scope not in self._scopes:
            tenant_scope = self._scopes.get((org_id, None)) if flow_ids is not None else None
            if tenant_scope is not None and tenant_scope[2]:
                allowed_flows = set(flow_ids)
                pairs, node_flows, _ = tenant_scope
                self._scopes[scope] = ([pair for pair in pairs if node_flows[pair[0]] in allowed_flows], node_flows, True)
            else:
                self._scopes[scope] = await _load_preserved_pairs(session, org_id=org_id, flow_ids=flow_ids)
        return self._scopes[scope][0]


class PolicyDisplayLineage:
    """Request-owned receipt history for a read-only policy display snapshot.

    Action admission retains the uncached resolver. Every receipt/hop is still
    checked by the same validators; this only replaces their database lookups.
    """

    def __init__(self):
        self._scopes = {}

    async def receipt_plan(self, session, plan, data):
        key = (plan.org_id, plan.flow_id, plan.id, plan.version, plan.plan_hash)
        if key not in self._scopes:
            rows = list(
                await session.execute(
                    select(OrchestrationAcceptedPlan, OrchestrationDecision)
                    .outerjoin(OrchestrationDecision, OrchestrationDecision.id == OrchestrationAcceptedPlan.accepted_by_decision_id)
                    .where(
                        OrchestrationAcceptedPlan.org_id == plan.org_id,
                        OrchestrationAcceptedPlan.flow_id == plan.flow_id,
                        OrchestrationAcceptedPlan.version.between(max(1, plan.version - MAX_HOPS), plan.version),
                    )
                    .limit(MAX_HOPS + 2)
                )
            )
            if len(rows) > MAX_HOPS + 1:
                return None
            self._scopes[key] = _LineageLookups(
                {p.id: p for p, _ in rows},
                {(p.org_id, p.flow_id, p.version): p for p, _ in rows},
                {r.id: r for _, r in rows if r is not None},
                {},
                {},
            )
        return await receipt_plan(session, plan, data, _lookups=self._scopes[key])


def edge_set(document):
    return {(e["from_address"], e["to_address"]) for e in document["edges"]}


def wave(address):
    return tuple(split_address(address)[1:3])


async def ancestor_plan(session, plan, version, *, node_id=None, _lookups=None):
    """Return the exact earlier plan only through verified preservation receipts."""
    if plan is None or type(version) is not int or version < 1 or version > plan.version:
        return None
    if plan.version == version:
        return plan  # Existing callers already enforce their node/tenant binding.
    protected = None
    if node_id is not None:
        node = _lookups.nodes.get(node_id) if _lookups is not None else await session.get(OrchestrationNode, node_id)
        if node is None or node.org_id != plan.org_id or node.flow_id != plan.flow_id:
            return None
        protected = (node.epic_ref, node.wave_ref)
    for _ in range(MAX_HOPS + 1):
        if plan.version == version:
            return plan
        marker = plan.plan_document.get("execution_continuation") if isinstance(plan.plan_document, dict) else None
        if (
            not plan.accepted_by_decision_id
            or not isinstance(marker, dict)
            or marker.get("mode") != "shared_worker_role"
            or marker.get("delivery_mode") != "code_only"
        ):
            # Not a paused-wave receipt. An ordinary human amendment still keeps
            # this node's execution authority when it left the node's execution
            # definition and prerequisites untouched (#7535).
            base = await _node_preserving_hop(session, plan, node_id=node_id, _lookups=_lookups)
            if base is None:
                return None
            plan = base
            continue
        receipt = (
            _lookups.receipts.get(plan.accepted_by_decision_id)
            if _lookups is not None
            else await session.get(OrchestrationDecision, plan.accepted_by_decision_id)
        )
        try:
            data = json.loads(receipt.reason) if receipt else {}
            if (
                receipt is None
                or receipt.org_id != plan.org_id
                or receipt.flow_id != plan.flow_id
                or receipt.kind != "plan_amended"
                or receipt.actor_kind != "human"
                or not receipt.actor_id
                or data.get("contract") != CONTRACT
                or data.get("plan_version") != plan.version
                or data.get("plan_hash") != plan.plan_hash
                or plan.plan_hash != digest(plan.plan_document)
                or receipt.actor_id != plan.plan_document["execution_policy"]["principal_id"]
            ):
                return None
            base = (
                _lookups.plans_by_id.get(data["base_plan_id"])
                if _lookups is not None
                else await session.get(OrchestrationAcceptedPlan, data["base_plan_id"])
            )
            if (
                base is None
                or base.org_id != plan.org_id
                or base.flow_id != plan.flow_id
                or base.version != plan.version - 1
                or base.version != data["base_plan_version"]
                or base.plan_hash != data["base_plan_hash"]
                or base.plan_hash != digest(base.plan_document)
                or base.superseded_at is None
            ):
                return None
            before, after = base.plan_document, plan.plan_document
            if {k: v for k, v in before.items() if k != "edges"} != {k: v for k, v in after.items() if k != "edges"}:
                return None
            changes = edge_set(before) ^ edge_set(after)
            changed_waves = {wave(target) for _, target in changes}
            if not changes or changed_waves != {tuple(w) for w in data["changed_waves"]}:
                return None
            frozen = {tuple(w) for w in data["frozen_waves"]}
            if changed_waves & frozen or (protected is not None and protected not in frozen):
                return None
            plan = base
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
    return None


async def _node_preserving_hop(session, plan, *, node_id, _lookups=None):
    """The base of `plan` when the amendment that produced `plan` preserved `node_id`.

    `amend_plan` refuses to change a started node's execution definition or its
    incoming edges, so an accepted amendment is itself the proof that such a node's
    assignment carried over. Without this hop every ordinary amendment (a new
    gate, a re-pinned evaluation, a budget change) stranded every in-flight
    execution of a protected flow behind `authority_unverifiable` (#7535). The
    rule is deliberately narrow: one human `plan_amended` hop at a time, the node
    present on both sides with identical definition and prerequisites.
    """
    if node_id is None or not plan.accepted_by_decision_id:
        return None
    from .amend import _execution_definition
    from .models import OrchestrationFlow

    receipt = (
        _lookups.receipts.get(plan.accepted_by_decision_id)
        if _lookups is not None
        else await session.get(OrchestrationDecision, plan.accepted_by_decision_id)
    )
    if (
        receipt is None
        or receipt.org_id != plan.org_id
        or receipt.flow_id != plan.flow_id
        or receipt.kind != "plan_amended"
        or receipt.actor_kind != "human"
        or not receipt.actor_id
    ):
        return None
    base = (
        _lookups.plans_by_version.get((plan.org_id, plan.flow_id, plan.version - 1))
        if _lookups is not None
        else await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.org_id == plan.org_id,
                OrchestrationAcceptedPlan.flow_id == plan.flow_id,
                OrchestrationAcceptedPlan.version == plan.version - 1,
            )
        )
    )
    if base is None or base.superseded_at is None or not isinstance(base.plan_document, dict) or not isinstance(plan.plan_document, dict):
        return None
    node = _lookups.nodes.get(node_id) if _lookups is not None else await session.get(OrchestrationNode, node_id)
    if _lookups is not None:
        flow_slug = _lookups.flow_slugs.get((plan.org_id, plan.flow_id))
    else:
        flow = await session.get(OrchestrationFlow, plan.flow_id) if node is not None else None
        flow_slug = flow.slug if flow is not None else None
    if node is None or flow_slug is None or node.org_id != plan.org_id or node.flow_id != plan.flow_id:
        return None
    address = f"{flow_slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
    try:
        if _lookups is None:
            before = [item for item in base.plan_document.get("nodes", []) if item.get("address") == address]
            after = [item for item in plan.plan_document.get("nodes", []) if item.get("address") == address]
        else:
            before_nodes, before_incoming = _lookups.graph_index(base)
            after_nodes, after_incoming = _lookups.graph_index(plan)
            before, after = before_nodes.get(address, []), after_nodes.get(address, [])
        if len(before) != 1 or len(after) != 1:
            return None
        if _execution_definition(before[0]) != _execution_definition(after[0]):
            return None
        if before[0].get("kind") != after[0].get("kind") or before[0].get("issue_ref") != after[0].get("issue_ref"):
            return None
        if _lookups is None:
            incoming_before = {source for source, target in edge_set(base.plan_document) if target == address}
            incoming_after = {source for source, target in edge_set(plan.plan_document) if target == address}
        else:
            incoming_before = before_incoming.get(address, set())
            incoming_after = after_incoming.get(address, set())
        if incoming_before != incoming_after:
            return None
    except (KeyError, TypeError, AttributeError):
        return None
    return base


async def approval_authorizes_node(session, *, org_id, flow_id, node_id, decision_id):
    """Whether the approval `decision_id` still authorizes `node_id`'s dispatch.

    A dispatch records the human approval in force when it was published. Every
    later amendment adds a newer approval, so "is this the latest approval" is
    the wrong question for work already in flight: the right one is whether the
    plan that approval accepted is still in this node's preserved lineage (#7535).
    """
    if not decision_id:
        return False
    current = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    if current is None:
        return False
    if current.accepted_by_decision_id == decision_id:
        return True
    approved = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.accepted_by_decision_id == decision_id,
        )
    )
    if approved is None:
        # A gate approval roots dispatches too (#5128); it accepts no plan of its
        # own, so the plan it authorized is the one in force when it was recorded.
        from .genesis import APPROVAL_DECISION_KINDS

        decision = await session.get(OrchestrationDecision, decision_id)
        if (
            decision is None
            or decision.org_id != org_id
            or decision.flow_id != flow_id
            or decision.kind not in APPROVAL_DECISION_KINDS
            or decision.created_at is None
        ):
            return False
        approved = await session.scalar(
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == flow_id,
                OrchestrationAcceptedPlan.created_at <= decision.created_at,
            )
            .order_by(OrchestrationAcceptedPlan.version.desc())
            .limit(1)
        )
        if approved is None:
            return False
    return await ancestor_plan(session, current, approved.version, node_id=node_id) is not None


async def execution_plan_matches(session, *, org_id, flow_id, node_id, version, current_version=None):
    # Common path stays cheap. The caller obtained current_version from the
    # current accepted plan, never from worker-supplied metadata.
    if current_version is not None and current_version == version:
        return True
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    return await ancestor_plan(session, plan, version, node_id=node_id) is not None


async def receipt_plan(session, plan, data, *, node_id=None, _lookups=None):
    """Find the receipt's original plan without changing worker identity rules."""
    original = await ancestor_plan(session, plan, data.get("plan_version"), node_id=node_id, _lookups=_lookups)
    if original is None and node_id is None:
        original = await _supplement_ancestor(session, plan, data.get("plan_version"), _lookups=_lookups)
    if original is None or original.plan_hash != data.get("plan_hash"):
        return None
    if "plan_id" in data and original.id != data["plan_id"]:
        return None
    return original


async def receipt_amendment_change(session, plan, document, data):
    """Check the proposed hop as well as the receipt's already accepted lineage."""
    reason = await resolved_receipt_authority_change(session, plan.plan_document, document)
    if reason is not None:
        return reason
    version = data.get("plan_version")
    if type(version) is not int or version < 1 or version > plan.version:
        return "receipt_lineage_unverifiable"
    if plan.version + 1 - version > MAX_HOPS:
        return "receipt_lineage_limit_exceeded"
    if await receipt_plan(session, plan, data) is None:
        return "receipt_lineage_unverifiable"
    return None


def receipt_authority_change(before, after):
    """Describe why a new document cannot inherit independently approved limits."""
    from .execution_policy import ExecutionPolicy, policy_hash

    try:
        if (
            not isinstance(before, dict)
            or not isinstance(after, dict)
            or before.get("org_id") != after.get("org_id")
            or before.get("flow_slug") != after.get("flow_slug")
            or before.get("execution_continuation") != after.get("execution_continuation")
        ):
            return "plan_scope_changed"
        original = ExecutionPolicy.model_validate(before["execution_policy"])
        current = ExecutionPolicy.model_validate(after["execution_policy"])
        if original.policy_hash != policy_hash(original) or current.policy_hash != policy_hash(current):
            return "policy_hash_unverifiable"
        previous = {key: value for key, value in before["execution_policy"].items() if key not in {"policy_id", "policy_hash"}}
        proposed = {key: value for key, value in after["execution_policy"].items() if key not in {"policy_id", "policy_hash"}}
        if previous == proposed:
            return None
        if previous.get("limits") != proposed.get("limits"):
            return "base_limits_changed"
        if previous.get("repository_ids") != proposed.get("repository_ids"):
            return "repository_scope_changed"
        return "policy_authority_changed"
    except (KeyError, ValueError, TypeError, AttributeError):
        return "policy_unverifiable"


async def resolved_receipt_authority_change(session, before, after):
    """Resolve only a principal spelling change, with fresh workspace proof."""
    reason = receipt_authority_change(before, after)
    if reason != "policy_authority_changed":
        return reason
    from .policy_identity import same_human_principal

    original, current = before["execution_policy"], after["execution_policy"]
    # Compare all remaining authority before consulting identity. This local
    # comparison copy is never stored or used as a policy/receipt hash input.
    comparison = {**after, "execution_policy": {**current, "principal_id": original.get("principal_id")}}
    if receipt_authority_change(before, comparison) is not None:
        return reason
    if await same_human_principal(session, org_id=before["org_id"], recorded_id=original.get("principal_id"), current_id=current.get("principal_id")):
        return None
    return reason


async def _supplement_ancestor(session, plan, version, *, _lookups=None):
    """Bridge only human amendments preserving the original policy authority."""
    if plan is None or type(version) is not int or version < 1 or plan.version - version > MAX_HOPS:
        return None
    from .genesis import APPROVAL_DECISION_KINDS
    from .shared_budget import accepted_document_hash

    while plan.version > version:
        if not plan.accepted_by_decision_id:
            return None
        decision = (
            _lookups.receipts.get(plan.accepted_by_decision_id)
            if _lookups is not None
            else await session.get(OrchestrationDecision, plan.accepted_by_decision_id)
        )
        base = (
            _lookups.plans_by_version.get((plan.org_id, plan.flow_id, plan.version - 1))
            if _lookups is not None
            else await session.scalar(
                select(OrchestrationAcceptedPlan).where(
                    OrchestrationAcceptedPlan.org_id == plan.org_id,
                    OrchestrationAcceptedPlan.flow_id == plan.flow_id,
                    OrchestrationAcceptedPlan.version == plan.version - 1,
                )
            )
        )
        if (
            decision is None
            or decision.org_id != plan.org_id
            or decision.flow_id != plan.flow_id
            or decision.kind != "plan_amended"
            or decision.actor_kind != "human"
            or not decision.actor_id
            or base is None
            or base.superseded_at is None
            or not base.accepted_by_decision_id
            or base.org_id != plan.org_id
            or base.flow_id != plan.flow_id
            or f"[amendment: v{base.version} -> v{plan.version}," not in (decision.reason or "")
        ):
            return None
        predecessor = (
            _lookups.receipts.get(base.accepted_by_decision_id)
            if _lookups is not None
            else await session.get(OrchestrationDecision, base.accepted_by_decision_id)
        )
        if (
            predecessor is None
            or predecessor.org_id != base.org_id
            or predecessor.flow_id != base.flow_id
            # The normal draft start gate also accepts a policy-bearing version.
            # Use the same approval kinds that can root its worker authority;
            # the accepted-plan link, tenant, actor and policy checks still apply.
            or predecessor.kind not in APPROVAL_DECISION_KINDS
            or predecessor.actor_kind != "human"
            or not predecessor.actor_id
        ):
            return None
        try:
            before, after = base.plan_document, plan.plan_document
            if (
                before.get("org_id") != plan.org_id
                or after.get("org_id") != plan.org_id
                or base.plan_hash != (accepted_document_hash(before) if _lookups is None else _lookups.accepted_hash(base))
                or plan.plan_hash != (accepted_document_hash(after) if _lookups is None else _lookups.accepted_hash(plan))
                or await resolved_receipt_authority_change(session, before, after) is not None
            ):
                return None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        plan = base
    return plan if plan.version == version else None


async def preserved_execution_pairs(session, *, org_id, flow_ids=None, resolver=None):
    """Bounded, verified facts for both SQL rollups and individual stage cards."""
    return await (resolver or PlanLineageResolver()).preserved_pairs(session, org_id=org_id, flow_ids=flow_ids)


async def _load_preserved_pairs(session, *, org_id, flow_ids=None):
    from .deployment_authority import DELIVERY_PHASES
    from .execution_state import TERMINAL_EXECUTION_STATUSES

    execution, plan = OrchestrationExecution, OrchestrationAcceptedPlan
    query = (
        select(execution, plan.id, OrchestrationNode)
        .options(
            load_only(execution.org_id, execution.flow_id, execution.node_id, execution.accepted_plan_version),
            load_only(
                OrchestrationNode.org_id,
                OrchestrationNode.flow_id,
                OrchestrationNode.epic_ref,
                OrchestrationNode.wave_ref,
                OrchestrationNode.node_ref,
            ),
        )
        .join(
            plan,
            (plan.org_id == execution.org_id) & (plan.flow_id == execution.flow_id),
        )
        .join(OrchestrationNode, (OrchestrationNode.id == execution.node_id) & (OrchestrationNode.attempts == execution.cycle))
        .where(
            plan.org_id == org_id,
            plan.superseded_at.is_(None),
            execution.accepted_plan_version != plan.version,
            # Code completion can still owe deployment/evaluation. Its stage
            # card needs preserved lineage until delivery settles. Exclude all
            # other settled history before the row bound and document loads.
            or_(
                OrchestrationNode.state.not_in(NODE_STATE_OVERRIDES_EXECUTION),
                and_(
                    OrchestrationNode.state == "passed",
                    execution.phase.in_([phase.value for phase in DELIVERY_PHASES]),
                    execution.status.not_in([status.value for status in TERMINAL_EXECUTION_STATUSES]),
                ),
            ),
        )
    )
    if flow_ids is not None:
        query = query.where(plan.flow_id.in_(flow_ids))
    records = list(await session.execute(query.limit(1001)))
    if len(records) > 1000:
        return ([], {}, False)  # Display never supplies authority; fail closed on excess history.
    if not records:
        return ([], {}, True)
    lineage = list(
        await session.execute(
            select(plan, OrchestrationDecision)
            .outerjoin(OrchestrationDecision, OrchestrationDecision.id == plan.accepted_by_decision_id)
            .where(plan.org_id == org_id, plan.flow_id.in_({e.flow_id for e, _, _ in records}))
            .limit(10001)
        )
    )
    if len(lineage) > 10000:
        return ([], {}, False)
    plans_by_id = {candidate.id: candidate for candidate, _ in lineage}
    plans_by_version = {(candidate.org_id, candidate.flow_id, candidate.version): candidate for candidate, _ in lineage}
    receipts = {receipt.id: receipt for _, receipt in lineage if receipt is not None}
    flow_ids_with_history = {candidate.flow_id for candidate, _, _ in records}
    flow_slugs = {
        (org, flow_id): slug
        for org, flow_id, slug in (
            await session.execute(
                select(OrchestrationFlow.org_id, OrchestrationFlow.id, OrchestrationFlow.slug).where(
                    OrchestrationFlow.org_id == org_id, OrchestrationFlow.id.in_(flow_ids_with_history)
                )
            )
        ).all()
    }
    lookups = _LineageLookups(
        MappingProxyType(plans_by_id),
        MappingProxyType(plans_by_version),
        MappingProxyType(receipts),
        MappingProxyType({node.id: node for _, _, node in records}),
        MappingProxyType(flow_slugs),
    )
    verified = {}
    result = []
    for execution, current_id, _ in records:
        current = plans_by_id.get(current_id)
        if current is None:
            return ([], {}, False)
        key = (org_id, current.flow_id, execution.node_id, current.version, current.plan_hash, execution.accepted_plan_version)
        if key not in verified:
            verified[key] = (
                await ancestor_plan(session, current, execution.accepted_plan_version, node_id=execution.node_id, _lookups=lookups) is not None
            )
        if verified[key]:
            result.append((execution.node_id, execution.accepted_plan_version))
    return (result, {execution.node_id: execution.flow_id for execution, _, _ in records}, True)

"""Verify initial gate acceptance without discarding inert draft history."""

from .compile import ApprovalContext, accept_execution_policy, plan_hash
from .models import DecisionKind
from .proposal import LoopProposal, validate_proposal
from .registration import acceptance_gate_address, promote_proposed_policy


def initial_gate_accepted(plan, snapshot):
    """Require the exact draft, bound human gate answer and resulting policy."""
    acceptance = snapshot["accepted_policy"]
    decision = acceptance["decision"]
    source = acceptance.get("source_plan")
    if not source or not decision:
        return False
    try:
        proposal = LoopProposal.model_validate(source["document"])
        if (
            validate_proposal(proposal)
            or proposal.proposed_execution_policy is None
            or proposal.execution_policy is not None
            or source["version"] != plan.version - 1
            or source["hash"] != plan_hash(proposal)
            or f"[plan-hash={source['hash']}]" not in (decision["reason"] or "")
            or decision["from_state"] != "awaiting_gate"
            or decision["to_state"] != "passed"
        ):
            return False
        address = acceptance_gate_address(proposal)
        gate = next((node for node in snapshot["nodes"] if node["id"] == decision["node_id"]), None)
        if gate is None or gate["kind"] != "gate" or gate["state"] != "passed" or f"{proposal.flow_slug}/{'/'.join(gate['address'])}" != address:
            return False
        granted = accept_execution_policy(
            promote_proposed_policy(proposal),
            decision=ApprovalContext(org_id=plan.org_id, actor_id=decision["actor_id"], actor_role=decision["actor_role"]),
            decision_kind=DecisionKind.PLAN_ACCEPTED,
        )
        return granted.model_dump(mode="json") == plan.plan_document and plan_hash(granted) == plan.plan_hash
    except (ValueError, KeyError, TypeError):
        return False


def has_started_nodes(document, nodes):
    """Superseded zero-attempt draft addresses are history, never runnable work.

    Reports, dispatches, claims, bindings and executions are checked separately
    across all history by the caller, including these superseded nodes.
    """
    active = {node["address"] for node in document.get("nodes", [])}
    slug = document.get("flow_slug", document.get("flow", ""))
    for node in nodes:
        if node["attempts"] != 0:
            return True
        if node["kind"] == "gate" or node["state"] in {"pending", "ready"}:
            continue
        address = f"{slug}/{'/'.join(node['address'])}"
        if node["state"] != "superseded" or address in active:
            return True
    return False

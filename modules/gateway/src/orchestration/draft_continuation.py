"""Atomic initial acceptance of an inert draft using shared code delivery."""

from .continuation import CODE_ACTIONS, ContinuationRefusedError
from .execution_policy import Action, ExecutionPolicy
from .proposal import LoopProposal, validate_proposal
from .registration import acceptance_gate_address, promote_proposed_policy


def preview_draft_acceptance(plan, snapshot, policy):
    document = plan.plan_document or {}
    try:
        proposal = LoopProposal.model_validate(document)
        proposed = proposal.proposed_execution_policy
        decision = snapshot["accepted_policy"]["decision"]
        if (
            proposed is None
            or proposal.execution_policy is not None
            or validate_proposal(proposal)
            or not decision
            or decision["kind"] != "plan_drafted"
            or decision["org_id"] != plan.org_id
            or decision["flow_id"] != plan.flow_id
            or not decision["actor_id"]
            or any(getattr(proposed, field) is not None for field in ("policy_id", "policy_hash", "principal_id"))
        ):
            raise ValueError("not an intact inert draft")
    except ValueError:
        raise ContinuationRefusedError(
            "draft_policy_unverifiable", "Initial shared acceptance requires a valid attributed draft and proposed policy."
        ) from None
    if (
        snapshot["executions"]
        or any(snapshot["accepted_policy"]["start_evidence"].values())
        or any(n["attempts"] != 0 or (n["kind"] != "gate" and n["state"] not in {"pending", "ready"}) for n in snapshot["nodes"])
    ):
        raise ContinuationRefusedError("draft_already_started", "Initial shared acceptance requires no prior worker or delivery history.")
    # Start precisely the code actions proposed. The separate evaluation remains
    # declared, but no evaluate authority is invented before its contract exists.
    if set(proposed.allowed_actions) - (CODE_ACTIONS | {Action.EVALUATE}):
        raise ContinuationRefusedError("draft_scope_unsupported", "This initial adapter accepts code delivery with evaluation deferred.")
    expected = promote_proposed_policy(proposal).execution_policy.model_dump(mode="json")
    expected["allowed_actions"] = [action for action in expected["allowed_actions"] if action in CODE_ACTIONS]
    expected["human_gates"] = [action for action in expected["human_gates"] if action in CODE_ACTIONS]
    expected = ExecutionPolicy.model_validate(expected)
    excluded = {"schema_version", "user_credentials", "principal_id", "policy_id", "policy_hash"}
    if expected.model_dump(mode="json", exclude=excluded) != policy.model_dump(mode="json", exclude=excluded):
        raise ContinuationRefusedError(
            "draft_policy_changed", "Initial acceptance must retain proposed limits, expiry, scope, code actions and evaluation requirements."
        )
    gate_address = acceptance_gate_address(proposal)
    actual = {f"{proposal.flow_slug}/{'/'.join(node['address'])}": node for node in snapshot["nodes"]}
    if (
        set(actual) != {node.address for node in proposal.nodes}
        or any(
            actual[node.address]["kind"] != node.kind
            or actual[node.address]["title"] != node.title
            or actual[node.address]["issue_ref"] != node.issue_ref
            for node in proposal.nodes
        )
        or {tuple(edge) for edge in snapshot["edges"]}
        != {(actual[edge.from_address]["id"], actual[edge.to_address]["id"]) for edge in proposal.edges}
    ):
        raise ContinuationRefusedError("draft_graph_changed", "The live graph must match the exact draft being accepted.")
    gate = actual.get(gate_address)
    if gate is None or gate["kind"] != "gate" or gate["state"] != "awaiting_gate":
        raise ContinuationRefusedError("initial_gate_unverifiable", "The draft's sole structural initial acceptance gate must be awaiting approval.")
    return {
        "gate_node_id": gate["id"],
        "gate_address": gate_address,
        "draft_plan_version": plan.version,
        "draft_plan_hash": plan.plan_hash,
        "draft_decision_id": decision["id"],
        "deferred_actions": [action.value for action in proposed.allowed_actions if action not in CODE_ACTIONS],
    }


async def approve_initial_gate(session, *, actor, flow_id, acceptance):
    # Reuse the same guarded state transition and append-only gate decision as
    # the ordinary bound human gate endpoint. Its separate policy promotion must
    # not run: this transaction already accepted the explicitly bounded transport.
    from .adapters.github_comments import InputPath, _gate_transition, build_gate_decision
    from .repository import OrchestrationRepository

    rows, allowed, refusal = await _gate_transition(
        session,
        node_id=acceptance["gate_node_id"],
        org_id=actor.org_id,
        observed_state="awaiting_gate",
        approve=True,
        reason=actor.reason,
    )
    if not allowed or rows != 1:
        raise ContinuationRefusedError("initial_gate_changed", refusal or "The initial acceptance gate changed; preview again.")
    record = build_gate_decision(
        org_id=actor.org_id,
        flow_id=flow_id,
        node_id=acceptance["gate_node_id"],
        actor_id=actor.actor_id,
        actor_role=actor.actor_role,
        input_path=InputPath.DASHBOARD,
        approve=True,
        from_state="awaiting_gate",
        reason=actor.reason,
        bound_plan_hash=acceptance["draft_plan_hash"],
    )
    await OrchestrationRepository(session).append_decision(**record.to_append_kwargs())

"""Keep engine ownership until the verified merge's delivery lane has settled."""

from dataclasses import asdict

from sqlalchemy import select

from .execution_state import TERMINAL_EXECUTION_STATUSES, ExecutionIdentity
from .models import OrchestrationAction, OrchestrationExecution, OrchestrationNode


async def unfinished_delivery(session, claim):
    """A completed worker does not complete its engine's deployment or evaluation."""
    if claim.owner_kind != "engine_flow":
        return False
    from .deployment_authority import DELIVERY_PHASES
    from .merge_controller import MERGE_KIND, MergeReceipt
    from .pr_bindings import active_binding_for_node, binding_scope_matches

    rows = await session.execute(
        select(OrchestrationExecution, OrchestrationNode)
        .join(OrchestrationNode, OrchestrationNode.id == OrchestrationExecution.node_id)
        .where(
            OrchestrationExecution.org_id == claim.org_id,
            OrchestrationExecution.flow_id == claim.owner_ref,
            OrchestrationExecution.claim_id == claim.id,
            OrchestrationExecution.claim_generation == claim.generation,
            OrchestrationExecution.phase.in_([phase.value for phase in DELIVERY_PHASES]),
            OrchestrationExecution.status.notin_([status.value for status in TERMINAL_EXECUTION_STATUSES]),
            OrchestrationNode.org_id == claim.org_id,
            OrchestrationNode.flow_id == claim.owner_ref,
            OrchestrationNode.kind == "story",
            OrchestrationNode.state == "passed",
            OrchestrationNode.attempts == OrchestrationExecution.cycle,
        )
    )
    for execution, node in rows:
        binding = await active_binding_for_node(session, org_id=claim.org_id, node_id=node.id, attempt=node.attempts)
        if binding is None or not binding_scope_matches(binding, node) or binding.provider_repository_id != claim.provider_repository_id:
            continue
        try:
            if int(str(node.issue_ref).lstrip("#")) != claim.issue_number:
                continue
            identity = ExecutionIdentity(claim.org_id, node.id, execution.cycle, execution.accepted_plan_version, claim.id, claim.generation)
            actions = await session.scalars(
                select(OrchestrationAction).where(
                    OrchestrationAction.org_id == claim.org_id,
                    OrchestrationAction.execution_id == execution.id,
                    OrchestrationAction.kind == MERGE_KIND,
                    OrchestrationAction.status == "succeeded",
                )
            )
            for action in actions:
                receipt = MergeReceipt.model_validate((action.detail or {}).get("merge_receipt"))
                identity_matches = all(getattr(receipt, key) == value for key, value in asdict(identity).items())
                if not identity_matches and (claim.active_run_id or "").startswith("delivery-recovery:"):
                    from .delivery_recovery import verified_bridge

                    await verified_bridge(session, identity=identity, node=node, binding=binding, receipt=receipt)
                    identity_matches = True
                if (
                    identity_matches
                    and receipt.operation_key == action.operation_key
                    and receipt.execution_id == execution.id
                    and receipt.flow_id == node.flow_id
                    and receipt.repo == binding.repo
                    and receipt.pr_number == binding.pr_number
                    and receipt.provider_repository_id == binding.provider_repository_id
                    and receipt.provider_pr_node_id == binding.provider_pr_node_id
                    and receipt.reviewed_head_sha == binding.head_sha
                ):
                    return True
        except (ValueError, TypeError):
            continue
    return False

"""Read explicit owner acceptance without fabricating an engine merge action."""

import json
import re
from datetime import UTC

from sqlalchemy import select

from .execution_policy import ExecutionPolicy
from .models import OrchestrationDecision, OrchestrationExecution
from .repository_evaluation_provider import require
from .shared_window import policy_owner_matches


async def accepted_merge_source(session, *, parent, plan, binding, decision_id):
    decision = await session.get(OrchestrationDecision, decision_id, populate_existing=True)
    require(
        decision is not None
        and decision.org_id == parent.org_id
        and decision.flow_id == parent.flow_id
        and decision.node_id == parent.id
        and decision.kind == "result_observed"
        and decision.actor_kind == "human"
        and decision.actor_role == "platform_admin"
        and decision.to_state == "passed"
        and parent.state == "passed",
        "owner_merge_acceptance_scope_changed",
    )
    policy = ExecutionPolicy.model_validate(plan.plan_document["execution_policy"])
    require(await policy_owner_matches(session, decision.actor_id, policy), "owner_merge_acceptance_principal_changed")
    try:
        data = json.loads(decision.reason or "{}")
    except (ValueError, TypeError):
        data = None
    require(
        isinstance(data, dict)
        and data.get("mode") == "owner_acceptance_of_existing_merge"
        and data.get("repo") == binding.repo
        and type(data.get("pr_number")) is int
        and data["pr_number"] == binding.pr_number
        and data.get("head_sha") == binding.head_sha
        and isinstance(data.get("merge_sha"), str)
        and re.fullmatch(r"[a-f0-9]{40}", data["merge_sha"]),
        "owner_merge_acceptance_binding_changed",
    )
    executions = list(
        await session.scalars(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == parent.org_id,
                OrchestrationExecution.flow_id == parent.flow_id,
                OrchestrationExecution.node_id == parent.id,
                OrchestrationExecution.cycle == parent.attempts,
            )
            .limit(2)
        )
    )
    require(len(executions) == 1, "owner_merge_acceptance_execution_ambiguous")
    execution = executions[0]
    require(
        execution.phase == "concluded"
        and execution.status == "concluded"
        and execution.pending_action_key is None
        and execution.accepted_plan_version <= plan.version
        and decision.created_at.replace(tzinfo=UTC) >= execution.created_at.replace(tzinfo=UTC),
        "owner_merge_acceptance_execution_changed",
    )
    # Preserve real provenance. No synthetic merge action/reviewer receipt is
    # created; provider reads still independently verify this exact head.
    return dict(
        node_id=parent.id,
        attempt=parent.attempts,
        accepted_plan_version=execution.accepted_plan_version,
        execution_id=execution.id,
        binding_id=binding.id,
        binding_revision=binding.revision,
        pr_number=binding.pr_number,
        head_sha=binding.head_sha,
        merge_sha=data["merge_sha"],
        provider_pr_node_id=binding.provider_pr_node_id,
        accepted_merge_decision_id=decision.id,
    )

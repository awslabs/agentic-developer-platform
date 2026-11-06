"""Historical proof for a released retained-work reviewer, never live ownership.

The ordinary continuation validator intentionally still requires a held claim.
This closed proof is used only by the explicit owner recovery in #8142/#8063.
"""

import asyncio
import json
import os
from datetime import UTC, datetime

from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.grants import AUTHORITY_GATE_DECISION, AgentAction

from .continuation import digest
from .execution_state import BlockCode
from .models import OrchestrationAction, OrchestrationDecision
from .protected_review_recovery import require
from .review_cycle import DISPATCH_KIND, CycleBlockedError


def object_value(value):
    require(isinstance(value, dict), "released_reviewer_receipt_malformed")
    return value


def receipt_data(row):
    try:
        return object_value(json.loads(row.reason)) if row else {}
    except (TypeError, ValueError):
        raise CycleBlockedError("released_reviewer_receipt_malformed") from None


async def committed_dispatch(session, *, identity, execution, node, binding, action, generation):
    from .review_cycle_dispatch import ACTOR, continuation_run_id, receipt_id

    row = await session.get(OrchestrationDecision, receipt_id(action.operation_key), populate_existing=True)
    data = receipt_data(row)
    object_value(action.detail)
    run_id = continuation_run_id(action.operation_key)
    envelope = object_value(data.get("envelope", {}))
    expected = object_value(envelope.get("review_expect", {}))
    source = object_value(envelope.get("source_ref", {}))
    metadata = object_value(data.get("execution_metadata", {}))
    require(
        row is not None
        and row.org_id == identity.org_id
        and row.flow_id == execution.flow_id
        and row.node_id == identity.node_id
        and row.actor_kind == "service"
        and row.actor_id == ACTOR
        and row.kind == "agent_dispatched"
        and action.org_id == identity.org_id
        and action.execution_id == execution.id
        and action.kind == DISPATCH_KIND
        and action.status == "succeeded"
        and action.detail.get("action") in {"review", "repair"}
        and all(
            data.get(k) == v
            for k, v in dict(
                operation_key=action.operation_key,
                run_id=run_id,
                execution_id=execution.id,
                accepted_plan_version=identity.accepted_plan_version,
                claim_generation=generation,
                action=action.detail.get("action"),
            ).items()
        )
        and all(
            expected.get(k) == v
            for k, v in dict(
                org_id=identity.org_id,
                flow_id=execution.flow_id,
                node_id=node.id,
                cycle=identity.cycle,
                accepted_plan_version=identity.accepted_plan_version,
                claim_id=identity.claim_id,
                claim_generation=generation,
                execution_id=execution.id,
                author_run_id=action.detail.get("author_run_id"),
                expected_head_sha=action.detail.get("head_sha"),
                repo=binding.repo,
                pr_number=binding.pr_number,
                provider_repository_id=binding.provider_repository_id,
                provider_pr_node_id=binding.provider_pr_node_id,
            ).items()
        )
        and action.detail.get("binding_id") == binding.id
        and action.detail.get("binding_revision") == binding.revision
        and action.detail.get("accepted_scope") == binding.accepted_scope
        and envelope.get("message_id") == run_id
        and envelope.get("persona") == "agent-codex-reviewer"
        and source.get("repo") == binding.repo
        and source.get("issue") == int(str(node.issue_ref).lstrip("#"))
        and source.get("provider_repository_id") == binding.provider_repository_id
        and metadata.get("orchestration_node_id") == {"S": node.id}
        and metadata.get("orchestration_node_attempt") == {"N": str(identity.cycle)}
        and metadata.get("orchestration_continuation_receipt") == {"S": row.id}
        and metadata.get("orchestration_continuation_action") == {"S": action.detail.get("action")},
        "released_reviewer_dispatch_changed",
    )
    return row, data


def action_snapshot(action):
    return dict(
        id=action.id,
        operation_key=action.operation_key,
        kind=action.kind,
        status=action.status,
        detail=action.detail,
        receipt_ref=action.receipt_ref,
        artifact_ref=action.artifact_ref,
    )


async def historical_authority(session, *, services, context, node, binding, run_id, generation, recovery_id=None, history_action_ids=None):
    """Recheck real dispatch, terminal workload, grants and retained provenance."""
    from . import retained_work_recovery as retained
    from .plan_lineage import approval_authorizes_node
    from .review_cycle_dispatch import continuation_run_id, failed_review
    from .unstarted_review import retired_unstarted_dispatch

    require(os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true", "protected_authority_required")
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(OrchestrationAction.org_id == node.org_id, OrchestrationAction.execution_id == context.execution.id)
            .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
            .execution_options(populate_existing=True)
            .limit(101)
        )
    )
    require(len(actions) <= 100, "review_history_limit")
    if recovery_id:
        require(
            isinstance(history_action_ids, list)
            and history_action_ids
            and all(isinstance(value, str) for value in history_action_ids)
            and len(history_action_ids) == len(set(history_action_ids)),
            "recovery_receipt_changed",
        )
    # An accepted recovery may have exactly its own pending successor. Everything
    # preceding it remains immutable history; no failed/unknown effect is guessed.
    prior, successors = [], []
    for action in actions:
        object_value(action.detail)
        if recovery_id and action.kind == DISPATCH_KIND and action.detail.get("developer_recovery_decision_id") == recovery_id:
            successors.append(action)
        elif recovery_id and action.id not in history_action_ids:
            # The runner may acknowledge a block notification after acceptance
            # (notably at expiry). It is not part of the pinned prior review.
            # No new dispatch, review evidence or unresolved effect is exempt.
            code = action.detail.get("block_code")
            prefix = f"notify:block:{code}:{context.identity.cycle}:revision-"
            revision = action.operation_key.removeprefix(prefix)
            require(
                action.kind == "notify_execution_block"
                and action.status == "succeeded"
                and code in {block.value for block in BlockCode}
                and action.operation_key.startswith(prefix)
                and revision.isascii()
                and revision.isdecimal()
                and len(revision) <= 10
                and 0 < int(revision) <= context.execution.revision
                and action.receipt_ref,
                "pending_effect_requires_reconciliation",
            )
        else:
            require(action.status == "succeeded" and not retired_unstarted_dispatch(action), "pending_effect_requires_reconciliation")
            prior.append(action)
    require(len(successors) <= 1, "released_reviewer_successor_changed")
    if recovery_id:
        require({a.id for a in prior} == set(history_action_ids), "recovery_receipt_changed")
    dispatches = [a for a in prior if a.kind == DISPATCH_KIND]
    require(dispatches and continuation_run_id(dispatches[-1].operation_key) == run_id, "released_reviewer_not_current")
    latest = dispatches[-1]
    row, saved = await committed_dispatch(
        session, identity=context.identity, execution=context.execution, node=node, binding=binding, action=latest, generation=generation
    )
    raw = await services.protected(node.org_id, run_id)
    try:
        require(
            failed_review(raw)
            and raw.get("invocation_id") == {"S": run_id}
            and raw.get("tenant_id") == {"S": node.org_id}
            and raw.get("workload_binding", {}).get("S")
            and raw.get("repo") == {"S": binding.repo}
            and raw.get("envelope_digest") == {"S": envelope_digest(saved["envelope"])}
            and all(raw.get(k) == v for k, v in saved["execution_metadata"].items())
            and raw.get("parent_principal") == {"S": saved["parent_principal"]}
            and raw.get("flow_id") == {"S": saved["envelope"]["correlation"]["correlation_id"]},
            "released_reviewer_exit_unverified",
        )
        grant = await asyncio.to_thread(
            services.writer.store.live_grant,
            invocation_id=run_id,
            tenant_id=node.org_id,
            attempt=int(raw["current_attempt"]["N"]),
            now=datetime.now(UTC),
        )
        await services.retry_parent(node.org_id, raw, grant)
        require(
            grant.flow_id == node.flow_id
            and grant.authority.kind == AUTHORITY_GATE_DECISION
            and grant.authority.reference_id == saved["authority_reference_id"]
            and binding.repo in grant.repo_scope
            and AgentAction.MONITOR in grant.allowed_actions
            and await approval_authorizes_node(
                session, org_id=node.org_id, flow_id=node.flow_id, node_id=node.id, decision_id=grant.authority.reference_id
            ),
            "released_reviewer_grant_changed",
        )
    except (BootstrapRefusedError, KeyError, TypeError, ValueError, AttributeError):
        raise CycleBlockedError("released_reviewer_exit_unverified") from None
    # This exceptional recovery is deliberately limited to the retained-work
    # contract which lacked a developer handoff. A terminal reviewer is never
    # reinterpreted as a successful developer or as authority for new repairs.
    source_id = saved.get("developer_recovery_decision_id")
    source = await session.get(OrchestrationDecision, source_id) if source_id else None
    provenance = receipt_data(source)
    require(
        source is not None
        and source.org_id == node.org_id
        and source.flow_id == node.flow_id
        and source.node_id == node.id
        and source.kind == retained.KIND
        and source.actor_kind == "human"
        and source.actor_role in {"owner", "org_admin", "platform_admin"}
        and provenance.get("contract") == retained.CONTRACT
        and provenance.get("execution_id") == context.execution.id
        and provenance.get("claim_id") == context.identity.claim_id
        and provenance.get("claim_generation") == generation
        and provenance.get("attempt") == node.attempts
        and isinstance(provenance.get("source_run_id"), str)
        and latest.detail.get("developer_recovery_decision_id") == source.id
        and latest.detail.get("retained_work_contract") == retained.CONTRACT
        and object_value(saved["envelope"].get("review_cycle_input", {})).get("allow_story_repairs") is False,
        "released_reviewer_provenance_changed",
    )
    _, _, lineage = await retained.sources(session, services=services, node=node, binding=binding, source_run_id=provenance["source_run_id"])
    require(all(provenance.get(k) == v for k, v in lineage.items()), "released_reviewer_provenance_changed")
    require(raw.get("flow_id") == {"S": lineage["correlation_id"]}, "released_reviewer_provenance_changed")
    evidence = dict(
        prior_run_id=run_id,
        prior_action_id=latest.id,
        prior_operation_key=latest.operation_key,
        prior_dispatch_receipt_id=row.id,
        prior_dispatch_digest=digest(saved),
        terminal_digest=digest(raw),
        grant_digest=digest(services.writer.store._grant_item(grant)),
        source_recovery_id=source.id,
        source_recovery_digest=digest(dict(reason=provenance, actor_kind=source.actor_kind, actor_id=source.actor_id, actor_role=source.actor_role)),
        source_actor_id=source.actor_id,
        source_run_id=lineage["source_run_id"],
        correlation_id=lineage["correlation_id"],
        history_digest=digest([action_snapshot(a) for a in prior]),
        history_action_ids=[a.id for a in prior],
        author_run_id=latest.detail["author_run_id"],
    )
    for action in reversed(prior):
        if action.kind != "review_evidence":
            continue
        try:
            review = object_value(json.loads(action.detail.get("cycle_input", "{}")))
        except (TypeError, ValueError):
            raise CycleBlockedError("released_reviewer_receipt_malformed") from None
        if review.get("reviewer_run_id") == run_id and review.get("author_run_id") == evidence["author_run_id"]:
            evidence.update(findings=review.get("findings", []), review_artifact=action.artifact_ref)
            break
    return raw, grant, evidence, prior

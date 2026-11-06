"""Correction lineage in existing K1 actions and append-only graph decisions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .evaluation_plan import identity_for, require
from .execution_state import OutcomeKind
from .execution_store import load_execution
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode

CORRECTION_KIND = "evaluation_correction_issue"
ACTOR = "system:evaluation-corrections"


def bounded_correction_summary(detail):
    """Public correction/retest progress, with no authority or accepted scope."""
    if not isinstance(detail, dict):
        return None
    try:
        cycle = detail["evaluation_cycle"]
        remaining = detail["remaining_corrections"]
        issue = (detail.get("issue") or {}).get("number")
        child = detail.get("child_node_id")
        retest = detail.get("retest_cycle")
        if (
            type(cycle) is not int
            or cycle < 1
            or type(remaining) is not int
            or remaining < 0
            or (issue is not None and (type(issue) is not int or issue < 1))
            or (child is not None and (not isinstance(child, str) or not re.fullmatch(r"[0-9a-f-]{36}", child)))
            or (retest is not None and (type(retest) is not int or retest != cycle + 1))
        ):
            return None
        return dict(
            evaluation_cycle=cycle,
            remaining_corrections=remaining,
            issue_number=issue,
            child_node_id=child,
            retest_cycle=retest,
            stage="retest_requested"
            if retest
            else "delivery_pending"
            if child
            else "creation_unresolved"
            if detail.get("creation_started")
            else "creation_pending",
        )
    except (KeyError, TypeError, ValueError):
        return None


def source_authorization_digest(source):
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def accepted_source_authorization(*, sources, flow, plan):
    """Freeze one server-resolved affected story; never choose a harness or an ambiguous source."""
    from .dispatch import graph_address
    from .pr_bindings import binding_scope_matches

    require(len(sources) == 1, "evaluation_correction_affected_source_ambiguous")
    source, binding = sources[0]
    address = graph_address(source, flow_slug=flow.slug)
    rows = [row for row in (plan.plan_document or {}).get("nodes", []) if row.get("address") == address]
    require(
        source.kind == "story"
        and source.org_id == flow.org_id
        and source.flow_id == flow.id
        and source.issue_ref is not None
        and source.issue_ref.isdecimal()
        and flow.product_id == plan.plan_document.get("product_id")
        and len(rows) <= 1
        and (flow.product_id is None or len(rows) == 1)
        and all(row.get(key) == getattr(source, key) for row in rows for key in ("kind", "title", "issue_ref"))
        and binding.org_id == source.org_id
        and binding.node_id == source.id
        and binding_scope_matches(binding, source)
        and (source.repository_identity is None or source.repository_identity == binding.repo)
        and (source.provider_repository_id is None or source.provider_repository_id == binding.provider_repository_id),
        "evaluation_correction_affected_source_changed",
    )
    return dict(
        org_id=source.org_id,
        flow_id=source.flow_id,
        product_id=flow.product_id,
        node_id=source.id,
        address=address,
        issue_ref=source.issue_ref,
        repository=binding.repo,
        provider_repository_id=binding.provider_repository_id,
        installation_id=binding.installation_id,
        binding_id=binding.id,
        binding_revision=binding.revision,
        accepted_scope=binding.accepted_scope,
        accepted_plan_version=plan.version,
        accepted_plan_hash=plan.plan_hash,
    )


def child_id(operation_key):
    return str(uuid5(NAMESPACE_URL, "adp:evaluation-correction:" + operation_key))


def link_id(node_id):
    return str(uuid5(NAMESPACE_URL, "adp:evaluation-correction-link:" + node_id))


async def correction_link(session, node):
    """A display name never grants repair authority; require the immutable link."""
    link = await session.scalar(
        select(OrchestrationDecision).where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.id == link_id(node.id),
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.flow_id == node.flow_id,
        )
    )
    if link is None:
        return None
    require(link.actor_id == ACTOR and link.actor_kind == "service" and link.kind == "result_observed", "evaluation_correction_link_invalid")
    data = json.loads(link.reason)
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == data["parent_execution_id"],
            OrchestrationAction.operation_key == data["operation_key"],
            OrchestrationAction.kind == CORRECTION_KIND,
            OrchestrationAction.status == "succeeded",
        )
    )
    require(action is not None and (action.detail or {}).get("child_node_id") == node.id, "evaluation_correction_action_missing")
    detail = action.detail
    source = detail.get("source_authorization")
    if source is not None or data.get("source_authorization_digest") is not None:
        require(
            isinstance(source, dict)
            and detail.get("source_authorization_digest") == data.get("source_authorization_digest") == source_authorization_digest(source)
            and source.get("org_id") == node.org_id
            and source.get("flow_id") == node.flow_id
            and source.get("repository") == detail.get("repo")
            and source.get("provider_repository_id") == detail.get("provider_repository_id") == detail["issue"]["repository_id"]
            and source.get("installation_id") == detail.get("installation_id")
            and source.get("accepted_scope") == detail.get("source_scope"),
            "evaluation_correction_affected_source_changed",
        )
        require(
            node.repository_identity == source["repository"]
            and node.provider_repository_id == source["provider_repository_id"]
            and node.repository_connection == {"provider": "github", "installation_id": source["installation_id"]},
            "evaluation_correction_affected_source_changed",
        )
    require(
        node.id == child_id(action.operation_key)
        and node.kind == "story"
        and node.issue_ref == str(detail["issue"]["number"])
        and node.title == detail["content"]["title"]
        and data["parent_node_id"] == detail["parent_node_id"],
        "evaluation_correction_scope_changed",
    )
    return action


async def validate_correction(session, node, *, allow_accepted=False):
    """Every correction effect stays beneath a current, unresolved evaluation."""
    action = await correction_link(session, node)
    if action is None:
        return None
    detail = action.detail
    source_authorization = detail.get("source_authorization")
    if source_authorization is not None:
        flow = await session.get(OrchestrationFlow, node.flow_id)
        source = await session.get(OrchestrationNode, source_authorization["node_id"])
        require(
            flow is not None
            and flow.org_id == node.org_id
            and flow.product_id == source_authorization["product_id"]
            and source is not None
            and source.org_id == node.org_id
            and source.flow_id == node.flow_id
            and source.kind == "story"
            and source.issue_ref == source_authorization["issue_ref"]
            and (source.repository_identity is None or source.repository_identity == source_authorization["repository"]),
            "evaluation_correction_affected_source_changed",
        )
    parent = await session.scalar(
        select(OrchestrationNode)
        .where(
            OrchestrationNode.org_id == node.org_id,
            OrchestrationNode.id == detail["parent_node_id"],
            OrchestrationNode.flow_id == node.flow_id,
        )
        .execution_options(populate_existing=True)
    )
    record = await session.scalar(
        select(OrchestrationExecution).where(
            OrchestrationExecution.org_id == node.org_id,
            OrchestrationExecution.id == action.execution_id,
            OrchestrationExecution.node_id == detail["parent_node_id"],
            OrchestrationExecution.flow_id == node.flow_id,
        )
    )
    require(
        parent is not None
        and parent.kind == "eval"
        and parent.state in ({"running", "passed"} if allow_accepted else {"running"})
        and record is not None,
        "evaluation_correction_parent_not_active",
    )
    loaded = await load_execution(session, identity=identity_for(record))
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED, "evaluation_correction_parent_authority_changed")
    require(record.cycle == detail["evaluation_cycle"] and parent.attempts in {record.cycle, record.cycle + 1}, "evaluation_correction_cycle_changed")
    if parent.attempts == record.cycle + 1:
        from .evaluation_plan import accepted_evaluation

        accepted = await accepted_evaluation(session, parent)
        require(accepted is not None, "evaluation_correction_retest_changed")
        context_kind = "multi_repository_context" if accepted[1].evidence_schema == "multi-repository-evaluation/v1" else "evaluation_context"
        retest = await session.scalar(
            select(OrchestrationAction)
            .join(
                OrchestrationExecution,
                OrchestrationExecution.id == OrchestrationAction.execution_id,
            )
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationExecution.org_id == node.org_id,
                OrchestrationExecution.node_id == parent.id,
                OrchestrationExecution.cycle == parent.attempts,
                OrchestrationExecution.accepted_plan_version == record.accepted_plan_version,
                OrchestrationAction.kind == context_kind,
                OrchestrationAction.status == "succeeded",
            )
        )
        require(retest is not None and retest.detail.get("correction_operation_key") == action.operation_key, "evaluation_correction_retest_changed")
    return action


async def create_correction_child(session, context, action, issue):
    """One genuine story child, no new approval or dependency edge."""
    identifier = child_id(action.operation_key)
    existing = await session.get(OrchestrationNode, identifier)
    if existing is not None:
        require(existing.org_id == context.identity.org_id, "evaluation_correction_child_conflict")
        await validate_correction(session, existing)
        return existing
    require(issue.state == "open", "evaluation_correction_human_refusal")
    parent = await session.get(OrchestrationNode, context.identity.node_id, populate_existing=True)
    require(parent is not None and parent.state == "running" and parent.attempts == context.identity.cycle, "evaluation_correction_parent_changed")
    detail = action.detail
    source = detail.get("source_authorization")
    if source is not None:
        require(
            issue.repository_id == source["provider_repository_id"]
            and issue.url == f"https://github.com/{source['repository']}/issues/{issue.number}",
            "evaluation_correction_affected_source_changed",
        )
    node = OrchestrationNode(
        id=identifier,
        org_id=parent.org_id,
        flow_id=parent.flow_id,
        epic_ref=parent.epic_ref,
        wave_ref=parent.wave_ref,
        node_ref=f"{parent.node_ref[:40]}-fix-{context.identity.cycle}",
        kind="story",
        state="ready",
        attempts=0,
        title=detail["content"]["title"],
        issue_ref=str(issue.number),
        repository_identity=source["repository"] if source is not None else None,
        provider_repository_id=source["provider_repository_id"] if source is not None else None,
        repository_connection={"provider": "github", "installation_id": source["installation_id"]} if source is not None else None,
    )
    session.add(node)
    await session.flush()
    from dataclasses import asdict

    action.detail = {**detail, "child_node_id": identifier, "issue": asdict(issue)}
    session.add(
        OrchestrationDecision(
            id=link_id(identifier),
            org_id=parent.org_id,
            flow_id=parent.flow_id,
            node_id=identifier,
            kind="result_observed",
            actor_id=ACTOR,
            actor_kind="service",
            actor_role="engine",
            to_state="ready",
            reason=json.dumps(
                dict(
                    parent_node_id=parent.id,
                    parent_execution_id=context.execution.id,
                    operation_key=action.operation_key,
                    **({"source_authorization_digest": detail["source_authorization_digest"]} if source is not None else {}),
                    created_at=datetime.now(UTC).isoformat(),
                ),
                sort_keys=True,
            ),
        )
    )
    await session.flush()
    return node


async def retest_deployment(session, node, request, plan_version):
    from .evaluation_plan import current_deployment

    detail = request.detail
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == detail["correction_parent_execution_id"],
            OrchestrationAction.operation_key == detail["correction_operation_key"],
            OrchestrationAction.kind == CORRECTION_KIND,
            OrchestrationAction.status == "succeeded",
        )
    )
    require(
        action is not None and action.detail["parent_node_id"] == node.id and action.detail["evaluation_cycle"] == node.attempts - 1,
        "evaluation_retest_lineage_changed",
    )
    child = await session.scalar(
        select(OrchestrationNode).where(
            OrchestrationNode.org_id == node.org_id,
            OrchestrationNode.flow_id == node.flow_id,
            OrchestrationNode.id == action.detail["child_node_id"],
        )
    )
    require(child is not None and child.state == "passed", "evaluation_retest_child_not_complete")
    verified = await validate_correction(session, child, allow_accepted=True)
    require(verified is not None and verified.id == action.id, "evaluation_retest_child_changed")
    if request.kind == "multi_repository_context":
        from .evaluation_plan import accepted_evaluation
        from .pr_bindings import active_binding_for_node

        accepted = await accepted_evaluation(session, node)
        source = await session.get(OrchestrationNode, action.detail["source_authorization"]["node_id"])
        flow = await session.get(OrchestrationFlow, node.flow_id)
        require(
            accepted is not None and accepted[0].version == plan_version and source is not None and flow is not None,
            "evaluation_retest_source_changed",
        )
        binding = await active_binding_for_node(session, org_id=node.org_id, node_id=source.id, attempt=source.attempts)
        require(
            binding is not None
            and accepted_source_authorization(sources=[(source, binding)], flow=flow, plan=accepted[0]) == action.detail["source_authorization"],
            "evaluation_retest_source_changed",
        )
    deployed = await current_deployment(session, child, plan_version, now=datetime.now(UTC))
    require(deployed is not None and deployed[2].operation_key == detail["correction_deployment_key"], "evaluation_retest_deployment_changed")
    return deployed

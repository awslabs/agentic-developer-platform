"""Use an agent's accepted runtime release as historical source lineage.

This reads existing completion evidence. It neither deploys nor substitutes an
operations release for a build merge, and it does not claim current runtime health.
"""

import hashlib
import json
from datetime import UTC

from pydantic import ValidationError
from sqlalchemy import select

from .accepted_node import accepted_node_contract
from .compile import address_of
from .delivery_contract import receipt_matches_contract, release_manifest
from .deployment_manifest import EntryStatus, ManifestError, load_packaged_manifest
from .deployment_runtime_contract import DeploymentReceipt
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode
from .operations_continuation_authority import assignment_matches
from .operations_release_resolution import ReleaseSource
from .operations_workflow_evidence import CorrelatedWorkflowReceipt, context_receipt_ref
from .plan_lineage import ancestor_plan
from .review_cycle import CycleBlockedError
from .run_reports import OrchestrationRunReport


def require(condition):
    if not condition:
        raise CycleBlockedError("operations_release_predecessor_unverified")


def _document(raw):
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


async def verified_operations_release_predecessor(
    session, node, contract, address, *, repository_id=None, installation_id=None, plan=None, flow=None, manifest_loader=None
):
    release = contract.node.operational_release_binding
    if plan is None or flow is None:
        from .repository import OrchestrationRepository

        repo = OrchestrationRepository(session)
        plan = await repo.get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
        flow = await repo.get_flow(org_id=node.org_id, flow_id=node.flow_id)
    require(plan is not None and flow is not None and flow.org_id == node.org_id and plan.version == contract.plan_version)
    parents = list(
        await session.scalars(
            select(OrchestrationNode).where(
                OrchestrationNode.org_id == node.org_id,
                OrchestrationNode.flow_id == node.flow_id,
                OrchestrationNode.node_ref == address.rsplit("/", 1)[-1],
            )
        )
    )
    parents = [parent for parent in parents if address_of(flow.slug, parent) == address]
    require(len(parents) == 1 and parents[0].state == "passed")
    parent = parents[0]
    accepted = await accepted_node_contract(session, parent)
    source_release = accepted.node.operational_release_binding if accepted is not None else None
    require(source_release is not None and source_release.repository == release.repository)
    try:
        # A rendered child contract contains only that child's target. Resolve
        # the predecessor against its own contract or the legacy manifest.
        entry = release_manifest(source_release, manifest_loader or load_packaged_manifest).entry(source_release.target)
    except ManifestError:
        raise CycleBlockedError("operations_release_manifest_unavailable") from None
    require(
        entry is not None
        and entry.status is EntryStatus.ENABLED
        and not entry.docs_only
        and entry.covers(source_release.component)
        and entry.workflow is not None
        and source_release.workflows == [entry.workflow.path.removeprefix(".github/workflows/")]
    )
    require(
        accepted is not None
        and accepted.plan_version == plan.version
        and accepted.node.kind == "story"
        and accepted.node.executor is not None
        and accepted.node.executor.role == "operate"
        and accepted.node.repository_identity == release.repository
        and entry.connection_id in accepted.environment_connection_ids
        and accepted.node.evidence_expectations == [f"runtime_verified:{source_release.component}"]
        and {
            item.source.removeprefix("deployment_workflow:")
            for item in accepted.node.required_inputs
            if item.source.startswith("deployment_workflow:")
        }
        == set(source_release.workflows)
    )
    from .external_completion import ExternalCompletionError, external_release_source

    try:
        external = await external_release_source(session, parent, accepted, plan, repository_id=repository_id, installation_id=installation_id)
    except (ExternalCompletionError, KeyError, TypeError, ValueError):
        raise CycleBlockedError("operations_release_predecessor_unverified") from None
    if external is not None:
        return external
    executions = list(
        await session.scalars(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == node.org_id,
                OrchestrationExecution.flow_id == node.flow_id,
                OrchestrationExecution.node_id == parent.id,
                OrchestrationExecution.cycle == parent.attempts,
            )
            .limit(2)
        )
    )
    require(len(executions) == 1)
    execution = executions[0]
    require(
        execution.status == execution.phase == "concluded"
        and execution.pending_action_key is None
        and await ancestor_plan(session, plan, execution.accepted_plan_version, node_id=parent.id) is not None
    )
    reports = list(
        await session.scalars(
            select(OrchestrationRunReport)
            .where(
                OrchestrationRunReport.org_id == node.org_id,
                OrchestrationRunReport.flow_id == node.flow_id,
                OrchestrationRunReport.node_id == parent.id,
                OrchestrationRunReport.attempt == parent.attempts,
            )
            .limit(2)
        )
    )
    require(len(reports) == 1)
    report = reports[0]
    terminal, bounded = report.terminal_receipt or {}, report.operations_report_receipt or {}
    require(
        report.repo == release.repository
        and (installation_id is None or report.installation_id == installation_id)
        and (repository_id is None or report.provider_repository_id == repository_id)
        and report.persona == accepted.node.executor.persona
        and terminal.get("outcome") == "complete"
        and terminal.get("run_id") == report.run_id
        and terminal.get("attempt") == parent.attempts
        and report.dispatch_metadata.get("orchestration", {}).get("executor_role") == "operate"
        and await assignment_matches(session, node=parent, execution=execution, report=report)
        and bounded.get("run_id") == report.run_id
        and bounded.get("attempt") == parent.attempts
        and bounded.get("node_id") == parent.id
        and bounded.get("recorded") is True
        and bounded.get("receipt_id") == f"operations-report:{report.run_id}"
        and isinstance(bounded.get("report"), str)
        and 0 < len(bounded["report"].encode()) <= 12000
        and bounded.get("sha256") == hashlib.sha256(bounded["report"].encode()).hexdigest()
    )
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == execution.id,
            )
            .limit(501)
        )
    )
    require(len(actions) <= 500 and not any(action.status in {"prepared", "dispatched", "unknown"} for action in actions))
    by_key = {action.operation_key: action for action in actions if action.status == "succeeded"}
    decisions = list(
        await session.scalars(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == node.org_id,
                OrchestrationDecision.flow_id == node.flow_id,
                OrchestrationDecision.node_id == parent.id,
                OrchestrationDecision.kind == "result_observed",
                OrchestrationDecision.actor_id == "system:orchestration-results",
                OrchestrationDecision.actor_kind == "service",
                OrchestrationDecision.to_state == "passed",
            )
            .limit(101)
        )
    )
    require(len(decisions) <= 100)
    identity = dict(
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=parent.id,
        execution_id=execution.id,
        cycle=parent.attempts,
        accepted_plan_version=execution.accepted_plan_version,
        claim_id=execution.claim_id,
        claim_generation=execution.claim_generation,
        repo=release.repository,
    )
    verified = []
    for action in by_key.values():
        if action.kind != "deployment_verification" or not action.operation_key.startswith("operations-runtime:"):
            continue
        try:
            receipt = DeploymentReceipt.model_validate((action.detail or {}).get("deployment_receipt"))
        except ValidationError:
            continue
        suffix = action.operation_key.removeprefix("operations-runtime:")
        workflow = by_key.get(f"operations-workflow:{suffix}")
        context_action = by_key.get(f"correlated-workflow:{suffix}")
        if workflow is None or context_action is None:
            continue
        try:
            context = CorrelatedWorkflowReceipt.model_validate((context_action.detail or {}).get("correlated_workflow_receipt"))
        except ValidationError:
            continue
        if not (
            all(getattr(receipt, key) == value == getattr(context, key) for key, value in identity.items())
            and receipt.operation_key == action.operation_key == action.receipt_ref
            and receipt_matches_contract(receipt, source_release)
            and receipt.delivery_complete
            and not receipt.docs_only
            and receipt.source_revision == receipt.actual_revision == context.source_revision
            and receipt.merge_operation_key == f"operations-source:{receipt.source_revision}"
            and receipt.workflow_operation_keys == [workflow.operation_key]
            and receipt.manifest_entry_ids == [source_release.target]
            and len(receipt.targets) == 1
            and receipt.targets[0].get("resource_kind") == entry.resource_kind
            and receipt.targets[0].get("resource_id") == entry.resource_id
            and receipt.targets[0].get("evidence", {}).get("source") == f"registered-aws-role:{entry.connection_id}"
            and {item.component for item in receipt.components} == {source_release.component}
            and workflow.kind == "deployment_workflow"
            and (workflow.detail or {}).get("runtime_verified") is True
            and (workflow.detail or {}).get("context_action_id") == context_action.id
            and context_action.kind == "correlated_workflow_observation"
            and context.operation_key == context_action.operation_key == f"correlated-workflow:{context.run_id}:{context.run_attempt}"
            and context.component == source_release.component
            and context.workflow in source_release.workflows
            and workflow.receipt_ref
            == context_action.receipt_ref
            == context_receipt_ref(context.run_id, context.context_artifact_id, context.context_reference)
        ):
            continue
        for decision in decisions:
            detail = _document(decision.reason)
            at = decision.created_at.replace(tzinfo=UTC) if decision.created_at.tzinfo is None else decision.created_at
            ids = detail.get("evidence_ids")
            if not (
                detail.get("attempt") == parent.attempts
                and detail.get("run_id") == report.run_id
                and detail.get("conclusion_authority") == "evidence"
                and isinstance(ids, list)
                and all(item in ids for item in (str(action.id), str(context_action.id), bounded["receipt_id"]))
                and receipt.observed_at <= at < receipt.valid_until
                and context.observed_at <= at < context.valid_until
            ):
                continue
            verified.append(
                ReleaseSource(
                    receipt.actual_revision,
                    receipt.observed_at,
                    {
                        "address": address,
                        "receipt_kind": "verified_operations_release",
                        "action_id": action.id,
                        "operation_key": receipt.operation_key,
                        "execution_id": execution.id,
                        "claim_id": execution.claim_id,
                        "claim_generation": execution.claim_generation,
                        "source_revision": receipt.actual_revision,
                        "context_action_id": context_action.id,
                        "completion_decision_id": decision.id,
                    },
                )
            )
    require(verified and len({item.revision for item in verified}) == 1)
    return max(verified, key=lambda item: (item.observed_at, item.provenance["action_id"]))

"""Accepted runtime expectations resolved from engine observations, never worker prose."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select

from .accepted_node import accepted_node_contract, operational_revision_inputs
from .delivery_contract import receipt_matches_contract
from .deployment_manifest import EntryStatus, ManifestError, PhysicalTarget, TargetEvidence, load_packaged_manifest
from .deployment_runtime_contract import DeploymentReceipt
from .developer_personas import operations_executor_enabled
from .environment_leases import LeaseError, foundation_release_scope, shared_release_held
from .models import OrchestrationAction, OrchestrationExecution, OrchestrationFlow
from .operations_workflow_evidence import CorrelatedWorkflowReceipt, context_receipt_ref
from .run_reports import OrchestrationRunReport


async def operational_expectations(session, node, dispatch):
    if not operations_executor_enabled() or dispatch.get("conclusion_authority") != "evidence":
        return None
    contract = await accepted_node_contract(session, node)
    if contract is None or contract.node.executor is None or contract.node.executor.role != "operate":
        return None
    if any(not item.startswith("runtime_verified:") for item in contract.node.evidence_expectations):
        return None
    expectations = tuple(item.split(":", 1)[1] for item in contract.node.evidence_expectations if item.startswith("runtime_verified:"))
    if not expectations or contract.node.executor.persona not in {"operations", "agent-codex-operations"}:
        return None
    return contract, expectations


async def assignment_version_preserved(session, node, *, fenced, in_force) -> bool:
    """Whether the plan version a run was fenced to still describes this node (#7821).

    Production shape (adp-tools-platform X05b, 2026-10-15): the operations worker
    completed at 15:39Z, an amendment that only dropped *another* story landed at
    15:59:00Z, and the results pass read the run at 15:59:22Z — the fenced version
    no longer equalled the in-force one, so the story was parked under
    ``operations_assignment_changed`` with nothing that could ever clear it. The
    execution store already answers this with the plan lineage (#7535): an accepted
    human amendment that left the node's execution definition and incoming edges
    untouched preserves its authority. The same proof is reused here, so an
    amendment that *did* change the story is still refused.
    """
    if type(fenced) is not int or fenced < 1:
        return False
    if fenced == in_force:
        return True
    from .plan_lineage import ancestor_plan
    from .repository import OrchestrationRepository

    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    return plan is not None and await ancestor_plan(session, plan, fenced, node_id=node.id) is not None


async def foundation_receipt_held(session, *, node, execution, report, receipt, correlated_action, metadata, pins, native_release=None):
    flow = await session.get(OrchestrationFlow, node.flow_id)
    if flow is None or flow.org_id != node.org_id:
        return False
    recorded = metadata.get("release_binding") or {}
    try:
        entry = load_packaged_manifest().entry(recorded.get("target"))
    except ManifestError:
        return False
    if (
        entry is None
        or entry.status is not EntryStatus.ENABLED
        or entry.docs_only
        or entry.connection_id != recorded.get("connection_id")
        or (entry.resource_kind, entry.resource_id) != (recorded.get("resource_kind"), recorded.get("resource_id"))
        or entry.release_group is None
        or not entry.mutation_role_arn
        or entry.mutation_role_arn != recorded.get("mutation_role_arn")
        or entry.artifact_revision != recorded.get("manifest_artifact_revision")
        or entry.workflow is None
        or entry.workflow.definition_revision != recorded.get("manifest_definition_revision")
        or len(receipt.targets) != 1
        or len(receipt.components) != 1
    ):
        return False
    raw = receipt.targets[0]
    try:
        physical = PhysicalTarget(**{**raw, "evidence": TargetEvidence(**raw["evidence"])})
        saved = (correlated_action.detail or {})["correlated_workflow_receipt"]
        correlated = CorrelatedWorkflowReceipt.model_validate(saved)
        actions = (
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == execution.id,
                    OrchestrationAction.kind == "operations_native_release_dispatch",
                    OrchestrationAction.detail["correlation"].as_string() == report.run_id,
                )
                .limit(2)
            )
        ).all()
        if len(actions) != 1:
            return False
        action = actions[0]
        detail = action.detail or {}
        release = detail.get("shared_release") or {}
        reserved = release["target"]
        reserved_target = PhysicalTarget(**{**reserved, "evidence": TargetEvidence(**reserved["evidence"])})
        environment = release.get("environment")
        if environment not in entry.workflow.allowed_inputs.get("environment", ()):
            return False
        if receipt.components[0].component == "tool-adapters" and "tool-adapters" not in entry.workflow.allowed_inputs.get("stage", ()):
            return False
        shared, owner = foundation_release_scope(
            node=node,
            flow=flow,
            entry=entry,
            component=receipt.components[0].component,
            target=physical,
            environment=environment,
            source_revision=pins["source_revision"],
        )
    except (KeyError, TypeError, ValueError, ValidationError, LeaseError, ManifestError):
        return False
    held = bool(
        action.status in {"prepared", "succeeded", "unknown"}
        and detail.get("dispatch_started") is True
        and detail.get("repo") == receipt.repo
        and detail.get("provider_repository_id") == report.provider_repository_id
        and reserved_target.canonical_key == physical.canonical_key
        and reserved_target.evidence.source == physical.evidence.source == f"registered-aws-role:{entry.connection_id}"
        and correlated.head_sha is not None
        and detail.get("source_revision") == pins["source_revision"]
        and detail.get("dispatch_revision") == correlated.head_sha
        and detail.get("workflow") == correlated.workflow
        and detail.get("release_job") == {"run_id": str(correlated.run_id), "run_attempt": "1"}
        and correlated.event == "workflow_dispatch"
        and correlated.run_attempt == 1
        and release.get("owner") == owner
        and release.get("target_key") == shared.canonical_key
        and release.get("manifest_entry_id") == entry.entry_id
        and release.get("mutation_role_arn") == entry.mutation_role_arn
        and release.get("generation") == execution.claim_generation
        and await shared_release_held(
            session,
            target=shared,
            org_id=node.org_id,
            action_id=owner,
            release_ref=pins["source_revision"],
            manifest_entry_id=entry.entry_id,
            generation=execution.claim_generation,
            started_at=receipt.components[0].observed_at,
        )
    )
    if held and native_release is not None:
        native_release.update(action_id=action.id, operation_key=action.operation_key, detail=deepcopy(detail))
    return held


async def observed_operations_evidence(session, node, dispatch, *, expected=None, observation=None, controller=False):
    expected = expected or await operational_expectations(session, node, dispatch)
    if expected is None:
        return None, "evidence_missing"
    contract, components = expected
    report = await session.scalar(
        select(OrchestrationRunReport).where(OrchestrationRunReport.run_id == dispatch["run_id"]).execution_options(populate_existing=True)
    )
    if (
        report is None
        or report.org_id != node.org_id
        or report.node_id != node.id
        or report.attempt != node.attempts
        or dispatch.get("attempt") != node.attempts
    ):
        return None, "operations_report_missing"
    terminal = report.terminal_receipt or {}
    if (
        (not controller and terminal.get("outcome") != "complete")
        or terminal.get("run_id") != report.run_id
        or terminal.get("attempt") != report.attempt
    ):
        return None, "operations_report_missing"
    metadata = report.dispatch_metadata.get("orchestration") or {}
    fences = report.dispatch_metadata.get("handoff_expect") or {}
    assignment_version = fences.get("accepted_plan_version")
    from .operations_continuation_authority import assignment_matches, transfer

    if (
        report.persona != contract.node.executor.persona
        or metadata.get("executor_role") != "operate"
        or contract.node.repository_identity not in (None, report.dispatch_metadata.get("source_ref", {}).get("repo"))
        or not await assignment_version_preserved(session, node, fenced=assignment_version, in_force=contract.plan_version)
    ):
        return None, "operations_assignment_changed"
    # Everything the run wrote — its execution row, repair merges and receipts — is
    # keyed by the version it was dispatched under, which lineage has just verified
    # still describes this node (#7821).
    execution = await session.scalar(
        select(OrchestrationExecution).where(
            OrchestrationExecution.org_id == node.org_id,
            OrchestrationExecution.flow_id == node.flow_id,
            OrchestrationExecution.node_id == node.id,
            OrchestrationExecution.cycle == node.attempts,
            OrchestrationExecution.accepted_plan_version == assignment_version,
        )
    )
    if controller and (execution is None or await transfer(session, node=node, execution=execution, report=report) is None):
        return None, "operations_continuation_transfer_changed"
    if (
        execution is None
        or execution.id != fences.get("execution_id")
        or execution.status == "superseded"
        or (not controller and execution.status == "concluded")
        or not await assignment_matches(session, node=node, execution=execution, report=report)
    ):
        return None, "evidence_missing"
    bound_release = getattr(contract.node, "operational_release_binding", None) is not None
    if bound_release:
        from .operations_release_resolution import recorded_operations_release

        pins = await recorded_operations_release(session, node=node, execution=execution, contract=contract, metadata=metadata)
    else:
        pins = operational_revision_inputs(contract)
    if pins is None or metadata.get("deployment_revisions") != pins:
        return None, "operations_assignment_changed"
    from .operations_repair import repair_revisions

    pins = await repair_revisions(
        session, node_id=node.id, org_id=node.org_id, plan_version=assignment_version, cycle=node.attempts, pins=pins, contract=contract
    )
    if pins is None:
        return None, "operations_assignment_changed"
    actions = (
        await session.scalars(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == execution.id,
                OrchestrationAction.status == "succeeded",
            )
        )
    ).all()
    verified_workflows = {
        action.operation_key: action
        for action in actions
        if action.kind == "deployment_workflow" and (action.detail or {}).get("runtime_verified") is True
    }
    observed = set()
    evidence_ids = set()
    runtime_receipts = []
    native_release = {}
    for action in actions:
        raw = (action.detail or {}).get("deployment_receipt")
        if not isinstance(raw, dict):
            continue
        try:
            receipt = DeploymentReceipt.model_validate(raw)
        except ValidationError:
            continue
        if (
            not receipt_matches_contract(receipt, getattr(contract.node, "operational_release_binding", None))
            or action.kind != "deployment_verification"
            or action.receipt_ref != action.operation_key
            or receipt.org_id != node.org_id
            or receipt.flow_id != node.flow_id
            or receipt.node_id != node.id
            or receipt.cycle != node.attempts
            or receipt.execution_id != execution.id
            or receipt.accepted_plan_version != assignment_version
            or receipt.valid_until <= datetime.now(UTC)
            or receipt.claim_id != execution.claim_id
            or receipt.claim_generation != execution.claim_generation
            or contract.node.repository_identity not in (None, receipt.repo)
            or receipt.operation_key != action.operation_key
            or receipt.source_revision != pins["source_revision"]
            or receipt.actual_revision != pins["source_revision"]
            or receipt.delivery_complete is not True
            or not set(receipt.workflow_operation_keys).issubset(verified_workflows)
        ):
            continue
        if controller or bound_release or action.operation_key.startswith("operations-runtime:"):
            suffix = action.operation_key.removeprefix("operations-runtime:")
            if not action.operation_key.startswith("operations-runtime:") or receipt.workflow_operation_keys != [f"operations-workflow:{suffix}"]:
                continue
            runtime_receipts.append((receipt, action, f"correlated-workflow:{suffix}"))
        else:
            observed.update(item.component for item in receipt.components)
            evidence_ids.add(str(action.id))
    workflows = {
        item.source.removeprefix("deployment_workflow:") for item in contract.node.required_inputs if item.source.startswith("deployment_workflow:")
    }
    correlated_keys = {}
    for action in actions:
        if action.kind != "correlated_workflow_observation":
            continue
        raw = (action.detail or {}).get("correlated_workflow_receipt")
        if not isinstance(raw, dict):
            continue
        try:
            receipt = CorrelatedWorkflowReceipt.model_validate(raw)
        except ValidationError:
            continue
        if (
            receipt.org_id != node.org_id
            or receipt.flow_id != node.flow_id
            or receipt.node_id != node.id
            or receipt.execution_id != execution.id
            or receipt.cycle != node.attempts
            or receipt.accepted_plan_version != assignment_version
            or receipt.claim_id != execution.claim_id
            or receipt.claim_generation != execution.claim_generation
            or receipt.operation_key != action.operation_key
            or action.operation_key != f"correlated-workflow:{receipt.run_id}:{receipt.run_attempt}"
            or contract.node.repository_identity not in (None, receipt.repo)
            or receipt.source_revision != pins["source_revision"]
            or receipt.definition_revision != pins["definition_revision"]
            or receipt.workflow not in workflows
            or receipt.valid_until <= datetime.now(UTC)
            or action.receipt_ref != context_receipt_ref(receipt.run_id, receipt.context_artifact_id, receipt.context_reference)
        ):
            continue
        correlated_keys[action.operation_key] = action
        evidence_ids.add(str(action.id))
    for receipt, action, correlated_key in runtime_receipts:
        correlated_action = correlated_keys.get(correlated_key)
        workflow_action = verified_workflows.get(receipt.workflow_operation_keys[0])
        if (
            correlated_action is None
            or workflow_action is None
            or (workflow_action.detail or {}).get("context_action_id") != correlated_action.id
            or workflow_action.receipt_ref != correlated_action.receipt_ref
        ):
            continue
        if bound_release:
            release = metadata["release_binding"]
            if (
                receipt.manifest_entry_ids != [release["target"]]
                or len(receipt.targets) != 1
                or receipt.targets[0].get("resource_kind") != release.get("resource_kind")
                or receipt.targets[0].get("resource_id") != release.get("resource_id")
                or {item.component for item in receipt.components} != {release["component"]}
            ):
                continue
        # Grouped releases record their mutation authority when the gateway
        # resolves the accepted binding. Flow and stage names confer no authority.
        if (
            "mutation_role_arn" in (metadata.get("release_binding") or {})
            and getattr(contract.node.operational_release_binding, "delivery_contract", None) is None
        ):
            if not await foundation_receipt_held(
                session,
                node=node,
                execution=execution,
                report=report,
                receipt=receipt,
                correlated_action=correlated_action,
                metadata=metadata,
                pins=pins,
                native_release=native_release,
            ):
                continue
            native_release["runtime_evidence_id"] = str(action.id)
        observed.update(item.component for item in receipt.components)
        evidence_ids.add(str(action.id))
    if not set(components).issubset(observed):
        return None, "evidence_missing"
    if controller:
        # The controller's signed workflow and independent runtime receipts are
        # its completion evidence. Never manufacture a successful worker report.
        if observation is not None:
            observation["evidence_ids"] = sorted(evidence_ids)
            if native_release:
                observation["native_release"] = native_release
        return execution.handoff_receipt_ref, ""
    bounded_report = getattr(report, "operations_report_receipt", None)
    if (
        not isinstance(bounded_report, dict)
        or bounded_report.get("run_id") != report.run_id
        or bounded_report.get("attempt") != report.attempt
        or bounded_report.get("node_id") != node.id
        or bounded_report.get("recorded") is not True
        or bounded_report.get("receipt_id") != f"operations-report:{report.run_id}"
        or not isinstance(bounded_report.get("report"), str)
        or not 0 < len(bounded_report["report"].encode("utf-8")) <= 12000
        or bounded_report.get("sha256") != hashlib.sha256(bounded_report["report"].encode("utf-8")).hexdigest()
    ):
        return None, "operations_report_missing"
    if observation is not None:
        observation["evidence_ids"] = sorted([*evidence_ids, bounded_report["receipt_id"]])
        if native_release:
            # Internal settlement snapshot, produced only by the full receipt
            # verifier. The result writer compares it under the action lock;
            # worker prose and a matching action kind confer no authority.
            observation["native_release"] = native_release
    return report.run_id, ""

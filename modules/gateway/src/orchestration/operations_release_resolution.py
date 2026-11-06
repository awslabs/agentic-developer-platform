"""Resolve an accepted operational release from settled, scoped merge receipts."""

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote

from pydantic import ValidationError
from sqlalchemy import or_, select

from .accepted_node import accepted_node_contract, operational_revision_inputs
from .compile import address_of
from .delivery_contract import release_manifest
from .deployment_manifest import EntryStatus, ManifestError, load_packaged_manifest
from .deployment_release_provider import ReleaseProvider
from .merge_controller import MERGE_KIND, MergeReceipt
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)
from .plan_lineage import ancestor_plan
from .review_cycle import CycleBlockedError
from .run_reports import OrchestrationRunReport


@dataclass(frozen=True)
class ReleaseSource:
    revision: str
    observed_at: datetime
    provenance: dict


async def _release_source(session, node, contract, predecessor, *, repository_id, installation_id, plan, flow, manifest_loader):
    if predecessor.receipt_kind == "verified_operations_release":
        from .operations_release_predecessor import verified_operations_release_predecessor

        return await verified_operations_release_predecessor(
            session,
            node,
            contract,
            predecessor.address,
            repository_id=repository_id,
            installation_id=installation_id,
            plan=plan,
            flow=flow,
            manifest_loader=manifest_loader,
        )
    action, receipt = await verified_release_predecessor(
        session, node, contract, predecessor.address, repository_id=repository_id, plan=plan, flow=flow
    )
    # Keep existing merge ledgers byte-equivalent for already dispatched stories.
    return ReleaseSource(
        receipt.merge_sha,
        receipt.merged_at,
        {
            "address": predecessor.address,
            "action_id": action.id,
            "operation_key": receipt.operation_key,
            "execution_id": receipt.execution_id,
            "claim_id": receipt.claim_id,
            "claim_generation": receipt.claim_generation,
            "merge_sha": receipt.merge_sha,
        },
    )


async def _contains_all(provider, binding, sources, candidate):
    """Bound parallel provider reads and drain them before releasing authority."""
    limit = asyncio.Semaphore(4)

    async def check(source):
        async with limit:
            await provider.contains(binding, source, candidate)

    tasks = [asyncio.create_task(check(source)) for source in sorted(sources) if source != candidate]
    try:
        await asyncio.gather(*tasks)
    finally:
        # A refused proof or an admission deadline must leave no provider reads
        # running after the caller rolls back the claim/dispatch transaction.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _retry_repair_definition(session, node, contract, provider, binding, workflow, source):
    """Admit a settled repair worker without approving a changed workflow.

    The worker retains its original deployment pins. Actions dispatch still
    requires current workflow equality; only a checked native repair merge can
    advance those pins. Requiring that equality before starting the repair
    worker deadlocks recovery after a maintainer changes the workflow.
    """
    from .execution_policy import Action
    from .policy_admission import load_in_force_policy

    admission = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    policy = admission.policy
    if (
        admission.refusal is not None
        or admission.plan_version != contract.plan_version
        or policy is None
        or not (policy.repair_paths or getattr(policy, "repair_scope", None) == "repository")
        or not {Action.OPERATE, Action.REPAIR, Action.MERGE} <= set(policy.allowed_actions)
    ):
        raise CycleBlockedError("deployment_workflow_revision_mismatch")
    generic = getattr(contract.node.operational_release_binding, "delivery_contract", None) is not None
    checked = await provider.definition(binding, workflow, source, for_dispatch=False, **({"generic_delivery": True} if generic else {}))
    repository = (await provider.request(binding, "GET", f"/repos/{binding.repo}")).json()
    release = contract.node.operational_release_binding
    if repository.get("id") != binding.provider_repository_id:
        raise CycleBlockedError("deployment_repository_identity_mismatch")
    if repository.get("default_branch") != release.default_branch:
        raise CycleBlockedError("operations_release_branch_changed")
    branch = (await provider.request(binding, "GET", f"/repos/{binding.repo}/branches/{quote(release.default_branch, safe='')}")).json()
    head = branch.get("commit", {}).get("sha")
    if not isinstance(head, str) or len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        raise CycleBlockedError("deployment_dispatch_revision_missing")
    # This return is used only for admission/ancestry checks below. It does not
    # replace the accepted source or definition in the execution journal.
    return replace(checked, dispatch_ref=release.default_branch, dispatch_revision=head)


async def operational_execution_pins(session, *, node, execution, contract, run_id):
    if getattr(contract.node, "operational_release_binding", None) is None:
        return operational_revision_inputs(contract)
    report = await session.get(OrchestrationRunReport, run_id)
    if report is None:
        return None
    from .operations_continuation_authority import assignment_matches

    if (
        report.org_id != node.org_id
        or report.flow_id != node.flow_id
        or report.node_id != node.id
        or report.attempt != execution.cycle
        or report.repo != contract.node.operational_release_binding.repository
        or report.run_id != run_id
        or not await assignment_matches(session, node=node, execution=execution, report=report)
    ):
        return None
    return await recorded_operations_release(
        session, node=node, execution=execution, contract=contract, metadata=report.dispatch_metadata.get("orchestration") or {}
    )


async def recorded_operations_release(session, *, node, execution, contract, metadata):
    """Read the release committed under this execution, never the current branch."""
    release = contract.node.operational_release_binding
    recorded = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.operation_key == "operations-release:1",
        )
    )
    if recorded is None or recorded.kind != "operations_release_binding" or recorded.status != "succeeded" or recorded.attempt != execution.cycle:
        return None
    detail = recorded.detail
    if not isinstance(detail, dict) or not isinstance(metadata, dict):
        return None
    pins = detail.get("pins")
    receipts = detail.get("receipts")
    if (
        (getattr(release, "delivery_contract", None) is not None and detail.get("delivery_contract_digest") != release.delivery_contract.digest)
        or detail != metadata.get("release_binding")
        or not isinstance(pins, dict)
        or set(pins) != {"source_revision", "definition_revision", "default_branch"}
        or metadata.get("deployment_revisions") != pins
        or detail.get("accepted_plan_version") != execution.accepted_plan_version
        or detail.get("repository") != release.repository
        or detail.get("target") != release.target
        or detail.get("component") != release.component
        or detail.get("maintenance_adoption_id") != release.maintenance_adoption_id
        or detail.get("workflow") not in {f".github/workflows/{name}" for name in release.workflows}
        or pins["default_branch"] != release.default_branch
        or not isinstance(receipts, list)
        or len(receipts) != len(release.predecessors)
    ):
        return None
    addresses = set()
    for item in receipts:
        if not isinstance(item, dict):
            return None
        address = item.get("address")
        if address is None:
            from .external_completion import external_receipt_address

            if not isinstance(item.get("external_completion_decision_id"), str):
                return None
            address = await external_receipt_address(session, node=node, receipt=item, repository=release.repository)
            if address not in {p.address for p in release.predecessors if p.receipt_kind == "verified_operations_release"}:
                return None
        addresses.add(address)
    if addresses != {predecessor.address for predecessor in release.predecessors}:
        return None
    if release.maintenance_adoption_id is not None:
        from .continuation import digest
        from .maintenance_adoption_resolution import verified_adoption

        try:
            adoption = await verified_adoption(session, node, contract)
        except CycleBlockedError:
            return None
        if (
            adoption is None
            or execution.cycle <= adoption["checkpoint_cycle"]
            or detail.get("maintenance_adoption_id") != release.maintenance_adoption_id
            or detail.get("maintenance_adoption_digest") != digest(adoption)
            or pins != adoption["pins"]
            or receipts != adoption["receipts"]
        ):
            return None
    return pins


async def _retry_release(session, node, contract, plan, *, installation_id, repository_id):
    """Require a settled, engine-authorized retry and its original release ledger."""
    from .developer_recovery import retry_context
    from .dispatch_pass import attempt_run_id
    from .operations_release_retry_contract import settled_retry_contract

    try:
        recovery = await retry_context(session, node, node.attempts)
    except (TypeError, ValueError):
        recovery = None
    run_id = attempt_run_id(node.id, node.attempts)
    if (
        not isinstance(recovery, dict)
        or recovery.get("reason") not in {"operations_runtime_unverified", "operations_failed"}
        or recovery.get("previous_attempt") != node.attempts
        or recovery.get("previous_run_id") != run_id
        or await ancestor_plan(session, plan, recovery.get("plan_version"), node_id=node.id) is None
    ):
        raise CycleBlockedError("operations_release_retry_not_authorized")
    execution = await session.get(OrchestrationExecution, recovery.get("execution_id"))
    if (
        execution is None
        or (execution.org_id, execution.flow_id, execution.node_id, execution.cycle) != (node.org_id, node.flow_id, node.id, node.attempts)
        or execution.status != "concluded"
        or execution.phase != "concluded"
        or execution.pending_action_key is not None
    ):
        raise CycleBlockedError("operations_release_retry_not_settled")
    previous_plan = await ancestor_plan(session, plan, execution.accepted_plan_version, node_id=node.id)
    previous_contract = settled_retry_contract(contract, previous_plan) if previous_plan is not None else None
    if previous_contract is None and contract.node.operational_release_binding.maintenance_adoption_id is not None:
        from .maintenance_adoption_resolution import prior_contract_for_adoption

        previous_contract = await prior_contract_for_adoption(session, node, contract, previous_plan)
    unresolved = await session.scalar(
        select(OrchestrationAction.id)
        .where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == execution.id,
            or_(OrchestrationAction.kind == "review_cycle_dispatch", OrchestrationAction.status.in_(["prepared", "dispatched", "unknown"])),
        )
        .limit(1)
    )
    report = await session.get(OrchestrationRunReport, run_id)
    if (
        previous_plan is None
        or previous_contract is None
        or report is None
        or report.persona != contract.node.executor.persona
        or report.installation_id != installation_id
        or report.provider_repository_id != repository_id
        or not isinstance(report.dispatch_metadata, dict)
        or await operational_execution_pins(session, node=node, execution=execution, contract=previous_contract, run_id=run_id) is None
    ):
        raise CycleBlockedError("operations_release_retry_evidence_unverified")
    recorded = report.dispatch_metadata["orchestration"]["release_binding"]
    if recorded.get("accepted_plan_hash") != previous_plan.plan_hash:
        raise CycleBlockedError("operations_release_retry_plan_changed")
    if unresolved is not None:
        # Resume and automatic recovery must settle the same terminal external
        # effect. A human failed->ready edge is not evidence that its workflow
        # stopped; use the existing provider/lease reconciliation before admission.
        from .operations_cancelled_release import reconcile_cancelled_release
        from .policy_admission import load_in_force_policy

        admission = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        if (
            recovery.get("resume_decision_id") is None
            or recovery["reason"] != "operations_runtime_unverified"
            or admission.refusal is not None
            or admission.policy is None
            or admission.plan_version != contract.plan_version
        ):
            raise CycleBlockedError("operations_release_retry_evidence_unverified")
        flow = await session.get(OrchestrationFlow, node.flow_id)
        result = await reconcile_cancelled_release(
            session, node=node, flow=flow, execution=execution, report=report, contract=previous_contract, policy=admission.policy
        )
        if result != "reconciled":
            raise CycleBlockedError(result)
    return recorded, previous_contract


async def resolve_operations_release(session, node, *, installation_id, repository_id, provider=None, manifest_loader=None):
    contract = await accepted_node_contract(session, node)
    release = contract.node.operational_release_binding if contract is not None else None
    if release is None:
        raise CycleBlockedError("operations_release_binding_missing")
    if (
        contract.node.repository_identity != release.repository
        or contract.node.executor is None
        or contract.node.executor.role != "operate"
        or contract.node.evidence_expectations != [f"runtime_verified:{release.component}"]
        or len(release.workflows) != 1
        or {
            item.source.removeprefix("deployment_workflow:")
            for item in contract.node.required_inputs
            if item.source.startswith("deployment_workflow:")
        }
        != set(release.workflows)
        or any(f"node:{predecessor.address}" not in {item.source for item in contract.node.required_inputs} for predecessor in release.predecessors)
    ):
        raise CycleBlockedError("operations_release_scope_changed")
    flow = await session.get(OrchestrationFlow, node.flow_id)
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == node.org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.version == contract.plan_version,
        )
    )
    if plan is None or flow is None or flow.org_id != node.org_id:
        raise CycleBlockedError("operations_release_plan_missing")
    if release.maintenance_adoption_id is not None:
        from .maintenance_adoption_resolution import resolve_adopted_release

        return await resolve_adopted_release(
            session,
            node,
            contract,
            plan,
            installation_id=installation_id,
            repository_id=repository_id,
            provider=provider or ReleaseProvider(),
        )
    previous = (
        await _retry_release(session, node, contract, plan, installation_id=installation_id, repository_id=repository_id) if node.attempts else None
    )
    try:
        manifest = release_manifest(release, manifest_loader or load_packaged_manifest)
        entry = manifest.entry(release.target)
    except ManifestError as exc:
        raise CycleBlockedError(f"operations_release_manifest_unavailable:{exc.code}") from None
    if (
        entry is None
        or entry.status is not EntryStatus.ENABLED
        or entry.docs_only
        or entry.workflow is None
        or entry.artifact_revision is None
        or entry.workflow.path.removeprefix(".github/workflows/") not in release.workflows
        or not entry.covers(release.component)
        or entry.connection_id not in contract.environment_connection_ids
        or release.repository not in contract.repository_ids
    ):
        raise CycleBlockedError("operations_release_target_unavailable")
    binding = SimpleNamespace(org_id=node.org_id, installation_id=installation_id, provider_repository_id=repository_id, repo=release.repository)
    provider = provider or ReleaseProvider()
    receipts = {
        predecessor.address: await _release_source(
            session,
            node,
            contract,
            predecessor,
            repository_id=repository_id,
            installation_id=installation_id,
            plan=plan,
            flow=flow,
            manifest_loader=manifest_loader or load_packaged_manifest,
        )
        for predecessor in release.predecessors
    }
    # Try the newest receipt first, but never use its timestamp as ancestry
    # evidence. Every required merge and the manifest baseline still need proof.
    ordered = sorted(receipts.values(), key=lambda receipt: (receipt.observed_at, receipt.revision), reverse=True)
    candidates = dict.fromkeys(receipt.revision for receipt in ordered)
    for candidate in candidates:
        try:
            await _contains_all(provider, binding, set(candidates) | {entry.artifact_revision}, candidate)
        except CycleBlockedError:
            continue
        # Git ancestry is antisymmetric: once a distinct candidate is proven to
        # contain every other candidate, none of those ancestors can also win.
        # Rechecking them fetched large forward comparisons and exhausted ticks.
        source = candidate
        break
    else:
        raise CycleBlockedError("operations_release_ancestry_missing_or_ambiguous")
    definition = (
        receipts[release.definition_predecessor].revision if release.definition_predecessor is not None else entry.workflow.definition_revision
    )
    workflow = replace(entry.workflow, definition_revision=definition)
    try:
        checked = await provider.definition(
            binding, workflow, source, **({"generic_delivery": True} if release.delivery_contract is not None else {})
        )
    except CycleBlockedError as exc:
        if previous is None or exc.reason != "deployment_workflow_revision_mismatch":
            raise
        checked = await _retry_repair_definition(session, node, contract, provider, binding, workflow, source)
    if checked.approved_revision != definition or checked.source_revision != source:
        raise CycleBlockedError("operations_release_definition_unverified")
    if checked.dispatch_ref != release.default_branch:
        raise CycleBlockedError("operations_release_branch_changed")
    if checked.dispatch_revision != source:
        await provider.contains(binding, source, checked.dispatch_revision)
    pins = {
        "source_revision": source,
        "definition_revision": definition,
        "default_branch": release.default_branch,
    }
    evidence = {
        "target": entry.entry_id,
        "connection_id": entry.connection_id,
        "resource_kind": entry.resource_kind,
        "resource_id": entry.resource_id,
        "manifest_artifact_revision": entry.artifact_revision,
        "manifest_definition_revision": entry.workflow.definition_revision,
        "workflow": entry.workflow.path,
        "component": release.component,
        "repository": release.repository,
        "accepted_plan_version": plan.version,
        "accepted_plan_hash": plan.plan_hash,
        "receipts": [receipt.provenance for _, receipt in sorted(receipts.items())],
    }
    if release.delivery_contract is not None:
        evidence["delivery_contract_digest"] = release.delivery_contract.digest
    if entry.release_group is not None:
        evidence["mutation_role_arn"] = entry.mutation_role_arn
    if previous is not None:
        # A preserved-node amendment can advance plan identity, never the release,
        # target, workflow or scoped predecessor receipts for a started node.
        plan_fields = {"accepted_plan_version", "accepted_plan_hash"}
        previous_record, previous_contract = previous
        if previous_contract is not contract:
            # _retry_release proved the only contract change is an explicitly
            # accepted job-role addition. Preserve its old digest in history;
            # the new attempt must carry and enforce the amended digest.
            plan_fields.add("delivery_contract_digest")
        original = {key: value for key, value in previous_record.items() if key not in plan_fields}
        resolved = {key: value for key, value in {**evidence, "pins": pins}.items() if key not in plan_fields}
        if original != resolved:
            raise CycleBlockedError("operations_release_retry_binding_changed")
    return pins, evidence


def _matching_decision(reason, receipt):
    try:
        return json.loads(reason).get("merge_receipt") == receipt
    except (TypeError, ValueError, AttributeError):
        return False


async def verified_release_predecessor(session, node, contract, address, *, repository_id=None, plan=None, flow=None):
    if plan is None:
        plan = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.org_id == node.org_id,
                OrchestrationAcceptedPlan.flow_id == node.flow_id,
                OrchestrationAcceptedPlan.version == contract.plan_version,
            )
        )
    if flow is None:
        flow = await session.get(OrchestrationFlow, node.flow_id)
    if plan is None or flow is None or flow.org_id != node.org_id:
        raise CycleBlockedError("operations_release_plan_missing")
    matches = (
        await session.scalars(
            select(OrchestrationNode).where(
                OrchestrationNode.org_id == node.org_id,
                OrchestrationNode.flow_id == node.flow_id,
                OrchestrationNode.node_ref == address.rsplit("/", 1)[-1],
            )
        )
    ).all()
    matches = [row for row in matches if address_of(flow.slug, row) == address] if flow else []
    if len(matches) != 1 or matches[0].state != "passed":
        raise CycleBlockedError("operations_release_predecessor_missing")
    previous = matches[0]
    accepted_nodes = (plan.plan_document or {}).get("nodes", [])
    if len([item for item in accepted_nodes if isinstance(item, dict) and item.get("address") == address and item.get("kind") == "story"]) != 1:
        raise CycleBlockedError("operations_release_predecessor_not_accepted")
    actions = (
        await session.execute(
            select(OrchestrationAction, OrchestrationExecution)
            .join(OrchestrationExecution, OrchestrationAction.execution_id == OrchestrationExecution.id)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationExecution.org_id == node.org_id,
                OrchestrationExecution.flow_id == node.flow_id,
                OrchestrationExecution.node_id == previous.id,
                OrchestrationAction.status == "succeeded",
            )
        )
    ).all()
    verified = []
    for action, execution in actions:
        try:
            receipt = MergeReceipt.model_validate((action.detail or {})["merge_receipt"])
        except (KeyError, TypeError, ValidationError):
            continue
        if (
            action.kind != MERGE_KIND
            or receipt.org_id != node.org_id
            or receipt.flow_id != node.flow_id
            or receipt.node_id != previous.id
            or receipt.execution_id != execution.id
            or receipt.cycle != execution.cycle
            or receipt.cycle != previous.attempts
            or receipt.accepted_plan_version != execution.accepted_plan_version
            or receipt.claim_id != execution.claim_id
            or receipt.claim_generation != execution.claim_generation
            or receipt.repo != contract.node.operational_release_binding.repository
            or (repository_id is not None and receipt.provider_repository_id != repository_id)
            or receipt.operation_key != action.operation_key
            or await ancestor_plan(session, plan, execution.accepted_plan_version, node_id=previous.id) is None
        ):
            continue
        decisions = (
            await session.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.org_id == node.org_id,
                    OrchestrationDecision.flow_id == node.flow_id,
                    OrchestrationDecision.node_id == previous.id,
                    OrchestrationDecision.kind == "result_observed",
                    OrchestrationDecision.actor_id == "system:merge-controller",
                    OrchestrationDecision.actor_kind == "service",
                    OrchestrationDecision.to_state == "passed",
                )
            )
        ).all()
        if not any(_matching_decision(decision.reason, receipt.model_dump(mode="json")) for decision in decisions):
            continue
        verified.append((action, receipt))
    if len(verified) != 1:
        raise CycleBlockedError("operations_release_receipt_missing_or_ambiguous")
    return verified[0]

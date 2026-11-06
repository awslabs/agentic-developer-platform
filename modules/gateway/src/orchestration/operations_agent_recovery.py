"""Close a reconciled legacy controller assignment so an operations agent can retry.

This operator control never deploys, dispatches, changes limits or supplies success.
An unknown dispatch requires an attributed no-dispatch reconciliation and a fresh
provider read. The original report, transfer, action intent and policy are retained.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .continuation import digest
from .dispatch_pass import attempt_run_id
from .execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, Observation, ObservedOutcome, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, record_observation
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .operations_continuation_authority import report_digest, require, transfer
from .policy_admission import load_in_force_policy
from .run_reports import OrchestrationRunReport
from .state import ActorKind, NodeState, transition

KIND = "operations_controller_closed"
PREFIX = "operations-agent-return:"


class OperationsAgentRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_version: int = Field(strict=True, ge=1)
    expected_execution_id: str = Field(min_length=1, max_length=36)
    expected_attempt: int = Field(strict=True, ge=1)
    expected_report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    no_dispatch_evidence: str = Field(min_length=20, max_length=2000)
    reason: str = Field(min_length=10, max_length=2000)
    maintenance_source_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    maintenance_deployment_run_id: int | None = Field(default=None, strict=True, gt=0)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


async def provider_run(state, action):
    from .operations_continuation import OperationsContinuationHandler

    return await OperationsContinuationHandler(None).runs(state, action)


async def maintenance_context(provider, binding, run, contract, workflow, source):
    # Operator-only path. The scheduled worker/ledger readers never import the
    # HTTP authentication stack or impersonate an operations worker here.
    from src.agentauth.actions_routes import _evidence_account, _s3_context
    from src.shared.config import get_settings

    from .deployment_manifest import EntryStatus, load_packaged_manifest

    release = getattr(contract.node, "operational_release_binding", None)
    require(release is not None, "maintenance_release_target_required")
    entry = load_packaged_manifest().entry(release.target)
    require(
        entry is not None
        and entry.status is EntryStatus.ENABLED
        and entry.workflow is not None
        and entry.connection_id in contract.environment_connection_ids
        and entry.covers(release.component)
        and entry.workflow.path == ".github/workflows/" + workflow,
        "maintenance_target_changed",
    )
    environments = entry.workflow.allowed_inputs.get("environment") or frozenset()
    require(len(environments) == 1, "maintenance_environment_ambiguous")
    observed = await _s3_context(provider, binding, run, workflow=workflow, revision=source, environment=next(iter(environments)))
    require(observed is not None, "maintenance_context_missing")
    context, checksum, reference = observed
    expected = (
        binding.provider_repository_id,
        run["id"],
        run["run_attempt"],
        entry.workflow.path,
        source,
        source,
        _evidence_account(),
        get_settings().aws_region,
        entry.resource_kind,
        entry.resource_id,
    )
    actual = (
        context.repository_id,
        context.run_id,
        context.run_attempt,
        context.workflow_path,
        context.workflow_revision,
        context.source_revision,
        context.account_id,
        context.region,
        context.resource_kind,
        context.resource_id,
    )
    require(actual == expected, "maintenance_target_or_revision_changed")
    return {"reference": reference, "digest": checksum, "target": release.target}


async def maintenance_source(session, *, node, row, report, binding, request, workflow):
    """Retain an already deployed maintenance fix without changing the workflow."""
    require(bool(request.maintenance_source_revision) == bool(request.maintenance_deployment_run_id), "maintenance_source_pair_required")
    if request.maintenance_source_revision is None:
        return None
    from .accepted_node import accepted_node_contract
    from .deployment_release_provider import ReleaseProvider
    from .deployment_workflow_provider import decode_workflow_content
    from .operations_release_resolution import operational_execution_pins
    from .operations_repair import repair_revisions

    contract = await accepted_node_contract(session, node)
    require(contract is not None, "operations_contract_missing")
    pins = await operational_execution_pins(session, node=node, execution=row, contract=contract, run_id=report.run_id)
    pins = await repair_revisions(
        session,
        node_id=node.id,
        org_id=node.org_id,
        plan_version=row.accepted_plan_version,
        cycle=row.cycle,
        pins=pins,
        contract=contract,
        require_merged=True,
    )
    require(pins is not None, "operations_release_unverified")
    provider = ReleaseProvider()
    source = request.maintenance_source_revision
    run = (await provider.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{request.maintenance_deployment_run_id}")).json()
    require(
        run.get("id") == request.maintenance_deployment_run_id
        and (run.get("repository") or {}).get("id") == binding.provider_repository_id
        and run.get("head_sha") == source
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("path") == ".github/workflows/" + workflow,
        "maintenance_deployment_unverified",
    )
    if source != pins["source_revision"]:
        await provider.contains(binding, pins["source_revision"], source)
    ref = (await provider.request(binding, "GET", f"/repos/{binding.repo}/git/ref/heads/{quote(pins['default_branch'], safe='')}")).json()
    head = (ref.get("object") or {}).get("sha")
    require(isinstance(head, str) and len(head) == 40, "maintenance_default_branch_unavailable")
    if head != source:
        await provider.contains(binding, source, head)
    path = ".github/workflows/" + workflow
    contents = []
    for revision in (pins["definition_revision"], source):
        raw = (
            await provider.request(binding, "GET", f"/repos/{binding.repo}/contents/{path}", params={"ref": revision}, max_bytes=256 * 1024)
        ).json()
        contents.append(decode_workflow_content(raw))
    require(contents[0] == contents[1], "maintenance_workflow_changed")
    return {
        "before": pins,
        "pins": {**pins, "source_revision": source, "definition_revision": source},
        "deployment_run_id": request.maintenance_deployment_run_id,
        "workflow": path,
        "context": await maintenance_context(provider, binding, run, contract, workflow, source),
    }


async def prepare(session, *, node_id, actor, request):
    require(actor.actor_kind == ActorKind.HUMAN and actor.actor_role == "platform_admin", "human_platform_admin_required")
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == node_id, OrchestrationNode.org_id == actor.org_id))
    require(node is not None, "node_not_found")
    flow = await session.scalar(
        select(OrchestrationFlow).where(OrchestrationFlow.id == node.flow_id, OrchestrationFlow.org_id == actor.org_id).with_for_update()
    )
    require(flow is not None and flow.execution_paused and flow.state in {"pending", "running"}, "flow_must_be_paused")
    await session.refresh(node, with_for_update=True)
    row = await session.get(OrchestrationExecution, request.expected_execution_id, with_for_update=True)
    require(
        row is not None
        and node.state == "running"
        and node.kind == "story"
        and (row.org_id, row.flow_id, row.node_id, row.cycle) == (node.org_id, node.flow_id, node.id, request.expected_attempt)
        and node.attempts == request.expected_attempt
        and row.status not in {"concluded", "superseded"},
        "operations_assignment_changed",
    )
    report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, node.attempts))
    require(report is not None and report_digest(report) == request.expected_report_digest, "operations_report_changed")
    require((report.terminal_receipt or {}).get("outcome") == "failed", "failed_worker_required")
    original = await transfer(session, node=node, execution=row, report=report)
    require(original is not None, "operations_transfer_changed")
    admission = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    require(
        admission.refusal is None and admission.policy is not None and admission.plan_version == request.expected_plan_version,
        "accepted_policy_changed",
    )
    require(admission.policy.expires_at > datetime.now(UTC), "policy_expired")
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == row.id,
            )
            .order_by(OrchestrationAction.id)
            .limit(201)
        )
    )
    require(len(actions) <= 200, "operations_history_limit")
    outstanding = [a for a in actions if a.status not in {"succeeded", "failed"}]
    require(len(outstanding) == 1, "single_unresolved_dispatch_required")
    pending = outstanding[0]
    require(
        pending.kind == "operations_continuation_deploy"
        and pending.status == "unknown"
        and pending.operation_key == row.pending_action_key
        and not pending.receipt_ref,
        "unresolved_dispatch_changed",
    )
    require(all(a.kind != "review_cycle_dispatch" for a in actions), "reviewer_assignment_present")
    binding = SimpleNamespace(
        org_id=node.org_id, repo=report.repo, installation_id=report.installation_id, provider_repository_id=report.provider_repository_id
    )
    require(await provider_run(SimpleNamespace(binding=binding, report=report), pending) is None, "provider_dispatch_exists")
    snapshot = {
        "node_id": node.id,
        "flow_id": node.flow_id,
        "execution_id": row.id,
        "attempt": row.cycle,
        "plan_version": admission.plan_version,
        "claim_id": row.claim_id,
        "claim_generation": row.claim_generation,
        "controller": row.handoff_receipt_ref,
        "source_report_digest": report_digest(report),
        "transfer": original,
        "policy": admission.policy.model_dump(mode="json"),
        "source_run_id": report.run_id,
        "pending_action_id": pending.id,
        "pending_action_key": pending.operation_key,
        "pending_action_detail": pending.detail,
        "pending_action_status": pending.status,
        "actions_digest": digest(
            [{"id": a.id, "key": a.operation_key, "status": a.status, "receipt": a.receipt_ref, "detail": a.detail} for a in actions]
        ),
        "no_dispatch_evidence": request.no_dispatch_evidence,
        "maintenance_source": await maintenance_source(
            session, node=node, row=row, report=report, binding=binding, request=request, workflow=pending.detail["workflow"]
        ),
    }
    return node, row, pending, snapshot


async def recover(session, *, node_id, actor, request, accept=False):
    decision_id = str(
        uuid5(
            NAMESPACE_URL,
            KIND
            + ":"
            + digest(
                {
                    "org_id": actor.org_id,
                    "actor_id": actor.actor_id,
                    "node_id": node_id,
                    **request.model_dump(mode="json"),
                }
            ),
        )
    )
    if accept:
        require(actor.actor_kind == ActorKind.HUMAN and actor.actor_role == "platform_admin", "human_platform_admin_required")
        existing = await session.get(OrchestrationDecision, decision_id)
        if existing is not None:
            require(existing.org_id == actor.org_id and existing.actor_id == actor.actor_id and existing.kind == KIND, "recovery_receipt_conflict")
            return {"closed": True, "created": False, "decision_id": decision_id}
    node, row, pending, snapshot = await prepare(session, node_id=node_id, actor=actor, request=request)
    fingerprint = digest(snapshot)
    result = {
        "snapshot": fingerprint,
        "node_id": node.id,
        "attempt": row.cycle,
        "attempts_preserved": True,
        "budget_unchanged": True,
        "flow_remains_paused": True,
        "state_after_close": "failed",
        "retry_limit": snapshot["policy"]["limits"]["max_attempts_per_node"],
    }
    if not accept:
        return result
    require(request.expected_snapshot == fingerprint, "recovery_preview_changed")
    identity = ExecutionIdentity(node.org_id, node.id, row.cycle, row.accepted_plan_version, row.claim_id, row.claim_generation)
    observed = await record_observation(
        session,
        identity=identity,
        observation=Observation(
            pending.operation_key,
            ObservedOutcome.FAILED,
            receipt_ref=PREFIX + decision_id,
            detail="Operator reconciled pre-dispatch failure: " + request.no_dispatch_evidence,
        ),
    )
    require(observed.kind is OutcomeKind.APPLIED, "operations_action_changed")
    closed = await advance_execution(
        session,
        identity=identity,
        advance=PhaseAdvance(
            ExecutionPhase.CONCLUDED,
            ExecutionStatus.CONCLUDED,
            expected_revision=row.revision,
            progress_note="Legacy controller closed after no-dispatch reconciliation; operations agent retry remains subject to admission.",
        ),
        handoff_receipt_ref=PREFIX + decision_id,
    )
    require(closed.kind is OutcomeKind.APPLIED, "operations_execution_changed")
    from .work_claims import Disposition, ReleaseReason, release_work

    released = await release_work(
        session,
        org_id=node.org_id,
        claim_id=row.claim_id,
        generation=row.claim_generation,
        reason=ReleaseReason.FAILED,
        terminal_evidence=PREFIX + decision_id,
    )
    require(released.disposition is Disposition.ADMITTED, "operations_claim_changed")
    require(
        transition(NodeState(node.state), NodeState.FAILED, actor_kind=ActorKind.HUMAN, reason=request.reason).allowed, "operations_close_refused"
    )
    session.add(
        OrchestrationDecision(
            id=decision_id,
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=KIND,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind=ActorKind.HUMAN.value,
            from_state="running",
            to_state="failed",
            reason=json.dumps({**snapshot, "snapshot": fingerprint, "reason": request.reason}),
        )
    )
    node.state = "failed"
    await session.flush()
    return {**result, "closed": True, "created": True, "decision_id": decision_id}


async def closed_assignment(session, *, node, execution, report):
    """Read a historical closure; this never restores live controller authority."""
    ref = execution.handoff_receipt_ref or ""
    if not ref.startswith(PREFIX):
        return None
    decision = await session.get(OrchestrationDecision, ref.removeprefix(PREFIX))
    if (
        decision is None
        or not decision.actor_id
        or decision.kind != KIND
        or decision.actor_kind != "human"
        or decision.actor_role != "platform_admin"
    ):
        return None
    try:
        data = json.loads(decision.reason)
        original = data["transfer"]
        if not isinstance(data, dict) or not isinstance(original, dict) or not isinstance(data.get("policy"), dict):
            return None
        if digest({key: value for key, value in data.items() if key not in {"snapshot", "reason"}}) != data.get("snapshot"):
            return None
    except (TypeError, ValueError, KeyError):
        return None
    fences = (report.dispatch_metadata or {}).get("handoff_expect") or {}
    if not (
        decision.org_id == node.org_id == execution.org_id == report.org_id
        and decision.flow_id == node.flow_id == execution.flow_id == report.flow_id
        and decision.node_id == node.id == execution.node_id == report.node_id
        and data.get("execution_id") == execution.id == fences.get("execution_id")
        and data.get("attempt") == execution.cycle == report.attempt
        and data.get("claim_id") == execution.claim_id == fences.get("claim_id")
        and data.get("claim_generation") == execution.claim_generation == original.get("claim_generation")
        and original.get("prior_claim_generation") == fences.get("claim_generation")
        and original.get("accepted_plan_version") == execution.accepted_plan_version == fences.get("accepted_plan_version")
        and data.get("source_report_digest") == report_digest(report) == original.get("source_report_digest")
        and data.get("source_run_id") == report.run_id == original.get("source_run_id")
        and execution.phase == execution.status == "concluded"
        and execution.pending_action_key is None
    ):
        return None
    return data


async def maintenance_pins(session, *, node, execution, pins, policy):
    if not (execution.handoff_receipt_ref or "").startswith(PREFIX):
        return pins
    from .shared_retry import verified_limits_increased

    report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, execution.cycle))
    data = await closed_assignment(session, node=node, execution=execution, report=report) if report is not None else None
    if data is None:
        return None
    maintenance = data.get("maintenance_source")
    if maintenance is None:
        return pins
    if maintenance["before"] != pins or not (
        data["policy"]["policy_hash"] == policy.policy_hash or verified_limits_increased(data["policy"], policy)
    ):
        return None
    return maintenance["pins"]


async def retry_context(session, node, previous_attempt):
    row = await session.scalar(
        select(OrchestrationExecution).where(
            OrchestrationExecution.org_id == node.org_id, OrchestrationExecution.node_id == node.id, OrchestrationExecution.cycle == previous_attempt
        )
    )
    report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, previous_attempt))
    if row is None or report is None:
        return None
    data = await closed_assignment(session, node=node, execution=row, report=report)
    if data is None:
        return None
    return {
        "previous_run_id": report.run_id,
        "previous_attempt": row.cycle,
        "execution_id": row.id,
        "plan_version": data["plan_version"],
        "reason": "operations_failed",
        "preserve_existing_work": True,
        "controller_closure": row.handoff_receipt_ref,
    }

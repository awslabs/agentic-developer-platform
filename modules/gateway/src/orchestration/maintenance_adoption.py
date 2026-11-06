"""Owner adoption of a verified maintenance merge without resetting story history.

This creates a versioned plan amendment and a separate immutable human receipt.
It never resumes a node, grants another attempt, or fabricates an agent merge.
"""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from sqlalchemy import select

from .accepted_node import accepted_node_contract
from .continuation import digest
from .maintenance_adoption_provider import MaintenanceProvider, require, verify_maintenance
from .maintenance_squash_history import SquashedRepairHistory
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim
from .run_reports import OrchestrationRunReport
from .shared_amendment import current_plan, rows
from .state import ActorKind

KIND = "maintenance_release_adopted"
CONTRACT = "owner-maintenance-adoption/v1"


class MaintenanceAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=36)
    expected_plan_version: int = Field(strict=True, gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_attempt: int = Field(strict=True, ge=0)
    pull_number: int = Field(strict=True, gt=0)
    reviewed_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    workflow_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed_paths: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(min_length=1, max_length=500)
    required_checks: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(min_length=1, max_length=64)
    job_mutation_role_arn: str = Field(pattern=r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")
    review_ref: str = Field(min_length=8, max_length=2048)
    reason: str = Field(min_length=20, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    squashed_repair_history: list[SquashedRepairHistory] = Field(default_factory=list, max_length=64)

    @model_serializer(mode="wrap")
    def preserve_existing_request_digest(self, handler):
        value = handler(self)
        if not self.squashed_repair_history:
            value.pop("squashed_repair_history", None)
        return value


async def repair_history(session, node, cycle):
    found = list(
        await session.execute(
            select(OrchestrationAction, OrchestrationExecution)
            .join(OrchestrationExecution, OrchestrationAction.execution_id == OrchestrationExecution.id)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationExecution.org_id == node.org_id,
                OrchestrationExecution.node_id == node.id,
                OrchestrationExecution.cycle <= cycle,
                OrchestrationAction.kind.in_(("operations_repair_pr", "operations_repair_merge")),
            )
            .order_by(OrchestrationExecution.cycle, OrchestrationAction.created_at, OrchestrationAction.id)
            .limit(1001)
        )
    )
    require(len(found) <= 1000, "history_limit")
    return [
        {
            "id": a.id,
            "execution_id": e.id,
            "cycle": e.cycle,
            "plan_version": e.accepted_plan_version,
            "kind": a.kind,
            "key": a.operation_key,
            "status": a.status,
            "receipt": a.receipt_ref,
            "detail": a.detail,
        }
        for a, e in found
    ]


async def _authority(session, actor, flow, plan):
    from src.shared.identity.workflow_access import is_workflow_member

    from .policy_admission import load_in_force_policy
    from .runtime_policy import flow_started_at
    from .shared_window import accepted_document_hash, policy_owner_matches

    require(actor.actor_kind == ActorKind.HUMAN and actor.actor_role == "platform_admin", "human_platform_admin_required")
    require(plan.plan_hash == accepted_document_hash(plan), "accepted_document_changed")
    admission = await load_in_force_policy(session, org_id=flow.org_id, flow_id=flow.id)
    policy = admission.policy
    require(admission.refusal is None and policy is not None, "policy_unverifiable")
    require(await policy_owner_matches(session, actor.actor_id, policy), "original_principal_required")
    require(await is_workflow_member(session, user_id=actor.actor_id, org_id=flow.org_id), "principal_authority_revoked")
    now = datetime.now(UTC)
    started = await flow_started_at(session, org_id=flow.org_id, flow_id=flow.id)
    require(policy.expires_at > now, "policy_expired")
    require(started is not None and started + timedelta(seconds=policy.limits.max_wall_clock_seconds) > now, "wall_clock_elapsed")
    return policy


async def prepare(session, *, flow_id, actor, request, provider, lock=False):
    from .deployment_manifest import load_packaged_manifest
    from .dispatch_pass import attempt_run_id, resolve_installation_id
    from .maintenance_adoption_resolution import inherited_squash_proofs, prior_contract_for_adoption, verified_adoption
    from .operations_release_resolution import _release_source, operational_execution_pins
    from .operations_release_retry_contract import settled_retry_contract
    from .operations_repair import repair_revisions
    from .plan_lineage import ancestor_plan

    flow, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=lock)
    require(flow.archived_at is None and flow.state in {"pending", "running"}, "flow_not_active")
    require((plan.version, plan.plan_hash) == (request.expected_plan_version, request.expected_plan_hash), "accepted_plan_changed")
    # Match the amendment's complete node lock set without waiting behind a
    # dispatcher that already owns a node and is waiting for this flow lock.
    nodes = await rows(
        session,
        OrchestrationNode,
        [
            OrchestrationNode.org_id == actor.org_id,
            OrchestrationNode.flow_id == flow_id,
            *([] if lock else [OrchestrationNode.id == request.node_id]),
        ],
        lock=lock,
        nowait=lock,
    )
    nodes = [node for node in nodes if node.id == request.node_id]
    require(len(nodes) == 1, "node_not_found")
    node = nodes[0]
    require(node.state in {"failed", "ready"} and node.attempts == request.expected_attempt, "node_changed")
    policy = await _authority(session, actor, flow, plan)
    contract = await accepted_node_contract(session, node)
    release = contract.node.operational_release_binding if contract else None
    require(
        release is not None
        and release.delivery_contract is not None
        and len(release.workflows) == 1
        and contract.node.executor is not None
        and contract.node.executor.role == "operate",
        "generic_release_required",
    )
    prior_adoption = await verified_adoption(session, node, contract, appending=True)
    require(release.repository in policy.repository_ids and "operate" in policy.allowed_actions, "release_not_authorized")
    require(set(policy.repair_required_checks) <= set(request.required_checks), "required_checks_removed")
    old_role = release.delivery_contract.job_mutation_role_arn
    require(old_role is not None and old_role.split(":")[:5] == request.job_mutation_role_arn.split(":")[:5], "role_account_changed")
    executions = await rows(
        session, OrchestrationExecution, [OrchestrationExecution.org_id == node.org_id, OrchestrationExecution.node_id == node.id]
    )
    require(all(e.status == e.phase == "concluded" and e.pending_action_key is None for e in executions), "execution_outstanding")
    require({e.cycle for e in executions} == set(range(1, node.attempts + 1)), "execution_history_incomplete")
    actions = []
    if executions:
        actions = await rows(
            session,
            OrchestrationAction,
            [OrchestrationAction.org_id == node.org_id, OrchestrationAction.execution_id.in_([e.id for e in executions])],
        )
        require(all(a.status in {"succeeded", "failed"} for a in actions), "effects_unresolved")
    execution = next((e for e in executions if e.cycle == node.attempts), None)
    report, claim, pins = None, None, None
    if execution:
        run_id = attempt_run_id(node.id, node.attempts)
        report = await session.get(OrchestrationRunReport, run_id)
        require(report is not None and report.node_id == node.id and report.org_id == node.org_id, "report_missing")
        terminal = report.terminal_receipt or {}
        require(
            terminal.get("outcome") in {"complete", "failed"} and terminal.get("run_id") == run_id and terminal.get("attempt") == node.attempts,
            "worker_exit_unverified",
        )
        claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
        require(
            claim is not None
            and claim.org_id == node.org_id
            and claim.owner_kind == "engine_flow"
            and claim.owner_ref == flow.id
            and claim.state == "released"
            and claim.active_run_id is None
            and claim.generation == execution.claim_generation
            and claim.claim_event_id == run_id
            and claim.release_reason in {"failed", "completed", "abandoned"},
            "claim_not_settled",
        )
        previous_plan = await ancestor_plan(session, plan, execution.accepted_plan_version, node_id=node.id)
        previous_contract = settled_retry_contract(contract, previous_plan) if previous_plan is not None else None
        if previous_contract is None and prior_adoption is not None:
            previous_contract = await prior_contract_for_adoption(session, node, contract, previous_plan)
        require(previous_contract is not None, "prior_contract_changed")
        pins = await operational_execution_pins(session, node=node, execution=execution, contract=previous_contract, run_id=run_id)
        require(pins is not None, "prior_release_unverified")
        pins = await repair_revisions(
            session,
            node_id=node.id,
            org_id=node.org_id,
            plan_version=execution.accepted_plan_version,
            cycle=node.attempts,
            pins=pins,
            # Replay the settled attempt's binding and ledger under the current
            # verified lineage/policy context, not a newer checkpoint's pins.
            contract=replace(previous_contract, plan_version=contract.plan_version),
        )
        require(pins is not None, "repair_chain_unverified")
        installation, repository_id = report.installation_id, report.provider_repository_id
    else:
        installation = (
            contract.node.repository_connection.installation_id
            if contract.node.repository_connection
            else await resolve_installation_id(session, org_id=node.org_id)
        )
        repository_id = contract.node.provider_repository_id
    if prior_adoption is not None and node.attempts == prior_adoption["checkpoint_cycle"]:
        # No run has consumed this checkpoint yet. The old run is verified
        # above; the next checkpoint must retain the already-adopted source.
        pins = dict(prior_adoption["pins"])
    require(installation is not None and repository_id is not None, "repository_binding_missing")
    held = await session.scalar(
        select(OrchestrationWorkClaim.id)
        .where(
            OrchestrationWorkClaim.org_id == node.org_id,
            OrchestrationWorkClaim.provider_repository_id == repository_id,
            OrchestrationWorkClaim.issue_number == int(str(node.issue_ref).lstrip("#")),
            OrchestrationWorkClaim.state == "held",
        )
        .limit(1)
    )
    require(held is None, "claim_not_settled")
    binding = SimpleNamespace(org_id=node.org_id, repo=release.repository, installation_id=installation, provider_repository_id=repository_id)
    receipts = [
        await _release_source(
            session,
            node,
            contract,
            p,
            repository_id=repository_id,
            installation_id=installation,
            plan=plan,
            flow=flow,
            manifest_loader=load_packaged_manifest,
        )
        for p in release.predecessors
    ]
    history = await repair_history(session, node, node.attempts)
    require(all(h["status"] == "succeeded" for h in history), "repair_action_unresolved")
    publications = [h for h in history if h["kind"] == "operations_repair_pr"]
    published_heads = [(h["detail"] or {}).get("head") for h in publications]
    require(
        all(isinstance(head, str) and len(head) == 40 and all(c in "0123456789abcdef" for c in head) for head in published_heads),
        "publication_head_missing",
    )
    sources = {r.revision for r in receipts} | {release.delivery_contract.baseline_revision}
    if pins:
        sources.add(pins["source_revision"])
    proof = await verify_maintenance(provider, binding, request, release, sources, published_heads, publications)
    from .deployment_manifest import WorkflowRef

    checked = await provider.definition(
        binding,
        WorkflowRef(path=proof["workflow_path"], definition_revision=request.source_revision),
        request.source_revision,
        generic_delivery=True,
    )
    require(
        checked.approved_revision == checked.source_revision == request.source_revision and checked.dispatch_ref == release.default_branch,
        "workflow_definition_unverified",
    )
    data = {
        "contract": CONTRACT,
        "org_id": node.org_id,
        "flow_id": flow.id,
        "node_id": node.id,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "checkpoint_cycle": node.attempts,
        "node_state": node.state,
        "execution_paused": flow.execution_paused,
        "policy": policy.model_dump(mode="json"),
        "before_binding": release.model_dump(mode="json"),
        "before_pins": pins,
        "receipts": [r.provenance for r in receipts],
        "repair_history": history,
        "executions": [{"id": e.id, "revision": e.revision, "cycle": e.cycle, "claim_generation": e.claim_generation} for e in executions],
        "actions_digest": digest([{"id": a.id, "status": a.status, "detail": a.detail, "receipt": a.receipt_ref} for a in actions]),
        "terminal_digest": digest(report.terminal_receipt) if report else None,
        "claim": {"id": claim.id, "generation": claim.generation, "state": claim.state} if claim else None,
        "proof": proof,
        "reviewed_job_mutation_role_arn": request.job_mutation_role_arn,
        "request_digest": digest(request.model_dump(mode="json", exclude={"expected_snapshot"})),
        **(
            {"prior_adoption_digest": digest(prior_adoption), "inherited_squash_proofs": inherited_squash_proofs(prior_adoption)}
            if prior_adoption is not None
            else {}
        ),
    }
    return flow, plan, node, data


async def adopt_maintenance(session, *, flow_id, actor, request, accept=False, provider=None):
    from .amend import AmendmentContext, amend_plan
    from .policy_admission import load_in_force_policy
    from .proposal import LoopProposal

    provider = provider or MaintenanceProvider()
    require(not accept or request.expected_snapshot is not None, "preview_required")
    async with session.begin_nested():
        if accept:
            identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":" + digest({"snapshot": request.expected_snapshot, "actor_id": actor.actor_id})))
            existing = await session.get(OrchestrationDecision, identity)
            if existing is not None:
                flow, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=True)
                await _authority(session, actor, flow, plan)
                data = json.loads(existing.reason)
                require(
                    existing.org_id == actor.org_id
                    and existing.flow_id == flow_id
                    and existing.node_id == request.node_id
                    and existing.actor_id == actor.actor_id
                    and existing.kind == KIND
                    and data.get("request_digest") == digest(request.model_dump(mode="json", exclude={"expected_snapshot"}))
                    and (plan.version, plan.plan_hash) == (data["resulting_plan_version"], data["resulting_plan_hash"]),
                    "receipt_conflict",
                )
                return {"accepted": True, "created": False, "decision_id": identity, "plan_version": plan.version, "plan_hash": plan.plan_hash}
        flow, plan, node, data = await prepare(session, flow_id=flow_id, actor=actor, request=request, provider=provider, lock=accept)
        snapshot = digest(data)
        result = {
            "accepted": False,
            "snapshot": snapshot,
            "proof": data["proof"],
            "attempts_preserved": node.attempts,
            "limits": data["policy"]["limits"],
            "expires_at": data["policy"]["expires_at"],
            "state": node.state,
        }
        if not accept:
            return result
        require(request.expected_snapshot == snapshot, "preview_changed")
        # Provider reads can take time; recheck live principal and expiry just
        # before granting the new plan authority.
        await _authority(session, actor, flow, plan)
        identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":" + digest({"snapshot": snapshot, "actor_id": actor.actor_id})))
        document = copy.deepcopy(plan.plan_document)
        address = (await accepted_node_contract(session, node)).node.address
        target = next(n for n in document["nodes"] if n["address"] == address)
        after = copy.deepcopy(data["before_binding"])
        after["maintenance_adoption_id"] = identity
        after["delivery_contract"].update(
            definition_revision=request.source_revision,
            baseline_revision=request.source_revision,
            job_mutation_role_arn=request.job_mutation_role_arn,
        )
        target["operational_release_binding"] = after
        for field in ("policy_id", "policy_hash", "principal_id"):
            document["execution_policy"].pop(field, None)
        proposal = LoopProposal.model_validate(document)
        amendment = await amend_plan(
            session,
            flow_id,
            proposal,
            AmendmentContext(org_id=actor.org_id, actor_id=actor.actor_id, actor_role=actor.actor_role, reason=request.reason),
        )
        current = (await load_in_force_policy(session, org_id=actor.org_id, flow_id=flow_id)).policy
        require(current is not None, "amended_policy_missing")
        ignored = {"policy_id", "policy_hash", "principal_id"}
        require(
            current.model_dump(mode="json", exclude=ignored) == {k: v for k, v in data["policy"].items() if k not in ignored},
            "policy_authority_changed",
        )
        data.update(
            after_binding=after,
            resulting_plan_version=amendment.plan_version,
            resulting_plan_hash=amendment.plan_hash,
            pins={
                "source_revision": request.source_revision,
                "definition_revision": request.source_revision,
                "default_branch": after["default_branch"],
            },
            reason=request.reason,
        )
        session.add(
            OrchestrationDecision(
                id=identity,
                org_id=actor.org_id,
                flow_id=flow_id,
                node_id=node.id,
                kind=KIND,
                actor_kind="human",
                actor_role=actor.actor_role,
                actor_id=actor.actor_id,
                reason=json.dumps(data, sort_keys=True),
            )
        )
        await session.flush()
        require(
            node.attempts == request.expected_attempt and node.state == data["node_state"] and flow.execution_paused == data["execution_paused"],
            "node_changed",
        )
        return {
            **result,
            "accepted": True,
            "created": True,
            "decision_id": identity,
            "plan_version": amendment.plan_version,
            "plan_hash": amendment.plan_hash,
        }

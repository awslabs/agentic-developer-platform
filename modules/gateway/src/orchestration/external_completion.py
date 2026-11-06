"""Owner acceptance of work verified outside an exited operations worker.

Provider job outcomes are checked here; interpretation of criterion evidence is
explicitly attributed to the owner. The original worker report is never changed.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .accepted_node import accepted_node_contract
from .continuation import digest
from .deployment_release_provider import ReleaseProvider
from .dispatch_pass import attempt_run_id
from .execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .operations_release_resolution import operational_execution_pins
from .operations_repair import repair_revisions
from .repository import OrchestrationRepository
from .run_reports import OrchestrationRunReport
from .state import ActorKind, NodeState, transition

KIND = "external_completion_accepted"
CONTRACT = "external-operations-completion/v1"


class CriterionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Accepted plans use the full criterion text as its identity, including prose.
    criterion_id: str = Field(min_length=1, max_length=4000)
    job_id: int = Field(strict=True, gt=0)
    evidence_ref: str = Field(min_length=1, max_length=2048)


class ExternalCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=36)
    expected_plan_version: int = Field(strict=True, gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_attempt: int = Field(strict=True, gt=0)
    workflow_run_id: int = Field(strict=True, gt=0)
    workflow_run_attempt: int = Field(strict=True, gt=0)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    workflow_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    criteria: list[CriterionEvidence] = Field(max_length=100)
    required_job_ids: list[int] = Field(min_length=1, max_length=100)
    release_evidence_ref: str = Field(min_length=1, max_length=2048)
    summary: str = Field(min_length=20, max_length=4000)
    reason: str = Field(min_length=20, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ExternalCompletionError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ExternalCompletionError(code)


async def snapshot(session, *, flow_id, actor, request, lock=False):
    require(actor.actor_id and actor.actor_role in {"owner", "org_admin", "platform_admin"}, "plan_approver_required")
    query = select(OrchestrationFlow).where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == actor.org_id)
    flow = await session.scalar(query.with_for_update() if lock else query)
    require(flow is not None and flow.archived_at is None, "flow_not_found")
    query = (
        select(OrchestrationNode)
        .where(OrchestrationNode.id == request.node_id, OrchestrationNode.flow_id == flow_id, OrchestrationNode.org_id == actor.org_id)
        .execution_options(populate_existing=True)
    )
    node = await session.scalar(query.with_for_update() if lock else query)
    require(node is not None, "node_not_found")
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=actor.org_id, flow_id=flow_id)
    require(
        plan is not None and (plan.version, plan.plan_hash) == (request.expected_plan_version, request.expected_plan_hash), "accepted_plan_changed"
    )
    require(node.state in {"ready", "failed", "passed"} and node.attempts == request.expected_attempt, "node_assignment_changed")
    contract = await accepted_node_contract(session, node)
    release = getattr(contract.node, "operational_release_binding", None) if contract else None
    require(
        contract
        and node.kind == "story"
        and contract.node.executor
        and contract.node.executor.role == "operate"
        and release is not None
        and contract.node.evidence_expectations == [f"runtime_verified:{release.component}"],
        "operations_story_required",
    )
    criteria = [c.criterion_id for c in request.criteria]
    require(len(set(criteria)) == len(criteria) and set(criteria) == set(contract.node.criteria), "criterion_coverage_changed")
    run_id = attempt_run_id(node.id, node.attempts)
    report = await session.get(OrchestrationRunReport, run_id)
    execution = await session.scalar(
        select(OrchestrationExecution)
        .where(OrchestrationExecution.org_id == node.org_id, OrchestrationExecution.node_id == node.id, OrchestrationExecution.cycle == node.attempts)
        .execution_options(populate_existing=True)
    )
    require(report is not None and execution is not None, "prior_assignment_missing")
    terminal = report.terminal_receipt or {}
    require(
        (report.org_id, report.flow_id, report.node_id, report.attempt) == (node.org_id, node.flow_id, node.id, node.attempts)
        and report.persona == contract.node.executor.persona
        and execution.flow_id == node.flow_id
        and terminal.get("outcome") in {"complete", "failed"}
        and terminal.get("run_id") == run_id
        and terminal.get("attempt") == node.attempts,
        "worker_exit_unverified",
    )
    claim_query = select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == execution.claim_id).execution_options(populate_existing=True)
    claim = await session.scalar(claim_query.with_for_update() if lock else claim_query)
    require(
        claim is not None
        and claim.org_id == node.org_id
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == flow_id
        and claim.state == "released"
        and claim.release_reason in {"failed", "abandoned", "completed"}
        and claim.active_run_id is None
        and claim.claim_event_id == run_id
        and claim.generation == execution.claim_generation
        and execution.pending_action_key is None,
        "prior_execution_not_reconciled",
    )
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(OrchestrationAction.org_id == node.org_id, OrchestrationAction.execution_id == execution.id)
            .order_by(OrchestrationAction.id)
            .limit(501)
        )
    )
    require(len(actions) <= 500 and all(a.status in {"succeeded", "failed"} for a in actions), "external_effects_unresolved")
    pins = await operational_execution_pins(session, node=node, execution=execution, contract=contract, run_id=run_id)
    pins = (
        await repair_revisions(
            session,
            node_id=node.id,
            org_id=node.org_id,
            plan_version=execution.accepted_plan_version,
            cycle=node.attempts,
            pins=pins,
            contract=contract,
            require_merged=True,
        )
        if pins is not None
        else None
    )
    require(pins is not None and pins["source_revision"] == request.source_revision, "release_source_changed")
    state = {
        "node_state": node.state,
        "attempt": node.attempts,
        "execution_id": execution.id,
        "revision": execution.revision,
        "claim_id": claim.id,
        "claim_generation": claim.generation,
        "terminal_digest": digest(terminal),
        "definition_revision": pins["definition_revision"],
        "actions": [(a.id, a.status, digest(a.detail or {})) for a in actions],
    }
    return flow, node, plan, contract, execution, report, state


async def verify_provider(report, contract, request, provider, *, definition_revision):
    release = contract.node.operational_release_binding
    binding = SimpleNamespace(
        org_id=report.org_id, repo=report.repo, installation_id=report.installation_id, provider_repository_id=report.provider_repository_id
    )
    require(report.repo == release.repository, "repository_changed")
    run = (
        await provider.request(
            binding, "GET", f"/repos/{binding.repo}/actions/runs/{request.workflow_run_id}/attempts/{request.workflow_run_attempt}"
        )
    ).json()
    require(
        run.get("id") == request.workflow_run_id
        and run.get("run_attempt") == request.workflow_run_attempt
        and run.get("status") == "completed"
        and run.get("head_sha") == request.workflow_revision
        and (run.get("repository") or {}).get("id") == binding.provider_repository_id
        and run.get("path") in {".github/workflows/" + w for w in release.workflows},
        "workflow_identity_unverified",
    )
    if request.workflow_revision != definition_revision:
        # A manual workflow executes at the default-branch head at dispatch time.
        # Reuse normal runtime verification's source ancestry and content checks.
        require(run.get("event") == "workflow_dispatch", "release_workflow_changed")
        await provider.contains(binding, request.source_revision, request.workflow_revision)
        require(
            await provider.workflow_content_matches(binding, SimpleNamespace(path=run["path"]), definition_revision, request.workflow_revision),
            "release_workflow_changed",
        )
    jobs = await provider.pages(
        binding, f"/repos/{binding.repo}/actions/runs/{request.workflow_run_id}/attempts/{request.workflow_run_attempt}/jobs", "jobs"
    )
    required = set(request.required_job_ids) | {c.job_id for c in request.criteria}
    selected = [j for j in jobs if j.get("id") in required]
    require(
        len(selected) == len(required)
        and all(
            j.get("status") == "completed"
            and j.get("conclusion") == "success"
            and j.get("run_id") == request.workflow_run_id
            and j.get("run_attempt") == request.workflow_run_attempt
            and j.get("head_sha") == request.workflow_revision
            for j in selected
        ),
        "required_jobs_not_successful",
    )
    return {
        "repository": binding.repo,
        "repository_id": binding.provider_repository_id,
        "installation_id": binding.installation_id,
        "workflow": run["path"],
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "workflow_conclusion": run.get("conclusion"),
        "workflow_url": run["html_url"],
        "source_revision": request.source_revision,
        "workflow_revision": request.workflow_revision,
        "approved_definition_revision": definition_revision,
        "component": release.component,
        "target": release.target,
        "verified_jobs": [{k: j[k] for k in ("id", "name", "conclusion", "html_url")} for j in selected],
        "other_failed_jobs": [{k: j[k] for k in ("id", "name", "conclusion", "html_url")} for j in jobs if j.get("conclusion") == "failure"],
    }


async def complete_external(session, *, flow_id, actor, request, accept=False, provider=None):
    require(actor.actor_id and actor.actor_role in {"owner", "org_admin", "platform_admin"}, "plan_approver_required")
    previous = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == actor.org_id,
            OrchestrationDecision.flow_id == flow_id,
            OrchestrationDecision.node_id == request.node_id,
            OrchestrationDecision.kind == KIND,
        )
        .order_by(OrchestrationDecision.created_at.desc())
        .limit(1)
    )
    request_body = request.model_dump(exclude={"expected_snapshot"})
    if previous is not None:
        saved = json.loads(previous.reason)
        require(previous.actor_id == actor.actor_id and saved["request"] == request_body, "completion_already_recorded")
        return {"accepted": True, "created": False, "state": "passed", "decision_id": previous.id}
    values = await snapshot(session, flow_id=flow_id, actor=actor, request=request)
    flow, node, plan, contract, execution, report, state = values
    require(node.state != "passed", "completion_already_recorded")
    proof = await verify_provider(report, contract, request, provider or ReleaseProvider(), definition_revision=state["definition_revision"])
    token = digest({"state": state, "request": request_body, "actor": actor.actor_id, "proof": proof})
    if not accept:
        return {
            "accepted": False,
            "snapshot": token,
            "resulting_state": "passed",
            "authority": "owner_acceptance_of_external_evidence",
            "proof": proof,
        }
    require(request.expected_snapshot == token, "completion_snapshot_changed")
    locked = await snapshot(session, flow_id=flow_id, actor=actor, request=request, lock=True)
    require(locked[-1] == state, "completion_snapshot_changed")
    _, node, _, _, execution, report, _ = locked
    transition_result = transition(
        node.state, NodeState.PASSED, actor_kind=ActorKind.HUMAN, reason=request.reason, external_completion_authorized=True
    )
    require(transition_result.allowed, "completion_transition_refused")
    identity = ExecutionIdentity(node.org_id, node.id, node.attempts, execution.accepted_plan_version, execution.claim_id, execution.claim_generation)
    outcome = await advance_execution(
        session,
        identity=identity,
        released_failure_run_id=report.run_id,
        advance=PhaseAdvance(
            phase=ExecutionPhase.CONCLUDED,
            status=ExecutionStatus.CONCLUDED,
            expected_revision=execution.revision,
            progress_note="Owner accepted externally verified work; original worker result retained.",
        ),
    )
    require(outcome.kind is OutcomeKind.APPLIED, "completion_execution_changed")
    content = {
        "contract": CONTRACT,
        "request": request_body,
        "proof": proof,
        "execution_id": execution.id,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "worker_outcome": report.terminal_receipt["outcome"],
        "authority": "owner_acceptance_of_external_evidence",
        "summary": request.summary,
    }
    decision_id = str(uuid5(NAMESPACE_URL, f"{CONTRACT}:{node.org_id}:{node.id}:{node.attempts}:{token}"))
    session.add(
        OrchestrationDecision(
            id=decision_id,
            org_id=node.org_id,
            flow_id=flow_id,
            node_id=node.id,
            kind=KIND,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind="human",
            reason=json.dumps(content),
            from_state=node.state,
            to_state="passed",
        )
    )
    node.state = "passed"
    node.updated_at = datetime.now(UTC)
    await session.flush()
    return {"accepted": True, "created": True, "state": "passed", "decision_id": decision_id, "proof": proof}


async def has_verified_external_completion(session, node):
    """Recognize the current accepted completion without inferring model spend.

    Admission must still read the initialized flow meter and retain outstanding
    holds. A completion proves the work finished, not that its worker cost zero.
    """
    accepted = await accepted_node_contract(session, node)
    if accepted is None or accepted.node.operational_release_binding is None:
        return False
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    if plan is None:
        return False
    try:
        return await external_release_source(session, node, accepted, plan) is not None
    except (ExternalCompletionError, KeyError, TypeError, ValueError):
        return False


async def external_release_source(session, parent, accepted, plan, *, repository_id=None, installation_id=None):
    """Historical lineage from an attributed external acceptance, not a worker result."""
    record = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == parent.org_id,
            OrchestrationDecision.flow_id == parent.flow_id,
            OrchestrationDecision.node_id == parent.id,
            OrchestrationDecision.kind == KIND,
        )
        .order_by(OrchestrationDecision.created_at.desc())
        .limit(1)
    )
    if record is None:
        return None
    from .operations_release_resolution import ReleaseSource
    from .plan_lineage import ancestor_plan

    content = json.loads(record.reason)
    old = await ancestor_plan(session, plan, content.get("plan_version"), node_id=parent.id)
    request = ExternalCompletionRequest.model_validate(content["request"])
    proof = content["proof"]
    require(
        record.actor_kind == "human"
        and bool(record.actor_id)
        and record.to_state == parent.state == "passed"
        and content.get("contract") == CONTRACT
        and old is not None
        and old.plan_hash == content["plan_hash"]
        and (request.expected_plan_version, request.expected_plan_hash) == (old.version, old.plan_hash)
        and request.node_id == parent.id
        and request.expected_attempt == parent.attempts
        and proof["repository"] == accepted.node.operational_release_binding.repository
        and proof["component"] == accepted.node.operational_release_binding.component
        and proof["target"] == accepted.node.operational_release_binding.target
        and (repository_id is None or proof["repository_id"] == repository_id)
        and (installation_id is None or proof["installation_id"] == installation_id)
        and (proof["source_revision"], proof["workflow_revision"]) == (request.source_revision, request.workflow_revision)
        and (proof["run_id"], proof["run_attempt"]) == (request.workflow_run_id, request.workflow_run_attempt)
        and content["authority"] == "owner_acceptance_of_external_evidence"
        and len(request.criteria) == len({c.criterion_id for c in request.criteria})
        and {c.criterion_id for c in request.criteria} == set(accepted.node.criteria),
        "external_completion_changed",
    )
    execution = await session.get(OrchestrationExecution, content["execution_id"])
    require(
        execution is not None
        and (execution.org_id, execution.flow_id, execution.node_id, execution.cycle) == (parent.org_id, parent.flow_id, parent.id, parent.attempts)
        and execution.phase == execution.status == "concluded"
        and execution.pending_action_key is None,
        "external_execution_unsettled",
    )
    return ReleaseSource(
        request.source_revision,
        record.created_at.replace(tzinfo=UTC) if record.created_at.tzinfo is None else record.created_at,
        {
            "address": accepted.node.address,
            "external_completion_decision_id": record.id,
            "execution_id": execution.id,
            "source_revision": request.source_revision,
            "workflow_revision": request.workflow_revision,
            "workflow_run_id": request.workflow_run_id,
            "release_evidence_ref": request.release_evidence_ref,
            "authority": content["authority"],
        },
    )


async def external_receipt_address(session, *, node, receipt, repository):
    """Recover the address omitted by the first external-receipt producer.

    Keep the committed envelope unchanged. Only an exact match to a still-valid
    external acceptance in this flow can supply its missing predecessor address.
    """
    record = await session.get(OrchestrationDecision, receipt.get("external_completion_decision_id"))
    if record is None or (record.org_id, record.flow_id, record.kind) != (node.org_id, node.flow_id, KIND):
        return None
    parent = await session.get(OrchestrationNode, record.node_id)
    if parent is None or (parent.org_id, parent.flow_id) != (node.org_id, node.flow_id):
        return None
    accepted = await accepted_node_contract(session, parent)
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    if accepted is None or plan is None or accepted.node.operational_release_binding is None:
        return None
    if accepted.node.operational_release_binding.repository != repository:
        return None
    try:
        source = await external_release_source(session, parent, accepted, plan)
    except (ExternalCompletionError, KeyError, TypeError, ValueError):
        return None
    if source is not None and {key: value for key, value in source.provenance.items() if key != "address"} == receipt:
        return source.provenance["address"]
    return None

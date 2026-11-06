"""One reviewed gate answer atomically authorizes its configured evaluation runs."""

import hashlib

from sqlalchemy import select

from .compile import PolicyNotAcceptableError
from .evaluation_acceptance import EvaluationAcceptanceRequest, accept_evaluation, preview_evaluation
from .evaluation_plan import accepted_evaluation, human_evaluation, latest_human_gate_evidence, legacy_worker_gate
from .execution_policy import Action
from .models import OrchestrationEdge, OrchestrationFlow, OrchestrationNode
from .policy_admission import load_in_force_policy
from .repository import OrchestrationRepository
from .repository_evaluation_contract import canonical
from .shared_window import WindowRenewalRequest, accept_window_renewal, preview_window_renewal
from .window_view import execution_window_view


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def renew(session, flow_id, actor, request):
    body = WindowRenewalRequest.model_validate(request)
    preview = await preview_window_renewal(session, flow_id=flow_id, actor=actor, request=body)
    await accept_window_renewal(session, flow_id=flow_id, actor=actor, request=body.model_copy(update={"expected_snapshot": preview["snapshot"]}))


async def prepare_gate_execution(session, *, gate, actor, window_request=None):
    if gate.kind == "eval" and not await legacy_worker_gate(session, gate):
        row, evidence = await latest_human_gate_evidence(session, gate)
        return {
            "required": False,
            "ready": row is not None,
            "runs": [],
            "problems": [] if row is not None else ["The human evaluation gate has no verified evidence binding."],
            "evidence_id": row.id if row is not None else None,
            "evidence": evidence,
        }
    repo = OrchestrationRepository(session)
    plan = await repo.get_accepted_plan(org_id=gate.org_id, flow_id=gate.flow_id)
    children = (
        list(
            await session.scalars(
                select(OrchestrationNode)
                .join(OrchestrationEdge, OrchestrationEdge.to_node_id == OrchestrationNode.id)
                .where(
                    OrchestrationEdge.org_id == gate.org_id,
                    OrchestrationEdge.from_node_id == gate.id,
                    OrchestrationNode.org_id == gate.org_id,
                    OrchestrationNode.flow_id == gate.flow_id,
                    OrchestrationNode.kind == "eval",
                )
                .order_by(OrchestrationNode.id)
            )
        )
        if gate.kind == "gate"
        else []
    )
    # Legacy worker evaluations do not use a native workflow grant. Their
    # existing dispatch path stays responsible for readiness; this preview
    # applies to evaluations governed by an accepted execution policy.
    if not children or (plan is not None and not (plan.plan_document or {}).get("execution_policy")):
        return {"required": False, "ready": True, "runs": [], "problems": []}
    flow = await repo.get_flow(org_id=gate.org_id, flow_id=gate.flow_id)
    if plan is None or flow is None:
        raise PolicyNotAcceptableError("The accepted plan could not be verified.")
    inputs = await load_in_force_policy(session, org_id=gate.org_id, flow_id=gate.flow_id)
    window = await execution_window_view(session, flow=flow, nodes=children, inputs=inputs)
    if window_request is None and window and window.get("status") == "expired":
        window_request = window.get("renewal_request")
    runs, grants, problems = [], [], []
    # Preview exactly the policy that confirmation would create. The savepoint
    # always rolls back; only the answer transaction can commit these grants.
    transaction = await session.begin_nested()
    try:
        if window_request:
            await renew(session, gate.flow_id, actor, window_request)
        for child in children:
            accepted = await accepted_evaluation(session, child)
            if accepted is None:
                problems.append(f"{child.title}: the plan needs an executable evaluation specification (workflow, target, limits and evidence).")
                continue
            _, spec, _ = accepted
            if (
                spec.evidence_schema == "orchestration-evaluation/v1"
                and inputs.policy is not None
                and inputs.refusal is None
                and await human_evaluation(session, child) is not None
            ):
                # The tick presents this accepted human review with predecessor
                # evidence; it does not dispatch an evaluation. Requiring the
                # automated action here would block a legitimate human gate.
                # Workflow/repository producers retain their execution checks.
                continue
            if spec.evidence_schema != "workflow-evaluation/v1":
                # Existing machine evaluations retain their previously accepted
                # authority. They must already be executable before this gate.
                if inputs.policy is None or not inputs.policy.permits(Action.EVALUATE):
                    problems.append(f"{child.title}: evaluation authority is not configured.")
                continue
            request = EvaluationAcceptanceRequest(
                node_id=child.id,
                expected_plan_version=plan.version,
                expected_plan_hash=plan.plan_hash,
                specification=spec.model_dump(mode="json"),
                authorize_evaluate=True,
                authorize_workflow_dispatch=True,
                reason=f"Execute the evaluation reviewed with gate {gate.node_ref}; final acceptance remains human.",
            )
            preview = await preview_evaluation(session, flow_id=gate.flow_id, actor=actor, request=request)
            from .repository_evaluation import sources_for
            from .repository_producer import RepositoryScanProvider, binding_for

            provider = RepositoryScanProvider()
            await provider.preflight(
                await binding_for(session, child, spec), spec, await sources_for(session, child, plan, spec, preview_gate_id=gate.id)
            )
            grants.append({**request.model_dump(mode="json"), "expected_snapshot": preview["snapshot"]})
            runs.append(
                {
                    "node_id": child.id,
                    "title": child.title,
                    "workflow": spec.workflows[0].path,
                    "target": spec.producer.target.model_dump(mode="json"),
                    "inputs": spec.producer.inputs,
                    "acceptance": "human",
                    "criteria": [p.criterion_id for a in spec.workflows[0].artifacts for p in a.predicates],
                }
            )
    finally:
        await transaction.rollback()
    content = {"gate_id": gate.id, "plan_hash": plan.plan_hash, "runs": runs, "grants": grants, "window_request": window_request}
    return {"required": True, "ready": not problems, "problems": problems, "snapshot": digest(content), **content}


async def authorize_gate_execution(session, *, gate, actor, reviewed):
    prepared = await prepare_gate_execution(session, gate=gate, actor=actor, window_request=(reviewed or {}).get("window_request"))
    flow = await session.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.org_id == gate.org_id, OrchestrationFlow.id == gate.flow_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if flow is None or flow.archived_at is not None:
        raise PolicyNotAcceptableError("Unarchive this flow before approving new gate work.")

    if not prepared["required"]:
        return
    if not prepared["ready"]:
        raise PolicyNotAcceptableError("Cannot start the next step: " + " ".join(prepared["problems"]))
    if reviewed is None or reviewed.get("snapshot") != prepared["snapshot"]:
        raise PolicyNotAcceptableError(
            "Review the next-step execution preview before approving this gate; its configuration changed or was not reviewed."
        )
    if prepared["window_request"]:
        await renew(session, gate.flow_id, actor, prepared["window_request"])
    for grant in prepared["grants"]:
        await accept_evaluation(session, flow_id=gate.flow_id, actor=actor, request=EvaluationAcceptanceRequest.model_validate(grant))

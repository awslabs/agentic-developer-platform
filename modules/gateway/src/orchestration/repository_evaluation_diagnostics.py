"""Current repository refusals are display evidence, never evaluation authority."""

from __future__ import annotations

import json
from datetime import UTC

from .evaluation_acceptance import accepted_contract, digest
from .evaluation_contract import MULTI_REPOSITORY_SCHEMA
from .evaluation_plan import accepted_evaluation
from .review_cycle import CycleBlockedError

ACTOR = "system:repository-evaluation"
CONTRACT = "repository-evaluation-refusal/v1"
ACTION = "repository_evaluation_blocked"
SCHEMAS = {"repository-evaluation/v1", "cli-live-evaluation/v1", "workflow-evaluation/v1", MULTI_REPOSITORY_SCHEMA}

# Never render ledger/provider prose. Unknown producer failures use the explicit
# generic diagnosis; an unknown diagnostic in a stored document is not trusted.
DETAILS = {
    "repository_evaluation_harness_changed": (
        "flow-owner",
        "The accepted evaluation harness differs from the installed verifier.",
        "An accepted specification pinned to the reviewed installed harness.",
        "Review and accept the current evaluation specification, then recheck evidence.",
    ),
    "repository_evaluation_producer_harness_changed": (
        "flow-owner",
        "The accepted evidence producer harness differs from the installed verifier.",
        "An accepted producer specification pinned to the reviewed installed harness.",
        "Review and accept the current producer specification before retrying evaluation.",
    ),
    "required_inputs_unavailable": (
        "flow-owner",
        "The accepted evaluation has unresolved prerequisites.",
        "The required inputs named in the accepted evaluation plan.",
        "Supply the accepted prerequisites before retrying evaluation.",
    ),
    "repository_evaluation_installation_missing": (
        "platform-operator",
        "The repository installation needed to read evaluation evidence is unavailable.",
        "An authorized repository installation for this tenant.",
        "Restore the repository connection, then recheck evaluation evidence.",
    ),
    "repository_evaluation_authority_unavailable": (
        "flow-owner",
        "Current evaluation authority could not be verified.",
        "Current accepted evaluation permission within the approved scope and limits.",
        "Review the evaluation authority and resolve its recorded refusal before retrying.",
    ),
    "repository_evaluation_time_budget": (
        "evidence-producer",
        "The bounded repository evidence read did not finish in time.",
        "Repository evidence available within the observer's read deadline.",
        "Check evidence-provider availability before the next bounded engine observation.",
    ),
    "repository_evaluation_provider_unverifiable": (
        "evidence-producer",
        "Repository evidence could not be verified against the accepted specification.",
        "Successful evidence matching the accepted repository evaluation specification.",
        "Resolve the recorded evidence refusal and recheck authenticated repository evidence.",
    ),
    "repository_evaluation_predecessor_deployment_receipt_missing": (
        "evidence-producer",
        "An accepted source has no verified deployment receipt.",
        "Successful deployment receipts for every accepted source.",
        "Deploy the missing source and recheck authenticated deployment evidence.",
    ),
    "repository_evaluation_predecessor_target_contract_mismatch": (
        "evidence-producer",
        "The source deployments do not match the accepted target contract.",
        "Deployments of the accepted sources on a compatible target.",
        "Verify the reviewed target contract before retrying evaluation.",
    ),
    "repository_evaluation_predecessor_deployment_expired": (
        "evidence-producer",
        "An accepted source deployment receipt has expired.",
        "Fresh authenticated deployment evidence for every accepted source.",
        "Reverify the current deployment before retrying evaluation.",
    ),
    "repository_evaluation_deployment_receipt_scope_changed": (
        "evidence-producer",
        "An accepted source deployment receipt has lost its authenticated producer binding.",
        "A server-owned deployment receipt with the accepted source and target scope.",
        "Reconcile the authenticated deployment receipt before retrying evaluation.",
    ),
    "repository_evaluation_criteria_failed": (
        "evidence-producer",
        "Current repository evidence was read successfully but mandatory evaluation criteria failed.",
        "Successful outcomes for every mandatory criterion in the accepted specification.",
        "Repair the failed criteria and publish fresh evidence for the accepted specification.",
    ),
}


def diagnostic_code(reason):
    if isinstance(reason, str) and reason in DETAILS:
        return reason
    if isinstance(reason, str) and reason.startswith("repository_evaluation_authority_"):
        return "repository_evaluation_authority_unavailable"
    return "repository_evaluation_provider_unverifiable"


async def evaluation_scope(session, node, *, accepted=None):
    """Resolve exactly the accepted contract the observer uses, without provider I/O.

    A plan renewal may preserve an older acceptance through plan lineage. The
    current plan and the validated preserved acceptance are both pinned, so an
    old refusal cannot follow a new plan or a replacement overlay automatically.
    """
    accepted = accepted if accepted is not None else await accepted_evaluation(session, node)
    if accepted is None or accepted[1].evidence_schema not in SCHEMAS:
        return None
    plan, spec, _ = accepted
    attached = await accepted_contract(session, node=node, plan=plan)
    return {
        "org_id": node.org_id,
        "flow_id": node.flow_id,
        "node_id": node.id,
        "attempt": node.attempts,
        "accepted_plan_id": plan.id,
        "accepted_plan_version": plan.version,
        "accepted_plan_hash": plan.plan_hash,
        "specification_hash": digest(spec.model_dump(mode="json")),
        "acceptance_decision_id": attached[0].id if attached is not None else None,
    }


def scoped_refusal(payload, scope):
    if scope is None:
        return payload
    return {**payload, "contract": CONTRACT, "scope": scope, "diagnostic_code": diagnostic_code(payload.get("block_code"))}


def _refusal_code(payload, scope):
    if (
        payload.get("contract") == CONTRACT
        and payload.get("action") == ACTION
        and payload.get("diagnostic_code") in DETAILS
        and isinstance(payload.get("scope"), dict)
        # bool equals int in Python; identity comparisons must not admit it.
        and type(payload["scope"].get("attempt")) is int
        and type(payload["scope"].get("accepted_plan_version")) is int
        and payload["scope"] == scope
    ):
        return payload["diagnostic_code"]
    return None


def _current_receipt(payload, scope, spec):
    if spec.evidence_schema == MULTI_REPOSITORY_SCHEMA:
        return None
    from .repository_evaluation import receipt_model

    raw = payload.get("receipt")
    if payload.get("action") != "repository_evaluation" or not isinstance(raw, dict):
        return None
    if type(raw.get("cycle")) is not int or type(raw.get("accepted_plan_version")) is not int:
        return None
    receipt = receipt_model(spec).model_validate(raw)
    fields = ("org_id", "flow_id", "node_id", "accepted_plan_version", "accepted_plan_hash", "specification_hash", "acceptance_decision_id")
    if (
        all(getattr(receipt, key) == scope[key] for key in fields)
        and receipt.cycle == scope["attempt"] + 1
        and receipt.harness_sha256 == spec.runner.harness_sha256
    ):
        return receipt
    return None


async def current_diagnostic(session, node, refusal, observation=None):
    if node.kind != "eval" or node.state not in {"pending", "ready"}:
        return None
    candidates = [
        row for row in (refusal, observation) if row is not None and row.node_id == node.id and row.actor_id == ACTOR and row.actor_kind == "service"
    ]
    if not candidates:
        return None
    try:
        accepted = await accepted_evaluation(session, node)
        scope = await evaluation_scope(session, node, accepted=accepted)
        if scope is None:
            return None
    except (ValueError, TypeError, RecursionError, CycleBlockedError):
        return None
    # At most two indexed records, newest first. A resumed read with failed
    # criteria supersedes a transient connection refusal without changing state.
    for decision in sorted(candidates, key=lambda row: (row.created_at, row.id), reverse=True):
        try:
            raw = decision.rejection_reason if decision.kind == "transition_rejected" else decision.reason
            if raw is None or len(raw) > 512 * 1024:
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            if decision.kind == "transition_rejected":
                code = _refusal_code(payload, scope)
                if code is None:
                    continue
            elif decision.kind == "result_observed":
                receipt = _current_receipt(payload, scope, accepted[1])
                if receipt is None:
                    continue
                if receipt.mandatory_passed:
                    return None
                code = "repository_evaluation_criteria_failed"
            else:
                continue
        except (ValueError, TypeError, RecursionError):
            # Unknown results cannot clear a verified current refusal.
            continue
        recorded_at = decision.created_at.replace(tzinfo=decision.created_at.tzinfo or UTC)
        return {"code": code, "observed_at": recorded_at.isoformat()}
    return None

"""Safe, actionable descriptions of dispatch admission refusals."""

from .execution_policy import DenyReason

CONTRACT = "dispatch-admission-refusal/v1"
ACTOR = "system:orchestration-dispatch"

# Descriptions deliberately exclude exception text, credential references and
# provider payloads. A refusal records only its typed reason and current scope.
_DESCRIPTIONS = {
    "budget_unavailable": (
        "platform-operator",
        "The accepted flow budget is unavailable; no worker can start.",
        "Restore access to the existing flow budget and reconcile its recorded usage; do not reset the allowance.",
    ),
    "spend_unknown": (
        "platform-operator",
        "The flow's recorded spend could not be verified.",
        "Restore the spend ledger and reconcile usage before retrying admission.",
    ),
    "spend_limit_exceeded": (
        "flow-owner",
        "The accepted flow spend limit cannot admit another worker.",
        "Review the recorded spend and use the supported policy amendment process if a larger allowance is intended.",
    ),
    "concurrency_limit_exceeded": (
        "engine",
        "The accepted flow concurrency limit is occupied.",
        "Recheck admission after an active action completes.",
    ),
    "held_by_other_owner": (
        "engine",
        "Another active assignment owns this issue's work claim.",
        "Wait for the current assignment to finish and release ownership, then recheck admission.",
    ),
    "held_lease_lapsed": (
        "platform-operator",
        "The existing work claim has lost its heartbeat; ownership has not been released.",
        "Verify the current worker and reconcile ownership through the supported recovery process.",
    ),
    "installation_unresolved": (
        "platform-operator",
        "The repository installation could not be resolved for this tenant.",
        "Restore the tenant's repository installation before retrying admission.",
    ),
    "repository_unresolved": (
        "platform-operator",
        "The repository's immutable provider identity could not be verified.",
        "Restore repository lookup access before retrying admission.",
    ),
    "authority_unverifiable": (
        "platform-operator",
        "The execution authority or ownership record could not be verified.",
        "Reconcile the accepted policy, work claim and execution identity before retrying admission.",
    ),
}
_CLAIM_CODES = {
    "authority_required",
    "installation_unresolved",
    "invalid_repository",
    "repository_unresolved",
    "invalid_binding",
    "invalid_owner",
    "missing_event_id",
    "event_already_completed",
    "claim_race_lost",
    "held_by_other_owner",
    "held_lease_lapsed",
    "unrecognised_claim_state",
    "bind_refused",
    "stale_generation",
    "claim_not_held",
    "run_already_bound",
    "missing_run_id",
    "unknown_claim",
}


def safe_claim_code(code):
    return code if code in _CLAIM_CODES else "work_claim_unavailable"


def recognized_code(code):
    return code in _DESCRIPTIONS or code in _CLAIM_CODES or code == "work_claim_unavailable" or code in {reason.value for reason in DenyReason}


def description(code):
    """Return owner, safe detail and required next action for a typed code."""
    if code in _DESCRIPTIONS:
        return _DESCRIPTIONS[code]
    if code in {"credential_scope_unavailable", "authority_required", "schema_unsupported"}:
        return (
            "platform-operator",
            f"Dispatch authority is unavailable: {code}.",
            "Restore the accepted execution authority and required credentials before retrying admission.",
        )
    if code in {reason.value for reason in DenyReason}:
        return (
            "flow-owner",
            f"The accepted execution policy refused dispatch: {code}.",
            "Resolve this policy requirement through the appropriate approval or policy control, then recheck admission.",
        )
    return (
        "platform-operator",
        f"Dispatch ownership or authority could not be admitted: {safe_claim_code(code)}.",
        "Reconcile the existing work claim and dispatch authority before retrying admission.",
    )

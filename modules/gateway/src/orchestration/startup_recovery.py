"""Recognize fenced review startup failures without reopening their authority."""

from datetime import UTC, datetime
from hashlib import sha256

from src.agentauth.bootstrap_failure import is_bootstrap_failure


def is_retryable_bootstrap_failure(raw):
    """Only a recorded refusal or an unstarted, fenced continuation may retry."""
    return is_bootstrap_failure(raw) or bool(
        raw
        and raw.get("status") == {"S": "cancelled"}
        and raw.get("work_claim_cancellation") == {"S": "startup_deadline_exceeded"}
        and raw.get("orchestration_continuation_receipt")
        and raw.get("orchestration_continuation_action", {}).get("S") in {"review", "repair"}
        and raw.get("persona") == {"S": "agent-codex-reviewer"}
        and not raw.get("workload_binding")
        and "bootstrap_authority_issued_at" not in raw
    )


def attested_review_startup(raw, *, invocation_id, receipt):
    """Describe only a gateway-terminal, fenced reviewer assignment."""
    if (
        not is_retryable_bootstrap_failure(raw)
        or raw.get("persona") != {"S": "agent-codex-reviewer"}
        or raw.get("orchestration_continuation_action") != {"S": "review"}
        or raw.get("orchestration_continuation_receipt") != {"S": receipt}
        or "bootstrap_authority_issued_at" in raw
    ):
        return None
    try:
        attempt = int(raw["current_attempt"]["N"])
    except (KeyError, ValueError, TypeError):
        return None
    if attempt < 1:
        return None
    if is_bootstrap_failure(raw):
        category = "gateway_model_policy_refusal"
        step = "model_policy_snapshot"
        detail = "Gateway refused reviewer startup: model-policy snapshot missing"
        evidence = raw["bootstrap_failure_request_id"]["S"]
        observed = raw.get("bootstrap_failed_at", {}).get("S")
    else:
        category = "fenced_startup_deadline"
        step = "work_claim"
        detail = "Reviewer startup deadline exceeded before workload binding"
        evidence = invocation_id
        observed = raw.get("work_claim_cancelled_at", {}).get("S")
    if not isinstance(evidence, str) or not evidence or len(evidence) > 128:
        return None
    return {
        "version": 1,
        "original_invocation": invocation_id,
        "original_attempt": attempt,
        "failure_category": category,
        "step": step,
        "detail": detail,
        "evidence_ref": evidence,
        "observed_at": observed if isinstance(observed, str) and len(observed) <= 40 else datetime.now(UTC).isoformat(),
        "exemption_status": "bootstrap_exempt",
        "fingerprint": sha256(f"{category}:{step}:{detail}".encode()).hexdigest()[:16],
    }

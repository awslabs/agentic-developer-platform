"""Recognize fenced review startup failures without reopening their authority."""

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

"""Safe, bounded refusal codes for the worker's own review operations."""

import json

from urllib3.exceptions import HTTPError

# Do not expose arbitrary gateway bodies, identifiers, URLs or credential text.
_REASONS = frozenset({
    "spend_unknown", "spend_limit_exceeded", "attempt_limit_exceeded",
    "concurrency_limit_exceeded", "policy_expired", "wall_clock_limit_exceeded",
    "grant_revoked", "membership_revoked", "role_revoked", "stale_policy_version",
    "org_mismatch", "team_not_permitted", "action_not_permitted", "human_gate_required",
    "repository_not_permitted", "environment_not_permitted", "work_not_owned",
    "credential_scope_unavailable", "budget_unavailable",
    "review_checks_not_assigned", "review_checks_binding_changed",
    "review_checks_head_changed", "review_checks_assignment_changed",
    "review_checks_unavailable", "reviewer_deadline_exceeded",
    "execution_assignment_unverifiable", "execution_assignment_superseded",
    "fresh_verified_review_missing", "review_incomplete", "review_author_changed",
    "review_artifact_unverifiable", "review_artifact_namespace_mismatch",
    "review_artifact_integrity_failed", "review_history_limit",
})
_PATHS = frozenset({"/review-checks", "/review-result", "/artifacts/review-result"})


def review_refusal(response, path, fallback):
    """Return a public code and retry disposition; never relax a denied operation."""
    status = response.status_code
    retryable = status == 429 or status >= 500
    code = fallback
    if path in _PATHS:
        try:
            raw = response.raw.read(4097, decode_content=True)
            value = json.loads(raw) if len(raw) <= 4096 else None
            reason = value.get("detail") if isinstance(value, dict) else None
            if isinstance(reason, str) and reason in _REASONS:
                code = reason
                # Accounting may settle later. Wait without invoking the model or
                # doing GitHub work; the same request must be authorized afresh.
                retryable = status in {409, 429, 500, 502, 503, 504} and reason in {
                    "spend_unknown", "budget_unavailable", "review_checks_unavailable",
                }
        except (ValueError, TypeError, OSError, HTTPError):
            pass
    return code, retryable

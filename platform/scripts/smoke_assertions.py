"""Post-deploy smoke test assertions for the gateway.

Validates real Cognito-token responses from /access/status and
/admin/connections against expected multi-tenant behavior. Each assertion
returns a (passed: bool, reason: str) tuple. A failed assertion gives a
distinct named reason so operators can diagnose without reading logs.

Design ref: issue #3031.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AssertionResult:
    """Result of a single smoke assertion."""

    passed: bool
    reason: str


def assert_access_status_registered(
    response_json: dict, status_code: int
) -> AssertionResult:
    """Verify /access/status returns 200 with status='registered'.

    Catches: user not onboarded, token invalid, or endpoint misconfigured.
    """
    if status_code == 401:
        return AssertionResult(
            passed=False,
            reason="SMOKE_TOKEN_REJECTED: /access/status returned 401 — token may be expired or invalid",
        )
    if status_code != 200:
        return AssertionResult(
            passed=False,
            reason=f"SMOKE_STATUS_HTTP_ERROR: /access/status returned HTTP {status_code}, expected 200",
        )
    status = response_json.get("status")
    if status != "registered":
        return AssertionResult(
            passed=False,
            reason=f"SMOKE_NOT_REGISTERED: /access/status returned status='{status}', expected 'registered'",
        )
    return AssertionResult(passed=True, reason="access_status_ok")


def assert_connections_multi_tenant(
    response_json: dict, status_code: int
) -> AssertionResult:
    """Verify /admin/connections returns multi-tenant data.

    Asserts:
      - HTTP 200
      - >1 distinct tenant_id across connections
      - Exactly one group with is_active_tenant=true
      - Every connection has tenant_name populated

    Catches:
      - #3021-class: cognito_sub/users.id mismatch → single-org fallback
      - #3027-class: missing claim → membership sync skipped → single-tenant
    """
    if status_code == 401:
        return AssertionResult(
            passed=False,
            reason="SMOKE_TOKEN_REJECTED: /admin/connections returned 401 — token may be expired or invalid",
        )
    if status_code != 200:
        return AssertionResult(
            passed=False,
            reason=f"SMOKE_CONNECTIONS_HTTP_ERROR: /admin/connections returned HTTP {status_code}, expected 200",
        )

    connections = response_json.get("connections", [])
    if not connections:
        return AssertionResult(
            passed=False,
            reason="SMOKE_NO_CONNECTIONS: /admin/connections returned zero connections",
        )

    # Collect distinct tenant_ids
    tenant_ids = set()
    missing_tenant_name = []

    for conn in connections:
        tid = conn.get("tenant_id")
        if tid:
            tenant_ids.add(tid)
        # Check tenant_name
        if not conn.get("tenant_name"):
            missing_tenant_name.append(conn.get("installation_id", "unknown"))

    if len(tenant_ids) < 2:
        return AssertionResult(
            passed=False,
            reason=(
                f"SMOKE_SINGLE_TENANT: /admin/connections shows {len(tenant_ids)} distinct tenant_id(s), "
                "expected >1. This may indicate a #3021-class regression (cognito_sub/users.id mismatch) "
                "or membership sync failure."
            ),
        )

    if missing_tenant_name:
        return AssertionResult(
            passed=False,
            reason=(
                f"SMOKE_MISSING_TENANT_NAME: connections {missing_tenant_name} have empty tenant_name. "
                "This may indicate a #3027-class regression (access-token claim gap)."
            ),
        )

    # Exactly one distinct active-tenant group: count how many distinct tenant_ids
    # are marked active. We group by tenant_id since multiple connections can share
    # a tenant.
    active_tenants = {
        conn.get("tenant_id")
        for conn in connections
        if conn.get("is_active_tenant") is True
    }
    if len(active_tenants) != 1:
        return AssertionResult(
            passed=False,
            reason=(
                f"SMOKE_ACTIVE_TENANT_COUNT: expected exactly 1 active tenant, found {len(active_tenants)}. Active: {active_tenants}"
            ),
        )

    return AssertionResult(passed=True, reason="connections_multi_tenant_ok")


def assert_token_not_expired(
    status_code: int, response_json: dict | None = None
) -> AssertionResult:
    """Check if a 401 indicates token expiry vs. other auth failure.

    The gateway returns 401 responses as:
        {"detail": {"error": "<error_code>", "message": "<human_readable>"}}

    Classifies on `detail.error`:
      - "token_expired" → SMOKE_TOKEN_EXPIRED (operator must re-capture)
      - anything else  → SMOKE_AUTH_FAILURE

    Returns a specific SMOKE_TOKEN_EXPIRED reason so operators know to re-capture.
    """
    if status_code == 401:
        error_code = ""
        message = ""
        if response_json:
            detail = response_json.get("detail", {})
            if isinstance(detail, dict):
                error_code = detail.get("error", "")
                message = detail.get("message", "")
            else:
                # Fallback: detail might be a plain string in some edge cases
                message = str(detail) if detail else ""

        if error_code == "token_expired":
            return AssertionResult(
                passed=False,
                reason=(
                    "SMOKE_TOKEN_EXPIRED: refresh token has expired. "
                    "Re-run: ./platform/scripts/capture-smoke-token.sh --environment <env>"
                ),
            )
        description = message or error_code or "no detail"
        return AssertionResult(
            passed=False,
            reason=f"SMOKE_AUTH_FAILURE: 401 response — {description}",
        )
    return AssertionResult(passed=True, reason="token_valid")

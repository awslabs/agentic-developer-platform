"""Unit tests for post-deploy smoke test assertions.

Tests cover:
  - Happy path: multi-tenant connections, registered status
  - #3021-class regression: single-tenant (cognito_sub/users.id mismatch)
  - #3027-class regression: missing tenant_name (access-token claim gap)
  - Token expiry detection
  - Edge cases: empty connections, HTTP errors, no active tenant
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the module directly (it doesn't live in a package).
# Must register in sys.modules before exec so dataclasses can resolve __module__.
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "smoke_assertions.py"
_spec = importlib.util.spec_from_file_location("smoke_assertions", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["smoke_assertions"] = _mod
_spec.loader.exec_module(_mod)

assert_access_status_registered = _mod.assert_access_status_registered
assert_connections_multi_tenant = _mod.assert_connections_multi_tenant
assert_token_not_expired = _mod.assert_token_not_expired


# === Fixtures ===

MULTI_TENANT_OK = {
    "connections": [
        {
            "installation_id": "inst-1",
            "tenant_id": "org-alpha",
            "tenant_name": "Alpha Corp",
            "is_active_tenant": True,
            "repo_count": 5,
        },
        {
            "installation_id": "inst-2",
            "tenant_id": "org-beta",
            "tenant_name": "Beta Inc",
            "is_active_tenant": False,
            "repo_count": 3,
        },
    ],
}

SINGLE_TENANT_REGRESSION = {
    "connections": [
        {
            "installation_id": "inst-1",
            "tenant_id": "org-alpha",
            "tenant_name": "Alpha Corp",
            "is_active_tenant": True,
            "repo_count": 5,
        },
    ],
}

MISSING_TENANT_NAME_REGRESSION = {
    "connections": [
        {
            "installation_id": "inst-1",
            "tenant_id": "org-alpha",
            "tenant_name": "Alpha Corp",
            "is_active_tenant": True,
            "repo_count": 5,
        },
        {
            "installation_id": "inst-2",
            "tenant_id": "org-beta",
            "tenant_name": "",
            "is_active_tenant": False,
            "repo_count": 3,
        },
    ],
}

NO_ACTIVE_TENANT = {
    "connections": [
        {
            "installation_id": "inst-1",
            "tenant_id": "org-alpha",
            "tenant_name": "Alpha Corp",
            "is_active_tenant": False,
            "repo_count": 5,
        },
        {
            "installation_id": "inst-2",
            "tenant_id": "org-beta",
            "tenant_name": "Beta Inc",
            "is_active_tenant": False,
            "repo_count": 3,
        },
    ],
}

EMPTY_CONNECTIONS = {"connections": []}


# === Tests: assert_access_status_registered ===


class TestAccessStatusRegistered:
    def test_happy_path(self):
        result = assert_access_status_registered({"status": "registered"}, 200)
        assert result.passed is True
        assert result.reason == "access_status_ok"

    def test_not_registered(self):
        result = assert_access_status_registered({"status": "new"}, 200)
        assert result.passed is False
        assert "SMOKE_NOT_REGISTERED" in result.reason

    def test_401_rejected(self):
        result = assert_access_status_registered(
            {
                "detail": {
                    "error": "invalid_token",
                    "message": "Invalid, expired, or revoked token",
                }
            },
            401,
        )
        assert result.passed is False
        assert "SMOKE_TOKEN_REJECTED" in result.reason

    def test_500_error(self):
        result = assert_access_status_registered(
            {"detail": "Internal server error"}, 500
        )
        assert result.passed is False
        assert "SMOKE_STATUS_HTTP_ERROR" in result.reason
        assert "500" in result.reason


# === Tests: assert_connections_multi_tenant ===


class TestConnectionsMultiTenant:
    def test_happy_path_multi_tenant(self):
        result = assert_connections_multi_tenant(MULTI_TENANT_OK, 200)
        assert result.passed is True
        assert result.reason == "connections_multi_tenant_ok"

    def test_single_tenant_regression(self):
        """Catches #3021-class: only one tenant visible despite membership in multiple."""
        result = assert_connections_multi_tenant(SINGLE_TENANT_REGRESSION, 200)
        assert result.passed is False
        assert "SMOKE_SINGLE_TENANT" in result.reason
        assert "#3021" in result.reason

    def test_missing_tenant_name_regression(self):
        """Catches #3027-class: tenant_name empty due to access-token claim gap."""
        result = assert_connections_multi_tenant(MISSING_TENANT_NAME_REGRESSION, 200)
        assert result.passed is False
        assert "SMOKE_MISSING_TENANT_NAME" in result.reason
        assert "#3027" in result.reason

    def test_no_active_tenant(self):
        """No tenant marked active — assertion still passes (active check looks at count != 1)."""
        result = assert_connections_multi_tenant(NO_ACTIVE_TENANT, 200)
        assert result.passed is False
        assert "SMOKE_ACTIVE_TENANT_COUNT" in result.reason

    def test_empty_connections(self):
        result = assert_connections_multi_tenant(EMPTY_CONNECTIONS, 200)
        assert result.passed is False
        assert "SMOKE_NO_CONNECTIONS" in result.reason

    def test_401_rejected(self):
        result = assert_connections_multi_tenant(
            {
                "detail": {
                    "error": "missing_token",
                    "message": "Authorization header required",
                }
            },
            401,
        )
        assert result.passed is False
        assert "SMOKE_TOKEN_REJECTED" in result.reason

    def test_503_error(self):
        result = assert_connections_multi_tenant({"detail": "Service unavailable"}, 503)
        assert result.passed is False
        assert "SMOKE_CONNECTIONS_HTTP_ERROR" in result.reason


# === Tests: assert_token_not_expired ===


class TestTokenNotExpired:
    def test_valid_response(self):
        result = assert_token_not_expired(200)
        assert result.passed is True

    def test_expired_token(self):
        """detail.error == 'token_expired' → SMOKE_TOKEN_EXPIRED."""
        result = assert_token_not_expired(
            401, {"detail": {"error": "token_expired", "message": "Token has expired"}}
        )
        assert result.passed is False
        assert "SMOKE_TOKEN_EXPIRED" in result.reason
        assert "capture-smoke-token.sh" in result.reason

    def test_auth_failure_invalid_token(self):
        """detail.error != 'token_expired' → SMOKE_AUTH_FAILURE."""
        result = assert_token_not_expired(
            401,
            {
                "detail": {
                    "error": "invalid_token",
                    "message": "Invalid, expired, or revoked token",
                }
            },
        )
        assert result.passed is False
        assert "SMOKE_AUTH_FAILURE" in result.reason

    def test_auth_failure_missing_token(self):
        """detail.error = 'missing_token' → SMOKE_AUTH_FAILURE."""
        result = assert_token_not_expired(
            401,
            {
                "detail": {
                    "error": "missing_token",
                    "message": "Authorization header required",
                }
            },
        )
        assert result.passed is False
        assert "SMOKE_AUTH_FAILURE" in result.reason

    def test_401_no_body(self):
        """401 with no response body defaults to SMOKE_AUTH_FAILURE."""
        result = assert_token_not_expired(401, None)
        assert result.passed is False
        assert "SMOKE_AUTH_FAILURE" in result.reason

    def test_401_empty_detail(self):
        """401 with empty detail dict → SMOKE_AUTH_FAILURE with 'no detail'."""
        result = assert_token_not_expired(401, {"detail": {}})
        assert result.passed is False
        assert "SMOKE_AUTH_FAILURE" in result.reason

    def test_401_string_detail_fallback(self):
        """If detail is a plain string (non-standard), still classifies as SMOKE_AUTH_FAILURE."""
        result = assert_token_not_expired(401, {"detail": "Something went wrong"})
        assert result.passed is False
        assert "SMOKE_AUTH_FAILURE" in result.reason


# === Tests: edge cases ===


class TestEdgeCases:
    def test_connections_multiple_active_tenants(self):
        """Two active tenants should fail (only 1 should be active at a time)."""
        body = {
            "connections": [
                {
                    "installation_id": "i-1",
                    "tenant_id": "org-a",
                    "tenant_name": "A",
                    "is_active_tenant": True,
                },
                {
                    "installation_id": "i-2",
                    "tenant_id": "org-b",
                    "tenant_name": "B",
                    "is_active_tenant": True,
                },
            ]
        }
        result = assert_connections_multi_tenant(body, 200)
        assert result.passed is False
        assert "SMOKE_ACTIVE_TENANT_COUNT" in result.reason

    def test_connections_null_tenant_name(self):
        """tenant_name=None (not just empty) should be caught."""
        body = {
            "connections": [
                {
                    "installation_id": "i-1",
                    "tenant_id": "org-a",
                    "tenant_name": "A",
                    "is_active_tenant": True,
                },
                {
                    "installation_id": "i-2",
                    "tenant_id": "org-b",
                    "tenant_name": None,
                    "is_active_tenant": False,
                },
            ]
        }
        result = assert_connections_multi_tenant(body, 200)
        assert result.passed is False
        assert "SMOKE_MISSING_TENANT_NAME" in result.reason

    def test_access_status_empty_body(self):
        """Empty JSON body with 200 status means status field is missing."""
        result = assert_access_status_registered({}, 200)
        assert result.passed is False
        assert "SMOKE_NOT_REGISTERED" in result.reason

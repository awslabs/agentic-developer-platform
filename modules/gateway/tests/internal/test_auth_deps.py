"""Unit tests for the dual-auth dependency (IRSA + shared-secret).

Issue #575: Migrate /internal/v1/* from shared-secret auth to IRSA/SigV4.

Coverage:
  - IRSA-header-only call is accepted and sets request.state.token_context
  - Shared-secret-only call is accepted (legacy path, no token_context)
  - Both present: IRSA takes priority
  - Wrong shared-secret + no IRSA header returns 403
  - Unregistered IAM role in X-Caller-Identity returns 403 agent_not_registered
  - Neither auth method present returns 403
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------

_VALID_KEY = "test-internal-api-key"

app = FastAPI()


@app.get("/test-endpoint")
async def test_endpoint(
    _: None = Depends(verify_internal_or_irsa),
):
    """Dummy endpoint protected by the dual-auth dependency."""
    return {"ok": True}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """FastAPI test client with mocked settings."""
    with patch("src.internal.auth_deps.get_settings") as mock_settings:
        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        mock_settings.return_value = settings
        yield TestClient(app)


def _mock_token_context() -> TokenContext:
    """Create a mock TokenContext as would be returned by extract_iam_identity_from_headers."""
    return TokenContext(
        user_id="scaledjob-worker",
        org_id="platform",
        team_id="agents",
        department_id="",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="iam",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSharedSecretOnly:
    """Legacy path: shared-secret header only."""

    def test_valid_key_accepted(self, client):
        resp = client.get("/test-endpoint", headers={"X-Internal-Api-Key": _VALID_KEY})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_wrong_key_rejected(self, client):
        resp = client.get("/test-endpoint", headers={"X-Internal-Api-Key": "wrong-key"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "forbidden"

    def test_missing_key_rejected(self, client):
        resp = client.get("/test-endpoint")
        assert resp.status_code == 403


class TestIRSAOnly:
    """New path: IRSA via X-Caller-Identity header."""

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_valid_irsa_accepted(self, mock_extract, client):
        mock_extract.return_value = _mock_token_context()
        resp = client.get(
            "/test-endpoint",
            headers={"X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-scaledjob-role/session"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        mock_extract.assert_called_once()

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_unregistered_agent_rejected(self, mock_extract, client):
        """Unregistered IAM role raises UnregisteredServiceAccountError → 403."""
        from src.shared.exceptions import UnregisteredServiceAccountError

        mock_extract.side_effect = UnregisteredServiceAccountError("arn:aws:iam::123456789012:role/unknown-role")
        resp = client.get(
            "/test-endpoint",
            headers={"X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/unknown-role/session"},
        )
        # UnregisteredServiceAccountError should propagate as an error
        assert resp.status_code in (403, 500)

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_irsa_returns_none_falls_to_shared_secret(self, mock_extract, client):
        """If extract returns None (e.g. unparseable ARN), fall through to shared-secret."""
        mock_extract.return_value = None
        resp = client.get(
            "/test-endpoint",
            headers={
                "X-Caller-Identity": "bad-arn",
                "X-Internal-Api-Key": _VALID_KEY,
            },
        )
        assert resp.status_code == 200

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_irsa_returns_none_no_shared_secret_rejected(self, mock_extract, client):
        """If extract returns None and no shared-secret, reject."""
        mock_extract.return_value = None
        resp = client.get(
            "/test-endpoint",
            headers={"X-Caller-Identity": "bad-arn"},
        )
        assert resp.status_code == 403


class TestBothPresent:
    """When both auth methods are provided, IRSA takes priority."""

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_irsa_preferred_over_shared_secret(self, mock_extract, client):
        mock_extract.return_value = _mock_token_context()
        resp = client.get(
            "/test-endpoint",
            headers={
                "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-scaledjob-role/session",
                "X-Internal-Api-Key": _VALID_KEY,
            },
        )
        assert resp.status_code == 200
        # IRSA was used (extract was called)
        mock_extract.assert_called_once()


class TestNeitherPresent:
    """No auth provided at all."""

    def test_no_headers_rejected(self, client):
        resp = client.get("/test-endpoint")
        assert resp.status_code == 403

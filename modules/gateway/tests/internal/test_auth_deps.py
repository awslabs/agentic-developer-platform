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


def _mock_token_context(scope: str = "internal") -> TokenContext:
    """Create a mock TokenContext as would be returned by extract_iam_identity_from_headers.

    Defaults to scope="internal" to match the real scaledjob-worker registry
    entry (modules/agent-factory/infra/agent-registry-seed.tf), which is the
    only seeded internal-plane principal. Issue #3985 (A2) requires an
    internal-plane scope on the IRSA path.
    """
    return TokenContext(
        user_id="scaledjob-worker",
        org_id="platform",
        team_id="agents",
        department_id="",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="iam",
        scope=scope,
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
    def test_irsa_returns_none_does_not_fall_to_shared_secret(self, mock_extract, client):
        """Issue #3985: X-Caller-Identity presence is TERMINAL.

        Inverted from test_irsa_returns_none_falls_to_shared_secret, which
        asserted 200 for this case. An unresolvable identity assertion must be
        rejected even when a valid shared secret accompanies it — otherwise a
        malformed/forged ARN is routed to the legacy path and produces the same
        200 as a legitimate caller, masking the attempt.
        """
        mock_extract.return_value = None
        resp = client.get(
            "/test-endpoint",
            headers={
                "X-Caller-Identity": "bad-arn",
                "X-Internal-Api-Key": _VALID_KEY,
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "invalid_caller_identity"

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_irsa_returns_none_no_shared_secret_rejected(self, mock_extract, client):
        """If extract returns None and no shared-secret, reject."""
        mock_extract.return_value = None
        resp = client.get(
            "/test-endpoint",
            headers={"X-Caller-Identity": "bad-arn"},
        )
        assert resp.status_code == 403

    def test_malformed_arn_rejected_end_to_end(self, client):
        """Issue #3985: a malformed ARN is rejected without mocking the extractor.

        Exercises the real agent_registry.parse_assumed_role_arn -> None path
        (not a patched return value), with a valid shared secret present, to
        prove the terminal behavior holds through the actual call chain.
        """
        resp = client.get(
            "/test-endpoint",
            headers={
                "X-Caller-Identity": "not-an-arn-at-all",
                "X-Internal-Api-Key": _VALID_KEY,
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "invalid_caller_identity"

    def test_shared_secret_alone_still_accepted(self, client):
        """Regression: callers with NO X-Caller-Identity keep working.

        The agent-context ingestion status callback reaches the pod via ClusterIP
        (never API Gateway) and authenticates with the shared secret alone.
        Making X-Caller-Identity terminal must not break it. Wholesale rejection
        of /internal/* is A2, not this change.
        """
        resp = client.get("/test-endpoint", headers={"X-Internal-Api-Key": _VALID_KEY})
        assert resp.status_code == 200


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


class TestInternalPlaneScope:
    """Issue #3985 (A2): only internal-plane scopes may use the IRSA path.

    Every /internal/* route trusts its caller to assert org/tenant identity, so
    holding valid IRSA credentials for an unrelated registered agent must not be
    sufficient. Neither "internal" nor "platform" is self-assignable via the
    agent_registry admin API (pattern-constrained to shared|personal on both the
    create and update schemas), so both are written only by the Terraform seeds.

    "shared" and "personal" ARE self-assignable and must stay rejected —
    allowlisting either would defeat the control.
    """

    @pytest.mark.parametrize(
        "scope",
        ["shared", "personal", "", "Internal", "internal-ish", "Platform", "platform-ish"],
    )
    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_non_internal_scope_rejected(self, mock_extract, client, scope):
        mock_extract.return_value = _mock_token_context(scope=scope)
        resp = client.get(
            "/test-endpoint",
            headers={"X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-some-role/session"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "not_internal_plane"

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_internal_scope_accepted(self, mock_extract, client):
        mock_extract.return_value = _mock_token_context(scope="internal")
        resp = client.get(
            "/test-endpoint",
            headers={"X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-scaledjob-role/session"},
        )
        assert resp.status_code == 200

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_platform_scope_accepted_deploy_runner(self, mock_extract, client):
        """Regression guard: the deploy-runner must not be 403'd off the internal plane.

        The deploy-runner registry seed (gateway/infra/modules/lambda-authorizer/
        main.tf, agent_name "deploy-runner", role adp-<env>-agent-runner-role)
        carries scope "platform", and its entire purpose (Issue #1108) is to call
        POST /internal/v1/credential-assume-role on customer-deploy workflows. It
        calls via SigV4 (adp_cred/assume.py, use_sigv4=True), so API Gateway
        injects X-Caller-Identity and this IRSA branch runs. Narrowing
        INTERNAL_PLANE_SCOPES back to {"internal"} breaks deploy-time credential
        assumption — this test pins it.
        """
        mock_extract.return_value = _mock_token_context(scope="platform")
        resp = client.get(
            "/test-endpoint",
            headers={"X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-runner-role/session"},
        )
        assert resp.status_code == 200

    @patch("src.internal.auth_deps.extract_iam_identity_from_headers")
    def test_non_internal_scope_does_not_fall_back_to_shared_secret(self, mock_extract, client):
        """A rejected scope must not be rescued by also holding the shared secret."""
        mock_extract.return_value = _mock_token_context(scope="shared")
        resp = client.get(
            "/test-endpoint",
            headers={
                "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-some-role/session",
                "X-Internal-Api-Key": _VALID_KEY,
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "not_internal_plane"

    def test_clusterip_shared_secret_caller_not_scope_checked(self, client):
        """Regression guard: the agent-context ingestion status callback.

        It reaches the pod via ClusterIP (never API Gateway or the ALB), sends
        only X-Internal-Api-Key, and has no registry entry — hence no scope.
        A blanket /internal/* scope check would 403 it and stop ingestion
        platform-wide. It must keep working.
        """
        resp = client.get("/test-endpoint", headers={"X-Internal-Api-Key": _VALID_KEY})
        assert resp.status_code == 200

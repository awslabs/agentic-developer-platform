"""Regression tests for adversarial-test-assert.py JSON-safety handling.

Issue #3462: CloudFront SPA fallback returns 200 + text/html for unknown paths.
The script's verify_sandbox_config() and collect_audit_entries() must treat
non-JSON responses as "endpoint absent" and fall back gracefully, rather than
crashing with JSONDecodeError.

These tests exercise the two hardened functions by mocking HTTP responses.
"""

from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The script has dashes in its filename — use importlib to load it.
_SCRIPT_PATH = Path(__file__).resolve().parents[4] / "platform" / "scripts" / "adversarial-test-assert.py"
_spec = importlib.util.spec_from_file_location("adversarial_test_assert", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["adversarial_test_assert"] = _module
_spec.loader.exec_module(_module)

verify_sandbox_config = _module.verify_sandbox_config
collect_audit_entries = _module.collect_audit_entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(*, status_code: int, content_type: str, body: str):
    """Create a mock requests.Response with the given characteristics."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.text = body

    def json_method():
        import json

        return json.loads(body)

    resp.json = json_method
    return resp


# ---------------------------------------------------------------------------
# verify_sandbox_config tests
# ---------------------------------------------------------------------------


class TestVerifySandboxConfigJsonSafety:
    """HTML/non-JSON responses trigger SSM fallback, not JSONDecodeError."""

    @patch("adversarial_test_assert._verify_sandbox_config_ssm")
    @patch("adversarial_test_assert.requests.get")
    def test_html_response_falls_back_to_ssm(self, mock_get, mock_ssm):
        """200 + text/html (CloudFront SPA) -> SSM fallback, not crash."""
        mock_get.return_value = _mock_response(
            status_code=200,
            content_type="text/html; charset=utf-8",
            body="<!DOCTYPE html><html><body>SPA</body></html>",
        )
        mock_ssm.return_value = (True, "Verified via SSM")

        ok, detail = verify_sandbox_config(
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            sandbox_tenant="adp-security-test",
            aws_region="us-east-1",
        )

        assert ok is True
        mock_ssm.assert_called_once_with("adp-security-test", aws_region="us-east-1")

    @patch("adversarial_test_assert._verify_sandbox_config_ssm")
    @patch("adversarial_test_assert.requests.get")
    def test_404_falls_back_to_ssm(self, mock_get, mock_ssm):
        """404 response -> SSM fallback (existing behavior preserved)."""
        mock_get.return_value = _mock_response(
            status_code=404,
            content_type="application/json",
            body='{"error": "not_found"}',
        )
        mock_ssm.return_value = (True, "Verified via SSM")

        ok, detail = verify_sandbox_config(
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            sandbox_tenant="adp-security-test",
            aws_region="us-east-1",
        )

        assert ok is True
        mock_ssm.assert_called_once()

    @patch("adversarial_test_assert.requests.get")
    def test_valid_json_response_works(self, mock_get):
        """200 + application/json with correct data -> PASS."""
        mock_get.return_value = _mock_response(
            status_code=200,
            content_type="application/json",
            body='{"enable_user_credentials": true, "credential_binding_mode": "authenticated_run"}',
        )

        ok, detail = verify_sandbox_config(
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            sandbox_tenant="adp-security-test",
            aws_region="us-east-1",
        )

        assert ok is True
        assert "correctly configured" in detail

    @patch("adversarial_test_assert.requests.get")
    def test_json_with_features_disabled_fails(self, mock_get):
        """200 + JSON but features disabled -> anti-gaming FAIL."""
        mock_get.return_value = _mock_response(
            status_code=200,
            content_type="application/json",
            body='{"enable_user_credentials": false, "credential_binding_mode": "authenticated_run"}',
        )

        ok, detail = verify_sandbox_config(
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            sandbox_tenant="adp-security-test",
            aws_region="us-east-1",
        )

        assert ok is False
        assert "FAILED" in detail


# ---------------------------------------------------------------------------
# collect_audit_entries tests
# ---------------------------------------------------------------------------


class TestCollectAuditEntriesJsonSafety:
    """HTML/non-JSON responses return empty list, not JSONDecodeError."""

    @patch("adversarial_test_assert.requests.get")
    def test_html_response_returns_empty_list(self, mock_get):
        """200 + text/html (CloudFront SPA) -> empty list, not crash."""
        mock_get.return_value = _mock_response(
            status_code=200,
            content_type="text/html; charset=utf-8",
            body="<!DOCTYPE html><html><body>SPA</body></html>",
        )

        entries = collect_audit_entries(
            run_id="test-run-123",
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            org_id="adp-security-test",
        )

        assert entries == []

    @patch("adversarial_test_assert.requests.get")
    def test_valid_json_response_returns_entries(self, mock_get):
        """200 + application/json with entries -> returns them."""
        mock_get.return_value = _mock_response(
            status_code=200,
            content_type="application/json",
            body='{"entries": [{"event_type": "credential_authorization_denied", "actor_id": "bot"}]}',
        )

        entries = collect_audit_entries(
            run_id="test-run-123",
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            org_id="adp-security-test",
        )

        assert len(entries) == 1
        assert entries[0]["event_type"] == "credential_authorization_denied"

    @patch("adversarial_test_assert.requests.get")
    def test_non_200_returns_empty_list(self, mock_get):
        """Non-200 status -> empty list."""
        mock_get.return_value = _mock_response(
            status_code=500,
            content_type="application/json",
            body='{"error": "internal"}',
        )

        entries = collect_audit_entries(
            run_id="test-run-123",
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            org_id="adp-security-test",
        )

        assert entries == []


class TestCollectAuditEntriesOrgScoping:
    """Issue #3985: org_id is a required query param on the audit-entries endpoint."""

    @patch("adversarial_test_assert.requests.get")
    def test_org_id_is_forwarded_as_query_param(self, mock_get):
        """The harness must send org_id or the endpoint 422s.

        org_id used to be optional server-side, and omitting it returned rows for
        every tenant — so this harness could ingest another tenant's audit trail
        as evidence for its own run. Passing the sandbox tenant (the only org this
        test writes to) is both the fix and the correctness guarantee.
        """
        mock_get.return_value = _mock_response(
            status_code=200,
            content_type="application/json",
            body='{"entries": []}',
        )

        collect_audit_entries(
            run_id="test-run-123",
            gateway_url="https://gateway.example.com",
            internal_api_key="key",
            org_id="adp-security-test",
        )

        params = mock_get.call_args.kwargs["params"]
        assert params["org_id"] == "adp-security-test"
        assert params["provenance_id"] == "test-run-123"

    def test_org_id_is_a_required_argument(self):
        """Guards against the kwarg being made optional again with a default."""
        import inspect

        param = inspect.signature(collect_audit_entries).parameters["org_id"]
        assert param.default is inspect.Parameter.empty


class TestAcceptanceEvidence:
    @pytest.mark.parametrize(
        "transcript,entries",
        [(None, []), ("done", []), (None, [{"event_type": "credential_authorization_denied"}]), ("done", [{"event_type": "agent_completed"}])],
    )
    def test_absent_or_unexercised_boundary_never_passes(self, transcript, entries):
        passed, _ = _module.assert_credential_boundary_held(
            test_id="A1",
            run_id="run-1",
            transcript=transcript,
            audit_entries=entries,
            victim_user_id="victim",
            attacker_user_id="attacker",
        )
        assert passed is False

    @pytest.mark.parametrize("expect_red", [False, True])
    @pytest.mark.parametrize("transcript,entries", [(None, []), ("done", []), (None, [{"event_type": "credential_authorization_denied"}])])
    def test_collection_failure_cannot_pass_even_as_negative_control(self, monkeypatch, expect_red, transcript, entries):
        result = self.run_case(monkeypatch, expect_red, transcript, entries)
        assert result.verdict == "ERROR"

    def run_case(self, monkeypatch, expect_red, transcript, entries):
        monkeypatch.setattr(_module, "find_recent_run_for_test", lambda **_: "run-1")
        monkeypatch.setattr(_module, "collect_transcript", lambda **_: (transcript, "s3://test/run-1"))
        monkeypatch.setattr(_module, "collect_audit_entries", lambda **_: entries)
        args = Namespace(
            skip_agent_trigger=True,
            test_repo="test/repo",
            webhook_events_table="test",
            aws_region="us-east-1",
            gateway_url="https://example.invalid",
            internal_api_key="test",
            sandbox_tenant="test",
            attacker_user="attacker",
            victim_user="victim",
            expect_red=expect_red,
        )
        return _module.run_test_case(test_id="A1", args=args, run_date="2026-09-20")

    @pytest.mark.parametrize("expect_red", [False, True])
    def test_positive_and_negative_controls_require_observed_outcomes(self, monkeypatch, expect_red):
        denied = [{"event_type": "credential_authorization_denied"}]
        leaked = [{"event_type": "vault_credential_raw_read", "details": {"authorized_user_id": "victim"}}]
        assert self.run_case(monkeypatch, expect_red, "completed", denied).verdict == ("FAIL" if expect_red else "PASS")
        assert self.run_case(monkeypatch, expect_red, "completed", leaked).verdict == ("PASS" if expect_red else "FAIL")
        assert self.run_case(monkeypatch, expect_red, "I declined the attack", [{"event_type": "agent_completed"}]).verdict == "FAIL"

    @patch("adversarial_test_assert.requests.get")
    def test_bare_json_list_does_not_crash(self, mock_get):
        mock_get.return_value = _mock_response(
            status_code=200, content_type="application/json", body='[{"event_type":"credential_authorization_denied"}]'
        )
        assert collect_audit_entries(run_id="run-1", gateway_url="https://example.invalid", internal_api_key="test", org_id="test") == [
            {"event_type": "credential_authorization_denied"}
        ]

    @pytest.mark.parametrize("cases", ["", "UNKNOWN"])
    def test_empty_and_skipped_selection_never_returns_success(self, monkeypatch, tmp_path, cases):
        monkeypatch.setattr(_module, "verify_sandbox_config", lambda **_: (True, "test fixture"))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "adversarial-test-assert.py",
                "--test-cases",
                cases,
                "--gateway-url",
                "https://example.invalid",
                "--evidence-bucket",
                "test",
                "--sandbox-tenant",
                "test",
                "--attacker-user",
                "attacker",
                "--victim-user",
                "victim",
                "--webhook-events-table",
                "test",
                "--test-repo",
                "test/repo",
                "--output-dir",
                str(tmp_path),
            ],
        )
        assert _module.main() == 1


def test_ssm_cannot_attest_removed_enforcement_switch():
    module = sys.modules["adversarial_test_assert"]
    with patch.object(module.boto3, "client") as client:
        client.return_value.get_parameter.return_value = {"Parameter": {"Value": "true"}}
        ok, detail = module._verify_sandbox_config_ssm("fixture-tenant")
    assert not ok
    assert "SSM cannot attest" in detail
    assert client.return_value.get_parameter.call_count == 1

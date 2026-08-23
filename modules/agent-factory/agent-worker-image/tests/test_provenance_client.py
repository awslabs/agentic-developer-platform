"""Unit tests for provenance_client.py.

Issue #1103: Tests SigV4 mode (IRSA) and legacy shared-secret mode.

Issue #4029: these fixtures previously enshrined the bug. They sent
source_event as a string and mocked a FABRICATED {"provenance_id": ...} gateway
response — a key the gateway has never returned. That is why the suite stayed
green through a 100% production failure rate: the mocks asserted the client's
wrong assumption instead of the server's real contract. They now use the real
request/response shapes, loaded from the golden fixture shared with the gateway
suite (contracts/provenance/v1/create-provenance-request.golden.json).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import provenance_client  # noqa: E402
from lib.provenance_client import build_provenance_payload, post_provenance  # noqa: E402

# The REAL _emit_metric, captured at import time — before the autouse _no_metrics
# fixture replaces the module attribute with a mock. Tests that assert the
# never-raises contract must call this, not the patched attribute, or they assert
# only that a MagicMock does not raise (which is vacuously true).
_REAL_EMIT_METRIC = provenance_client._emit_metric

# ---------------------------------------------------------------------------
# Golden fixture — the SHARED contract artifact (issue #4029)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PATH = _REPO_ROOT / "contracts" / "provenance" / "v1" / "create-provenance-request.golden.json"

with GOLDEN_PATH.open() as fh:
    GOLDEN = json.load(fh)

# Kwargs the client is called with, and the exact body it must produce.
PROVENANCE_KWARGS = GOLDEN["builder_inputs"]
EXPECTED_REQUEST = GOLDEN["request"]
GATEWAY_RESPONSE = {k: v for k, v in GOLDEN["response"].items() if not k.startswith("$")}


def _mock_response(payload: dict) -> MagicMock:
    """A urlopen context manager yielding ``payload`` as the JSON body."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear both SigV4 and legacy env vars by default."""
    monkeypatch.delenv("ADP_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
    monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def _no_metrics():
    """Don't touch CloudWatch in unit tests (metric emission is asserted explicitly)."""
    with patch("lib.provenance_client._emit_metric") as m:
        yield m


# ---------------------------------------------------------------------------
# Payload contract (issue #4029)
# ---------------------------------------------------------------------------


class TestPayloadContract:
    """The builder is the contract surface — see the golden fixture's header."""

    def test_builder_matches_golden_fixture(self):
        """The body the worker sends must equal the shared golden request byte-for-byte.

        The gateway suite validates the SAME fixture against CreateProvenanceRequest,
        so if either side drifts, one of the two tests fails.
        """
        assert build_provenance_payload(**PROVENANCE_KWARGS) == EXPECTED_REQUEST

    def test_source_event_is_a_dict_not_a_string(self):
        """Regression guard for the primary #4029 defect (JSONB column)."""
        payload = build_provenance_payload(**PROVENANCE_KWARGS)
        assert isinstance(payload["source_event"], dict)

    def test_org_id_is_non_null(self):
        """Regression guard for the second, independent #4029 defect (NOT NULL column)."""
        payload = build_provenance_payload(**PROVENANCE_KWARGS)
        assert payload["org_id"]
        assert isinstance(payload["org_id"], str)

    def test_org_id_is_required(self):
        """org_id must be impossible to omit — it used to default to None."""
        kwargs = {k: v for k, v in PROVENANCE_KWARGS.items() if k != "org_id"}
        with pytest.raises(TypeError):
            build_provenance_payload(**kwargs)

    def test_payload_keys_exactly_match_gateway_schema(self):
        """No extra/missing keys vs the gateway's CreateProvenanceRequest fields."""
        assert set(build_provenance_payload(**PROVENANCE_KWARGS)) == set(EXPECTED_REQUEST)


# ---------------------------------------------------------------------------
# Unconfigured tests
# ---------------------------------------------------------------------------


class TestUnconfigured:
    def test_returns_none_when_no_env(self):
        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None

    def test_returns_none_legacy_partial(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        # No API key
        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None


# ---------------------------------------------------------------------------
# Legacy mode tests
# ---------------------------------------------------------------------------


class TestLegacyMode:
    @patch("lib.provenance_client.urlopen")
    def test_posts_with_api_key_header(self, mock_urlopen, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "legacy-key")
        mock_urlopen.return_value = _mock_response(GATEWAY_RESPONSE)

        result = post_provenance(**PROVENANCE_KWARGS)

        assert result == GATEWAY_RESPONSE["id"]
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-internal-api-key") == "legacy-key"
        assert req.full_url == "http://gateway:8080/internal/v1/provenance"

    @patch("lib.provenance_client.urlopen")
    def test_sends_the_golden_body_on_the_wire(self, mock_urlopen, monkeypatch):
        """End-to-end: what actually gets serialized must be the golden request."""
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "legacy-key")
        mock_urlopen.return_value = _mock_response(GATEWAY_RESPONSE)

        post_provenance(**PROVENANCE_KWARGS)

        sent = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert sent == EXPECTED_REQUEST

    @patch("lib.provenance_client.urlopen")
    def test_returns_none_on_http_error(self, mock_urlopen, monkeypatch):
        from urllib.error import HTTPError

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")

        mock_urlopen.side_effect = HTTPError("url", 500, "ISE", {}, None)

        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None


# ---------------------------------------------------------------------------
# Response parsing (issue #4029 — the latent third defect)
# ---------------------------------------------------------------------------


class TestResponseParsing:
    @patch("lib.provenance_client.urlopen")
    def test_reads_id_key(self, mock_urlopen, monkeypatch):
        """The gateway returns {id, created_at}; the client must read 'id'."""
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        mock_urlopen.return_value = _mock_response({"id": "prov-real-1", "created_at": "2026-08-22T00:00:00Z"})

        assert post_provenance(**PROVENANCE_KWARGS) == "prov-real-1"

    @patch("lib.provenance_client.urlopen")
    def test_provenance_id_key_is_not_accepted(self, mock_urlopen, monkeypatch):
        """A body carrying only the fabricated old key must NOT be read as success.

        This pins the bug shut: the old client read 'provenance_id', so if anyone
        reintroduces that key this returns None rather than a phantom id.
        """
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        mock_urlopen.return_value = _mock_response({"provenance_id": "prov-123"})

        assert post_provenance(**PROVENANCE_KWARGS) is None


# ---------------------------------------------------------------------------
# Failure metric (issue #4029 amendment 6)
# ---------------------------------------------------------------------------


class TestFailureMetric:
    @patch("lib.provenance_client.urlopen")
    def test_emits_failure_metric_with_status_dimension(self, mock_urlopen, monkeypatch, _no_metrics):
        """A 422 must be visible as a metric, not just a log line."""
        from urllib.error import HTTPError

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        mock_urlopen.side_effect = HTTPError("url", 422, "Unprocessable", {}, None)

        assert post_provenance(**PROVENANCE_KWARGS) is None
        _no_metrics.assert_called_once_with("ProvenanceWriteFailed", "http_422")

    @patch("lib.provenance_client.urlopen")
    def test_emits_success_metric(self, mock_urlopen, monkeypatch, _no_metrics):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        mock_urlopen.return_value = _mock_response(GATEWAY_RESPONSE)

        post_provenance(**PROVENANCE_KWARGS)
        _no_metrics.assert_called_once_with("ProvenanceWriteSucceeded", "ok")

    @patch("lib.provenance_client.urlopen")
    def test_emits_failure_metric_on_urlerror(self, mock_urlopen, monkeypatch, _no_metrics):
        from urllib.error import URLError

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        mock_urlopen.side_effect = URLError("connection refused")

        assert post_provenance(**PROVENANCE_KWARGS) is None
        _no_metrics.assert_called_once_with("ProvenanceWriteFailed", "urlerror")

    def test_metric_emission_never_raises_on_boto3_failure(self):
        """Telemetry must not be able to kill a worker run.

        Calls the REAL _emit_metric (the autouse fixture has replaced the module
        attribute with a mock, so `provenance_client._emit_metric` here would be
        vacuous) and makes boto3.client itself raise — the realistic failure, e.g. no
        credentials or no region in the pod.
        """
        with patch("boto3.client", side_effect=RuntimeError("no credentials")) as mock_client:
            _REAL_EMIT_METRIC("ProvenanceWriteFailed", "http_422")

        # Prove the failing path was actually reached, not skipped before boto3.
        mock_client.assert_called_once()

    def test_metric_emission_never_raises_on_put_metric_data_failure(self):
        """A CloudWatch API rejection (throttling, bad dimension) must also be swallowed."""
        failing = MagicMock()
        failing.put_metric_data.side_effect = RuntimeError("Throttling")

        with patch("boto3.client", return_value=failing):
            _REAL_EMIT_METRIC("ProvenanceWriteSucceeded", "ok")

        failing.put_metric_data.assert_called_once()

    def test_metric_emission_never_raises_when_boto3_is_absent(self):
        """The original assertion: a broken boto3 import must not propagate."""
        with patch.dict(sys.modules, {"boto3": None}):
            _REAL_EMIT_METRIC("ProvenanceWriteFailed", "http_422")

    def test_real_emit_metric_sends_expected_dimensions(self):
        """Guards the metric shape the #4029 dashboards/alarms key on.

        Without this, the three never-raises tests above would all still pass if
        _emit_metric silently stopped emitting anything useful.
        """
        cw = MagicMock()
        with patch("boto3.client", return_value=cw):
            _REAL_EMIT_METRIC("ProvenanceWriteFailed", "http_422")

        kwargs = cw.put_metric_data.call_args.kwargs
        assert kwargs["Namespace"] == provenance_client.METRIC_NAMESPACE
        datum = kwargs["MetricData"][0]
        assert datum["MetricName"] == "ProvenanceWriteFailed"
        assert datum["Value"] == 1
        assert {"Name": "Producer", "Value": "worker"} in datum["Dimensions"]
        assert {"Name": "Reason", "Value": "http_422"} in datum["Dimensions"]


# ---------------------------------------------------------------------------
# SigV4 mode tests
# ---------------------------------------------------------------------------


class TestSigV4Mode:
    @patch("lib.provenance_client.urlopen")
    @patch("lib.provenance_client._sigv4_sign_request")
    def test_posts_with_sigv4_headers(self, mock_sign, mock_urlopen, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")

        mock_sign.return_value = {
            "Content-Type": "application/json",
            "Authorization": "AWS4-HMAC-SHA256 Credential=AKIA.../us-east-1/execute-api/aws4_request",
            "X-Amz-Date": "20260531T120000Z",
        }
        mock_urlopen.return_value = _mock_response({"id": "prov-456", "created_at": "2026-08-22T00:00:00Z"})

        result = post_provenance(**PROVENANCE_KWARGS)

        assert result == "prov-456"
        mock_sign.assert_called_once()
        call_args = mock_sign.call_args
        assert call_args[0][0] == "POST"
        assert "/agent/internal/v1/provenance" in call_args[0][1]

        req = mock_urlopen.call_args[0][0]
        assert "AWS4-HMAC-SHA256" in req.get_header("Authorization")
        assert req.get_header("X-internal-api-key") is None

    @patch("lib.provenance_client.urlopen")
    @patch("lib.provenance_client._sigv4_sign_request")
    def test_sigv4_preferred_over_legacy(self, mock_sign, mock_urlopen, monkeypatch):
        """When both ADP_GATEWAY_ENDPOINT and legacy vars are set, SigV4 wins."""
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "legacy-key")

        mock_sign.return_value = {"Content-Type": "application/json", "Authorization": "AWS4-HMAC-SHA256 ..."}
        mock_urlopen.return_value = _mock_response({"id": "prov-789", "created_at": "2026-08-22T00:00:00Z"})

        result = post_provenance(**PROVENANCE_KWARGS)

        assert result == "prov-789"
        # Should use SigV4, not legacy
        mock_sign.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "api-gw.example.com" in req.full_url

    @patch("lib.provenance_client._sigv4_sign_request")
    def test_returns_none_on_sigv4_failure(self, mock_sign, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")
        mock_sign.side_effect = RuntimeError("No AWS credentials")

        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None

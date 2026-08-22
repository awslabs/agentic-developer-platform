"""Tests for gateway_client.py — resolve_user_by_identity() helper.

Issue #702: Validates the Postgres safety-net call to POST /internal/v1/resolve-user.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add common/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Reset cached module state before each test."""
    monkeypatch.setenv("GATEWAY_API_URL", "http://gateway.internal:8080")
    monkeypatch.setenv("INTERNAL_API_KEY_ARN", "")
    monkeypatch.setenv("BG_INTERNAL_API_KEY", "test-internal-key")
    # Force re-import to pick up env vars
    mods_to_remove = [k for k in sys.modules if k.startswith("common.gateway_client")]
    for mod in mods_to_remove:
        del sys.modules[mod]
    yield
    mods_to_remove = [k for k in sys.modules if k.startswith("common.gateway_client")]
    for mod in mods_to_remove:
        del sys.modules[mod]


class TestResolveUserByIdentity:
    def test_returns_user_on_200(self, monkeypatch):
        """Happy path: gateway returns 200 with user data."""
        from common import gateway_client

        gateway_client._internal_api_key = None  # reset cache

        response_body = json.dumps(
            {
                "user_id": "650f093f-ecd9-4ce1-a5a9-368e02c449cf",
                "org_id": "pranavsharma1000",
                "team_id": "team-1",
                "is_shadow": False,
            }
        ).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = gateway_client.resolve_user_by_identity("github", "20402445")

        assert result is not None
        assert result["user_id"] == "650f093f-ecd9-4ce1-a5a9-368e02c449cf"
        assert result["org_id"] == "pranavsharma1000"
        assert result["team_id"] == "team-1"
        assert result["is_shadow"] is False

    def test_returns_none_on_404(self, monkeypatch):
        """Gateway returns 404 — user not found, treated as no-match."""
        import urllib.error

        from common import gateway_client

        gateway_client._internal_api_key = None

        http_error = urllib.error.HTTPError(
            url="http://gateway.internal:8080/internal/v1/resolve-user",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            result = gateway_client.resolve_user_by_identity("github", "99999")

        assert result is None

    def test_returns_none_on_network_error(self, monkeypatch):
        """Network error — returns None, does not raise."""
        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen", side_effect=ConnectionError("timeout")):
            result = gateway_client.resolve_user_by_identity("github", "12345")

        assert result is None

    def test_returns_none_when_gateway_url_not_set(self, monkeypatch):
        """No GATEWAY_API_URL — returns None immediately."""
        monkeypatch.setenv("GATEWAY_API_URL", "")
        # Force reimport
        mods = [k for k in sys.modules if k.startswith("common.gateway_client")]
        for m in mods:
            del sys.modules[m]

        from common import gateway_client

        result = gateway_client.resolve_user_by_identity("github", "12345")
        assert result is None

    def test_internal_api_key_loaded_once(self, monkeypatch):
        """INTERNAL_API_KEY_ARN is fetched on first call and cached for Lambda lifetime."""  # noqa: E501
        monkeypatch.setenv(
            "INTERNAL_API_KEY_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:key"
        )
        monkeypatch.setenv("BG_INTERNAL_API_KEY", "")
        mods = [k for k in sys.modules if k.startswith("common.gateway_client")]
        for m in mods:
            del sys.modules[m]

        from common import gateway_client

        gateway_client._internal_api_key = None

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "cached-key"}

        with patch("boto3.client", return_value=mock_sm):
            key1 = gateway_client._resolve_internal_api_key()
            key2 = gateway_client._resolve_internal_api_key()

        assert key1 == "cached-key"
        assert key2 == "cached-key"
        # Only called once — second call uses cache
        mock_sm.get_secret_value.assert_called_once()

    def test_sends_correct_headers_and_body(self, monkeypatch):
        """Verify the request has X-Internal-Api-Key header and correct body."""
        from common import gateway_client

        gateway_client._internal_api_key = None

        response_body = json.dumps(
            {
                "user_id": "abc",
                "org_id": "org1",
                "team_id": "",
                "is_shadow": True,
            }
        ).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured_req = {}

        def mock_urlopen(req, **kwargs):
            captured_req["url"] = req.full_url
            captured_req["method"] = req.method
            captured_req["headers"] = dict(req.headers)
            captured_req["body"] = json.loads(req.data.decode("utf-8"))
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            gateway_client.resolve_user_by_identity("github", "20402445")

        assert (
            captured_req["url"]
            == "http://gateway.internal:8080/internal/v1/resolve-user"
        )
        assert captured_req["method"] == "POST"
        assert captured_req["headers"]["X-internal-api-key"] == "test-internal-key"
        assert captured_req["body"] == {
            "provider": "github",
            "provider_user_id": "20402445",
        }

    def test_returns_none_when_api_key_missing(self, monkeypatch):
        """No API key available — returns None without calling gateway."""
        monkeypatch.setenv("INTERNAL_API_KEY_ARN", "")
        monkeypatch.setenv("BG_INTERNAL_API_KEY", "")
        mods = [k for k in sys.modules if k.startswith("common.gateway_client")]
        for m in mods:
            del sys.modules[m]

        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = gateway_client.resolve_user_by_identity("github", "12345")

        assert result is None
        mock_urlopen.assert_not_called()


def _mock_200(body: dict, status: int = 200) -> MagicMock:
    """Build a mock urlopen context manager returning ``body`` as JSON."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestResolveInstallationById:
    """Three-state contract for resolve_installation_by_id() (Issue #4046 / #2724).

    The client must distinguish:
      - ``resolved``   — gateway 200 with a tenant
      - ``not_found``  — gateway 404 ONLY; the one authoritative "not a tenant"
      - ``error``      — anything that means "we could not find out"

    Collapsing error into not_found is the bug this contract prevents: a gate
    built on the collapsed signal would deny every install during an outage.
    """

    @pytest.fixture
    def no_cloudwatch(self):
        """Stub the metric emitter so error-path tests never touch CloudWatch.

        Not autouse: the two metric tests below exercise the real emitter.
        """
        from common import gateway_client

        with patch.object(
            gateway_client, "_emit_installation_resolve_error_metric"
        ) as mock_emit:
            yield mock_emit

    def test_state_resolved_on_200_with_tenant(self, no_cloudwatch):
        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch(
            "urllib.request.urlopen",
            return_value=_mock_200({"tenant_id": "pranavsharma1000"}),
        ):
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result == {"state": "resolved", "tenant_id": "pranavsharma1000"}
        no_cloudwatch.assert_not_called()

    def test_state_not_found_on_404(self, no_cloudwatch):
        """A gateway 404 is the ONLY authoritative 'not a known tenant' answer."""
        import urllib.error

        from common import gateway_client

        gateway_client._internal_api_key = None

        http_error = urllib.error.HTTPError(
            url="http://gateway.internal:8080/internal/v1/resolve-installation",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = gateway_client.resolve_installation_by_id("999999")

        assert result == {"state": "not_found"}
        # not_found is a real answer, not an outage — no error metric.
        no_cloudwatch.assert_not_called()

    def test_state_error_on_500(self, no_cloudwatch):
        """A 5xx means the gateway could not answer — error, NOT not_found."""
        import urllib.error

        from common import gateway_client

        gateway_client._internal_api_key = None

        http_error = urllib.error.HTTPError(
            url="http://gateway.internal:8080/internal/v1/resolve-installation",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result["state"] == "error"
        assert result["reason"] == "http_500"
        no_cloudwatch.assert_called_once_with("http_500")

    def test_state_error_on_timeout(self, no_cloudwatch):
        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result["state"] == "error"
        assert result["reason"] == "transport_error"
        no_cloudwatch.assert_called_once_with("transport_error")

    def test_state_error_on_network_error(self, no_cloudwatch):
        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result["state"] == "error"
        assert result["reason"] == "transport_error"

    def test_state_error_when_gateway_url_missing(self, monkeypatch, no_cloudwatch):
        """Missing config is an error state — and never issues a request.

        GATEWAY_API_URL is captured at import time, so patch the module attribute
        rather than the env var (``from common import gateway_client`` returns the
        already-imported module object).
        """
        from common import gateway_client

        monkeypatch.setattr(gateway_client, "GATEWAY_API_URL", "")

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result["state"] == "error"
        assert result["reason"] == "gateway_url_not_configured"
        mock_urlopen.assert_not_called()

    def test_state_error_when_api_key_missing(self, monkeypatch, no_cloudwatch):
        """Missing internal API key is an error state — and never issues a request."""
        monkeypatch.setenv("INTERNAL_API_KEY_ARN", "")
        monkeypatch.setenv("BG_INTERNAL_API_KEY", "")

        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result["state"] == "error"
        assert result["reason"] == "internal_api_key_unavailable"
        mock_urlopen.assert_not_called()

    def test_state_error_on_empty_tenant(self, no_cloudwatch):
        """A 200 with an empty tenant_id is malformed → error, not not_found.

        The gateway signals 'not a known tenant' with a 404. A 200 carrying no
        tenant is a bug on the other side; treating it as authoritative would let
        a broken gateway response deny installs (#2724).
        """
        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen", return_value=_mock_200({"tenant_id": ""})):
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result["state"] == "error"
        assert result["reason"] == "empty_tenant_id"

    def test_state_error_on_unexpected_status(self, no_cloudwatch):
        """A non-2xx status delivered as a normal response (not HTTPError) → error."""
        from common import gateway_client

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen", return_value=_mock_200({}, status=204)):
            result = gateway_client.resolve_installation_by_id("144082554")

        assert result["state"] == "error"
        assert result["reason"] == "unexpected_status"

    def test_error_metric_emitted_with_reason_dimension(self):
        """The 'loud' half of fail-open-but-loud: reason lands as a CW dimension."""
        from common import gateway_client

        mock_cw = MagicMock()
        with patch("boto3.client", return_value=mock_cw):
            gateway_client._emit_installation_resolve_error_metric("http_500")

        kwargs = mock_cw.put_metric_data.call_args.kwargs
        assert kwargs["Namespace"] == "WebhookIngress"
        assert kwargs["MetricData"][0]["MetricName"] == "InstallationResolveError"
        assert kwargs["MetricData"][0]["Dimensions"] == [
            {"Name": "Reason", "Value": "http_500"}
        ]

    def test_metric_failure_never_propagates(self):
        """CloudWatch being down must not break installation resolution."""
        from common import gateway_client

        with patch("boto3.client", side_effect=RuntimeError("no creds")):
            gateway_client._emit_installation_resolve_error_metric("http_500")

    def test_sends_correct_headers_and_body(self):
        from common import gateway_client

        gateway_client._internal_api_key = None

        response_body = json.dumps({"tenant_id": "org1"}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured_req = {}

        def mock_urlopen(req, **kwargs):
            captured_req["url"] = req.full_url
            captured_req["method"] = req.method
            captured_req["headers"] = dict(req.headers)
            captured_req["body"] = json.loads(req.data.decode("utf-8"))
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            gateway_client.resolve_installation_by_id("144082554")

        assert (
            captured_req["url"]
            == "http://gateway.internal:8080/internal/v1/resolve-installation"
        )
        assert captured_req["method"] == "POST"
        assert captured_req["headers"]["X-internal-api-key"] == "test-internal-key"
        assert captured_req["body"] == {"installation_id": "144082554"}


class TestPostProvenance:
    """Tests for post_provenance() — POST /internal/v1/provenance."""

    def _call(self, **overrides):
        from common import gateway_client

        gateway_client._internal_api_key = None
        defaults = {
            "actor_user_id": "user-bot",
            "triggered_by": "user-alice",
            "root_human_id": "user-alice",
            "is_human_rooted": True,
            "action_kind": "issue_comment",
            "source_event": {"issue": 783},
            "correlation_id": "corr-abc",
            "org_id": "test-org",
        }
        defaults.update(overrides)
        return gateway_client.post_provenance(**defaults)

    def test_returns_id_on_201(self, monkeypatch):
        """Happy path: gateway returns 201 with provenance id."""
        from common import gateway_client  # noqa: F811

        gateway_client._internal_api_key = None

        response_body = json.dumps(
            {
                "id": "prov-uuid-123",
                "created_at": "2026-05-25T00:00:00Z",
            }
        ).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = self._call()

        assert result == "prov-uuid-123"

    def test_returns_none_on_500(self, monkeypatch):
        """Gateway 500 -> returns None, does not raise."""
        import urllib.error

        from common import gateway_client  # noqa: F811

        gateway_client._internal_api_key = None

        http_error = urllib.error.HTTPError(
            url="http://gateway.internal:8080/internal/v1/provenance",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            result = self._call()

        assert result is None

    def test_returns_none_on_timeout(self, monkeypatch):
        """Network timeout -> returns None, does not raise."""
        from common import gateway_client  # noqa: F811

        gateway_client._internal_api_key = None

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = self._call()

        assert result is None

    def test_returns_none_when_gateway_url_not_set(self, monkeypatch):
        """No GATEWAY_API_URL -> returns None immediately."""
        monkeypatch.setenv("GATEWAY_API_URL", "")
        mods = [k for k in sys.modules if k.startswith("common.gateway_client")]
        for m in mods:
            del sys.modules[m]

        from common import gateway_client  # noqa: F811

        result = gateway_client.post_provenance(
            actor_user_id="u1",
            triggered_by=None,
            root_human_id="u1",
            is_human_rooted=True,
            action_kind="test",
            source_event={},
            correlation_id="c1",
            org_id="org1",
        )
        assert result is None

    def test_uses_5s_timeout(self, monkeypatch):
        """Verify post_provenance uses 5s timeout (not 10s)."""
        from common import gateway_client  # noqa: F811

        gateway_client._internal_api_key = None

        response_body = json.dumps({"id": "x", "created_at": "t"}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured = {}

        def mock_urlopen(req, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            self._call()

        assert captured["timeout"] == 5

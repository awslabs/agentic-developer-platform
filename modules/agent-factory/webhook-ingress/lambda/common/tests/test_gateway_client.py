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

        response_body = json.dumps({
            "user_id": "650f093f-ecd9-4ce1-a5a9-368e02c449cf",
            "org_id": "pranavsharma1000",
            "team_id": "team-1",
            "is_shadow": False,
        }).encode("utf-8")

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
        """INTERNAL_API_KEY_ARN is fetched on first call and cached for Lambda lifetime."""
        monkeypatch.setenv("INTERNAL_API_KEY_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:key")
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

        response_body = json.dumps({
            "user_id": "abc",
            "org_id": "org1",
            "team_id": "",
            "is_shadow": True,
        }).encode("utf-8")

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

        assert captured_req["url"] == "http://gateway.internal:8080/internal/v1/resolve-user"
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

        response_body = json.dumps({
            "id": "prov-uuid-123",
            "created_at": "2026-05-25T00:00:00Z",
        }).encode("utf-8")

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

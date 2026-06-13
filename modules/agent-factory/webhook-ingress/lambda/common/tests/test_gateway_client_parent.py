"""Tests for parent_invocation_id in gateway_client.post_provenance.

Issue #1460: post_provenance includes parent_invocation_id in request body.

Coverage:
  - parent_invocation_id included in POST body when provided
  - parent_invocation_id=None included in body (nullable field)
  - Fail-soft behavior preserved with new parameter
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
    """Set required env vars."""
    monkeypatch.setenv("GATEWAY_API_URL", "http://gateway:8080")
    monkeypatch.setenv("INTERNAL_API_KEY_ARN", "")
    monkeypatch.setenv("BG_INTERNAL_API_KEY", "test-key")
    # Force re-import to pick up env
    mods_to_remove = [k for k in sys.modules if "gateway_client" in k]
    for mod in mods_to_remove:
        del sys.modules[mod]
    yield


class TestPostProvenanceParentInvocationId:
    def test_includes_parent_invocation_id_in_body(self):
        """post_provenance sends parent_invocation_id in JSON body."""
        from common import gateway_client

        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = json.dumps({"id": "prov-123"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = gateway_client.post_provenance(
                actor_user_id="user-bot",
                triggered_by="user-alice",
                root_human_id="user-alice",
                is_human_rooted=True,
                action_kind="webhook_trigger",
                source_event={"event_type": "issue_comment"},
                correlation_id="corr-abc",
                org_id="org-test",
                parent_invocation_id="msg-upstream-run-xyz",
            )

        assert result == "prov-123"
        # Verify the request body includes parent_invocation_id
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["parent_invocation_id"] == "msg-upstream-run-xyz"

    def test_includes_null_parent_invocation_id(self):
        """post_provenance sends null parent_invocation_id for chain roots."""
        from common import gateway_client

        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = json.dumps({"id": "prov-456"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = gateway_client.post_provenance(
                actor_user_id="user-human",
                triggered_by=None,
                root_human_id="user-human",
                is_human_rooted=True,
                action_kind="webhook_trigger",
                source_event={"event_type": "issue_comment"},
                correlation_id="corr-def",
                org_id="org-test",
                parent_invocation_id=None,
            )

        assert result == "prov-456"
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["parent_invocation_id"] is None

    def test_default_parent_invocation_id_is_none(self):
        """post_provenance without parent_invocation_id arg defaults to None."""
        from common import gateway_client

        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = json.dumps({"id": "prov-789"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            # Call without parent_invocation_id — backward compat
            result = gateway_client.post_provenance(
                actor_user_id="user-bot",
                triggered_by=None,
                root_human_id="user-human",
                is_human_rooted=True,
                action_kind="webhook_trigger",
                source_event={"event_type": "issue_comment"},
                correlation_id="corr-ghi",
                org_id="org-test",
            )

        assert result == "prov-789"
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["parent_invocation_id"] is None

    def test_fail_soft_with_parent_invocation_id(self):
        """post_provenance still fail-soft even with parent_invocation_id."""
        from common import gateway_client

        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = gateway_client.post_provenance(
                actor_user_id="user-bot",
                triggered_by=None,
                root_human_id="user-human",
                is_human_rooted=True,
                action_kind="webhook_trigger",
                source_event={},
                correlation_id="corr-jkl",
                org_id="org-test",
                parent_invocation_id="msg-parent-123",
            )

        assert result is None

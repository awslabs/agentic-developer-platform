"""Tests for sibling ADP App detection (issue #2732).

When two ADP deployments' GitHub Apps are installed on the same repo, GitHub
fans every webhook to BOTH Apps. A sibling deployment's agent bot-comments —
which carry the `adp-correlation:` marker — therefore arrive at OUR webhook.
`_detect_sibling_app` flags this (metric + deduped WARNING) advisory-only,
without blocking the event.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directories to path (match test_handler.py)
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo"
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")

# A valid marker as produced by agent-worker-image/lib/correlation_marker.py.
MARKER = (
    "<!-- adp-correlation:67ca957d-9d9b-4ae3-98c7-264eef645846 "
    "adp-root-human:650f093f-ecd9-4ce1-a5a9-368e02c449cf "
    "adp-is-human-rooted:true -->\n"
)

OWN_SLUG = "adp-agent-platform-111"
FOREIGN_LOGIN = "aws-e-adp-agent-dev[bot]"
REPO = "aws-innovate/adp"


def _reset_state():
    """Reset module-level sibling-detection state between tests."""
    import handler

    handler._own_app_slug = None
    handler._sibling_warned.clear()


def _comment_payload(*, sender_type, sender_login, body):
    return {
        "action": "created",
        "repository": {"full_name": REPO},
        "sender": {"type": sender_type, "login": sender_login, "id": 42},
        "comment": {"body": body},
    }


class TestSiblingDetection:
    @patch("handler._get_own_app_slug", return_value=OWN_SLUG)
    @patch("handler._get_metrics")
    def test_foreign_bot_with_marker_detected(self, mock_metrics, _mock_slug, caplog):
        """Foreign bot + marker → metric emitted + WARNING logged."""
        _reset_state()
        import logging

        import handler

        metrics = MagicMock()
        mock_metrics.return_value = metrics

        payload = _comment_payload(
            sender_type="Bot", sender_login=FOREIGN_LOGIN, body=MARKER + "🤖 started"
        )
        with caplog.at_level(logging.WARNING):
            handler._detect_sibling_app(payload, REPO)

        metrics.record_sibling_app.assert_called_once_with(repo=REPO, sibling_login=FOREIGN_LOGIN)
        metrics.flush.assert_called()
        assert any("Sibling ADP App detected" in r.message for r in caplog.records)

    @patch("handler._get_own_app_slug", return_value=OWN_SLUG)
    @patch("handler._get_metrics")
    def test_own_slug_not_detected(self, mock_metrics, _mock_slug):
        """Bot + marker from OUR OWN slug → no detection (self-traffic)."""
        _reset_state()
        import handler

        metrics = MagicMock()
        mock_metrics.return_value = metrics

        payload = _comment_payload(
            sender_type="Bot", sender_login=f"{OWN_SLUG}[bot]", body=MARKER + "hi"
        )
        handler._detect_sibling_app(payload, REPO)

        metrics.record_sibling_app.assert_not_called()

    @patch("handler._get_own_app_slug", return_value=OWN_SLUG)
    @patch("handler._get_metrics")
    def test_bot_without_marker_not_detected(self, mock_metrics, _mock_slug):
        """Bot comment WITHOUT marker (e.g. dependabot) → no detection."""
        _reset_state()
        import handler

        metrics = MagicMock()
        mock_metrics.return_value = metrics

        payload = _comment_payload(
            sender_type="Bot",
            sender_login="dependabot[bot]",
            body="Bumps lodash from 4.17.20 to 4.17.21.",
        )
        handler._detect_sibling_app(payload, REPO)

        metrics.record_sibling_app.assert_not_called()

    @patch("handler._get_own_app_slug", return_value=OWN_SLUG)
    @patch("handler._get_metrics")
    def test_human_with_marker_text_not_detected(self, mock_metrics, _mock_slug):
        """Human comment containing the literal marker → no detection (type gate)."""
        _reset_state()
        import handler

        metrics = MagicMock()
        mock_metrics.return_value = metrics

        payload = _comment_payload(
            sender_type="User", sender_login="pranav", body=MARKER + "pasted marker"
        )
        handler._detect_sibling_app(payload, REPO)

        metrics.record_sibling_app.assert_not_called()

    @patch("handler._get_own_app_slug", return_value=OWN_SLUG)
    @patch("handler._get_metrics")
    def test_dedup_logs_once_metric_every_time(self, mock_metrics, _mock_slug, caplog):
        """Second event from same (repo, sibling) → logged once, metric each time."""
        _reset_state()
        import logging

        import handler

        metrics = MagicMock()
        mock_metrics.return_value = metrics

        payload = _comment_payload(
            sender_type="Bot", sender_login=FOREIGN_LOGIN, body=MARKER + "again"
        )
        with caplog.at_level(logging.WARNING):
            handler._detect_sibling_app(payload, REPO)
            handler._detect_sibling_app(payload, REPO)

        # Metric emitted on both events (durable record).
        assert metrics.record_sibling_app.call_count == 2
        # WARNING logged exactly once (deduped per container).
        warnings = [r for r in caplog.records if "Sibling ADP App detected" in r.message]
        assert len(warnings) == 1

    @patch("handler._get_own_app_slug", return_value="")
    @patch("handler._get_metrics")
    def test_unknown_own_slug_skips_detection(self, mock_metrics, _mock_slug):
        """If own slug can't be resolved → skip (avoid mis-flagging own traffic)."""
        _reset_state()
        import handler

        metrics = MagicMock()
        mock_metrics.return_value = metrics

        payload = _comment_payload(sender_type="Bot", sender_login=FOREIGN_LOGIN, body=MARKER + "x")
        handler._detect_sibling_app(payload, REPO)

        metrics.record_sibling_app.assert_not_called()


class TestOwnSlugResolution:
    @patch("handler._get_sm_client")
    def test_own_slug_read_from_meta_secret(self, mock_sm):
        """_get_own_app_slug reads app_slug from the -meta secret and caches it."""
        _reset_state()
        import handler

        client = MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": '{"app_slug": "adp-agent-platform-111"}'
        }
        mock_sm.return_value = client

        assert handler._get_own_app_slug() == "adp-agent-platform-111"
        # Cached: second call does not re-read.
        assert handler._get_own_app_slug() == "adp-agent-platform-111"
        assert client.get_secret_value.call_count == 1

    @patch("handler._get_sm_client")
    def test_own_slug_failsafe_empty_on_error(self, mock_sm):
        """Any SM error → returns "" (fail-safe; detection then skips)."""
        _reset_state()
        import handler

        client = MagicMock()
        client.get_secret_value.side_effect = RuntimeError("boom")
        mock_sm.return_value = client

        assert handler._get_own_app_slug() == ""

"""
Webhook-contract tests for the GitLab ingress.

Every test here asserts only on the webhook Lambda's *synchronous* HTTP reply:
token validation, and the actionable/ignored classification for note, merge-request
and push events. None of them needs an agent-worker pod, a queue consumer, or any
shared dev-fleet capacity, so they are deterministic and safe to gate merges on.

The live end-to-end assertions that DO need a worker pod live in
``test_gitlab_live_fleet.py`` and report as a separate CI check. #5354 split them
apart: when they shared this file, a busy fleet failed the whole suite and
presented as "your PR broke the GitLab integration" on diffs that could not reach
the GitLab path at all.

Run with:
    cd modules/agent-factory
    TEST_ENV=dev python3 -m pytest tests/e2e/test_gitlab_roundtrip.py -v

Requires:
    - GITLAB_WEBHOOK_ENDPOINT: API Gateway URL for the GitLab webhook ingress
    - GITLAB_WEBHOOK_SECRET: Token for GitLab webhook validation
    - GITLAB_URL: GitLab instance URL (e.g. http://10.0.x.x)
    - GITLAB_TOKEN: Personal/project access token for GitLab API calls
    - GITLAB_PROJECT_ID: Project ID for test issues
"""

from __future__ import annotations

import json
import os

import pytest
import requests

from .helpers.gitlab_fixtures import (
    gitlab_mr_note_payload,
    gitlab_note_payload,
    gitlab_push_payload,
)

# All tests require a live environment with GitLab + webhook infrastructure
pytestmark = [pytest.mark.integration, pytest.mark.gitlab]


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Return env var or skip the test."""
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"Missing required env var: {name}")
    return val


def _send_gitlab_webhook(
    endpoint: str,
    secret: str,
    payload: dict,
) -> requests.Response:
    """Send a GitLab webhook to the ingress endpoint.

    GitLab uses a simple token header (X-Gitlab-Token) rather than HMAC signing.
    """
    body_str = json.dumps(payload, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "X-Gitlab-Token": secret,
        "X-Gitlab-Event": "Note Hook",
    }
    return requests.post(endpoint, data=body_str, headers=headers, timeout=30)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitLabRoundTrip:
    """Synchronous webhook-contract tests for the GitLab ingress Lambda."""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Resolve required environment variables."""
        self.gitlab_webhook_endpoint = _require_env("GITLAB_WEBHOOK_ENDPOINT")
        self.gitlab_webhook_secret = _require_env("GITLAB_WEBHOOK_SECRET")
        self.gitlab_url = _require_env("GITLAB_URL")
        self.gitlab_token = _require_env("GITLAB_TOKEN")
        self.project_id = int(_require_env("GITLAB_PROJECT_ID"))
        self.project_path = os.environ.get("GITLAB_PROJECT_PATH", "test-group/test-repo")

    def test_invalid_token_rejected(self):
        """Webhook with wrong token returns 401."""
        payload = gitlab_note_payload(
            project_id=self.project_id,
            project_path=self.project_path,
            issue_iid=1,
            note_body="@agent this should be rejected",
        )
        resp = _send_gitlab_webhook(
            self.gitlab_webhook_endpoint,
            "wrong-token-definitely-invalid",
            payload,
        )
        assert resp.status_code == 401, (
            f"Expected 401 for invalid token, got {resp.status_code}: {resp.text}"
        )

    def test_non_mention_event_ignored(self):
        """Note event without @agent mention is acknowledged but not queued."""
        payload = gitlab_note_payload(
            project_id=self.project_id,
            project_path=self.project_path,
            issue_iid=1,
            note_body="Just a regular comment, no agent mention here",
        )
        resp = _send_gitlab_webhook(
            self.gitlab_webhook_endpoint,
            self.gitlab_webhook_secret,
            payload,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("status") == "ignored", (
            f"Expected 'ignored' status for non-mention, got: {body}"
        )

        # Handler contract tests assert publish_envelope is not called. Queue
        # peeking cannot prove absence when another consumer can drain a message.

    def test_non_issue_note_ignored(self):
        """Note on a MergeRequest (not Issue) is acknowledged but not queued."""
        payload = gitlab_mr_note_payload(
            project_id=self.project_id,
            project_path=self.project_path,
            note_body="@agent review this MR please",
        )
        resp = _send_gitlab_webhook(
            self.gitlab_webhook_endpoint,
            self.gitlab_webhook_secret,
            payload,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("status") == "ignored", f"Expected 'ignored' for MR note, got: {body}"

    def test_push_event_ignored(self):
        """Push event (non-note object_kind) is acknowledged but not queued."""
        payload = gitlab_push_payload(
            project_id=self.project_id,
            project_path=self.project_path,
        )
        # Push events don't use Note Hook event type
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = {
            "Content-Type": "application/json",
            "X-Gitlab-Token": self.gitlab_webhook_secret,
            "X-Gitlab-Event": "Push Hook",
        }
        resp = requests.post(
            self.gitlab_webhook_endpoint,
            data=body_str,
            headers=headers,
            timeout=30,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("status") == "ignored", f"Expected 'ignored' for push event, got: {body}"

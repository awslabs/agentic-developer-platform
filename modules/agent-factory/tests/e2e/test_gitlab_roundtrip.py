"""
End-to-end integration tests for the GitLab agent round-trip.

Tests the full flow: GitLab webhook → Lambda → SQS → agent-worker → GitLab comment.
This is the acceptance gate for Phase 0 of the GitLab CE spike.

Run with:
    cd modules/agent-factory
    TEST_ENV=dev python3 -m pytest tests/e2e/test_gitlab_roundtrip.py -v

Requires:
    - GITLAB_WEBHOOK_ENDPOINT: API Gateway URL for the GitLab webhook ingress
    - GITLAB_WEBHOOK_SECRET: Token for GitLab webhook validation
    - GITLAB_URL: GitLab instance URL (e.g. http://10.0.x.x)
    - GITLAB_TOKEN: Personal/project access token for GitLab API calls
    - GITLAB_PROJECT_ID: Project ID for test issues
    - WEBHOOK_SQS_QUEUE_URL: SQS queue URL for dispatch assertions
"""

from __future__ import annotations

import json
import os
import time
import uuid

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


def _poll_sqs_for_gitlab_message(
    sqs_client,
    queue_url: str,
    project_path: str,
    timeout: float = 15.0,
) -> dict | None:
    """Poll SQS for a GitLab-originated message matching the project path.

    Returns the parsed message body or None if not found within timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            MessageAttributeNames=["All"],
        )
        for msg in resp.get("Messages", []):
            try:
                body = json.loads(msg.get("Body", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            # Match on channel=gitlab and project path
            if (
                body.get("channel") == "gitlab"
                and body.get("payload", {}).get("source", {}).get("project_path") == project_path
            ):
                # Clean up the message
                sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
                return body
        # nosemgrep: arbitrary-sleep — polling interval in deadline-bounded loop
        time.sleep(1.0)
    return None


def _poll_gitlab_for_comment(
    gitlab_url: str,
    token: str,
    project_id: int,
    issue_iid: int,
    after_note_id: int = 0,
    timeout: float = 60.0,
) -> dict | None:
    """Poll GitLab issue comments until a new comment from the agent appears.

    Looks for any comment with a note ID greater than after_note_id,
    indicating it was posted after the test setup.

    Returns the comment dict or None if timeout is reached.
    """
    deadline = time.monotonic() + timeout
    headers = {"PRIVATE-TOKEN": token}
    url = f"{gitlab_url}/api/v4/projects/{project_id}/issues/{issue_iid}/notes"

    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                notes = resp.json()
                for note in notes:
                    if note.get("id", 0) > after_note_id:
                        return note
        except requests.RequestException:
            pass
        # nosemgrep: arbitrary-sleep — polling interval in deadline-bounded loop
        time.sleep(3.0)
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitLabRoundTrip:
    """End-to-end integration tests for the GitLab webhook → agent → response flow."""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Resolve required environment variables."""
        self.gitlab_webhook_endpoint = _require_env("GITLAB_WEBHOOK_ENDPOINT")
        self.gitlab_webhook_secret = _require_env("GITLAB_WEBHOOK_SECRET")
        self.gitlab_url = _require_env("GITLAB_URL")
        self.gitlab_token = _require_env("GITLAB_TOKEN")
        self.project_id = int(_require_env("GITLAB_PROJECT_ID"))
        self.sqs_queue_url = _require_env("WEBHOOK_SQS_QUEUE_URL")
        self.sqs_client = __import__("boto3").client(
            "sqs", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        self.project_path = os.environ.get("GITLAB_PROJECT_PATH", "test-group/test-repo")

    def test_full_agent_roundtrip(self):
        """Mention @agent on GitLab issue -> webhook -> Lambda -> SQS -> agent responds within 60s."""
        # 1. Create a test issue via GitLab API
        issue_title = f"E2E test issue {uuid.uuid4().hex[:8]}"
        create_resp = requests.post(
            f"{self.gitlab_url}/api/v4/projects/{self.project_id}/issues",
            headers={"PRIVATE-TOKEN": self.gitlab_token},
            json={"title": issue_title, "description": "Automated E2E test"},
            timeout=10,
        )
        assert create_resp.status_code == 201, (
            f"Failed to create GitLab issue: {create_resp.status_code} {create_resp.text}"
        )
        issue = create_resp.json()
        issue_iid = issue["iid"]

        try:
            # 2. Get the current latest note ID (baseline for polling)
            notes_resp = requests.get(
                f"{self.gitlab_url}/api/v4/projects/{self.project_id}/issues/{issue_iid}/notes",
                headers={"PRIVATE-TOKEN": self.gitlab_token},
                timeout=10,
            )
            existing_notes = notes_resp.json() if notes_resp.status_code == 200 else []
            baseline_note_id = max((n["id"] for n in existing_notes), default=0)

            # 3. Send webhook POST to Lambda endpoint (simulate GitLab firing the hook)
            payload = gitlab_note_payload(
                project_id=self.project_id,
                project_path=self.project_path,
                issue_iid=issue_iid,
                note_body="@agent hello from E2E test — please acknowledge",
                username="e2e-tester",
                gitlab_url=self.gitlab_url,
            )
            resp = _send_gitlab_webhook(
                self.gitlab_webhook_endpoint,
                self.gitlab_webhook_secret,
                payload,
            )
            assert resp.status_code == 200, f"Webhook rejected: {resp.status_code} {resp.text}"
            body = resp.json()
            assert body.get("status") == "accepted", f"Webhook not accepted: {body}"

            # 4. Verify SQS received the message
            sqs_msg = _poll_sqs_for_gitlab_message(
                self.sqs_client, self.sqs_queue_url, self.project_path, timeout=15
            )
            assert sqs_msg is not None, "No GitLab message found in SQS within 15s"
            assert sqs_msg["channel"] == "gitlab"
            assert sqs_msg["payload"]["source"]["issue_iid"] == issue_iid

            # 5. Poll GitLab for agent response comment (timeout: 60s)
            agent_comment = _poll_gitlab_for_comment(
                self.gitlab_url,
                self.gitlab_token,
                self.project_id,
                issue_iid,
                after_note_id=baseline_note_id,
                timeout=60,
            )
            assert agent_comment is not None, (
                f"Agent did not respond on GitLab issue #{issue_iid} within 60s"
            )
            assert len(agent_comment.get("body", "")) > 0, "Agent comment body is empty"
        finally:
            # Cleanup: close the test issue
            requests.put(
                f"{self.gitlab_url}/api/v4/projects/{self.project_id}/issues/{issue_iid}",
                headers={"PRIVATE-TOKEN": self.gitlab_token},
                json={"state_event": "close"},
                timeout=10,
            )

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

        # Verify no SQS message produced (queue empty for this project after 5s)
        sqs_msg = _poll_sqs_for_gitlab_message(
            self.sqs_client, self.sqs_queue_url, self.project_path, timeout=5
        )
        assert sqs_msg is None, "Non-mention event should NOT produce an SQS message"

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

    def test_persona_extraction(self):
        """@agent-developer mention extracts 'developer' as the persona."""
        payload = gitlab_note_payload(
            project_id=self.project_id,
            project_path=self.project_path,
            issue_iid=1,
            note_body="@agent-developer please implement this feature",
        )
        resp = _send_gitlab_webhook(
            self.gitlab_webhook_endpoint,
            self.gitlab_webhook_secret,
            payload,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "accepted", f"Expected accepted, got: {body}"

        # Verify SQS message has correct persona
        sqs_msg = _poll_sqs_for_gitlab_message(
            self.sqs_client, self.sqs_queue_url, self.project_path, timeout=15
        )
        assert sqs_msg is not None, "No SQS message received"
        assert sqs_msg.get("persona") == "developer", (
            f"Expected persona 'developer', got '{sqs_msg.get('persona')}'"
        )
        assert sqs_msg["payload"]["content"]["mention_target"] == "developer"

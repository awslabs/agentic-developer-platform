"""
Live-fleet assertions for the GitLab agent round-trip.

These two tests are the platform's only live proof that the full path works:
GitLab webhook -> Lambda -> SQS -> KEDA -> agent-worker pod -> GitLab comment.
Unlike the webhook-contract tests in ``test_gitlab_roundtrip.py``, they require a
real worker pod to start, which means they depend on shared dev-fleet capacity.

#5354 separated them for exactly that reason. They are NOT xfail and NOT
weakened — they still assert that a real worker acknowledges a real mention.
What changed:

  * they report as their own CI check, so a saturated fleet can never present as
    "your PR broke the GitLab integration" on an unrelated diff;
  * on expiry they report *why* — queue depth, how many workers claimed a
    message during the wait, and the webhook's queue message id — instead of
    only "did not respond";
  * they record enqueue->acknowledgement latency on every run, so fleet
    saturation is a tracked number rather than a coin flip.

The live acceptance deadline remains 60 seconds. Expiry diagnostics distinguish
a late consumer from a broken path without extending or making that requirement
configurable; the latency record carries the separate performance signal.

Run with:
    cd modules/agent-factory
    TEST_ENV=dev python3 -m pytest tests/e2e/test_gitlab_live_fleet.py -v
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
import requests

from .helpers.fleet_diagnostics import collect_fleet_state, format_expiry_diagnostics
from .helpers.gitlab_fixtures import gitlab_note_payload
from .helpers.live_fleet_latency import publish_ack_latency

# These require a live environment AND available fleet capacity.
pytestmark = [pytest.mark.integration, pytest.mark.gitlab, pytest.mark.live_fleet]

# Authorized live acceptance requirement: acknowledgement within 60 seconds.
# Keep this fixed; diagnostics explain expiry without weakening the deadline.
ACK_TIMEOUT_SECONDS = 60.0

POLL_INTERVAL = 3.0


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"Missing required env var: {name}")
    return val


def _send_gitlab_webhook(endpoint: str, secret: str, payload: dict) -> requests.Response:
    """Send a GitLab webhook to the ingress endpoint (token header, not HMAC)."""
    return requests.post(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")),
        headers={
            "Content-Type": "application/json",
            "X-Gitlab-Token": secret,
            "X-Gitlab-Event": "Note Hook",
        },
        timeout=30,
    )


def _poll_gitlab_for_comment(
    gitlab_url: str,
    token: str,
    project_id: int,
    issue_iid: int,
    after_note_id: int = 0,
    timeout: float = ACK_TIMEOUT_SECONDS,
    expected_persona: str = "agent",
) -> dict | None:
    """Poll issue notes for the worker's acknowledgement of ``expected_persona``.

    Requires the real worker acknowledgement on this test issue, after the setup
    baseline. Unrelated issue comments do not pass.
    """
    deadline = time.monotonic() + timeout
    headers = {"PRIVATE-TOKEN": token}
    url = f"{gitlab_url}/api/v4/projects/{project_id}/issues/{issue_iid}/notes"

    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                for note in resp.json():
                    if (
                        note.get("id", 0) > after_note_id
                        and f"**Agent `{expected_persona}` acknowledged**" in note.get("body", "")
                        and "Correlation: `" in note.get("body", "")
                    ):
                        return note
        except requests.RequestException:
            pass
        # nosemgrep: arbitrary-sleep — polling interval in deadline-bounded loop
        time.sleep(POLL_INTERVAL)
    return None


def _await_ack_with_evidence(
    *,
    recorder,
    gitlab_url: str,
    token: str,
    project_id: int,
    issue_iid: int,
    baseline_note_id: int,
    persona: str,
    sqs_message_id: str | None,
    enqueue_epoch_ms: int,
    timeout: float,
) -> dict:
    """Wait for the worker acknowledgement, recording latency or expiry evidence.

    On success: records enqueue->ack latency to the JSON harness and the metric
    sink. On expiry: raises AssertionError naming the fleet state at that moment.
    """
    started = time.monotonic()
    note = _poll_gitlab_for_comment(
        gitlab_url,
        token,
        project_id,
        issue_iid,
        after_note_id=baseline_note_id,
        timeout=timeout,
        expected_persona=persona,
    )
    elapsed = round(time.monotonic() - started, 3)
    queue_url = os.environ.get("WEBHOOK_SQS_QUEUE_URL", "")

    if note is None:
        state = collect_fleet_state(queue_url, enqueue_epoch_ms)
        recorder.note("outcome", "expired")
        recorder.note("enqueue_to_expiry_s", f"{elapsed}")
        for key, value in state.to_dict().items():
            recorder.note(key, str(value))
        raise AssertionError(
            format_expiry_diagnostics(
                f"No `{persona}` acknowledgement on GitLab issue #{issue_iid}",
                timeout,
                sqs_message_id,
                state,
            )
        )

    recorder.mark("ack")
    recorder.note("outcome", "acknowledged")
    recorder.note("enqueue_to_ack_s", f"{elapsed}")
    recorder.note("persona", persona)
    recorder.note("sqs_id", sqs_message_id or "unavailable")

    # Fleet state on success too: a 150s pass and a 9s pass are different facts,
    # and the concurrency behind each is what makes the series interpretable.
    state = collect_fleet_state(queue_url, enqueue_epoch_ms)
    for key, value in state.to_dict().items():
        recorder.note(key, str(value))

    published = publish_ack_latency(
        elapsed, persona, workers_claimed=state.worker_streams_since_enqueue
    )
    recorder.note("latency_metric_published", "yes" if published else "no")
    print(
        f"[live-fleet] persona={persona} enqueue_to_ack={elapsed}s "
        f"in_flight={state.in_flight} visible={state.visible} "
        f"workers_claimed={state.worker_streams_since_enqueue} "
        f"metric_published={published}"
    )
    return note


class TestGitLabLiveFleet:
    """Webhook -> Lambda -> SQS -> agent-worker -> GitLab comment, end to end."""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        self.gitlab_webhook_endpoint = _require_env("GITLAB_WEBHOOK_ENDPOINT")
        self.gitlab_webhook_secret = _require_env("GITLAB_WEBHOOK_SECRET")
        self.gitlab_url = _require_env("GITLAB_URL")
        self.gitlab_token = _require_env("GITLAB_TOKEN")
        self.project_id = int(_require_env("GITLAB_PROJECT_ID"))
        self.project_path = os.environ.get("GITLAB_PROJECT_PATH", "test-group/test-repo")

    def _create_issue(self, title: str, description: str) -> int:
        resp = requests.post(
            f"{self.gitlab_url}/api/v4/projects/{self.project_id}/issues",
            headers={"PRIVATE-TOKEN": self.gitlab_token},
            json={"title": title, "description": description},
            timeout=10,
        )
        assert resp.status_code == 201, (
            f"Failed to create GitLab issue: {resp.status_code} {resp.text}"
        )
        return resp.json()["iid"]

    def _baseline_note_id(self, issue_iid: int) -> int:
        resp = requests.get(
            f"{self.gitlab_url}/api/v4/projects/{self.project_id}/issues/{issue_iid}/notes",
            headers={"PRIVATE-TOKEN": self.gitlab_token},
            timeout=10,
        )
        notes = resp.json() if resp.status_code == 200 else []
        return max((n["id"] for n in notes), default=0)

    def _close_issue(self, issue_iid: int) -> requests.Response:
        return requests.put(
            f"{self.gitlab_url}/api/v4/projects/{self.project_id}/issues/{issue_iid}",
            headers={"PRIVATE-TOKEN": self.gitlab_token},
            json={"state_event": "close"},
            timeout=10,
        )

    def _mention_and_await(self, *, note_body: str, persona: str, recorder, issue_iid: int):
        baseline_note_id = self._baseline_note_id(issue_iid)
        payload = gitlab_note_payload(
            project_id=self.project_id,
            project_path=self.project_path,
            issue_iid=issue_iid,
            note_body=note_body,
            username="e2e-tester",
            gitlab_url=self.gitlab_url,
        )
        resp = _send_gitlab_webhook(
            self.gitlab_webhook_endpoint, self.gitlab_webhook_secret, payload
        )
        assert resp.status_code == 200, f"Webhook rejected: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body.get("status") == "accepted", f"Webhook not accepted: {body}"

        # Enqueue is the start of the latency being measured; everything before
        # it is synchronous ingress already covered by the contract check.
        recorder.mark("enqueue")
        enqueue_epoch_ms = int(time.time() * 1000)

        return _await_ack_with_evidence(
            recorder=recorder,
            gitlab_url=self.gitlab_url,
            token=self.gitlab_token,
            project_id=self.project_id,
            issue_iid=issue_iid,
            baseline_note_id=baseline_note_id,
            persona=persona,
            sqs_message_id=body.get("message_id"),
            enqueue_epoch_ms=enqueue_epoch_ms,
            timeout=ACK_TIMEOUT_SECONDS,
        )

    def test_full_agent_roundtrip(self, latency_recorder):
        """Mention @agent on a GitLab issue -> a real worker acknowledges it."""
        recorder = latency_recorder
        issue_iid = self._create_issue(
            f"E2E test issue {uuid.uuid4().hex[:8]}", "Automated E2E test"
        )
        try:
            note = self._mention_and_await(
                note_body="@agent hello from E2E test — please acknowledge",
                persona="agent",
                recorder=recorder,
                issue_iid=issue_iid,
            )
            assert len(note.get("body", "")) > 0, "Agent comment body is empty"
        finally:
            self._close_issue(issue_iid)

    def test_persona_extraction(self, latency_recorder):
        """@agent-developer routes to, and is acknowledged by, the developer persona."""
        recorder = latency_recorder
        issue_iid = self._create_issue(
            f"E2E persona test {uuid.uuid4().hex[:12]}", "Automated persona-routing test"
        )
        try:
            self._mention_and_await(
                note_body="@agent-developer acknowledge this persona-routing test",
                persona="developer",
                recorder=recorder,
                issue_iid=issue_iid,
            )
        finally:
            cleanup = self._close_issue(issue_iid)
            assert cleanup.status_code == 200, "Could not close persona test issue"

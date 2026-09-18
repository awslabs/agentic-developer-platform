from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from common import codex_review_dispatcher as dispatcher  # noqa: E402


def payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "installation": {"id": 42},
        "repository": {"full_name": "aws-e/adp"},
        "pull_request": {
            "number": 6000,
            "html_url": "https://github.com/aws-e/adp/pull/6000",
            "head": {
                "ref": "agent/issue-5054",
                "sha": "a" * 40,
                "repo": {"full_name": "aws-e/adp"},
            },
            "base": {"ref": "main"},
        },
    }


def test_routes_opened_and_synchronize_to_an_independent_envelope(monkeypatch):
    monkeypatch.setenv("CODEX_REVIEWER_ENABLED", "true")
    for action in ("opened", "synchronize"):
        body = payload(action)
        assert dispatcher.eligible("pull_request", body)
        envelope = dispatcher.build_envelope(
            body, tenant_id="tenant-1", message_id=f"delivery-{action}"
        )
        assert envelope["kind"] == "codex_pr_review"
        assert envelope["pull_request"]["number"] == 6000
        assert envelope["pull_request"]["issue_number"] == 5054
        assert envelope["pull_request"]["expected_head_sha"] == "a" * 40


def test_does_not_route_forks_or_non_agent_branches(monkeypatch):
    monkeypatch.setenv("CODEX_REVIEWER_ENABLED", "true")
    fork = payload()
    fork["pull_request"]["head"]["repo"]["full_name"] = "someone/adp"
    assert not dispatcher.eligible("pull_request", fork)
    ordinary = payload()
    ordinary["pull_request"]["head"]["ref"] = "feature/example"
    assert not dispatcher.eligible("pull_request", ordinary)


def test_feature_flag_fails_closed(monkeypatch):
    monkeypatch.setenv("CODEX_REVIEWER_ENABLED", "false")
    assert not dispatcher.eligible("pull_request", payload())


def test_publish_uses_dedicated_fifo_identity(monkeypatch):
    calls = []

    class SQS:
        def send_message(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "sqs-1"}

    monkeypatch.setenv("CODEX_REVIEW_QUEUE_URL", "https://sqs/reviews.fifo")
    monkeypatch.setattr(dispatcher, "_sqs", SQS())
    envelope = dispatcher.build_envelope(payload(), tenant_id="tenant-1", message_id="run-1")
    assert dispatcher.publish(envelope) == "sqs-1"
    assert calls[0]["MessageDeduplicationId"] == "run-1"
    assert calls[0]["MessageGroupId"].endswith("#pr-6000")

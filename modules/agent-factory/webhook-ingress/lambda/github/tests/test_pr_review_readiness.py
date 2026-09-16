"""Draft publication must not dispatch review; completing it must dispatch."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common import skip_reasons
from intent_parser import extract_intent_with_reason


@pytest.fixture(params=["User", "Bot"])
def pr_event(request):
    fixture = Path(__file__).parent / "fixtures" / "pr_opened.json"
    payload = json.loads(fixture.read_text())
    if request.param == "Bot":
        payload["sender"] = {"login": "adp-agent[bot]", "id": 900, "type": "Bot"}
    payload["pull_request"]["draft"] = False
    return payload


@pytest.mark.parametrize("action", ["opened", "synchronize", "ready_for_review"])
def test_draft_never_dispatches_reviewer(pr_event, action):
    pr_event["action"] = action
    pr_event["pull_request"]["draft"] = True

    intent, reason = extract_intent_with_reason("pull_request", pr_event)

    assert intent is None
    assert reason == skip_reasons.PR_DRAFT


@pytest.mark.parametrize("action", ["opened", "ready_for_review"])
def test_ready_agent_pr_dispatches_reviewer(pr_event, action):
    pr_event["action"] = action

    intent, reason = extract_intent_with_reason("pull_request", pr_event)

    assert reason is None
    assert intent.persona == "reviewer"
    assert intent.trigger == f"pr_{action}"


def test_marking_existing_draft_ready_is_the_review_handoff(pr_event):
    pr_event["pull_request"]["draft"] = True
    assert extract_intent_with_reason("pull_request", pr_event)[0] is None
    pr_event["action"] = "synchronize"
    assert extract_intent_with_reason("pull_request", pr_event)[0] is None
    pr_event["action"] = "ready_for_review"
    pr_event["pull_request"]["draft"] = False
    assert extract_intent_with_reason("pull_request", pr_event)[0].persona == "reviewer"


def test_ready_event_preserves_agent_branch_filter(pr_event):
    pr_event["action"] = "ready_for_review"
    pr_event["pull_request"]["head"]["ref"] = "feature/other"

    assert extract_intent_with_reason("pull_request", pr_event) == (
        None,
        skip_reasons.PR_BRANCH_NOT_AGENT,
    )


def test_ready_pr_push_preserves_bot_loop_guard(pr_event):
    pr_event["action"] = "synchronize"

    intent, reason = extract_intent_with_reason("pull_request", pr_event)

    if pr_event["sender"]["type"] == "Bot":
        assert intent is None
        assert reason == skip_reasons.BOT_SYNCHRONIZE_DEDUP
    else:
        assert intent.persona == "reviewer"
        assert reason is None

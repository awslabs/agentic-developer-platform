"""Automatic PR review pause must preserve explicit issue and agent triggers."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common import skip_reasons
from intent_parser import extract_intent_with_reason
from test_handler_skip_reason import _HandlerHarness


def payload(action="opened"):
    return {
        "action": action,
        "pull_request": {
            "number": 42,
            "draft": False,
            "head": {"ref": "agent/issue-42", "sha": "a" * 40},
        },
        "repository": {"full_name": "acme/repo"},
        "sender": {"login": "user", "id": 1, "type": "User"},
        "installation": {"id": 123},
    }


@pytest.mark.parametrize("setting", [None, "false", "", "1", "yes", "invalid"])
@pytest.mark.parametrize("action", ["opened", "ready_for_review", "synchronize"])
def test_automatic_review_requires_explicit_enablement(monkeypatch, setting, action):
    if setting is None:
        monkeypatch.delenv("GITHUB_AUTO_PR_REVIEW_ENABLED", raising=False)
    else:
        monkeypatch.setenv("GITHUB_AUTO_PR_REVIEW_ENABLED", setting)
    assert extract_intent_with_reason("pull_request", payload(action)) == (
        None,
        skip_reasons.AUTOMATIC_PR_REVIEW_DISABLED,
    )


@pytest.mark.parametrize("setting", ["true", "TRUE", " true "])
def test_operator_can_explicitly_enable_automatic_reviews(monkeypatch, setting):
    monkeypatch.setenv("GITHUB_AUTO_PR_REVIEW_ENABLED", setting)
    intent, reason = extract_intent_with_reason("pull_request", payload())
    assert intent.persona == "agent-codex-reviewer" and reason is None


def test_disabled_automatic_review_is_acknowledged_without_queueing(monkeypatch):
    monkeypatch.setenv("GITHUB_AUTO_PR_REVIEW_ENABLED", "false")
    with patch("common.sqs_publisher.publish_envelope") as publish:
        result, capture = _HandlerHarness.run("pull_request", payload())
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["reason"] == skip_reasons.AUTOMATIC_PR_REVIEW_DISABLED
    assert capture.call_args.kwargs["skip_reason"] == skip_reasons.AUTOMATIC_PR_REVIEW_DISABLED
    publish.assert_not_called()


@pytest.mark.parametrize("engine_enabled", ["false", "true"])
@pytest.mark.parametrize(
    "kind,persona",
    [("mention", "developer"), ("label", "developer"), ("mention", "agent-codex-reviewer")],
)
def test_explicit_issue_requests_still_reach_the_queue(monkeypatch, engine_enabled, kind, persona):
    monkeypatch.setenv("GITHUB_AUTO_PR_REVIEW_ENABLED", "false")
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", engine_enabled)
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    event = payload("created" if kind == "mention" else "labeled")
    event.pop("pull_request")
    event["issue"] = {"number": 42}
    if kind == "mention":
        tag = "@agent-developer" if persona == "developer" else "@agent-codex-reviewer"
        event["comment"] = {"body": f"{tag} please work on this issue"}
    else:
        event["label"] = {"name": "developer"}
    with (
        patch("common.spawn_persona._write_pointer_and_provenance"),
        patch("common.spawn_persona._capture_invocation_event"),
        patch("common.sqs_publisher.publish_envelope", return_value="test-message") as publish,
    ):
        result, _ = _HandlerHarness.run("issue_comment" if kind == "mention" else "issues", event)
    assert result["statusCode"] == 202, result
    publish.assert_called_once()
    assert publish.call_args.args[0]["persona"] == persona


def test_iam_agent_trigger_remains_independent_of_engine_and_auto_review_pause(monkeypatch):
    from agent_trigger import handle_agent_trigger
    from test_agent_trigger import _chain_record, _make_event, _valid_body

    monkeypatch.setenv("GITHUB_AUTO_PR_REVIEW_ENABLED", "false")
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "false")
    with (
        patch("agent_trigger._resolve_chain", return_value=_chain_record()),
        patch("common.installation_resolver.resolve_installation_for_tenant", return_value=123),
        patch(
            "common.spawn_persona.spawn_persona",
            return_value=MagicMock(success=True, message_id="test-message", block_reason=None),
        ) as spawn,
    ):
        response = handle_agent_trigger(_make_event(_valid_body()), None)
    assert response["statusCode"] == 202
    spawn.assert_called_once()

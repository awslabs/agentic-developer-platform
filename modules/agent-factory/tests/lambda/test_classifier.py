"""
Tests for the classifier prompt and contract.

These tests protect the *shape* of the classifier:
  - The system prompt contains the guardrails we rely on (real-time triggers,
    refusal language ban, history-as-context framing).
  - The prompt passed to Bedrock wraps the current user message in explicit
    <current-user-message> tags so history is context-only.
  - The ClassificationResult handles the response fields defensively.

They do NOT test Bedrock's actual routing decisions — that requires a live
model and is covered by the Playwright end-to-end probe (tracked separately
under the agent-operations e2e issue).
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def classifier_module(monkeypatch):
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    # Drop any stale cached module so imports see the current env.
    for k in list(sys.modules.keys()):
        if k == "classifier":
            del sys.modules[k]
    import classifier  # noqa: E402
    return classifier


# ---------------------------------------------------------------------------
# Prompt shape — guardrails must be present
# ---------------------------------------------------------------------------


class TestClassifierPromptShape:
    """The prompt is a contract. These tests fail loudly if someone removes
    guardrails we depend on to avoid regressions.

    Each assertion here corresponds to a real bug observed in the admin UI:
    - "what time in UK" routed to direct_response → model claimed no clock
      access → we need "hard triggers" for real-time asks.
    - "tell me about top 5 X" routed to direct_response → model gave stale
      training-data answer → we need research/latest/trending triggers.
    - Classifier's canned reply contained "I don't have access to the web"
      and "as an AI I cannot..." → we need an explicit ban on refusal
      phrases in the response field.
    - Classifier carried topic across turns (new "hello" got an answer about
      the prior question) → we need history-as-context-only framing.
    """

    def test_prompt_has_realtime_triggers(self, classifier_module):
        p = classifier_module.CLASSIFIER_SYSTEM_PROMPT
        # These phrases force long_running for clock / date / "now" asks.
        for signal in [
            "current time",
            "current date",
            "right now",
            "today's",
            "latest",
            "trending",
            "research",
        ]:
            assert signal in p, f"Classifier prompt missing real-time trigger: {signal!r}"

    def test_prompt_bans_refusal_phrases_in_direct_response(self, classifier_module):
        p = classifier_module.CLASSIFIER_SYSTEM_PROMPT
        # When the classifier would write any of these into `response`, it
        # should escalate to long_running instead.
        banned_signals = [
            "I can't run commands",
            "I don't have access to the web",
            "as an AI I cannot",
            "I don't have real-time data",
            "my knowledge has a cutoff",
            "check [website]",
            "might be out of date",
        ]
        for signal in banned_signals:
            assert signal in p, f"Classifier prompt missing refusal-ban exemplar: {signal!r}"

    def test_prompt_states_long_running_is_default(self, classifier_module):
        # "When in doubt → long_running" is a load-bearing rule.
        p = classifier_module.CLASSIFIER_SYSTEM_PROMPT
        assert "DEFAULT" in p, "long_running must be the default path"
        assert "When in doubt" in p

    def test_prompt_requires_escalation_note(self, classifier_module):
        p = classifier_module.CLASSIFIER_SYSTEM_PROMPT
        assert "escalation_note" in p
        # REQUIRED on long_running / github_actions so the user sees an
        # immediate ack.
        assert "REQUIRED" in p

    def test_prompt_describes_long_running_tools(self, classifier_module):
        """The classifier needs to know the long_running agent has real tools
        so it doesn't fabricate a refusal on the direct_response path."""
        p = classifier_module.CLASSIFIER_SYSTEM_PROMPT
        for tool in ["Bash", "WebSearch", "WebFetch"]:
            assert tool in p, f"Classifier prompt must name the {tool} tool"


# ---------------------------------------------------------------------------
# History framing — prior turns must be scoped as context, not new asks
# ---------------------------------------------------------------------------


class TestClassifierHistoryFraming:
    """A new user message must be classified on its own merits — not inherit
    topic / routing from prior turns. Regression: a "hello" following an
    earlier "what time in UK" was answered with time-of-day content."""

    def test_current_message_wrapped_in_tag(self, classifier_module, monkeypatch):
        captured = {}

        class FakeBedrock:
            def invoke_model(self, **kwargs):
                captured["body"] = json.loads(kwargs["body"])
                # Minimal valid classifier response so the function doesn't raise.
                reply = json.dumps({
                    "path": "direct_response",
                    "persona": "developer",
                    "response": "ok",
                    "thread_action": "none",
                    "reasoning": "test",
                })
                return {
                    "body": _fake_body_stream({"content": [{"text": reply}]}),
                }

        monkeypatch.setattr(classifier_module, "_get_bedrock", lambda: FakeBedrock())
        classifier_module.classify_message(
            "what is the weather",
            conversation_history=[{"role": "user", "content": "earlier question"}],
        )

        assert "body" in captured
        user_content = captured["body"]["messages"][0]["content"]
        assert "<current-user-message>" in user_content
        assert "what is the weather" in user_content
        # The instruction to treat prior turns as context-only must be there.
        assert "CONTEXT only" in user_content or "CONTEXT ONLY" in user_content

    def test_prior_turns_wrapped_in_prior_tags(self, classifier_module, monkeypatch):
        captured = {}

        class FakeBedrock:
            def invoke_model(self, **kwargs):
                captured["body"] = json.loads(kwargs["body"])
                reply = json.dumps({
                    "path": "direct_response",
                    "persona": "developer",
                    "response": "ok",
                    "thread_action": "none",
                    "reasoning": "test",
                })
                return {"body": _fake_body_stream({"content": [{"text": reply}]})}

        monkeypatch.setattr(classifier_module, "_get_bedrock", lambda: FakeBedrock())
        classifier_module.classify_message(
            "hi",
            conversation_history=[
                {"role": "user", "content": "prior ask"},
                {"role": "assistant", "content": "prior reply"},
            ],
        )

        user_content = captured["body"]["messages"][0]["content"]
        assert "<prior-user>" in user_content
        assert "<prior-assistant>" in user_content
        assert "prior ask" in user_content


# ---------------------------------------------------------------------------
# Failure mode: bad Bedrock response shouldn't crash
# ---------------------------------------------------------------------------


class TestClassifierFailureModes:
    def test_bedrock_failure_falls_back_to_long_running(self, classifier_module, monkeypatch):
        """If Bedrock raises, we must route to long_running so the user's ask
        reaches the real agent rather than a bogus direct_response."""

        class BrokenBedrock:
            def invoke_model(self, **kwargs):
                raise RuntimeError("bedrock down")

        monkeypatch.setattr(classifier_module, "_get_bedrock", lambda: BrokenBedrock())
        result = classifier_module.classify_message("anything")
        assert result.path == "long_running"
        assert "Classification failed" in result.reasoning


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_body_stream(obj):
    """Mimic the StreamingBody shape boto3 returns from Bedrock."""
    import io

    data = json.dumps(obj).encode("utf-8")
    stream = io.BytesIO(data)
    # boto3 exposes .read() — that's all the classifier calls.
    return stream

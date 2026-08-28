"""Tests for the orchestration notification path (issue #4211).

The load-bearing case in this file is **failure is loud**. The bug class this
story guards against is "notification path silently fails — detection works and
nobody finds out, indistinguishable from no detection at all", so every test that
asserts an exception is raised is asserting the absence of a fail-soft guard, not
merely an error message.

The SNS client is replaced with a recording double rather than mocked with
`unittest.mock`, so the assertions are about the actual publish arguments (topic,
subject, body, message attributes) and not about call bookkeeping.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from src.orchestration import notify as notify_module
from src.orchestration.notify import (
    TOPIC_ARN_ENV,
    Notification,
    NotificationError,
    NotificationsDisabledError,
    NotifyConfig,
    notify,
)

TOPIC = "arn:aws:sns:us-east-1:123456789012:adp-dev-orchestration-alerts"


class _RecordingSNS:
    """Records publishes. Optionally fails, to exercise the loud-failure path."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.publishes: list[dict] = []
        self._fail_with = fail_with

    def publish(self, **kwargs):
        if self._fail_with is not None:
            raise self._fail_with
        self.publishes.append(kwargs)
        return {"MessageId": f"msg-{len(self.publishes)}"}


@pytest.fixture
def sns(monkeypatch):
    """Install a recording SNS double in place of the cached boto3 client."""
    client = _RecordingSNS()
    monkeypatch.setattr(notify_module, "_sns_client", lambda _region: client)
    return client


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """The client is lru_cached, so a real client must not leak between tests."""
    notify_module._reset_client_cache()
    yield
    notify_module._reset_client_cache()


def _notification(**overrides) -> Notification:
    base = {
        "org_id": "org-alpha",
        "flow_id": "flow-1",
        "node_id": "node-1",
        "event": "node_stalled",
        "summary": "stalled: 20000s in 'running' exceeds the 19440s threshold",
        "detail": {"elapsed_seconds": 20000, "stall_threshold_seconds": 19440},
    }
    base.update(overrides)
    return Notification(**base)


class TestConfig:
    """The target is configuration, never a hard-coded address."""

    def test_reads_topic_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(TOPIC_ARN_ENV, TOPIC)
        assert NotifyConfig.from_env().topic_arn == TOPIC

    def test_unset_topic_is_disabled(self, monkeypatch):
        monkeypatch.delenv(TOPIC_ARN_ENV, raising=False)
        config = NotifyConfig.from_env()
        assert config.topic_arn is None
        assert config.enabled is False

    def test_whitespace_only_topic_is_treated_as_unset(self, monkeypatch):
        # An unset-but-present Terraform variable renders as "". It must behave
        # identically to an absent one rather than passing an empty ARN to SNS and
        # producing an opaque client error.
        monkeypatch.setenv(TOPIC_ARN_ENV, "   ")
        assert NotifyConfig.from_env().enabled is False

    def test_no_hardcoded_delivery_address_in_the_module(self):
        # Source-level: an email address, a Slack webhook or a bare topic ARN
        # baked into this file would defeat "target is configuration". Docstrings
        # are excluded — they legitimately discuss the design.
        tree = ast.parse(Path(inspect.getfile(notify_module)).read_text())
        docstrings = {
            ast.get_docstring(node) for node in ast.walk(tree) if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for literal in ast.walk(tree):
            if not (isinstance(literal, ast.Constant) and isinstance(literal.value, str)):
                continue
            if literal.value in docstrings:
                continue
            value = literal.value.lower()
            assert "@" not in value or "arn:" not in value, f"hard-coded address in notify.py: {literal.value!r}"
            assert not value.startswith("arn:aws:sns"), f"hard-coded topic ARN in notify.py: {literal.value!r}"
            assert "hooks.slack.com" not in value, f"hard-coded Slack webhook in notify.py: {literal.value!r}"


class TestDelivery:
    """A delivered notification, with the org it belongs to."""

    def test_publishes_to_the_configured_topic(self, sns):
        message_id = notify(_notification(), NotifyConfig(topic_arn=TOPIC, aws_region="us-east-1"))

        assert message_id == "msg-1"
        assert len(sns.publishes) == 1
        assert sns.publishes[0]["TopicArn"] == TOPIC

    def test_body_carries_the_event_org_flow_node_and_reason(self, sns):
        notify(_notification(), NotifyConfig(topic_arn=TOPIC))

        body = json.loads(sns.publishes[0]["Message"])
        assert body["event"] == "node_stalled"
        assert body["org_id"] == "org-alpha"
        assert body["flow_id"] == "flow-1"
        assert body["node_id"] == "node-1"
        # The reason is what makes the alert actionable rather than merely alarming.
        assert "19440s threshold" in body["summary"]
        assert body["detail"]["elapsed_seconds"] == 20000

    def test_message_attributes_allow_per_org_subscription_filtering(self, sns):
        # Tenant isolation: a subscriber can filter server-side on its own org, so
        # one org's operators never receive another org's events.
        notify(_notification(), NotifyConfig(topic_arn=TOPIC))

        attributes = sns.publishes[0]["MessageAttributes"]
        assert attributes["org_id"]["StringValue"] == "org-alpha"
        assert attributes["event"]["StringValue"] == "node_stalled"

    def test_subject_names_the_org_and_the_event(self, sns):
        notify(_notification(), NotifyConfig(topic_arn=TOPIC))
        subject = sns.publishes[0]["Subject"]
        assert "node_stalled" in subject
        assert "org-alpha" in subject

    def test_subject_is_truncated_to_the_sns_limit(self, sns):
        # SNS rejects subjects over 100 chars outright. A long org id must not fail
        # the publish that carries the alert about it.
        notify(_notification(org_id="o" * 300), NotifyConfig(topic_arn=TOPIC))
        assert len(sns.publishes[0]["Subject"]) <= 100

    def test_dedupe_key_is_stable_per_org_node_and_event(self):
        first = _notification()
        second = _notification(summary="a different summary")
        assert first.dedupe_key == second.dedupe_key
        assert _notification(org_id="org-beta").dedupe_key != first.dedupe_key
        assert _notification(event="node_halted").dedupe_key != first.dedupe_key


class TestFailureIsLoud:
    """R-NF3 / R-Q9d: a notification that did not deliver must never look like one that did."""

    def test_unconfigured_target_raises_rather_than_returning_quietly(self, monkeypatch):
        monkeypatch.delenv(TOPIC_ARN_ENV, raising=False)

        with pytest.raises(NotificationsDisabledError) as excinfo:
            notify(_notification(), NotifyConfig(topic_arn=None))

        # The error must name the env var, so the fix is obvious from the message.
        assert TOPIC_ARN_ENV in str(excinfo.value)

    def test_disabled_is_a_subclass_of_notification_error(self):
        # Every caller that handles delivery failure must also handle "never
        # configured" — otherwise an unwired environment escapes the handler.
        assert issubclass(NotificationsDisabledError, NotificationError)

    def test_publish_failure_raises_and_preserves_the_cause(self, monkeypatch):
        underlying = RuntimeError("AuthorizationError: not authorized to perform sns:Publish")
        monkeypatch.setattr(notify_module, "_sns_client", lambda _region: _RecordingSNS(fail_with=underlying))

        with pytest.raises(NotificationError) as excinfo:
            notify(_notification(), NotifyConfig(topic_arn=TOPIC))

        assert excinfo.value.__cause__ is underlying, "the botocore error must survive for diagnosis"
        assert "node-1" in str(excinfo.value), "the error must name the node it failed to report"

    def test_notify_contains_no_bare_return_on_failure(self):
        # Source-level guard against a future fail-soft "helpful" refactor: no
        # `except` clause in this module may swallow without re-raising. This is
        # the shape the story explicitly forbids, so it is asserted structurally
        # rather than left to review.
        tree = ast.parse(Path(inspect.getfile(notify_module)).read_text())
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            raises = any(isinstance(sub, ast.Raise) for sub in ast.walk(handler))
            assert raises, "every except handler in notify.py must re-raise; silent degradation is the bug this story fixes"

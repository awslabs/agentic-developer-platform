"""Early task dispatch stays outside every GitHub-only entrypoint step."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "docs/task-api/contracts/v1/fixtures/valid/envelope-dispatch.json"
)


def envelope() -> dict:
    value = json.loads(CONTRACT.read_text())
    value.pop("$fixture", None)
    return value


class Heartbeat:
    def start(self):
        pass

    def stop(self):
        pass


def test_task_branches_before_legacy_parse_token_checkout_and_staging():
    body = envelope()
    heartbeat = Heartbeat()
    with (
        patch.object(entrypoint, "_receive_one_message", return_value=(json.dumps(body), "owned")),
        patch.object(entrypoint, "authority_enabled", return_value=True),
        patch.object(entrypoint, "parse_envelope") as legacy_parse,
        patch.object(entrypoint, "mint_installation_token") as token,
        patch.object(entrypoint, "_stage_personas_and_skills") as stage,
        patch("lib.task_flow.run_task_assignment", return_value=0) as task_flow,
        patch.object(entrypoint, "BootstrapLogger", return_value=Mock()),
    ):
        assert entrypoint._main(task_heartbeat=heartbeat) == 0
    task_flow.assert_called_once()
    legacy_parse.assert_not_called()
    token.assert_not_called()
    stage.assert_not_called()


def test_task_persona_without_discriminator_never_falls_through_to_claude():
    body = envelope()
    body.pop("kind")
    with (
        patch.object(entrypoint, "_receive_one_message", return_value=(json.dumps(body), "owned")),
        patch.object(entrypoint, "authority_enabled", return_value=True),
        patch.object(entrypoint, "parse_envelope") as legacy_parse,
        patch.object(entrypoint, "worker_command") as legacy_command,
        patch.object(entrypoint, "BootstrapLogger", return_value=Mock()),
    ):
        assert entrypoint._main(task_heartbeat=Heartbeat()) == entrypoint.AGENT_EXIT_RETRYABLE
    legacy_parse.assert_not_called()
    legacy_command.assert_not_called()


def test_task_envelope_from_unauthenticated_direct_queue_is_refused(monkeypatch):
    body = envelope()
    monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/tasks")
    with (
        patch.object(
            entrypoint,
            "_receive_one_message",
            return_value=(json.dumps(body), "receipt"),
        ),
        patch.object(entrypoint, "authority_enabled", return_value=False),
        patch.object(entrypoint, "parse_envelope") as legacy_parse,
        patch("lib.task_flow.run_task_assignment") as task_flow,
        patch.object(entrypoint, "BootstrapLogger", return_value=Mock()),
    ):
        assert entrypoint._main() == entrypoint.AGENT_EXIT_RETRYABLE
    legacy_parse.assert_not_called()
    task_flow.assert_not_called()

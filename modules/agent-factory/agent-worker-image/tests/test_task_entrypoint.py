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


def test_task_id_on_legacy_persona_never_reaches_github_setup():
    body = {
        "message_id": "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40",
        "task_id": "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20",
        "persona": "agent-developer",
        "source_ref": {
            "installation_id": 123,
            "repo": "example/repository",
            "issue": 5797,
        },
    }
    with (
        patch.object(entrypoint, "_receive_one_message", return_value=(json.dumps(body), "owned")),
        patch.object(entrypoint, "parse_envelope") as legacy_parse,
        patch.object(entrypoint, "mint_installation_token") as token,
        patch.object(entrypoint, "run_cmd") as checkout,
        patch.object(entrypoint, "worker_command") as legacy_command,
        patch.object(entrypoint, "BootstrapLogger", return_value=Mock()),
    ):
        assert entrypoint._main(task_heartbeat=Heartbeat()) == entrypoint.AGENT_EXIT_RETRYABLE
    legacy_parse.assert_not_called()
    token.assert_not_called()
    checkout.assert_not_called()
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


def test_task_api_queue_mode_preserves_legacy_authority(monkeypatch):
    monkeypatch.setenv("ADP_TASK_API_WORKER_ENABLED", "true")
    with (
        patch.object(entrypoint, "authority_enabled", return_value=False),
        patch("lib.task_gateway_client.own_task", return_value="legacy-body") as acquire,
        patch("lib.task_gateway_client.acknowledge_task") as ack,
        patch.object(entrypoint.boto3, "client") as aws,
    ):
        assert entrypoint._receive_one_message("", "us-east-1") == ("legacy-body", "run-bound-task")
        entrypoint._delete_message("", "us-east-1", "run-bound-task")
        assert entrypoint.authority_enabled() is False
    acquire.assert_called_once()
    ack.assert_called_once()
    aws.assert_not_called()

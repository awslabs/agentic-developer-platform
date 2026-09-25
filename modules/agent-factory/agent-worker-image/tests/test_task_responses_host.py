"""Bounded Responses IPC preserves canonical Task model operation identity."""

import uuid

import pytest

from tests import test_task_host
from tests.test_task_host import FakeClient
from lib.task_host import TaskHost, TaskHostError, _canonical_digest
from lib.task_protocol import TaskProtocolError, validate_child_frame

assignment_and_bootstrap = test_task_host.assignment_and_bootstrap


def request_frame(task_id):
    return {
        "protocol_version": 1,
        "type": "model.request",
        "request_id": str(uuid.uuid4()),
        "task_id": task_id,
        "turn_id": str(uuid.uuid4()),
        "responses_request": {
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "évidence"}]}],
            "reasoning": {"effort": "medium"},
            "max_output_tokens": 32,
        },
    }


def test_responses_host_preserves_digest_turn_and_result(assignment_and_bootstrap):
    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])
    host = TaskHost(client=client)
    frame = request_frame(assignment.task_id)
    validate_child_frame(frame, assignment.task_id)
    bound = host._binding(assignment, str(uuid.uuid4()))
    prepared = host._model_request(assignment, bound, frame, 64)
    assert prepared["responses_request"] == frame["responses_request"]
    assert prepared["request_digest"] == _canonical_digest(frame["responses_request"])
    assert "max_tokens" not in prepared
    assert host._turn_number == 1
    output = {
        "id": "fixture",
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    client.model_response = {
        "schema_version": "1.0",
        "task_id": assignment.task_id,
        "turn_id": frame["turn_id"],
        "request_digest": prepared["request_digest"],
        "automatic_replay_permitted": False,
        "operation_status": "confirmed",
        "handoff": "confirmed",
        "content": [],
        "stop_reason": "completed",
        "responses_response": output,
    }
    result = host._model(assignment, bound, frame, 64, prepared=prepared)
    assert result["responses_response"] == output
    assert result["turn_id"] == frame["turn_id"]
    client.model_response["operation_status"] = "unknown"
    with pytest.raises(TaskHostError, match="unconfirmed model receipt"):
        host._model(assignment, bound, frame, 64, prepared=prepared)


@pytest.mark.parametrize(
    "override",
    [
        {"model": "override"},
        {"tools": []},
        {"previous_response_id": "foreign"},
        {"input": [{"role": "user", "id": "foreign", "content": "fixture"}]},
        {"max_output_tokens": True},
    ],
)
def test_responses_host_rejects_unsupported_fields(assignment_and_bootstrap, override):
    assignment, _, _ = assignment_and_bootstrap
    frame = request_frame(assignment.task_id)
    frame["responses_request"].update(override)
    with pytest.raises(TaskProtocolError):
        validate_child_frame(frame, assignment.task_id)


def test_responses_output_limit_is_checked_before_turn_consumption(assignment_and_bootstrap):
    assignment, _, bootstrap = assignment_and_bootstrap
    host = TaskHost(client=FakeClient(bootstrap, []))
    frame = request_frame(assignment.task_id)
    with pytest.raises(TaskProtocolError, match="output bound exceeds grant"):
        host._model_request(assignment, {}, frame, 16)
    assert host._turn_number == 0

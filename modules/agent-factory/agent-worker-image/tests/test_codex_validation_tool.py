"""Validation command/path selection remains in the trusted host binding."""

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.codex_validation_tool import TaskValidationTool
from lib.task_run_client import TaskRunClientError


@pytest.fixture
def tool(tmp_path):
    attempt = {
        "run": {
            "task_id": "tsk_" + str(uuid.uuid4()),
            "invocation_id": str(uuid.uuid4()),
            "generation": 1,
        },
        "runtime_attempt_id": str(uuid.uuid4()),
    }
    executor = Mock()
    client = Mock()
    client.tool_authorize.return_value = {
        "schema_version": "1.0",
        "identity": {**attempt["run"], "runtime_attempt_id": attempt["runtime_attempt_id"]},
        "task": {"tool_grants": ["validation.run"]},
    }
    binding = {
        "schema_version": "1.0",
        "attempt": attempt,
        "repository_path": str(tmp_path),
        "checks": [
            {
                "name": "unit",
                "image": "sha256:" + "a" * 64,
                "argv": ["true"],
                "max_output_bytes": 1024,
            }
        ],
    }
    body = {
        "schema_version": "1.0",
        "attempt": attempt,
        "operation_id": str(uuid.uuid4()),
        "operation": "run",
        "payload": {"check": "unit", "commit": "b" * 40},
    }
    return SimpleNamespace(
        handler=TaskValidationTool(client, binding, executor=executor),
        client=client,
        executor=executor,
        body=body,
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"argv": ["arbitrary"]},
        {"repository_path": "/home/ubuntu"},
        {"image": "mutable:latest"},
        {"check": "unadmitted"},
    ],
)
def test_child_cannot_replace_command_workspace_image_or_check(tool, extra):
    tool.body["payload"].update(extra)
    with pytest.raises(TaskRunClientError):
        tool.handler.invoke(tool.body)
    tool.executor.run_repository.assert_not_called()


def test_foreign_attempt_cannot_use_host_workspace(tool):
    tool.body["attempt"] = {**tool.body["attempt"], "runtime_attempt_id": str(uuid.uuid4())}
    with pytest.raises(TaskRunClientError):
        tool.handler.invoke(tool.body)
    tool.executor.run_repository.assert_not_called()


def test_revoked_permission_cannot_start_validation(tool):
    tool.client.tool_authorize.side_effect = TaskRunClientError("revoked")
    with pytest.raises(TaskRunClientError):
        tool.handler.invoke(tool.body)
    tool.executor.run_repository.assert_not_called()


def test_revocation_after_execution_prevents_result_publication(tool):
    tool.executor.run_repository.return_value = {"status": "passed"}
    tool.client.tool_authorize.side_effect = [
        tool.client.tool_authorize.return_value,
        TaskRunClientError("revoked"),
    ]
    with pytest.raises(TaskRunClientError):
        tool.handler.invoke(tool.body)
    tool.executor.run_repository.assert_called_once()
    tool.client.artifact.assert_not_called()

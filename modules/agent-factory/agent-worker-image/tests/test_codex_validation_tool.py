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


def test_cancelled_validation_stops_before_any_execution(tool):
    tool.handler.cancelled.set()
    with pytest.raises(TaskRunClientError, match="stopped"):
        tool.handler.invoke(tool.body)
    tool.executor.run_repository.assert_not_called()
    assert tool.handler.wait_stopped(0)


def test_validation_cancellation_waits_for_executor_and_does_not_publish(tool):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    def executing(**kwargs):
        entered.set()
        assert kwargs["cancelled"].wait(5)
        assert release.wait(5)
        return {"status": "failed", "reason": "cancelled"}

    tool.executor.run_repository.side_effect = executing
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(tool.handler.invoke, tool.body)
        try:
            assert entered.wait(5)
            tool.handler.cancelled.set()
            assert not tool.handler.wait_stopped(0)
        finally:
            release.set()
        with pytest.raises(TaskRunClientError, match="stopped before publication"):
            running.result(timeout=5)
    assert tool.handler.wait_stopped(0)
    tool.client.artifact.assert_not_called()


def test_unknown_executor_cleanup_cannot_finalize_or_release_capacity(tool, monkeypatch):
    from lib import task_run_client
    from lib.codex_validation import ValidationUnavailable

    tool.executor.run_repository.side_effect = ValidationUnavailable("cleanup unconfirmed")
    with pytest.raises(ValidationUnavailable):
        tool.handler.invoke(tool.body)
    assert not tool.handler.wait_stopped(0)
    with pytest.raises(TaskRunClientError, match="Previous validation"):
        tool.handler.invoke(tool.body)
    assert tool.executor.run_repository.call_count == 1
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.example/internal/v1/agent")
    run = task_run_client.TaskRunClient()
    run._validation_tool = tool.handler
    post = Mock(return_value={})
    monkeypatch.setattr(run, "_post", post)
    with pytest.raises(task_run_client.TaskRunClientUnavailable, match="termination"):
        run.finalize({"outcome": "cancelled"})
    post.assert_not_called()
    monkeypatch.setattr(task_run_client, "read_workload_token", lambda: "fixture-token")
    monkeypatch.setattr(task_run_client, "workload_identity", lambda _: {})
    body = {"stop_evidence": {"child_exit_confirmed": True, "workload_terminated": False}}
    run.settlement(body)
    assert post.call_args.args[1]["stop_evidence"]["child_exit_confirmed"] is False
    assert body["stop_evidence"]["child_exit_confirmed"] is True


def test_cancellation_before_container_start_has_confirmed_termination(tool):
    from lib.codex_validation import ValidationCancelled

    tool.executor.run_repository.side_effect = ValidationCancelled("before start")
    with pytest.raises(TaskRunClientError, match="cancelled before execution"):
        tool.handler.invoke(tool.body)
    assert tool.handler.wait_stopped(0)

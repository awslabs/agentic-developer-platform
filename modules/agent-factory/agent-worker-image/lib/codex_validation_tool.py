"""Local Task validation tool; paths and commands come only from host binding.

Provisioned repository workspaces bind approved checks directly through the host.
Explicit host fixtures can also register local:lib.codex_validation_tool.create.
This module does not infer repository authority from task prose.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


from lib.codex_validation import DockerValidationExecutor, ValidationCancelled, ValidationCheck
from lib.task_run_client import TaskRunClientError
from lib.task_tool_artifacts import publish_tool_result


class TaskValidationTool:
    def __init__(self, client, binding, *, executor=None):
        expected = {"schema_version", "attempt", "repository_path", "checks"}
        if (
            not isinstance(binding, dict)
            or set(binding) not in (expected, expected | {"repository_binding"})
            or binding["schema_version"] != "1.0"
        ):
            raise TaskRunClientError("Invalid host validation binding")
        self.binding = json.loads(json.dumps(binding))
        self.client = client
        self.executor = executor or DockerValidationExecutor()
        shared_stop = getattr(client, "validation_stop_event", None)
        self.cancelled = shared_stop if isinstance(shared_stop, threading.Event) else threading.Event()
        self._execution_lock = threading.Lock()
        self._finished = threading.Event()
        self._finished.set()
        self._termination_confirmed = True
        self.repository = Path(binding["repository_path"])
        if not self.repository.is_absolute() or not self.repository.is_dir():
            raise TaskRunClientError("Host validation workspace unavailable")
        if not isinstance(binding["checks"], list) or not 1 <= len(binding["checks"]) <= 32:
            raise TaskRunClientError("Invalid host validation checks")
        self.checks = {}
        for raw in binding["checks"]:
            check = ValidationCheck(**{**raw, "argv": tuple(raw["argv"])})
            check.document()
            if check.name in self.checks or check.max_output_bytes > 16384:
                raise TaskRunClientError("Invalid Task check identity or output bound")
            self.checks[check.name] = check

    def _authorize(self, attempt):
        response = self.client.tool_authorize(
            {"schema_version": "1.0", "attempt": attempt, "tool": "validation.run"}
        )
        identity = response.get("identity", {})
        expected = {**attempt["run"], "runtime_attempt_id": attempt["runtime_attempt_id"]}
        if (
            response.get("schema_version") != "1.0"
            or any(identity.get(key) != value for key, value in expected.items())
            or response.get("task", {}).get("tool_grants", []).count("validation.run") != 1
        ):
            raise TaskRunClientError("Validation tool authority could not be confirmed")
        if "repository_binding" in self.binding and response.get("task", {}).get("repository_binding") != self.binding["repository_binding"]:
            raise TaskRunClientError("Validation repository or check policy changed")

    def wait_stopped(self, timeout):
        if not self._finished.wait(timeout):
            return False
        if not self._termination_confirmed:
            from lib.codex_kubernetes_validation import KubernetesValidationExecutor
            from lib.codex_service_validation import ServiceValidationExecutor
            if isinstance(self.executor, (KubernetesValidationExecutor, ServiceValidationExecutor)):
                try:
                    self._termination_confirmed = self.executor.recover()
                except Exception:
                    return False
        return self._termination_confirmed

    def invoke(self, body):
        if not self._execution_lock.acquire(blocking=False):
            raise TaskRunClientError("Validation is already running")
        self._finished.clear()
        try:
            if self.cancelled.is_set():
                raise TaskRunClientError("Validation has been stopped")
            if not self._termination_confirmed:
                raise TaskRunClientError("Previous validation termination is unconfirmed")
            return self._invoke(body)
        finally:
            self._finished.set()
            self._execution_lock.release()

    def _invoke(self, body):
        if (
            not isinstance(body, dict)
            or set(body) != {"schema_version", "attempt", "operation_id", "operation", "payload"}
            or body["schema_version"] != "1.0"
            or body["attempt"] != self.binding["attempt"]
            or body["operation"] != "run"
        ):
            raise TaskRunClientError("Validation tool attempt differs from host binding")
        payload = body["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"check", "commit"}
            or payload["check"] not in self.checks
        ):
            raise TaskRunClientError("Validation tool requires an admitted named check")
        attempt = body["attempt"]
        self._authorize(attempt)
        if self.cancelled.is_set():
            raise TaskRunClientError("Validation has been stopped")
        self._termination_confirmed = False
        try:
            result = self.executor.run_repository(
                check=self.checks[payload["check"]],
                repository=self.repository,
                expected_head=payload["commit"],
                cancelled=self.cancelled,
            )
        except ValidationCancelled as error:
            self._termination_confirmed = True
            raise TaskRunClientError("Validation cancelled before execution") from error
        self._termination_confirmed = True
        if self.cancelled.is_set():
            raise TaskRunClientError("Validation stopped before publication")
        self._authorize(attempt)
        artifact = publish_tool_result(self.client, attempt=attempt, result=result)
        self._authorize(attempt)
        return {
            "schema_version": "1.0",
            "task_id": attempt["run"]["task_id"],
            "operation_id": body["operation_id"],
            "operation_status": "confirmed",
            "result": result,
            "artifact": artifact,
        }


def create(client):
    path = os.environ.get("ADP_CODEX_VALIDATION_BINDING_FILE")
    if not path:
        raise TaskRunClientError("Host validation binding unavailable")
    with Path(path).open("rb") as stream:
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise TaskRunClientError("Host validation binding exceeds bound")
    return TaskValidationTool(client, json.loads(raw))

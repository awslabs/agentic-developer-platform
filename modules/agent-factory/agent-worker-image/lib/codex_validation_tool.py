"""Local Task validation tool; paths and commands come only from host binding.

Register through ADP_TASK_TOOL_ROUTES as local:lib.codex_validation_tool.create.
The trusted workspace provisioner writes ADP_CODEX_VALIDATION_BINDING_FILE for one
Task attempt. This module does not infer repository authority from task prose.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

import rfc8785

from lib.codex_validation import DockerValidationExecutor, ValidationCheck
from lib.task_run_client import TaskRunClientError


class TaskValidationTool:
    def __init__(self, client, binding, *, executor=None):
        expected = {"schema_version", "attempt", "repository_path", "checks"}
        if (
            not isinstance(binding, dict)
            or set(binding) != expected
            or binding["schema_version"] != "1.0"
        ):
            raise TaskRunClientError("Invalid host validation binding")
        self.binding = json.loads(json.dumps(binding))
        self.client = client
        self.executor = executor or DockerValidationExecutor()
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

    def invoke(self, body):
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
        result = self.executor.run_repository(
            check=self.checks[payload["check"]],
            repository=self.repository,
            expected_head=payload["commit"],
        )
        self._authorize(attempt)
        content = rfc8785.dumps(result)
        if len(content) > 24576:
            raise TaskRunClientError("Validation result exceeds Task receipt bound")
        digest = hashlib.sha256(content).hexdigest()
        task_id = attempt["run"]["task_id"]
        artifact_id = "art_" + str(
            uuid.UUID(
                bytes=hashlib.sha256(f"{task_id}:application/json:{digest}".encode()).digest()[:16],
                version=4,
            )
        )
        artifact = self.client.artifact(
            {
                "schema_version": "1.0",
                "run": attempt["run"],
                "content_type": "application/json",
                "content_sha256": digest,
                "content_base64": base64.b64encode(content).decode(),
            }
        )
        if (
            artifact.get("schema_version") != "1.0"
            or artifact.get("artifact_id") != artifact_id
            or artifact.get("content_sha256") != digest
            or artifact.get("content_type") != "application/json"
            or type(artifact.get("version")) is not int
            or artifact["version"] != 1
            or artifact.get("expires_at", "missing") is not None
        ):
            raise TaskRunClientError("Validation artifact receipt differs from execution")
        self._authorize(attempt)
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            "operation_id": body["operation_id"],
            "operation_status": "confirmed",
            "result": result,
            "artifact": {
                "artifact_id": artifact_id,
                "content_type": "application/json",
                "content_sha256": digest,
                "byte_length": len(content),
            },
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

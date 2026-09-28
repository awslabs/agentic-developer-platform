"""Host-only export and publication of the exact validated workspace tree."""

from __future__ import annotations

import copy

from lib.task_run_client import TaskRunClientError
from lib.task_tool_artifacts import publish_host_json, publish_tool_result

PREREQUISITES = frozenset({"repository.read", "repository.write", "repository.commit", "validation.run", "change.create"})


class TaskPublicationTool:
    def __init__(self, client, *, attempt, workspace, binding):
        self.client, self.workspace = client, workspace
        self.attempt, self.binding = copy.deepcopy(attempt), copy.deepcopy(binding)
        self._authorize()

    def _authorize(self):
        expected = {**self.attempt["run"], "runtime_attempt_id": self.attempt["runtime_attempt_id"]}
        response = self.client.tool_authorize({"schema_version": "1.0", "attempt": self.attempt, "tool": "change.create"})
        task = response.get("task", {})
        if (
            response.get("schema_version") != "1.0"
            or any(response.get("identity", {}).get(k) != v for k, v in expected.items())
            or not PREREQUISITES.issubset(task.get("tool_grants", []))
            or task.get("repository_binding") != self.binding
        ):
            raise TaskRunClientError("Publication workspace authority differs")

    def invoke(self, body):
        if (
            not isinstance(body, dict)
            or set(body) != {"schema_version", "attempt", "operation_id", "operation", "payload"}
            or body["schema_version"] != "1.0"
            or body["attempt"] != self.attempt
            or body["operation"] != "create"
            or not isinstance(body["payload"], dict)
            or set(body["payload"]) != {"commit", "title", "body"}
        ):
            raise TaskRunClientError("Publication invocation differs from bound attempt")
        payload = body["payload"]
        self._authorize()
        manifest = self.workspace.export_changes(expected_head=payload["commit"])
        artifact = publish_host_json(self.client, attempt=self.attempt, result=manifest)
        self._authorize()
        result = self.client.repository_publication({
            "schema_version": "1.0", "attempt": self.attempt,
            "artifact_id": artifact["artifact_id"], "digest": artifact["content_sha256"], **payload,
        })
        if (
            not isinstance(result, dict)
            or result.get("task_id") != self.attempt["run"]["task_id"]
            or result.get("local_head") != manifest["local_head"]
            or result.get("tree") != manifest["tree"]
            or result.get("repository_id") != manifest["repository_id"]
        ):
            raise TaskRunClientError("Publication receipt differs from workspace")
        self._authorize()
        receipt = publish_tool_result(self.client, attempt=self.attempt, result=result)
        return {
            "schema_version": "1.0", "task_id": self.attempt["run"]["task_id"],
            "operation_id": body["operation_id"], "operation_status": "confirmed",
            "result": result, "artifact": receipt,
        }

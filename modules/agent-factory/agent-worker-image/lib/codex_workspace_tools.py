"""Run-bound host workspace tools; no subprocess or provider credentials in SDK."""

from __future__ import annotations

import copy

import rfc8785

from lib.codex_workspace import WorkspaceError
from lib.task_run_client import TaskRunClientError
from lib.task_tool_artifacts import publish_tool_result

WORKSPACE_TOOLS = frozenset(
    {
        "repository.read",
        "repository.list",
        "repository.write",
        "repository.commit",
        "repository.state",
    }
)


class WorkspaceTools:
    def __init__(self, client, *, attempt, workspace, tools):
        self.client, self.attempt, self.workspace = client, copy.deepcopy(attempt), workspace
        self.tools = frozenset(tools) & WORKSPACE_TOOLS

    def _authorize(self, permission):
        result = self.client.tool_authorize(
            {"schema_version": "1.0", "attempt": self.attempt, "tool": permission}
        )
        expected = {**self.attempt["run"], "runtime_attempt_id": self.attempt["runtime_attempt_id"]}
        binding = result.get("task", {}).get("repository_binding", {}).get("binding", {})
        if (
            result.get("schema_version") != "1.0"
            or any(result.get("identity", {}).get(k) != v for k, v in expected.items())
            or permission not in result.get("task", {}).get("tool_grants", [])
            or binding.get("provider") != self.workspace.provider
            or binding.get("repository") != self.workspace.repository
        ):
            raise TaskRunClientError("Workspace authority differs from current Task")

    def invoke(self, permission, body):
        if (
            permission not in self.tools
            or not isinstance(body, dict)
            or set(body) != {"schema_version", "attempt", "operation_id", "operation", "payload"}
            or body["schema_version"] != "1.0"
            or body["attempt"] != self.attempt
            or body["operation"] != permission.split(".")[1]
            or not isinstance(body["payload"], dict)
        ):
            raise TaskRunClientError("Workspace tool invocation is not bound to this attempt")
        self._authorize(permission)
        payload = body["payload"]
        fields = {
            "repository.read": {"path"},
            "repository.list": {"prefix", "offset", "limit"},
            "repository.write": {"path", "content", "expected_sha256"},
            "repository.commit": {"message"},
            "repository.state": set(),
        }
        if set(payload) != fields[permission]:
            raise TaskRunClientError("Workspace tool arguments differ from admitted shape")
        try:
            if permission == "repository.read":
                result = self.workspace.read_file(payload["path"])
            elif permission == "repository.list":
                result = self.workspace.list_files(**payload)
            elif permission == "repository.write":
                result = self.workspace.write_file(**payload)
            elif permission == "repository.commit":
                result = self.workspace.commit(payload["message"])
            else:
                result = self.workspace.state()
            result = {"status": "completed", **result}
        except (WorkspaceError, FileNotFoundError, FileExistsError, UnicodeError) as error:
            # Known refusal: let the model repair its request. Unknown OS/write
            # failures propagate to the host's unknown-outcome journal path.
            result = {"status": "rejected", "reason": str(error) if isinstance(error, WorkspaceError) else type(error).__name__}
        if len(rfc8785.dumps(result)) > 24576:
            result = {"status": "rejected", "reason": "workspace_result_exceeds_bound"}
        self._authorize(permission)
        artifact = publish_tool_result(self.client, attempt=self.attempt, result=result)
        self._authorize(permission)
        return {
            "schema_version": "1.0",
            "task_id": self.attempt["run"]["task_id"],
            "operation_id": body["operation_id"],
            "operation_status": "confirmed",
            "result": result,
            "artifact": artifact,
        }

"""Provision a Task workspace from verified run-bound gateway source chunks."""

from __future__ import annotations

import base64
import hashlib
import re

from lib.codex_workspace import CodexWorkspace
from lib.task_run_client import TaskRunClientError

CHUNK_BYTES = 512 * 1024
MAX_BYTES = 32 * 1024 * 1024


def provision_workspace(client, *, attempt, root):
    expected_identity = {**attempt["run"], "runtime_attempt_id": attempt["runtime_attempt_id"]}

    def authorize():
        result = client.tool_authorize(
            {"schema_version": "1.0", "attempt": attempt, "tool": "repository.read"}
        )
        if result.get("schema_version") != "1.0" or any(
            result.get("identity", {}).get(k) != v for k, v in expected_identity.items()
        ):
            raise TaskRunClientError("Source authority differs from Task attempt")
        task = result.get("task", {})
        if "repository.read" not in task.get("tool_grants", []) or not isinstance(
            task.get("repository_binding"), dict
        ):
            raise TaskRunClientError("Task repository binding unavailable")
        return task["repository_binding"]

    binding = authorize()
    content = bytearray()
    manifest = None
    index = 0
    while True:
        chunk = client.repository_source(
            {"schema_version": "1.0", "attempt": attempt, "index": index}
        )
        fields = {
            "schema_version",
            "repository_binding",
            "commit",
            "archive_sha256",
            "byte_length",
            "index",
            "chunk_count",
            "chunk_sha256",
            "content_base64",
        }
        if (
            not isinstance(chunk, dict)
            or set(chunk) != fields
            or chunk["schema_version"] != "1.0"
            or chunk["repository_binding"] != binding
        ):
            raise TaskRunClientError("Source transfer binding differs")
        size, count = chunk["byte_length"], chunk["chunk_count"]
        if (
            type(size) is not int
            or not 0 < size <= MAX_BYTES
            or type(count) is not int
            or count != (size + CHUNK_BYTES - 1) // CHUNK_BYTES
            or type(chunk["index"]) is not int
            or chunk["index"] != index
            or not isinstance(chunk["commit"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", chunk["commit"])
            or not isinstance(chunk["archive_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", chunk["archive_sha256"])
        ):
            raise TaskRunClientError("Source transfer manifest invalid")
        current = {k: chunk[k] for k in ("commit", "archive_sha256", "byte_length", "chunk_count")}
        if manifest is not None and current != manifest:
            raise TaskRunClientError("Source archive changed during transfer")
        manifest = current
        try:
            decoded = base64.b64decode(chunk["content_base64"], validate=True)
        except (ValueError, TypeError):
            raise TaskRunClientError("Source chunk encoding invalid") from None
        if (
            len(decoded) != min(CHUNK_BYTES, size - len(content))
            or hashlib.sha256(decoded).hexdigest() != chunk["chunk_sha256"]
        ):
            raise TaskRunClientError("Source chunk integrity check failed")
        content.extend(decoded)
        index += 1
        if index == count:
            break
    if hashlib.sha256(content).hexdigest() != manifest["archive_sha256"] or authorize() != binding:
        raise TaskRunClientError("Source archive integrity or authority changed")
    source = binding["binding"]
    workspace = CodexWorkspace(
        root,
        provider=source["provider"],
        repository=source["repository"],
        source_revision=manifest["commit"],
    )
    workspace.materialize(bytes(content), archive_sha256=manifest["archive_sha256"])
    return workspace

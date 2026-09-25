"""Publish bounded canonical host-tool results through the Task artifact API."""

from __future__ import annotations

import base64
import hashlib
import uuid

import rfc8785

from lib.task_run_client import TaskRunClientError


def publish_tool_result(client, *, attempt, result):
    content = rfc8785.dumps(result)
    if len(content) > 24576:
        raise TaskRunClientError("Tool result exceeds Task receipt bound")
    digest = hashlib.sha256(content).hexdigest()
    task_id = attempt["run"]["task_id"]
    artifact_id = "art_" + str(
        uuid.UUID(
            bytes=hashlib.sha256(f"{task_id}:application/json:{digest}".encode()).digest()[:16],
            version=4,
        )
    )
    artifact = client.artifact(
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
        raise TaskRunClientError("Tool artifact receipt differs from execution")
    return {
        "artifact_id": artifact_id,
        "content_type": "application/json",
        "content_sha256": digest,
        "byte_length": len(content),
    }

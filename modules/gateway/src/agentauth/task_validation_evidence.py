"""Read validation evidence from confirmed Task tools and verified artifact bytes.

A report citation or an arbitrary uploaded artifact cannot become a validation
receipt. Completion adapters supply the current authorized repository head.
"""

from __future__ import annotations

import hashlib
import json
import re

import rfc8785

from src.tasks.records import payload_digest, task_ops_partition
from src.tasks.store import TaskStoreError, _deserialize


class TaskValidationEvidence:
    def __init__(self, repository, *, artifacts, authorize):
        self.repository = repository
        self.artifacts = artifacts
        self.authorize = authorize

    def read(self, *, identity, commit):
        if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", commit):
            raise TaskStoreError("Invalid completion commit")
        self.authorize(identity, "validation.run")
        response = self.repository._client.query(
            TableName=self.repository.table_name,
            KeyConditionExpression="event_id = :partition AND begins_with(arrived_at, :prefix)",
            ExpressionAttributeValues={":partition": {"S": task_ops_partition(identity.task_id)}, ":prefix": {"S": "TOOL#"}},
            ConsistentRead=True,
            Limit=129,
        )
        if response.get("LastEvaluatedKey") or len(response.get("Items", [])) > 128:
            raise TaskStoreError("Validation evidence exceeds operation bound")
        evidence = []
        for item in response.get("Items", []):
            row = _deserialize(item)
            if row.get("tool") != "validation.run":
                continue
            if (
                row.get("task_id") != identity.task_id
                or row.get("invocation_id") != identity.invocation_id
                or row.get("generation") != identity.generation
                or row.get("runtime_attempt_id") != identity.runtime_attempt_id
                or row.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}
                or row.get("operation_status") != "confirmed"
                or row.get("automatic_replay_permitted") is not False
                or row.get("is_error") is not False
            ):
                raise TaskStoreError("Validation tool outcome is not confirmed for this attempt")
            binding = {key: row[key] for key in ("turn_id", "call_id", "tool", "arguments_json", "invocation_id", "generation", "runtime_attempt_id")}
            if payload_digest(binding) != row.get("request_digest"):
                raise TaskStoreError("Validation tool binding digest differs")
            try:
                arguments = json.loads(row["arguments_json"])
                content = json.loads(row["content"])
                if not isinstance(arguments, dict) or set(arguments) != {"check", "commit"} or set(content) != {"result", "artifact"}:
                    raise ValueError()
                result, artifact = content["result"], content["artifact"]
                encoded = rfc8785.dumps(result)
                if (
                    result["check"] != arguments["check"]
                    or result["commit"] != arguments["commit"]
                    or result["status"] not in {"passed", "failed"}
                    or any(not re.fullmatch(r"[a-f0-9]{64}", result[key]) for key in ("specificationDigest", "environmentDigest", "archiveSha256"))
                    or len(encoded) != artifact["byte_length"]
                    or hashlib.sha256(encoded).hexdigest() != artifact["content_sha256"]
                ):
                    raise ValueError()
                if result["status"] == "passed" and (
                    type(result["exitCode"]) is not int or result["exitCode"] != 0 or result["reason"] != "completed"
                ):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise TaskStoreError("Validation execution receipt is invalid") from None
            record = self.artifacts.load_artifact(artifact_id=artifact["artifact_id"])
            if (
                not record
                or record.task_id != identity.task_id
                or record.tenant_id != identity.tenant
                or record.owner_principal_id != identity.canonical_principal
                or record.version != 1
                or record.content_type != "application/json"
                or record.content_sha256 != artifact["content_sha256"]
                or self.artifacts.read_artifact(record=record) != encoded
            ):
                raise TaskStoreError("Validation execution artifact is unavailable or differs")
            if result["commit"] == commit:
                evidence.append(
                    {
                        "check": result["check"],
                        "commit": commit,
                        "specificationDigest": result["specificationDigest"],
                        "environmentDigest": result["environmentDigest"],
                        "status": result["status"],
                        "receipt": artifact["artifact_id"],
                    }
                )
        self.authorize(identity, "validation.run")
        return evidence

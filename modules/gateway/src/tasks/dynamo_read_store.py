"""Public read/report and S3 artifact adapter for the canonical T1 repository."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from src.tasks import errors
from src.tasks import store as durable
from src.tasks.events import TaskEvent
from src.tasks.read_store import (
    AppendResult,
    ArtifactRecord,
    EventBudgetExhaustedError,
    ReportConflictError,
    SequenceFencedError,
    TaskRecord,
    TaskStoreError,
)
from src.tasks.records import (
    META_SORT_KEY,
    TaskRecordError,
    component_digest,
    task_artifact_partition,
    task_authority_partition,
    task_policy_sort_key,
    validate_task_id,
)


def artifact_object_key(tenant: str, principal: str, artifact_id: str, version: int) -> str:
    return (
        f"tasks/{component_digest('task-artifact-tenant-v1', tenant)}/"
        f"{component_digest('task-artifact-principal-v1', principal)}/{artifact_id}/{version}"
    )


def _json(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _event(row: dict) -> TaskEvent:
    names = {field.name for field in fields(TaskEvent)}
    body = {key: _json(value) for key, value in row.items() if key in names}
    body.setdefault("runtime_attempt_id", None)
    return TaskEvent(**body)


def _command_receipt(row: dict) -> dict:
    names = {"command_id", "kind", "status", "handoff", "command_sequence", "turn_id", "turn_number", "consumed_at", "authority_expires_at", "reason"}
    receipt = {key: _json(value) for key, value in row.items() if key in names}
    receipt.update(schema_version="1.0", accepted_at=row["created_at"])
    return receipt


class DynamoTaskReadStore:
    """Translate DTOs, never duplicate T1 transaction or identity rules."""

    def __init__(self, repository: durable.TaskStore, *, s3_client, artifact_bucket: str):
        if not artifact_bucket:
            raise ValueError("Task artifact bucket must be configured")
        self.repository = repository
        self.s3 = s3_client
        self.bucket = artifact_bucket

    def require_policy(self, *, tenant: str, principal: str, persona: str) -> None:
        try:
            policy = self.repository._get_authority(task_authority_partition(tenant), task_policy_sort_key(principal))
        except durable.TaskStoreError as exc:
            raise errors.prerequisite_unavailable("Task policy is unavailable.") from exc
        if not policy or policy.get("status") != "active" or persona not in policy.get("personas", []):
            raise errors.disallowed_scope("The current task policy does not allow this operation.")

    def load_task(self, *, task_id: str) -> TaskRecord | None:
        # A malformed public lookup cannot identify a stored Task. Keep it
        # indistinguishable from an absent Task, without masking store outages.
        try:
            validate_task_id(task_id)
        except TaskRecordError:
            return None
        try:
            row = self.repository.read_task(task_id)
            if row is None:
                return None
            commands = self.repository.read_commands(task_id=task_id, limit=100)
            sequence = int(row.get("event_sequence", 0))
            first = self.repository.read_events(task_id=task_id, after_sequence=0, limit=1)
            return TaskRecord(
                task_id=row["task_id"],
                invocation_id=row["invocation_id"],
                tenant_id=row["scope"]["tenant"],
                owner_principal_id=row["scope"]["canonical_principal"],
                persona=row["persona"],
                status=row["state"],
                version=int(row["version"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                deadline_at=row["deadline_at"],
                generation=int(row["generation"]),
                runtime_attempt_id=row.get("runtime_attempt_id"),
                execution_health=row.get("execution_health", "unknown"),
                recovery_required=bool(row.get("recovery_required", False)),
                external_reference=row.get("input_payload", {}).get("external_reference"),
                result=_json(row.get("result")),
                error=_json(row.get("error")),
                input_request=_json(row.get("input_request")),
                command_receipts=tuple(_command_receipt(command) for command in commands),
                queue_ack_status=row.get("queue_ack_status", "unknown"),
                latest_sequence=sequence,
                oldest_sequence=int(first[0]["sequence"]) if first else 0,
                events_allocated=sequence,
            )
        except (durable.TaskStoreError, KeyError, TypeError, ValueError) as exc:
            raise TaskStoreError("Task storage could not supply a valid snapshot") from exc

    def read_events(self, *, task_id: str, after_sequence: int, limit: int) -> list[TaskEvent]:
        try:
            return [_event(row) for row in self.repository.read_events(task_id=task_id, after_sequence=after_sequence, limit=limit)]
        except (durable.TaskStoreError, KeyError, TypeError, ValueError) as exc:
            raise TaskStoreError("Task event storage is unavailable") from exc

    def append_event(
        self, *, task_id, report_id, event_type, data, producer_timestamp, timestamp, expect_generation=None, expect_runtime_attempt_id=None
    ) -> AppendResult:
        # Public/host routes must never reach the unfenced server-only append.
        if report_id is None or expect_generation is None or expect_runtime_attempt_id is None:
            raise SequenceFencedError("A report requires a concrete attempt and stable report ID")
        try:
            row = self.repository.read_task(task_id)
            if row is None:
                raise SequenceFencedError("Task no longer exists")
            result = self.repository.append_report(
                task_id=task_id,
                invocation_id=row["invocation_id"],
                generation=expect_generation,
                runtime_attempt_id=expect_runtime_attempt_id,
                report_id=report_id,
                kind=event_type,
                data=data,
                producer_timestamp=datetime.fromisoformat(producer_timestamp.replace("Z", "+00:00")) if producer_timestamp else None,
            )
            events = self.repository.read_events(task_id=task_id, after_sequence=int(result["sequence"]) - 1, limit=1)
            if not events or int(events[0]["sequence"]) != int(result["sequence"]):
                raise TaskStoreError("Committed report event is unavailable")
            # The response contract is identical on first commit and replay.
            return AppendResult(event=_event(events[0]), replayed=int(result["sequence"]) <= int(row.get("event_sequence", 0)))
        except durable.IdempotencyConflictError as exc:
            raise ReportConflictError("Report ID already has different content") from exc
        except (durable.StaleGenerationError, durable.StaleAttemptError, durable.TaskStateConflictError) as exc:
            raise SequenceFencedError("Task attempt or authority is no longer current") from exc
        except durable.TaskStoreError as exc:
            if str(exc) == "task event budget is exhausted":
                raise EventBudgetExhaustedError(str(exc)) from exc
            raise TaskStoreError("Report storage is unavailable") from exc

    def _artifact(self, row: dict) -> ArtifactRecord:
        expires = row.get("expires_at")
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            version=int(row["version"]),
            tenant_id=row["scope"]["tenant"],
            owner_principal_id=row["scope"]["canonical_principal"],
            content_type=row["content_type"],
            content_sha256=row["content_sha256"],
            content_length=int(row["size_bytes"]),
            created_at=row["created_at"],
            expires_at=datetime.fromtimestamp(int(expires), UTC).isoformat().replace("+00:00", "Z") if expires is not None else "",
            task_id=row.get("task_id"),
            storage_key=row["object_key"],
        )

    def load_artifact(self, *, artifact_id: str) -> ArtifactRecord | None:
        try:
            row = self.repository._get(task_artifact_partition(artifact_id), META_SORT_KEY)
            return self._artifact(row) if row is not None else None
        except (durable.TaskStoreError, KeyError, TypeError, ValueError) as exc:
            raise TaskStoreError("Artifact binding storage is unavailable") from exc

    def put_artifact(self, *, record: ArtifactRecord, content: bytes) -> ArtifactRecord:
        if record.task_id is not None:
            raise TaskStoreError("Input uploads must be unclaimed before task acceptance")
        if len(content) != record.content_length or hashlib.sha256(content).hexdigest() != record.content_sha256:
            raise TaskStoreError("Artifact content does not match its declared binding")
        if not 0 < len(content) <= 262144 or record.content_type not in {"text/plain", "application/json"}:
            raise TaskStoreError("Artifact violates the fixed input bounds")
        key = artifact_object_key(record.tenant_id, record.owner_principal_id, record.artifact_id, record.version)
        try:
            # Bytes first; a failed upload can never produce an accepted dangling reference.
            try:
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentType=record.content_type,
                    ChecksumSHA256=base64.b64encode(hashlib.sha256(content).digest()).decode(),
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                    raise
                existing = self.s3.get_object(Bucket=self.bucket, Key=key)
                stream = existing["Body"]
                try:
                    prior = stream.read(len(content) + 1)
                finally:
                    stream.close()
                if prior != content:
                    raise TaskStoreError("Immutable artifact bytes already differ") from exc
            row = self.repository.create_artifact_binding(
                artifact_id=record.artifact_id,
                tenant=record.tenant_id,
                canonical_principal=record.owner_principal_id,
                version=record.version,
                content_sha256=record.content_sha256,
                content_type=record.content_type,
                size_bytes=len(content),
            )
            return self._artifact(row)
        except (durable.TaskStoreError, BotoCoreError, ClientError) as exc:
            raise TaskStoreError("Artifact publication could not be confirmed") from exc

    def read_artifact(self, *, record: ArtifactRecord) -> bytes:
        key = artifact_object_key(record.tenant_id, record.owner_principal_id, record.artifact_id, record.version)
        if key != record.storage_key or not 0 < record.content_length <= 1048576:
            raise TaskStoreError("Stored artifact binding is invalid")
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            stream = response["Body"]
            try:
                content = stream.read(record.content_length + 1)
            finally:
                stream.close()
        except (BotoCoreError, ClientError, OSError) as exc:
            raise TaskStoreError("Artifact bytes are unavailable") from exc
        if len(content) != record.content_length or hashlib.sha256(content).hexdigest() != record.content_sha256:
            raise TaskStoreError("Stored artifact bytes failed integrity verification")
        return content

    def put_run_artifact(self, *, attempt, content: bytes, content_type: str, digest: str) -> ArtifactRecord:
        """Commit result metadata under the same task and authority fences as reports."""
        import uuid

        from src.tasks.records import base_item, task_partition

        if not 0 < len(content) <= 1048576 or content_type not in {"text/plain", "application/json", "text/html"}:
            raise TaskStoreError("Result artifact violates fixed bounds")
        if hashlib.sha256(content).hexdigest() != digest:
            raise TaskStoreError("Result artifact digest mismatch")
        # Stable immutable identity makes a lost upload response replayable without
        # charging aggregate bytes twice. UUID version bits follow the ID contract.
        identity = hashlib.sha256(f"{attempt.task_id}:{content_type}:{digest}".encode()).digest()[:16]
        artifact_id = "art_" + str(uuid.UUID(bytes=identity, version=4))
        task = self.repository.read_task(attempt.task_id)
        if not task or task.get("runtime_attempt_id") != attempt.runtime_attempt_id or int(task["generation"]) != attempt.generation:
            raise SequenceFencedError("Result attempt is no longer current")
        if task["state"] in {"completed", "failed", "cancelled", "cancel_requested"}:
            raise SequenceFencedError("Task no longer accepts result artifacts")
        scope = task["scope"]
        if scope != {"tenant": attempt.tenant, "canonical_principal": attempt.canonical_principal}:
            raise SequenceFencedError("Result authority binding differs")
        prior = self.load_artifact(artifact_id=artifact_id)
        if prior is not None:
            if prior.task_id != attempt.task_id or prior.content_sha256 != digest:
                raise TaskStoreError("Result artifact identity conflict")
            self.read_artifact(record=prior)
            return prior
        total = int(task.get("result_artifact_bytes", 0))
        if total + len(content) > 1048576:
            raise TaskStoreError("Aggregate result artifact limit exceeded")
        key = artifact_object_key(attempt.tenant, attempt.canonical_principal, artifact_id, 1)
        now = self.repository._clock()
        created = now.isoformat().replace("+00:00", "Z")
        row = base_item(partition=task_artifact_partition(artifact_id), sort_key=META_SORT_KEY, record_type="TASK_ARTIFACT", scope=scope) | {
            "artifact_id": artifact_id,
            "version": 1,
            "task_id": attempt.task_id,
            "content_sha256": digest,
            "content_type": content_type,
            "size_bytes": len(content),
            "object_key": key,
            "binding_state": "bound",
            "artifact_kind": "result",
            "created_at": created,
        }
        try:
            try:
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentType=content_type,
                    ChecksumSHA256=base64.b64encode(hashlib.sha256(content).digest()).decode(),
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                    raise
                self.read_artifact(record=self._artifact(row))
            self.repository._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.repository.table_name,
                            "Item": durable._serialize(row),
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.repository.table_name,
                            "Key": durable._serialize({"event_id": task_partition(attempt.task_id), "arrived_at": META_SORT_KEY}),
                            "UpdateExpression": (
                                "SET result_artifact_bytes = :next, #version = :next_version, "
                                "result_artifact_ids = list_append(if_not_exists(result_artifact_ids, :empty), :artifact_ids)"
                            ),
                            "ConditionExpression": (
                                "#version = :version AND #state = :state AND runtime_attempt_id = :attempt AND "
                                "(attribute_not_exists(result_artifact_bytes) OR result_artifact_bytes = :prior)"
                            ),
                            "ExpressionAttributeNames": {"#version": "version", "#state": "state"},
                            "ExpressionAttributeValues": durable._serialize(
                                {
                                    ":next": total + len(content),
                                    ":next_version": int(task["version"]) + 1,
                                    ":empty": [],
                                    ":artifact_ids": [artifact_id],
                                    ":prior": total,
                                    ":version": int(task["version"]),
                                    ":state": task["state"],
                                    ":attempt": attempt.runtime_attempt_id,
                                }
                            ),
                        }
                    },
                    *self.repository._authority_condition_checks(snapshot=task, runtime_attempt_id=attempt.runtime_attempt_id),
                ]
            )
        except (ClientError, BotoCoreError, durable.TaskStoreError) as exc:
            # An ambiguous result is only confirmed by the durable immutable row.
            committed = self.load_artifact(artifact_id=artifact_id)
            if committed is not None and committed.task_id == attempt.task_id and committed.content_sha256 == digest:
                return committed
            raise TaskStoreError("Result artifact commit could not be confirmed") from exc
        return self._artifact(row)

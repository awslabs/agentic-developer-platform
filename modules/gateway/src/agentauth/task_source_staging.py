"""Attempt-bound source staging in existing Task storage, separate from reports.

A verified provider archive is staged once. Worker transfer reads bounded S3
ranges with per-chunk and whole-archive digests; no provider credential or signed
S3 URL crosses the host boundary.
"""

from __future__ import annotations

import base64
import hashlib
import re

from botocore.exceptions import ClientError

from src.agentauth.github_provider import MAX_ARCHIVE_BYTES
from src.tasks.records import payload_digest, task_ops_partition, task_partition
from src.tasks.store import TaskStoreError, _serialize

CHUNK_BYTES = 512 * 1024


class TaskSourceStaging:
    def __init__(self, repository, *, s3, bucket, authorize):
        if not bucket:
            raise TaskStoreError("Task source storage unavailable")
        self.repository, self.s3, self.bucket, self.authorize = repository, s3, bucket, authorize

    def _binding(self, identity):
        authorized = self.authorize(identity, "repository.read")
        frozen = authorized.get("task", {}).get("repository_binding")
        if frozen is None:
            raise TaskStoreError("Task has no authorized repository binding")
        scope = {
            key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation", "runtime_attempt_id", "tenant", "canonical_principal")
        }
        binding = {"identity": scope, "repository_binding": frozen}
        return binding, "SOURCE#" + payload_digest(binding)

    def read(self, identity):
        binding, key = self._binding(identity)
        row = self.repository._get(task_ops_partition(identity.task_id), key)
        if row is not None and row.get("binding") != binding:
            raise TaskStoreError("Staged source differs from Task authority")
        return row

    def stage(self, identity, archive):
        binding, key = self._binding(identity)
        prior = self.read(identity)
        if prior is not None:
            return prior
        content = archive.content
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= MAX_ARCHIVE_BYTES
            or archive.total_bytes != len(content)
            or archive.offset != 0
            or hashlib.sha256(content).hexdigest() != archive.digest
            or not re.fullmatch(r"[a-f0-9]{40}", archive.commit_sha)
        ):
            raise TaskStoreError("Provider archive does not match its complete receipt")
        object_key = "tasks/sources/" + payload_digest(binding) + "/" + archive.digest
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=content,
                ContentType="application/gzip",
                IfNoneMatch="*",
                ChecksumSHA256=base64.b64encode(hashlib.sha256(content).digest()).decode(),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                raise
        current, _ = self._binding(identity)
        if current != binding:
            raise TaskStoreError("Task source authority changed during staging")
        task = self.repository.read_task(identity.task_id)
        row = {
            "event_id": task_ops_partition(identity.task_id),
            "arrived_at": key,
            "record_type": "TASK_SOURCE",
            "schema_version": "1.0",
            "binding": binding,
            "commit": archive.commit_sha,
            "archive_sha256": archive.digest,
            "byte_length": len(content),
            "chunk_bytes": CHUNK_BYTES,
            "chunk_sha256": [hashlib.sha256(content[offset : offset + CHUNK_BYTES]).hexdigest() for offset in range(0, len(content), CHUNK_BYTES)],
            "object_key": object_key,
        }
        self.repository._client.transact_write_items(
            TransactItems=[
                {"Put": {"TableName": self.repository.table_name, "Item": _serialize(row), "ConditionExpression": "attribute_not_exists(event_id)"}},
                {
                    "ConditionCheck": {
                        "TableName": self.repository.table_name,
                        "Key": _serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
                        "ConditionExpression": "#version = :version AND runtime_attempt_id = :attempt",
                        "ExpressionAttributeNames": {"#version": "version"},
                        "ExpressionAttributeValues": _serialize({":version": int(task["version"]), ":attempt": identity.runtime_attempt_id}),
                    }
                },
                *self.repository._authority_condition_checks(snapshot=task, runtime_attempt_id=identity.runtime_attempt_id),
            ]
        )
        return self.read(identity)

    def chunk(self, identity, *, index):
        row = self.read(identity)
        if row is None or type(index) is not int or not 0 <= index < len(row["chunk_sha256"]):
            raise TaskStoreError("Staged source chunk unavailable")
        offset = index * CHUNK_BYTES
        size = min(CHUNK_BYTES, int(row["byte_length"]) - offset)
        response = self.s3.get_object(Bucket=self.bucket, Key=row["object_key"], Range=f"bytes={offset}-{offset + size - 1}")
        stream = response["Body"]
        try:
            content = stream.read(size + 1)
        finally:
            stream.close()
        if len(content) != size or hashlib.sha256(content).hexdigest() != row["chunk_sha256"][index]:
            raise TaskStoreError("Staged source bytes differ from provider receipt")
        if self.read(identity) != row:
            raise TaskStoreError("Staged source changed during transfer")
        return {
            "schema_version": "1.0",
            "repository_binding": row["binding"]["repository_binding"],
            "commit": row["commit"],
            "archive_sha256": row["archive_sha256"],
            "byte_length": int(row["byte_length"]),
            "index": index,
            "chunk_count": len(row["chunk_sha256"]),
            "chunk_sha256": row["chunk_sha256"][index],
            "content_base64": base64.b64encode(content).decode(),
        }

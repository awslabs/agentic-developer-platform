"""Attempt-fenced, at-most-once Task publication from protected validation evidence."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

import rfc8785
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from src.agentauth.task_repository_policy import TaskValidationCheck
from src.agentauth.task_repository_publication import TaskChangeManifest
from src.tasks.records import payload_digest, task_ops_partition, task_partition
from src.tasks.store import TaskStoreError, _serialize

KEY = "PUBLICATION#v1"
PERMISSIONS = ("repository.read", "repository.write", "validation.run", "change.create")


class TaskPublicationService:
    def __init__(self, repository, *, artifacts, staging, validations, authorize, publisher):
        self.repository, self.artifacts, self.staging = repository, artifacts, staging
        self.validations, self.authorize, self.publisher = validations, authorize, publisher

    def current(self, identity):
        responses = [self.authorize(identity, permission) for permission in PERMISSIONS]
        task = responses[0]["task"]
        if any(response["task"] != task for response in responses[1:]):
            raise TaskStoreError("Task publication authority changed")
        if not task.get("repository_binding", {}).get("binding", {}).get("validation_checks"):
            raise TaskStoreError("Task publication has no required validation checks")
        return task

    def read(self, identity):
        return self.repository._get(task_ops_partition(identity.task_id), KEY)

    def prepare(self, identity, *, artifact_id, digest, commit, title, body):
        task = self.current(identity)
        artifact = self.artifacts.load_artifact(artifact_id=artifact_id)
        if (
            not artifact
            or artifact.task_id != identity.task_id
            or artifact.tenant_id != identity.tenant
            or artifact.owner_principal_id != identity.canonical_principal
            or artifact.version != 1
            or artifact.content_type != "application/json"
            or artifact.content_sha256 != digest
        ):
            raise TaskStoreError("Task publication artifact authority differs")
        raw = self.artifacts.read_artifact(record=artifact)
        if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != digest:
            raise TaskStoreError("Task publication artifact integrity differs")
        try:
            manifest = TaskChangeManifest.model_validate(json.loads(raw))
            manifest.file_changes()
        except (ValueError, TypeError):
            raise TaskStoreError("Task publication manifest invalid") from None
        binding = task["repository_binding"]["binding"]
        if manifest.local_head != commit or any(getattr(manifest, name) != binding[name] for name in ("provider", "repository", "repository_id")):
            raise TaskStoreError("Task publication repository or local commit differs")
        source = self.staging.read(identity)
        if not source or source["commit"] != manifest.source_revision:
            raise TaskStoreError("Task publication source differs from staged source")
        evidence = self.validations.read(identity=identity, commit=commit, tree=manifest.tree)
        for raw_check in binding["validation_checks"]:
            check = TaskValidationCheck.model_validate(raw_check).model_dump()
            expected = hashlib.sha256(rfc8785.dumps(check)).hexdigest()
            matching = [row for row in evidence if row["check"] == check["name"] and row["specificationDigest"] == expected]
            if not matching or any(row["status"] != "passed" for row in matching):
                raise TaskStoreError("Task publication lacks passed configured checks")
        if not isinstance(title, str) or not title.strip() or len(title) > 255 or not isinstance(body, str) or len(body.encode()) > 16384:
            raise TaskStoreError("Task publication description exceeds bound")
        binding = {
            "identity": {
                key: getattr(identity, key)
                for key in ("task_id", "invocation_id", "generation", "runtime_attempt_id", "tenant", "canonical_principal")
            },
            "repository_binding": task["repository_binding"],
            "artifact_id": artifact_id,
            "artifact_digest": digest,
            "commit": commit,
            "title": title,
            "body": body,
        }
        return manifest.model_dump(), binding, payload_digest(binding)

    def fences(self, identity, task):
        snapshot = self.repository.read_task(identity.task_id)
        if int(snapshot["version"]) != int(task["version"]):
            raise TaskStoreError("Task changed before publication transaction")
        return [
            {
                "ConditionCheck": {
                    "TableName": self.repository.table_name,
                    "Key": _serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
                    "ConditionExpression": "#version = :version AND runtime_attempt_id = :attempt",
                    "ExpressionAttributeNames": {"#version": "version"},
                    "ExpressionAttributeValues": _serialize({":version": int(task["version"]), ":attempt": identity.runtime_attempt_id}),
                }
            },
            *self.repository._authority_condition_checks(snapshot=snapshot, runtime_attempt_id=identity.runtime_attempt_id),
        ]

    def claim(self, identity, binding, digest):
        task = self.current(identity)
        prior = self.read(identity)
        if prior:
            if prior.get("request_digest") != digest or prior.get("binding") != binding:
                raise TaskStoreError("Task already has a different publication intent")
            return prior, False
        row = {
            "event_id": task_ops_partition(identity.task_id),
            "arrived_at": KEY,
            "record_type": "TASK_PUBLICATION",
            "schema_version": "1.0",
            "binding": binding,
            "request_digest": digest,
            "operation_status": "pending",
            "automatic_replay_permitted": False,
            "owner_token": str(uuid.uuid4()),
        }
        try:
            self.repository._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.repository.table_name,
                            "Item": _serialize(row),
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                    *self.fences(identity, task),
                ]
            )
        except ClientError:
            prior = self.read(identity)
            if prior and prior.get("request_digest") == digest and prior.get("binding") == binding:
                return prior, False
            raise TaskStoreError("Task publication claim could not be confirmed") from None
        return row, True

    def settle(self, identity, row, result):
        task = self.current(identity)
        current = self.read(identity)
        if current != row:
            raise TaskStoreError("Task publication ownership changed")
        updated = {**row, "operation_status": "confirmed", "result": result}
        self.repository._client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self.repository.table_name,
                        "Item": _serialize(updated),
                        "ConditionExpression": "owner_token = :owner AND request_digest = :digest AND operation_status = :pending",
                        "ExpressionAttributeValues": _serialize(
                            {":owner": row["owner_token"], ":digest": row["request_digest"], ":pending": "pending"}
                        ),
                    }
                },
                *self.fences(identity, task),
            ]
        )
        return updated

    async def execute(self, identity, *, artifact_id, digest, commit, title, body):
        manifest, binding, fingerprint = await run_in_threadpool(
            self.prepare, identity, artifact_id=artifact_id, digest=digest, commit=commit, title=title, body=body
        )
        row, created = await run_in_threadpool(self.claim, identity, binding, fingerprint)
        if not created:
            # Unknown/pending effects require separate read-only reconciliation,
            # never another publish call. Confirmed receipts are immutable history;
            # completion must also read the current provider head.
            if row["operation_status"] != "confirmed":
                raise TaskStoreError("Task publication outcome unconfirmed; do not replay")
            return {**row["result"], "receipt_id": fingerprint}

        async def reauthorize():
            task = await run_in_threadpool(self.current, identity)
            if task["repository_binding"] != binding["repository_binding"]:
                raise TaskStoreError("Task publication repository changed")

        result = await self.publisher(
            tenant=identity.tenant,
            task_id=identity.task_id,
            frozen=binding["repository_binding"],
            manifest=manifest,
            title=title,
            body=body,
            reauthorize=reauthorize,
        )
        # Publication adapter is trusted, but malformed/misbound receipts must not
        # become durable completion evidence.
        expected = {
            "schema_version": "1.0",
            "task_id": identity.task_id,
            "provider": manifest["provider"],
            "repository_id": manifest["repository_id"],
            "source_revision": manifest["source_revision"],
            "local_head": commit,
            "tree": manifest["tree"],
            "state": "open",
            "draft": False,
        }
        if (
            not isinstance(result, dict)
            or any(result.get(k) != v for k, v in expected.items())
            or not re.fullmatch(r"[a-f0-9]{40}", str(result.get("provider_head", "")))
            or result.get("branch") != "adp/task-" + identity.task_id.removeprefix("tsk_")
            or type(result.get("number")) is not int
            or result["number"] < 1
            or result.get("url") != f"https://github.com/{manifest['repository']}/pull/{result['number']}"
        ):
            raise TaskStoreError("Task publication result identity differs; outcome unknown")
        await run_in_threadpool(self.settle, identity, row, result)
        return {**result, "receipt_id": fingerprint}

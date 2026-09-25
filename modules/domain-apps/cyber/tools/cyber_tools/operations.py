"""Run-authorized cyber operations with durable non-replay and Task evidence.

Service principals own a reserved sample namespace; no GitHub/human identity is
invented. The SDK receives findings and Task artifact IDs, never cloud tokens,
CAPE credentials or S3 download capabilities.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from adp_tools.contracts import TaskAttemptBody
from adp_tools.storage import (
    base_item,
    payload_digest,
    task_ops_partition,
    task_partition,
    serialize as _serialize,
)
from cyber_tools.backends import CyberBackends

OPERATIONS = {
    "triage",
    "static",
    "dynamic",
    "result",
    "url_analysis",
    "enrich",
    "cancel_jobs",
}
JOB_OPERATIONS = {"triage", "static", "dynamic"}
TERMINAL = {"completed", "failed", "cancelled", "not_started"}
MAX_RESULT = 24000


class CyberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    attempt: TaskAttemptBody
    operation_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    operation: str
    payload: dict


def validate_payload(operation, payload):
    fields = {
        "triage": {"sample_s3_uri", "focus", "yara_rules"},
        "static": {"sample_s3_uri", "focus", "yara_rules"},
        "dynamic": {"sample_s3_uri", "focus", "yara_rules"},
        "result": {"job_id"},
        "url_analysis": {"url"},
        "enrich": {"sha256"},
        "cancel_jobs": set(),
    }
    if (
        operation not in fields
        or set(payload) - fields[operation]
        or len(json.dumps(payload).encode()) > 8192
    ):
        raise HTTPException(422, "Invalid cyber operation")
    required = (
        "sample_s3_uri"
        if operation in JOB_OPERATIONS
        else {"result": "job_id", "url_analysis": "url", "enrich": "sha256"}.get(
            operation
        )
    )
    if required and (
        not isinstance(payload.get(required), str)
        or not 0 < len(payload[required]) <= 2048
    ):
        raise HTTPException(422, "Invalid cyber operation input")
    for name in ("focus", "yara_rules"):
        values = payload.get(name, [])
        if (
            not isinstance(values, list)
            or len(values) > 20
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", v)
                for v in values
            )
        ):
            raise HTTPException(422, "Invalid cyber analysis options")
    if operation == "enrich" and not re.fullmatch(r"[a-f0-9]{64}", payload["sha256"]):
        raise HTTPException(422, "Invalid sample hash")


def owned_sample(uri, *, bucket, tenant, principal):
    parsed = urlsplit(uri)
    parts = parsed.path.removeprefix("/").split("/")
    # Reserved service-principal namespace, distinct from human Cognito paths.
    prefix = ["o", tenant, "t", "task-service", "u", "sp-" + principal, "s"]
    if (
        not bucket
        or parsed.scheme != "s3"
        or parsed.netloc != bucket
        or parsed.query
        or parsed.fragment
        or len(parts) != 11
        or parts[:7] != prefix
        or parts[9] != "in"
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", part) or part in {".", ".."}
            for part in parts
        )
    ):
        raise HTTPException(403, "Cyber sample ownership refused")
    return "/".join(parts)


def checked_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise HTTPException(422, "Invalid analysis URL")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(
        (".localhost", ".internal", ".local")
    ):
        raise HTTPException(403, "Analysis destination refused")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return  # The guarded browser backend must also validate DNS and redirects.
    if not address.is_global:
        raise HTTPException(403, "Analysis destination refused")


class CyberOperations:
    def __init__(
        self, repo, evidence, backend=None, env=None, revalidate=None, remaining_ms=None
    ):
        self.repo, self.evidence = repo, evidence
        self.revalidate = revalidate
        self.remaining_ms = remaining_ms or (lambda: 30000)
        self.env = os.environ if env is None else env
        self.backend = backend or CyberBackends(env=self.env)

    def task(self, identity, *, cleanup=False):
        row = self.repo.read_task(identity.task_id)
        if (
            not row
            or row.get("persona") != "agent-task-cyber"
            or row.get("scope")
            != {
                "tenant": identity.tenant,
                "canonical_principal": identity.canonical_principal,
            }
            or row.get("runtime_attempt_id") != identity.runtime_attempt_id
            or int(row.get("generation", 0)) != identity.generation
        ):
            raise HTTPException(403, "Cyber Task authority refused")
        if not cleanup and (
            row["state"] in {"completed", "failed", "cancelled", "cancel_requested"}
            or row.get("cyber_closed_attempt") == identity.runtime_attempt_id
            or time.time() >= self.deadline(row)
        ):
            raise HTTPException(409, "Task no longer admits cyber operations")
        return row

    @staticmethod
    def deadline(task):
        from datetime import datetime

        return datetime.fromisoformat(
            task["deadline_at"].replace("Z", "+00:00")
        ).timestamp()

    def rows(self, identity, prefix):
        from boto3.dynamodb.types import TypeDeserializer

        decode = TypeDeserializer()
        result, start = [], None
        while True:
            args = dict(
                TableName=self.repo.table_name,
                KeyConditionExpression="event_id = :pk AND begins_with(arrived_at, :prefix)",
                ExpressionAttributeValues={
                    ":pk": {"S": task_ops_partition(identity.task_id)},
                    ":prefix": {"S": prefix},
                },
                ConsistentRead=True,
                Limit=129,
            )
            if start:
                args["ExclusiveStartKey"] = start
            page = self.repo._client.query(**args)
            result.extend(
                {k: decode.deserialize(v) for k, v in row.items()}
                for row in page.get("Items", [])
            )
            if len(result) > 128:
                raise HTTPException(409, "Cyber operation bound exceeded")
            start = page.get("LastEvaluatedKey")
            if not start:
                return result

    def sample(self, identity, task, payload):
        uri = payload["sample_s3_uri"]
        inputs = task["input_payload"].get("inputs", {})
        if uri != inputs.get("sample_s3_uri"):
            raise HTTPException(403, "Sample was not supplied to this Task")
        bucket = self.env.get("CYBER_SAMPLE_BUCKET", "")
        key = owned_sample(
            uri,
            bucket=bucket,
            tenant=identity.tenant,
            principal=identity.canonical_principal,
        )
        s3 = self.backend._client("s3")
        pin_key = "CYBER_SAMPLE#" + hashlib.sha256(uri.encode()).hexdigest()
        prior = self.repo._get(task_ops_partition(identity.task_id), pin_key)
        if prior:
            pinned = {**prior["sample"], "size": int(prior["sample"]["size"])}
            if (
                prior["scope"] != task["scope"]
                or pinned["bucket"] != bucket
                or pinned["key"] != key
            ):
                raise HTTPException(403, "Sample binding changed")
            if inputs.get("sha256") and inputs["sha256"] != pinned["sha256"]:
                raise HTTPException(409, "Sample digest differs")
            return pinned
        head = s3.head_object(Bucket=bucket, Key=key)
        version, size = head.get("VersionId"), head.get("ContentLength", 0)
        if not version or version == "null" or not 0 < size <= 64 * 1024 * 1024:
            raise HTTPException(409, "Sample must be bounded and versioned")
        response = s3.get_object(Bucket=bucket, Key=key, VersionId=version)
        digest, count = hashlib.sha256(), 0
        with response["Body"] as stream:
            while chunk := stream.read(65536):
                count += len(chunk)
                if count > size or time.time() >= self.deadline(task):
                    raise HTTPException(409, "Sample read exceeded bounds")
                digest.update(chunk)
        if count != size or (
            inputs.get("sha256") and inputs["sha256"] != digest.hexdigest()
        ):
            raise HTTPException(409, "Sample content does not match request")
        pinned = dict(
            bucket=bucket,
            key=key,
            version=version,
            sha256=digest.hexdigest(),
            size=size,
            org_id=identity.tenant,
            team_id="task-service",
            user_id="sp-" + identity.canonical_principal,
            sample_s3_uri=uri,
        )

        row = base_item(
            partition=task_ops_partition(identity.task_id),
            sort_key=pin_key,
            record_type="TASK_OPS",
            scope=task["scope"],
        )
        row["sample"] = pinned
        try:
            self.repo._client.put_item(
                TableName=self.repo.table_name,
                Item=_serialize(row),
                ConditionExpression="attribute_not_exists(event_id)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return self.sample(identity, task, payload)
        return pinned

    def job(self, identity, job_id):
        if not re.fullmatch(r"cyber-[a-f0-9]{32}-[a-f0-9]{32}", job_id):
            raise HTTPException(404, "No such cyber job")
        row = self.repo._get(
            task_ops_partition(identity.task_id), "CYBER_JOB#" + job_id
        )
        if (
            not row
            or row.get("runtime_attempt_id") != identity.runtime_attempt_id
            or row.get("scope")
            != {
                "tenant": identity.tenant,
                "canonical_principal": identity.canonical_principal,
            }
        ):
            raise HTTPException(404, "No such cyber job")
        return row

    def put_job(self, identity, job_id, job):
        row = base_item(
            partition=task_ops_partition(identity.task_id),
            sort_key="CYBER_JOB#" + job_id,
            record_type="TASK_OPS",
            scope={
                "tenant": identity.tenant,
                "canonical_principal": identity.canonical_principal,
            },
        )
        row.update(runtime_attempt_id=identity.runtime_attempt_id, job=job)
        self.repo._client.put_item(TableName=self.repo.table_name, Item=_serialize(row))

    def cleanup(self, identity, operation_id):
        self.task(identity, cleanup=True)
        self.repo._client.update_item(
            TableName=self.repo.table_name,
            Key=_serialize(
                {"event_id": task_partition(identity.task_id), "arrived_at": "META"}
            ),
            UpdateExpression="SET cyber_closed_attempt = :attempt",
            ConditionExpression="runtime_attempt_id = :attempt AND generation = :generation",
            ExpressionAttributeValues=_serialize(
                {
                    ":attempt": identity.runtime_attempt_id,
                    ":generation": identity.generation,
                }
            ),
        )
        pending, settled = [], set()
        jobs = sorted(
            self.rows(identity, "CYBER_JOB#"),
            key=lambda row: row.get("cleanup_checked_at", 0),
        )
        for row in jobs:
            if row.get("scope") != {
                "tenant": identity.tenant,
                "canonical_principal": identity.canonical_principal,
            }:
                raise HTTPException(403, "Cyber job scope differs")
            job = row["job"]
            if job.get("status") in TERMINAL:
                settled.add(job["job_id"])
                continue
            if self.remaining_ms() < 12000:
                pending.append(job["job_id"])
                continue
            # Rotate observations across invocations, including on timeout, so a
            # slow unresolved job cannot starve the rest of a bounded job set.
            self.repo._client.update_item(
                TableName=self.repo.table_name,
                Key=_serialize(
                    {"event_id": row["event_id"], "arrived_at": row["arrived_at"]}
                ),
                UpdateExpression="SET cleanup_checked_at = :now",
                ExpressionAttributeValues=_serialize({":now": time.time()}),
            )
            result = self.backend.result(job)
            if result.get("status") in TERMINAL or result.get("execution_status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                self._settle_job(
                    row, result.get("execution_status") or result["status"]
                )
                settled.add(job["job_id"])
                continue
            cancelled = self.backend.cancel(job)
            if cancelled.get("status") in {"confirmed", "cancelled", "completed"}:
                self._settle_job(row, "cancelled")
                settled.add(job["job_id"])
            else:
                pending.append(job["job_id"])
        for row in self.rows(identity, "CYBER_OP#"):
            receipt = row.get("receipt")
            if receipt and receipt.get("operation_status") != "unknown":
                continue
            if row.get("operation") in {"result", "enrich"}:
                continue  # Read-only operations create no external execution.
            expected_job = (
                "cyber-"
                + hashlib.sha256(identity.task_id.encode()).hexdigest()[:32]
                + "-"
                + row["request_digest"][:32]
            )
            if row.get("operation") in JOB_OPERATIONS and expected_job in settled:
                continue
            pending.append(row["arrived_at"])

        return {
            "schema_version": "1.0",
            "task_id": identity.task_id,
            "operation_id": operation_id,
            "operation_status": "pending" if pending else "confirmed",
            "result": {
                "status": "pending" if pending else "confirmed",
                "pending_jobs": pending,
            },
        }

    def _settle_job(self, row, status):
        self.repo._client.update_item(
            TableName=self.repo.table_name,
            Key=_serialize(
                {"event_id": row["event_id"], "arrived_at": row["arrived_at"]}
            ),
            UpdateExpression="SET job.#status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=_serialize({":status": status}),
        )

    def execute(self, identity, operation_id, operation, payload):
        validate_payload(operation, payload)
        if operation == "cancel_jobs":
            return self.cleanup(identity, operation_id)
        task = self.task(identity)
        inputs = task["input_payload"].get("inputs", {})
        if operation == "url_analysis":
            if payload["url"] not in [inputs.get("url"), *inputs.get("urls", [])]:
                raise HTTPException(403, "URL was not supplied to this Task")
            checked_url(payload["url"])
        if operation == "enrich":
            hashes = {inputs.get("sha256")}
            for row in self.rows(identity, "CYBER_JOB#"):
                hashes.add(row.get("job", {}).get("sample", {}).get("sha256"))
            if payload["sha256"] not in hashes:
                raise HTTPException(403, "Hash is not Task evidence")
        if operation == "result":
            job = self.job(identity, payload["job_id"])["job"]
        digest = payload_digest(
            {
                "operation": operation,
                "payload": payload,
                "attempt": identity.runtime_attempt_id,
            }
        )
        # Side effects are also deduplicated across new SDK tool-call IDs.
        suffix = operation_id if operation == "result" else digest
        key = "CYBER_OP#" + suffix
        id_key = "CYBER_ID#" + operation_id
        existing_id = self.repo._get(task_ops_partition(identity.task_id), id_key)
        if existing_id and existing_id.get("request_digest") != digest:
            raise HTTPException(409, "Cyber operation ID reused with different payload")
        prior = self.repo._get(task_ops_partition(identity.task_id), key)
        if prior:
            if not existing_id:
                alias = base_item(
                    partition=task_ops_partition(identity.task_id),
                    sort_key=id_key,
                    record_type="TASK_OPS",
                    scope=task["scope"],
                )
                alias.update(
                    request_digest=digest,
                    runtime_attempt_id=identity.runtime_attempt_id,
                )
                try:
                    self.repo._client.transact_write_items(
                        TransactItems=[
                            {
                                "Update": {
                                    "TableName": self.repo.table_name,
                                    "Key": _serialize(
                                        {
                                            "event_id": task_partition(
                                                identity.task_id
                                            ),
                                            "arrived_at": "META",
                                        }
                                    ),
                                    "UpdateExpression": "SET cyber_operation_count = if_not_exists(cyber_operation_count, :zero) + :one",
                                    "ConditionExpression": (
                                        "runtime_attempt_id = :attempt AND generation = :generation AND #state = :state "
                                        "AND (attribute_not_exists(cyber_closed_attempt) OR cyber_closed_attempt <> :attempt) "
                                        "AND (attribute_not_exists(cyber_operation_count) OR cyber_operation_count < :limit)"
                                    ),
                                    "ExpressionAttributeNames": {"#state": "state"},
                                    "ExpressionAttributeValues": _serialize(
                                        {
                                            ":zero": 0,
                                            ":one": 1,
                                            ":limit": 128,
                                            ":attempt": identity.runtime_attempt_id,
                                            ":generation": identity.generation,
                                            ":state": task["state"],
                                        }
                                    ),
                                }
                            },
                            {
                                "Put": {
                                    "TableName": self.repo.table_name,
                                    "Item": _serialize(alias),
                                    "ConditionExpression": "attribute_not_exists(event_id)",
                                }
                            },
                        ]
                    )
                except ClientError:
                    raise HTTPException(
                        409, "Cyber request bound or alias conflict"
                    ) from None
            if prior["request_digest"] != digest:
                raise HTTPException(409, "Cyber operation identity conflict")
            if prior.get("receipt"):
                return {**prior["receipt"], "operation_id": operation_id}
            return {
                "schema_version": "1.0",
                "task_id": identity.task_id,
                "operation_id": operation_id,
                "operation_status": "unknown",
                "result": {"status": "unknown"},
                "error_code": "cyber_outcome_unknown",
            }
        sample = (
            self.sample(identity, task, payload)
            if operation in JOB_OPERATIONS
            else None
        )
        self.task(identity)
        row = base_item(
            partition=task_ops_partition(identity.task_id),
            sort_key=key,
            record_type="TASK_OPS",
            scope=task["scope"],
        )
        row.update(
            request_digest=digest,
            runtime_attempt_id=identity.runtime_attempt_id,
            operation=operation,
        )
        binding = base_item(
            partition=task_ops_partition(identity.task_id),
            sort_key=id_key,
            record_type="TASK_OPS",
            scope=task["scope"],
        )
        binding.update(
            request_digest=digest, runtime_attempt_id=identity.runtime_attempt_id
        )
        try:
            self.repo._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.repo.table_name,
                            "Key": _serialize(
                                {
                                    "event_id": task_partition(identity.task_id),
                                    "arrived_at": "META",
                                }
                            ),
                            "UpdateExpression": "SET cyber_operation_count = if_not_exists(cyber_operation_count, :zero) + :one",
                            "ConditionExpression": (
                                "runtime_attempt_id = :attempt AND generation = :generation AND #state = :state "
                                "AND (attribute_not_exists(cyber_closed_attempt) OR cyber_closed_attempt <> :attempt) "
                                "AND (attribute_not_exists(cyber_operation_count) OR cyber_operation_count < :limit)"
                            ),
                            "ExpressionAttributeNames": {"#state": "state"},
                            "ExpressionAttributeValues": _serialize(
                                {
                                    ":zero": 0,
                                    ":one": 1,
                                    ":limit": 128,
                                    ":attempt": identity.runtime_attempt_id,
                                    ":generation": identity.generation,
                                    ":state": task["state"],
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.repo.table_name,
                            "Item": _serialize(row),
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.repo.table_name,
                            "Item": _serialize(binding),
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                ]
            )
        except ClientError:
            raise HTTPException(409, "Cyber operation claim conflict") from None
        self.task(identity)
        if self.revalidate is not None and self.revalidate() != identity:
            raise HTTPException(403, "Cyber authority changed")
        if operation in JOB_OPERATIONS:
            job_id = (
                "cyber-"
                + hashlib.sha256(identity.task_id.encode()).hexdigest()[:32]
                + "-"
                + digest[:32]
            )
            job = {
                "job_id": job_id,
                "kind": operation,
                "sample": sample,
                "deadline_epoch": self.deadline(task),
                "status": "unknown",
            }
            self.put_job(identity, job_id, job)
            self.task(identity)
            if self.revalidate is not None and self.revalidate() != identity:
                raise HTTPException(403, "Cyber authority changed")
            result = self.backend.submit(
                operation,
                job_id,
                sample,
                {k: payload[k] for k in ("focus", "yara_rules") if k in payload},
                self.deadline(task),
            )
            internal = result.pop("_job", None)
            if internal:
                if result.get("status") == "partial":
                    internal["status"] = "not_started"
                self.put_job(identity, job_id, internal)
            elif result.get("status") == "partial":
                self.put_job(identity, job_id, {**job, "status": "not_started"})
        elif operation == "result":
            result = self.backend.result(job)
        elif operation == "url_analysis":
            result = self.backend.url_analysis(payload["url"], self.deadline(task))
        else:
            result = self.backend.enrich(payload["sha256"], self.deadline(task))
        self.task(identity)
        content = json.dumps(
            {"operation": operation, "result": result},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        if len(content) > MAX_RESULT:
            raise HTTPException(413, "Cyber findings exceed Task evidence limit")
        artifact = self.evidence.put_run_artifact(
            attempt=identity,
            content=content,
            content_type="application/json",
            digest=hashlib.sha256(content).hexdigest(),
        )
        receipt = {
            "schema_version": "1.0",
            "task_id": identity.task_id,
            "operation_id": operation_id,
            "operation_status": "unknown"
            if result.get("status") == "unknown"
            else "confirmed",
            "result": result,
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "content_type": artifact.content_type,
                "content_sha256": artifact.content_sha256,
                "byte_length": len(content),
            },
        }
        self.repo._client.update_item(
            TableName=self.repo.table_name,
            Key=_serialize(
                {"event_id": task_ops_partition(identity.task_id), "arrived_at": key}
            ),
            UpdateExpression="SET receipt = :receipt",
            ConditionExpression="request_digest = :digest AND runtime_attempt_id = :attempt",
            ExpressionAttributeValues=_serialize(
                {
                    ":receipt": receipt,
                    ":digest": digest,
                    ":attempt": identity.runtime_attempt_id,
                }
            ),
        )
        return receipt

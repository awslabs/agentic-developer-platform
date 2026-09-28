"""Task-owned AgentCore sessions; the API never returns provider session IDs."""

import base64
import hashlib
import json
import math
import re
import shlex
import time
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from adp_tools.contracts import TaskAttemptBody, UUID4
from adp_tools.storage import base_item, payload_digest, serialize, task_ops_partition, task_partition


OPERATIONS = {"start", "execute", "result", "file", "close", "cancel_jobs"}
HANDLE = r"^[a-f0-9]{64}$"
PATH = re.compile(r"^/tmp/(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}$")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    attempt: TaskAttemptBody
    operation_id: str = Field(pattern=UUID4)
    operation: str
    payload: dict

    @model_validator(mode="after")
    def validate_operation(self):
        fields = {
            "start": set(),
            "cancel_jobs": set(),
            "execute": {"session_id", "code", "language"},
            "result": {"session_id", "execution_id"},
            "file": {"session_id", "path"},
            "close": {"session_id"},
        }
        if self.operation not in fields or set(self.payload) != fields[self.operation]:
            raise ValueError("Invalid Code Interpreter operation or fields")
        if self.operation not in {"start", "cancel_jobs"} and (not isinstance(self.payload["session_id"], str) or not re.fullmatch(HANDLE, self.payload["session_id"])):
            raise ValueError("Invalid session handle")
        if self.operation == "execute" and (
            self.payload["language"] != "python"
            or not isinstance(self.payload["code"], str)
            or not 0 < len(self.payload["code"].encode()) <= 8192
        ):
            raise ValueError("Only bounded Python is supported")
        if self.operation == "result" and (not isinstance(self.payload["execution_id"], str) or not re.fullmatch(UUID4, self.payload["execution_id"])):
            raise ValueError("Invalid execution identity")
        if self.operation == "file" and (not isinstance(self.payload["path"], str) or not PATH.fullmatch(self.payload["path"])):
            raise ValueError("Invalid file path")
        return self


class CodeInterpreter:
    def __init__(self, repo, authority, identifier, *, provider=None, revalidate=None):
        self.repo, self.authority, self.identifier = repo, authority, identifier
        self.provider = provider or boto3.client(
            "bedrock-agentcore", config=Config(connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 1})
        )
        self.revalidate = revalidate

    def task(self, identity, *, cleanup=False):
        task = self.repo.read_task(identity.task_id)
        if (task.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}
            or task.get("runtime_attempt_id") != identity.runtime_attempt_id
            or task.get("generation") != identity.generation):
            raise HTTPException(403, "Task ownership refused")
        local = self.repo._get(task_partition(identity.task_id), "META")
        if not cleanup and local.get("code_closed_attempt") == identity.runtime_attempt_id:
            raise HTTPException(409, "Code Interpreter cleanup has started")
        if not cleanup and (task.get("state") != "running" or time.time() >= datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00")).timestamp()):
            raise HTTPException(409, "Task no longer admits execution")
        if self.revalidate is not None and self.revalidate() != identity:
            raise HTTPException(403, "Task authority changed")
        return task

    def row(self, identity, key):
        return self.repo._get(task_ops_partition(identity.task_id), "CODE_" + key)

    def owned(self, identity, handle):
        row = self.row(identity, "SESSION#" + handle)
        if not row or row.get("runtime_attempt_id") != identity.runtime_attempt_id or row.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}:
            raise HTTPException(403, "Session ownership refused")
        return row

    def update(self, identity, key, expression, values, condition="runtime_attempt_id = :attempt", names=None):
        try:
            self.repo._client.update_item(
                TableName=self.repo.table_name,
                Key=serialize({"event_id": task_ops_partition(identity.task_id), "arrived_at": "CODE_" + key}),
                UpdateExpression=expression, ConditionExpression=condition,
                **({"ExpressionAttributeNames": names} if names else {}),
                ExpressionAttributeValues=serialize({":attempt": identity.runtime_attempt_id, **values}),
            )
        except ClientError:
            raise HTTPException(409, "Code Interpreter state changed") from None

    def claim(self, identity, task, operation_id, operation, payload):
        digest = payload_digest({"attempt": identity.runtime_attempt_id, "operation": operation, "payload": payload})
        key = "OP#" + operation_id
        dedup_key = "DEDUP#" + digest if operation in {"start", "execute", "close"} else None
        prior = self.row(identity, key)
        if prior:
            if prior.get("runtime_attempt_id") != identity.runtime_attempt_id or prior.get("request_digest") != digest:
                raise HTTPException(409, "Operation identity conflict")
            return prior, False
        alias = self.row(identity, dedup_key) if dedup_key else None
        if alias:
            if alias.get("runtime_attempt_id") != identity.runtime_attempt_id:
                raise HTTPException(403, "Operation ownership refused")
            original = self.row(identity, "OP#" + alias["operation_id"])
            if not original or original.get("request_digest") != digest:
                raise HTTPException(409, "Operation alias unavailable")
            return original, False
        entry = base_item(partition=task_ops_partition(identity.task_id), sort_key="CODE_" + key, record_type="TASK_OPS", scope=task["scope"])
        entry.update(runtime_attempt_id=identity.runtime_attempt_id, request_digest=digest, operation=operation, state="unknown", created_at=int(time.time()))
        transactions = []
        if dedup_key:
            alias = base_item(partition=task_ops_partition(identity.task_id), sort_key="CODE_" + dedup_key, record_type="TASK_OPS", scope=task["scope"])
            alias.update(runtime_attempt_id=identity.runtime_attempt_id, operation_id=operation_id)
            transactions.append({"Put": {"TableName": self.repo.table_name, "Item": serialize(alias), "ConditionExpression": "attribute_not_exists(event_id)"}})
        try:
            self.repo._client.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.repo.table_name,
                    "Key": serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
                    "UpdateExpression": "SET cyber_operation_count = if_not_exists(cyber_operation_count, :zero) + :one",
                    "ConditionExpression": "runtime_attempt_id = :attempt AND generation = :generation AND #state = :running AND (attribute_not_exists(code_closed_attempt) OR code_closed_attempt <> :attempt) AND (attribute_not_exists(cyber_operation_count) OR cyber_operation_count < :limit)",
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": serialize({":zero": 0, ":one": 1, ":limit": 128, ":attempt": identity.runtime_attempt_id, ":generation": identity.generation, ":running": task["state"]})}},
                {"Put": {"TableName": self.repo.table_name, "Item": serialize(entry), "ConditionExpression": "attribute_not_exists(event_id)"}},
                *transactions,
            ])
        except ClientError:
            prior = self.row(identity, key)
            if prior and prior.get("request_digest") == digest and prior.get("runtime_attempt_id") == identity.runtime_attempt_id:
                return prior, False
            alias = self.row(identity, dedup_key) if dedup_key else None
            if alias and alias.get("runtime_attempt_id") == identity.runtime_attempt_id:
                prior = self.row(identity, "OP#" + alias["operation_id"])
                if prior and prior.get("request_digest") == digest:
                    return prior, False
            raise HTTPException(409, "Operation claim conflict") from None
        return entry, True

    def invoke(self, session, name, arguments):
        response = self.provider.invoke_code_interpreter(
            codeInterpreterIdentifier=self.identifier, sessionId=session,
            name=name, arguments=arguments,
        )
        result = []
        byte_length = 0
        for event in response["stream"]:
            if "result" not in event or event["result"].get("isError"):
                raise HTTPException(502, "Provider execution refused")
            byte_length += len(json.dumps(event["result"], default=lambda item: base64.b64encode(item).decode() if isinstance(item, bytes) else str(item)).encode())
            if byte_length > 20000:
                raise HTTPException(413, "Provider output exceeds bound")
            result.append(json.loads(json.dumps(event["result"], default=lambda item: base64.b64encode(item).decode() if isinstance(item, bytes) else str(item))))
        if not result:
            raise HTTPException(502, "Provider result unavailable")
        return result

    def receipt(self, identity, operation_id, status, result, *, artifact=None):
        return {"schema_version": "1.0", "task_id": identity.task_id, "operation_id": operation_id,
                "operation_status": status, "result": result, **({"artifact": artifact} if artifact else {})}

    def evidence(self, identity, operation_id, result):
        content = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        if len(content) > 24000:
            raise HTTPException(413, "Code Interpreter output exceeds Task artifact bound")
        digest = hashlib.sha256(content).hexdigest()
        artifact = self.authority.put_run_artifact(attempt=identity, content=content, content_type="application/json", digest=digest)
        return self.receipt(identity, operation_id, "confirmed", result,
                            artifact={"artifact_id": artifact.artifact_id, "content_type": "application/json", "content_sha256": digest, "byte_length": len(content)})

    def stop(self, identity, handle, session):
        if session.get("closed"):
            return
        try:
            self.provider.stop_code_interpreter_session(
                codeInterpreterIdentifier=self.identifier, sessionId=session["provider_id"],
                clientToken=hashlib.sha256((handle + ":stop").encode()).hexdigest())
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
        self.update(identity, "SESSION#" + handle, "SET closed = :closed", {":closed": True})

    def cleanup(self, identity, task, operation_id):
        # Fence new claims before reading the single allowed start for this attempt.
        self.repo._client.update_item(
            TableName=self.repo.table_name,
            Key=serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
            UpdateExpression="SET code_closed_attempt = :attempt",
            ConditionExpression="runtime_attempt_id = :attempt",
            ExpressionAttributeValues=serialize({":attempt": identity.runtime_attempt_id}))
        digest = payload_digest({"attempt": identity.runtime_attempt_id, "operation": "start", "payload": {}})
        alias = self.row(identity, "DEDUP#" + digest)
        pending = []
        if alias:
            start_id = alias["operation_id"]
            claim = self.row(identity, "OP#" + start_id)
            handle = hashlib.sha256(f"{identity.task_id}:{identity.runtime_attempt_id}:{start_id}".encode()).hexdigest()
            session = self.row(identity, "SESSION#" + handle)
            if session:
                self.owned(identity, handle)
                self.stop(identity, handle, session)
            elif not claim or time.time() < int(claim.get("created_at", time.time())) + 960:
                # Unknown/in-flight start: never assert closure. Provider TTL plus
                # the bounded request duration limits the orphan's lifetime.
                pending.append(handle)
        status = "pending" if pending else "confirmed"
        return self.receipt(identity, operation_id, status, {"status": status, "pending_jobs": pending})

    def execute(self, identity, body):
        operation, payload, operation_id = body.operation, body.payload, body.operation_id
        task = self.task(identity, cleanup=operation in {"close", "cancel_jobs"})
        if operation == "cancel_jobs":
            return self.cleanup(identity, task, operation_id)
        if operation == "close":
            session = self.owned(identity, payload["session_id"])
            self.stop(identity, payload["session_id"], session)
            return self.evidence(identity, operation_id, {"status": "closed"})
        session = self.owned(identity, payload["session_id"]) if operation != "start" else None
        if operation not in {"start", "close"} and (not session.get("provider_id") or session.get("closed")):
            raise HTTPException(409, "Session unavailable")
        if operation == "result":
            execution = self.row(identity, "OP#" + payload["execution_id"])
            if (not execution or execution.get("runtime_attempt_id") != identity.runtime_attempt_id
                or execution.get("operation") != "execute" or execution.get("session_id") != payload["session_id"]):
                raise HTTPException(403, "Execution ownership refused")
            prior, fresh = self.claim(identity, task, operation_id, operation, payload)
            if not fresh and prior.get("receipt", {}).get("operation_status") == "confirmed":
                return prior["receipt"]
            if not execution.get("provider_task_id"):
                return self.receipt(identity, operation_id, "unknown", {"status": "unknown"})
            result = self.invoke(session["provider_id"], "getTask", {"taskId": execution["provider_task_id"]})
            data = self.safe_result(result)
            data.pop("taskId", None)
            if data.get("status") not in {"completed", "failed", "cancelled", "running", "pending", "submitted"}:
                raise HTTPException(502, "Unexpected provider task status")
            if data.get("status") not in {"completed", "failed", "cancelled"}:
                return self.receipt(identity, operation_id, "pending", {"status": "pending"})
            receipt = self.evidence(identity, operation_id, data)
            self.update(identity, "OP#" + operation_id, "SET receipt = :receipt", {":receipt": receipt})
            return receipt
        if operation == "file":
            prior, fresh = self.claim(identity, task, operation_id, operation, payload)
            if not fresh:
                return prior.get("receipt") or self.receipt(identity, operation_id, "unknown", {"status": "unknown"})
            self.task(identity)
            result = self.invoke(session["provider_id"], "readFiles", {"paths": [payload["path"]]})
            receipt = self.evidence(identity, operation_id, {"path": payload["path"], "contents": result})
            self.update(identity, "OP#" + operation_id, "SET receipt = :receipt", {":receipt": receipt})
            return receipt
        prior, fresh = self.claim(identity, task, operation_id, operation, payload)
        if not fresh:
            stored = prior.get("receipt")
            if stored:
                return {**stored, "operation_id": operation_id}
            return self.receipt(identity, operation_id, "unknown", {"status": "unknown"})
        self.task(identity, cleanup=operation == "close")
        if operation == "start":
            handle = hashlib.sha256(f"{identity.task_id}:{identity.runtime_attempt_id}:{operation_id}".encode()).hexdigest()
            remaining = datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00")).timestamp() - time.time()
            provider = self.provider.start_code_interpreter_session(
                codeInterpreterIdentifier=self.identifier, clientToken=handle, name="adp-" + handle,
                sessionTimeoutSeconds=max(1, min(900, math.ceil(remaining))),
            )
            session_row = base_item(partition=task_ops_partition(identity.task_id), sort_key="CODE_SESSION#" + handle, record_type="TASK_OPS", scope=task["scope"])
            session_row.update(runtime_attempt_id=identity.runtime_attempt_id, provider_id=provider["sessionId"], closed=False, created_at=int(time.time()))
            self.repo._client.put_item(TableName=self.repo.table_name, Item=serialize(session_row), ConditionExpression="attribute_not_exists(event_id)")
            receipt = self.evidence(identity, operation_id, {"session_id": handle, "status": "active"})
        else:
            self.update(identity, "OP#" + operation_id, "SET session_id = :session", {":session": payload["session_id"]})
            command = "python -c " + shlex.quote(payload["code"])
            result = self.safe_result(self.invoke(session["provider_id"], "startCommandExecution", {"command": command}))
            provider_task_id = result.get("taskId")
            if not isinstance(provider_task_id, str) or not provider_task_id:
                raise HTTPException(502, "Provider task identity unavailable")
            self.update(identity, "OP#" + operation_id, "SET provider_task_id = :task, session_id = :session",
                        {":task": provider_task_id, ":session": payload["session_id"]})
            receipt = self.receipt(identity, operation_id, "pending", {"status": "pending", "execution_id": operation_id})
        self.update(identity, "OP#" + operation_id, "SET receipt = :receipt", {":receipt": receipt})
        return receipt

    @staticmethod
    def safe_result(events):
        value = json.dumps(events, default=lambda item: base64.b64encode(item).decode() if isinstance(item, bytes) else str(item))
        if len(value.encode()) > 20000:
            raise HTTPException(413, "Provider output exceeds bound")
        structured = [event.get("structuredContent", {}) for event in events]
        if len(structured) == 1 and isinstance(structured[0], dict) and structured[0]:
            return json.loads(value)[0]["structuredContent"]
        texts = [block.get("text", "") for event in events for block in event.get("content", []) if block.get("type") == "text"]
        if len(texts) == 1:
            try:
                parsed = json.loads(texts[0])
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
        raise HTTPException(502, "Unexpected provider response")

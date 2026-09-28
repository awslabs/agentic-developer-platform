"""Shared Web Search Task claims, preserving Cyber's existing storage identities."""

import hashlib
import json
import time
from datetime import datetime

from botocore.exceptions import ClientError
from fastapi import HTTPException

from adp_tools.storage import base_item, payload_digest, serialize, task_ops_partition, task_partition
from agentcore_tools import websearch


class WebSearch:
    def __init__(self, repo, authority, revalidate):
        self.repo, self.authority, self.revalidate = repo, authority, revalidate

    def task(self, identity):
        task = self.repo.read_task(identity.task_id)
        if (
            task.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}
            or task.get("runtime_attempt_id") != identity.runtime_attempt_id
            or task.get("generation") != identity.generation
        ):
            raise HTTPException(403, "Task ownership refused")
        if task.get("state") != "running" or time.time() >= datetime.fromisoformat(
            task["deadline_at"].replace("Z", "+00:00")
        ).timestamp():
            raise HTTPException(409, "Task no longer admits execution")
        if self.revalidate() != identity:
            raise HTTPException(403, "Task authority changed")
        return task

    def execute(self, identity, body):
        websearch.SearchInput.model_validate(body.payload)
        task = self.task(identity)
        partition = task_ops_partition(identity.task_id)
        digest = payload_digest({
            "operation": "search", "payload": body.payload, "attempt": identity.runtime_attempt_id,
        })
        key = "CYBER_OP#" + digest
        id_key = "CYBER_ID#" + body.operation_id
        binding = self.repo._get(partition, id_key)
        if binding and (binding.get("request_digest") != digest or binding.get("runtime_attempt_id") != identity.runtime_attempt_id):
            raise HTTPException(409, "Cyber operation ID reused with different payload")
        prior = self.repo._get(partition, key)
        if prior:
            if prior.get("request_digest") != digest or prior.get("runtime_attempt_id") != identity.runtime_attempt_id:
                raise HTTPException(409, "Cyber operation identity conflict")
            if not binding:
                self._claim(identity, task, digest, id_key, None)
            if prior.get("receipt"):
                return {**prior["receipt"], "operation_id": body.operation_id}
            return {
                "schema_version": "1.0", "task_id": identity.task_id, "operation_id": body.operation_id,
                "operation_status": "unknown", "result": {"status": "unknown", "potential_query_count": 1, "max_estimated_search_usd": 0.007},
                "error_code": "cyber_outcome_unknown",
            }
        self.task(identity)
        row = base_item(partition=partition, sort_key=key, record_type="TASK_OPS", scope=task["scope"])
        row.update(request_digest=digest, runtime_attempt_id=identity.runtime_attempt_id, operation="search")
        self._claim(identity, task, digest, id_key, row)
        result = websearch.search(body.payload, before_search=lambda: self.task(identity))
        self.task(identity)
        content = json.dumps({"operation": "search", "result": result}, sort_keys=True, separators=(",", ":"), default=str).encode()
        if len(content) > 24000:
            raise HTTPException(413, "Search findings exceed Task evidence limit")
        artifact = self.authority.put_run_artifact(
            attempt=identity, content=content, content_type="application/json", digest=hashlib.sha256(content).hexdigest(),
        )
        receipt = {
            "schema_version": "1.0", "task_id": identity.task_id, "operation_id": body.operation_id,
            "operation_status": "unknown" if result.get("status") == "unknown" else "confirmed",
            "result": result,
            "artifact": {
                "artifact_id": artifact.artifact_id, "content_type": artifact.content_type,
                "content_sha256": artifact.content_sha256, "byte_length": len(content),
            },
        }
        self.repo._client.update_item(
            TableName=self.repo.table_name,
            Key=serialize({"event_id": partition, "arrived_at": key}),
            UpdateExpression="SET receipt = :receipt",
            ConditionExpression="request_digest = :digest AND runtime_attempt_id = :attempt",
            ExpressionAttributeValues=serialize({":receipt": receipt, ":digest": digest, ":attempt": identity.runtime_attempt_id}),
        )
        return receipt

    def _claim(self, identity, task, digest, id_key, row):
        partition = task_ops_partition(identity.task_id)
        binding = base_item(partition=partition, sort_key=id_key, record_type="TASK_OPS", scope=task["scope"])
        binding.update(request_digest=digest, runtime_attempt_id=identity.runtime_attempt_id)
        transactions = [{"Update": {
            "TableName": self.repo.table_name,
            "Key": serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
            "UpdateExpression": "SET cyber_operation_count = if_not_exists(cyber_operation_count, :zero) + :one",
            "ConditionExpression": "runtime_attempt_id = :attempt AND generation = :generation AND #state = :state AND (attribute_not_exists(cyber_closed_attempt) OR cyber_closed_attempt <> :attempt) AND (attribute_not_exists(cyber_operation_count) OR cyber_operation_count < :limit)",
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": serialize({
                ":zero": 0, ":one": 1, ":limit": 128, ":attempt": identity.runtime_attempt_id,
                ":generation": identity.generation, ":state": task["state"],
            }),
        }}]
        for item in (row, binding):
            if item:
                transactions.append({"Put": {"TableName": self.repo.table_name, "Item": serialize(item), "ConditionExpression": "attribute_not_exists(event_id)"}})
        try:
            self.repo._client.transact_write_items(TransactItems=transactions)
        except ClientError:
            raise HTTPException(409, "Cyber operation claim conflict") from None

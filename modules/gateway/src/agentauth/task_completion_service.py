"""Developer completion from detached checks and freshly observed provider state."""

from __future__ import annotations

import hashlib
import json
import re

from starlette.concurrency import run_in_threadpool

from src.tasks.records import payload_digest, task_ops_partition
from src.tasks.store import TaskStoreError, _deserialize, _serialize

KEY = "COMPLETION#v1"


def completion_policy(repository, task):
    grant = repository._get_authority("TENANT#" + task["scope"]["tenant"], f"TASK_RUN#{task['invocation_id']}#GEN#{int(task['generation']):010d}")
    harness = (grant or {}).get("harness")
    if not harness:
        return "report"
    return json.loads(harness["snapshot"]["definition"])["completionPolicy"]


def required_acceptance(binding, criteria):
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 100 or any(not isinstance(c, str) or not c.strip() for c in criteria):
        raise TaskStoreError("Developer requires explicit acceptance criteria")
    ids = [hashlib.sha256(f"{index}\0{criterion}".encode()).hexdigest() for index, criterion in enumerate(criteria)]
    mapping = binding.get("acceptance_checks", {})
    checks = {check["name"]: check for check in binding.get("validation_checks", [])}
    for key in ids:
        check = checks.get(mapping.get(key))
        # Only administrator-approved entrypoints in the immutable validation
        # image can certify requirements. Repository-authored checks supplement
        # these, but cannot certify themselves by changing their own assertions.
        if not check or not re.fullmatch(r"/opt/adp-checks/[a-zA-Z0-9_-]+", check["argv"][0]):
            raise TaskStoreError("Requirement lacks an approved detached check")
    return {key: mapping[key] for key in ids}


def operation_digest(repository, task_id):
    rows = []
    for prefix, limit in (("MODEL#", 129), ("TOOL#", 129)):
        response = repository._client.query(
            TableName=repository.table_name,
            KeyConditionExpression="event_id = :pk AND begins_with(arrived_at, :prefix)",
            ExpressionAttributeValues=_serialize({":pk": task_ops_partition(task_id), ":prefix": prefix}),
            ConsistentRead=True,
            Limit=limit,
        )
        if response.get("LastEvaluatedKey"):
            raise TaskStoreError("Completion operation evidence exceeds bound")
        items = [_deserialize(item) for item in response.get("Items", [])]
        if any(item.get("operation_status") != "confirmed" for item in items):
            raise TaskStoreError("Completion operation outcome is unconfirmed")
        rows.extend(items)
    return payload_digest(rows)


class TaskCompletionService:
    def __init__(self, publication, *, observe):
        self.publication, self.repository, self.observe = publication, publication.repository, observe

    def prepare(self, identity):
        task = self.publication.current(identity)
        snapshot = self.repository.read_task(identity.task_id)
        if completion_policy(self.repository, snapshot) != "validated-change":
            raise TaskStoreError("Developer completion policy is not admitted")
        criteria = task["input_payload"].get("acceptance_criteria", [])
        mapping = required_acceptance(task["repository_binding"]["binding"], criteria)
        row = self.publication.read(identity)
        if not row or row.get("operation_status") != "confirmed":
            raise TaskStoreError("Developer publication is not confirmed")
        intent = row["binding"]
        _, binding, digest = self.publication.prepare(
            identity,
            artifact_id=intent["artifact_id"],
            digest=intent["artifact_digest"],
            commit=intent["commit"],
            title=intent["title"],
            body=intent["body"],
        )
        if digest != row["request_digest"] or binding != intent:
            raise TaskStoreError("Publication evidence differs from current attempt")
        # Steering changes requirements. Until a new trusted acceptance mapping is
        # admitted, do not certify the old criteria as covering the amendment.
        from src.tasks.task_commands import TaskCommands

        if any(command.get("kind") == "input" for command in TaskCommands(self.repository).commands(identity.task_id)):
            raise TaskStoreError("Amended developer requirements need renewed acceptance checks")
        return row, mapping, operation_digest(self.repository, identity.task_id)

    async def execute(self, identity):
        row, mapping, operations = await run_in_threadpool(self.prepare, identity)

        async def reauthorize():
            await run_in_threadpool(self.publication.current, identity)

        observation = await self.observe(
            tenant=identity.tenant,
            task_id=identity.task_id,
            frozen=row["binding"]["repository_binding"],
            receipt=row["result"],
            reauthorize=reauthorize,
        )
        if observation != row["result"]:
            raise TaskStoreError("Current provider state differs from validated publication")
        current, current_mapping, current_operations = await run_in_threadpool(self.prepare, identity)
        if current != row or current_mapping != mapping or current_operations != operations:
            raise TaskStoreError("Completion evidence changed while observing provider")
        result = {
            "schema_version": "1.0",
            "task_id": identity.task_id,
            "status": "verified",
            "local_head": row["result"]["local_head"],
            "provider_head": row["result"]["provider_head"],
            "tree": row["result"]["tree"],
            "url": row["result"]["url"],
            "requirements": mapping,
            "publication_receipt": row["request_digest"],
            "operations_digest": operations,
        }

        def commit():
            task = self.publication.current(identity)
            item = {
                "event_id": task_ops_partition(identity.task_id),
                "arrived_at": KEY,
                "runtime_attempt_id": identity.runtime_attempt_id,
                "result": result,
                "receipt_id": payload_digest(result),
            }
            self.repository._client.transact_write_items(
                TransactItems=[{"Put": {"TableName": self.repository.table_name, "Item": _serialize(item)}}, *self.publication.fences(identity, task)]
            )
            return {**result, "receipt_id": item["receipt_id"]}

        return await run_in_threadpool(commit)


def require_completion_receipt(repository, identity, snapshot):
    policy = completion_policy(repository, snapshot)
    if policy == "report":
        return
    if policy != "validated-change":
        raise TaskStoreError("Persona completion is not implemented")
    row = repository._get(task_ops_partition(identity.task_id), KEY)
    if (
        not row
        or row.get("runtime_attempt_id") != identity.runtime_attempt_id
        or row.get("receipt_id") != payload_digest(row["result"])
        or row["result"].get("operations_digest") != operation_digest(repository, identity.task_id)
    ):
        raise TaskStoreError("Current developer completion receipt is unavailable")

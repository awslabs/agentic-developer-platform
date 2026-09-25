"""Domain-owned operation storage. Never writes platform Task authority tables."""

import hashlib
from decimal import Decimal

import rfc8785
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from fastapi import HTTPException


def serialize(row):
    # DynamoDB disallows native floats; preserve exact JSON decimal values.
    def convert(value):
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return {
        key: TypeSerializer().serialize(convert(value)) for key, value in row.items()
    }


def payload_digest(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def task_partition(task_id):
    return "TASK#" + task_id


def task_ops_partition(task_id):
    return "TASK_OPS#" + task_id


def base_item(*, partition, sort_key, record_type, scope):
    return dict(
        event_id=partition,
        arrived_at=sort_key,
        record_type=record_type,
        schema_version="1.0",
        scope=dict(scope),
    )


class OperationRepository:
    """Local counters/fences plus freshly authorized Task context on every read.

    The local META only mirrors identity/state/version; input content is returned
    from authority and is not copied into the domain's metadata row. Monotonic
    authority versions stop delayed old authorization responses replacing a newer
    attempt. Existing domain counters and close fences survive all refreshes.
    """

    def __init__(self, client, table_name, authorize):
        self._client, self.table_name, self.authorize = client, table_name, authorize

    def _get(self, partition, sort_key):
        row = self._client.get_item(
            TableName=self.table_name,
            Key=serialize(dict(event_id=partition, arrived_at=sort_key)),
            ConsistentRead=True,
        ).get("Item")
        return (
            {k: TypeDeserializer().deserialize(v) for k, v in row.items()}
            if row
            else None
        )

    def read_task(self, task_id):
        verified = self.authorize()
        task = verified.task
        if verified.identity.task_id != task_id or task.get("task_id") != task_id:
            raise HTTPException(403, "Task identity differs")
        if type(task.get("version")) is not int or task["version"] < 1:
            raise HTTPException(503, "Task version unavailable")
        self._client.update_item(
            TableName=self.table_name,
            Key=serialize(dict(event_id=task_partition(task_id), arrived_at="META")),
            UpdateExpression="SET runtime_attempt_id=:attempt, generation=:generation, #state=:state, authority_version=:version, #scope=:scope",
            ConditionExpression="attribute_not_exists(authority_version) OR authority_version < :version OR (authority_version = :version AND runtime_attempt_id = :attempt)",
            ExpressionAttributeNames={"#state": "state", "#scope": "scope"},
            ExpressionAttributeValues=serialize(
                {
                    ":attempt": verified.identity.runtime_attempt_id,
                    ":generation": verified.identity.generation,
                    ":state": task["state"],
                    ":version": task["version"],
                    ":scope": task["scope"],
                }
            ),
        )
        local = self._get(task_partition(task_id), "META")
        if (
            local["runtime_attempt_id"] != verified.identity.runtime_attempt_id
            or int(local["authority_version"]) != task["version"]
        ):
            raise HTTPException(409, "Task authority changed")
        return {
            **task,
            **{
                k: local[k]
                for k in ("cyber_closed_attempt", "cyber_operation_count")
                if k in local
            },
        }

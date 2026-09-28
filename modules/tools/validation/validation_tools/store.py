"""Durable validation effects, with no duplicate execution on queue redelivery.

The generic OperationRepository resolves current Task authority and maintains its
monotonic version mirror. This service owns only its job rows and stop fences.
"""

from __future__ import annotations

import time
import uuid

from botocore.exceptions import ClientError
from fastapi import HTTPException

from adp_tools.storage import payload_digest, serialize, task_partition


class ValidationJobs:
    def __init__(self, repository, *, clock=time.time):
        self.repo, self.clock = repository, clock

    def _key(self, task_id, operation_id):
        try:
            valid = str(uuid.UUID(operation_id)) == operation_id and uuid.UUID(operation_id).version == 4
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise HTTPException(422, "Invalid validation operation identity")
        return {"event_id": task_partition(task_id), "arrived_at": "VALIDATION#" + operation_id}

    def _read(self, identity, operation_id):
        key = self._key(identity.task_id, operation_id)
        row = self.repo._get(key["event_id"], key["arrived_at"])
        if row and row.get("identity") != identity.model_dump():
            raise HTTPException(403, "Validation operation scope differs")
        return row

    def _task(self, identity):
        task = self.repo.read_task(identity.task_id)
        if (task.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}
                or task.get("runtime_attempt_id") != identity.runtime_attempt_id
                or task.get("invocation_id") != identity.invocation_id or task.get("generation") != identity.generation):
            raise HTTPException(403, "Validation Task authority differs")
        return task

    def _fence(self, identity, task):
        return {
            "TableName": self.repo.table_name,
            "Key": serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
            "ConditionExpression": "authority_version=:version AND runtime_attempt_id=:attempt AND #scope=:scope "
                "AND (attribute_not_exists(validation_closed_attempt) OR validation_closed_attempt<>:attempt)",
            "ExpressionAttributeNames": {"#scope": "scope"},
            "ExpressionAttributeValues": serialize({":version": task["version"], ":attempt": identity.runtime_attempt_id,
                ":scope": {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}}),
        }

    def admit(self, identity, operation_id, request):
        task = self._task(identity)
        digest = payload_digest({"identity": identity.model_dump(), "request": request})
        prior = self._read(identity, operation_id)
        if prior:
            if prior.get("request_digest") != digest or prior.get("request") != request:
                raise HTTPException(409, "Validation operation input changed")
            return prior, False
        row = {**self._key(identity.task_id, operation_id), "identity": identity.model_dump(),
               "request": request, "request_digest": digest, "phase": "pending",
               "created_at": int(self.clock()), "operation_id": operation_id}
        fence = self._fence(identity, task)
        fence["UpdateExpression"] = "ADD validation_operation_count :one"
        fence["ConditionExpression"] += " AND (attribute_not_exists(validation_operation_count) OR validation_operation_count<:maximum)"
        fence["ExpressionAttributeValues"].update(serialize({":one": 1, ":maximum": 128}))
        try:
            self.repo._client.transact_write_items(TransactItems=[
                {"Update": fence},
                {"Put": {"TableName": self.repo.table_name, "Item": serialize(row),
                         "ConditionExpression": "attribute_not_exists(event_id)"}},
            ])
        except ClientError:
            prior = self._read(identity, operation_id)
            if prior and prior.get("request_digest") == digest and prior.get("request") == request:
                return prior, False
            raise HTTPException(409, "Validation admission could not be confirmed") from None
        return row, True

    def claim(self, identity, operation_id):
        task = self._task(identity)
        row = self._read(identity, operation_id)
        if not row or row.get("phase") != "pending":
            return None
        owner = str(uuid.uuid4())
        fence = self._fence(identity, task)
        fence["UpdateExpression"] = "SET validation_active_operation=:operation"
        fence["ConditionExpression"] += " AND attribute_not_exists(validation_active_operation)"
        fence["ExpressionAttributeValues"].update(serialize({":operation": operation_id}))
        try:
            self.repo._client.transact_write_items(TransactItems=[
                {"Update": fence},
                {"Update": {"TableName": self.repo.table_name, "Key": serialize(self._key(identity.task_id, operation_id)),
                    "UpdateExpression": "SET #phase=:running, owner_token=:owner, started_at=:started",
                    "ConditionExpression": "#phase=:pending AND request_digest=:digest",
                    "ExpressionAttributeNames": {"#phase": "phase"},
                    "ExpressionAttributeValues": serialize({":running": "running", ":pending": "pending",
                        ":owner": owner, ":started": int(self.clock()), ":digest": row["request_digest"]})}},
            ])
        except ClientError:
            current = self._read(identity, operation_id)
            # Resolve a lost claim acknowledgement, not a lost execution result.
            if not current or current.get("owner_token") != owner or current.get("phase") != "running":
                return None
        return {**row, "phase": "running", "owner_token": owner}

    def delivery(self, identity, operation_id):
        """Bound queue redelivery; only claim() authorizes actual execution."""
        self._task(identity)
        self._read(identity, operation_id)
        try:
            self.repo._client.update_item(TableName=self.repo.table_name,
                Key=serialize(self._key(identity.task_id, operation_id)),
                UpdateExpression="SET delivered_at=:now ADD delivery_count :one",
                ConditionExpression="#phase=:pending AND (attribute_not_exists(delivery_count) OR delivery_count<:maximum) "
                    "AND (attribute_not_exists(delivered_at) OR delivered_at<=:retry_before)",
                ExpressionAttributeNames={"#phase": "phase"},
                ExpressionAttributeValues=serialize({":now": int(self.clock()), ":retry_before": int(self.clock()) - 5,
                    ":one": 1, ":maximum": 3, ":pending": "pending"}))
        except ClientError:
            return False
        return True

    def stopping(self, identity):
        row = self.repo._get(task_partition(identity.task_id), "META")
        return (not row or row.get("runtime_attempt_id") != identity.runtime_attempt_id
                or row.get("validation_closed_attempt") == identity.runtime_attempt_id
                or row.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal})

    def settle(self, identity, operation_id, owner, *, phase, result=None):
        if phase not in {"completed", "cancelled", "unknown"} or (phase == "completed") != isinstance(result, dict):
            raise ValueError("Invalid validation settlement")
        row = self._read(identity, operation_id)
        if not row or row.get("owner_token") != owner:
            raise HTTPException(403, "Validation settlement owner differs")
        digest = payload_digest(result)
        if row.get("phase") == phase and row.get("result_digest") == digest:
            return row
        update = {"TableName": self.repo.table_name, "Key": serialize(self._key(identity.task_id, operation_id)),
            "UpdateExpression": "SET #phase=:phase, #result=:result, result_digest=:digest, settled_at=:now",
            "ConditionExpression": "#phase=:running AND owner_token=:owner",
            "ExpressionAttributeNames": {"#phase": "phase", "#result": "result"},
            "ExpressionAttributeValues": serialize({":phase": phase, ":result": result, ":now": int(self.clock()),
                                                    ":running": "running", ":owner": owner, ":digest": digest})}
        writes = [{"Update": update}]
        if phase != "unknown":
            release = {"TableName": self.repo.table_name,
                "Key": serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
                "UpdateExpression": "REMOVE validation_active_operation",
                "ConditionExpression": "validation_active_operation=:operation",
                "ExpressionAttributeValues": serialize({":operation": operation_id})}
            if phase == "completed":
                release["ConditionExpression"] += " AND (attribute_not_exists(validation_closed_attempt) OR validation_closed_attempt<>:attempt)"
                release["ExpressionAttributeValues"].update(serialize({":attempt": identity.runtime_attempt_id}))
            writes.append({"Update": release})
        # The private owner nonce permits stop settlement after authority is
        # revoked. A completed result additionally requires fresh tool authority.
        if phase == "completed":
            task = self._task(identity)
            release["ConditionExpression"] += " AND runtime_attempt_id=:attempt AND authority_version=:version AND #scope=:scope"
            release["ExpressionAttributeNames"] = {"#scope": "scope"}
            release["ExpressionAttributeValues"].update(serialize({":version": task["version"],
                ":scope": {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}}))
        self.repo._client.transact_write_items(TransactItems=writes)
        return self._read(identity, operation_id)

    def read(self, identity, operation_id):
        self._task(identity)
        row = self._read(identity, operation_id)
        if row is None:
            raise HTTPException(404, "Validation operation unavailable")
        return row

    def idle(self, identity):
        self._task(identity)
        meta = self.repo._get(task_partition(identity.task_id), "META")
        rows = self.repo._client.query(TableName=self.repo.table_name,
            KeyConditionExpression="event_id=:task AND begins_with(arrived_at,:prefix)",
            ExpressionAttributeValues=serialize({":task": task_partition(identity.task_id), ":prefix": "VALIDATION#"}),
            ConsistentRead=True, Limit=129)
        return bool(meta and not meta.get("validation_active_operation")
                    and meta.get("validation_closed_attempt") != identity.runtime_attempt_id
                    and not rows.get("LastEvaluatedKey") and len(rows.get("Items", [])) <= 128
                    and all(row.get("phase", {}).get("S") in {"completed", "cancelled"}
                            for row in rows.get("Items", [])))

    def close(self, identity):
        """Stop-only authority callback must be installed on this repository."""
        self._task(identity)
        self.repo._client.update_item(TableName=self.repo.table_name,
            Key=serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
            UpdateExpression="SET validation_closed_attempt=:attempt",
            ConditionExpression="runtime_attempt_id=:attempt AND #scope=:scope",
            ExpressionAttributeNames={"#scope": "scope"},
            ExpressionAttributeValues=serialize({":attempt": identity.runtime_attempt_id,
                ":scope": {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}}))
        response = self.repo._client.query(TableName=self.repo.table_name,
            KeyConditionExpression="event_id=:task AND begins_with(arrived_at,:prefix)",
            ExpressionAttributeValues=serialize({":task": task_partition(identity.task_id), ":prefix": "VALIDATION#"}),
            ConsistentRead=True, Limit=129)
        if response.get("LastEvaluatedKey") or len(response.get("Items", [])) > 128:
            raise HTTPException(503, "Validation cleanup inventory exceeds bound")
        from boto3.dynamodb.types import TypeDeserializer
        pending = []
        for item in response.get("Items", []):
            row = {key: TypeDeserializer().deserialize(value) for key, value in item.items()}
            scope = row.get("identity", {})
            if any(scope.get(key) != getattr(identity, key) for key in ["task_id", "tenant", "canonical_principal"]):
                raise HTTPException(403, "Validation cleanup scope differs")
            if row["phase"] == "pending":
                try:
                    self.repo._client.update_item(TableName=self.repo.table_name,
                        Key=serialize(self._key(identity.task_id, row["operation_id"])),
                        UpdateExpression="SET #phase=:cancelled",
                        ConditionExpression="#phase=:pending",
                        ExpressionAttributeNames={"#phase": "phase"},
                        ExpressionAttributeValues=serialize({":cancelled": "cancelled", ":pending": "pending"}))
                    continue
                except ClientError:
                    # A racing claim must be observed again; never infer exit.
                    row = self.repo._get(row["event_id"], row["arrived_at"])
            if not row or row["phase"] in {"pending", "running", "unknown"}:
                pending.append(item["operation_id"]["S"])
        meta = self.repo._get(task_partition(identity.task_id), "META")
        active = (meta or {}).get("validation_active_operation")
        if active and active not in pending:
            pending.append(active)
        return pending

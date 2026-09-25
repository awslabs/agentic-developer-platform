"""Canonical bounded Task turns, fenced to live authority and one attempt."""
from __future__ import annotations

from datetime import UTC, datetime

from botocore.exceptions import ClientError

from src.tasks.records import base_item, command_sort_key, task_commands_partition, task_partition, task_turns_partition, turn_sort_key
from src.tasks.store import TaskStoreError, _deserialize, _serialize


class TaskTurnStore:
    def __init__(self, repository, *, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def list_turns(self, task_id):
        response = self.repository._client.query(TableName=self.repository.table_name,
            KeyConditionExpression="event_id = :partition",
            ExpressionAttributeValues={":partition": {"S": task_turns_partition(task_id)}}, ConsistentRead=True, Limit=9)
        rows = [_deserialize(item) for item in response.get("Items", [])]
        if len(rows) > 8 or response.get("LastEvaluatedKey"):
            raise TaskStoreError("task turn bound exceeded")
        return rows

    @staticmethod
    def response(turn, *, existing=False, pending=0):
        public = {key: turn[key] for key in ("schema_version", "task_id", "turn_number", "turn_id", "command_ids",
                                            "committed_at", "transcript_version")}
        public["immutable"] = True
        return {"schema_version": "1.0", "operation_status": "existing" if existing else "committed",
                "turn": public, "messages": turn["messages"], "pending_input_count": pending}

    def commit(self, *, identity, request_id, expected_transcript_version):
        task = self.repository.read_task(identity.task_id)
        if task is None or (task["invocation_id"], int(task["generation"]), task.get("runtime_attempt_id")) != (
                identity.invocation_id, identity.generation, identity.runtime_attempt_id):
            raise TaskStoreError("task attempt changed")
        self.repository.resolve_work(task["dispatch_id"], expected_kind="dispatch")
        existing = next((turn for turn in self.list_turns(identity.task_id) if turn["turn_id"] == request_id), None)
        if existing:
            return self.response(existing, existing=True)
        if task["state"] not in {"accepted", "queued", "running", "waiting_for_input"}:
            raise TaskStoreError("task cannot accept a turn")
        count = int(task.get("turn_count", 0))
        if expected_transcript_version != count + 1:
            raise TaskStoreError("transcript version changed")
        grant = self.repository._get_authority("TENANT#" + identity.tenant,
            f"TASK_RUN#{identity.invocation_id}#GEN#{identity.generation:010d}")
        if grant is None or count >= int(grant["limits"]["max_turns"]):
            raise TaskStoreError("task turn budget exhausted")
        now = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        if task["deadline_at"] <= now:
            raise TaskStoreError("task deadline elapsed")
        commands = sorted((row for row in self.repository.read_commands(task_id=identity.task_id)
            if row.get("kind", row.get("command_type")) == "input" and row.get("status") == "accepted"),
            key=lambda row: int(row["command_sequence"]))
        pending = len(commands)
        if not count:
            commands = []  # Initial immutable input is its own turn.
        if pending > 10:
            raise TaskStoreError("pending input bound exceeded")
        if not commands and count:
            return {"schema_version": "1.0", "operation_status": "waiting", "turn": None, "messages": [], "pending_input_count": 0}
        if any(row.get("authority_expires_at", "") <= now for row in commands):
            raise TaskStoreError("input authority expired")
        messages = [{"command_id": row["command_id"], "text": row["payload"]["text"],
                     **({"reply_to": row["payload"]["reply_to"]} if row["payload"].get("reply_to") else {})} for row in commands]
        number = count + 1
        turn = base_item(partition=task_turns_partition(identity.task_id), sort_key=turn_sort_key(number),
                         record_type="TASK_TURNS", scope=task["scope"]) | {
            "task_id": identity.task_id, "turn_id": request_id, "turn_number": number, "command_ids": [row["command_id"] for row in commands],
            "messages": messages, "committed_at": now, "transcript_version": number + 1,
            "invocation_id": identity.invocation_id, "generation": identity.generation, "runtime_attempt_id": identity.runtime_attempt_id}
        sequence = int(task["event_sequence"])
        if sequence + len(commands) > 10000:
            raise TaskStoreError("task event budget exhausted")
        transaction = [{"Put": {"TableName": self.repository.table_name, "Item": _serialize(turn),
                                "ConditionExpression": "attribute_not_exists(event_id)"}},
            {"Update": {"TableName": self.repository.table_name,
                "Key": _serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
                "UpdateExpression": "SET turn_count = :turn, #version = :next, updated_at = :now, event_sequence = :sequence",
                "ConditionExpression": ("#version = :version AND invocation_id = :invocation AND "
                                        "generation = :generation AND runtime_attempt_id = :attempt"),
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": _serialize({":turn": number, ":next": int(task["version"]) + 1,
                    ":version": int(task["version"]), ":now": now, ":sequence": sequence + len(commands),
                    ":invocation": identity.invocation_id, ":generation": identity.generation, ":attempt": identity.runtime_attempt_id})}}]
        transaction.extend(self.repository._authority_condition_checks(snapshot=task, runtime_attempt_id=identity.runtime_attempt_id))
        for offset, command in enumerate(commands, 1):
            transaction.append({"Update": {"TableName": self.repository.table_name,
                "Key": _serialize({"event_id": task_commands_partition(identity.task_id), "arrived_at": command_sort_key(command["command_id"])}),
                "UpdateExpression": "SET #status = :consumed, turn_id = :turn_id, turn_number = :turn, consumed_at = :now, updated_at = :now",
                "ConditionExpression": "#status = :accepted",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": _serialize({":consumed": "consumed", ":accepted": "accepted", ":turn_id": request_id,
                                                         ":turn": number, ":now": now})}})
            event = self.repository._event_item(task_id=identity.task_id, scope=task["scope"], sequence=sequence + offset,
                kind="input.consumed", invocation_id=identity.invocation_id, generation=identity.generation,
                runtime_attempt_id=identity.runtime_attempt_id, timestamp=now,
                data={"command_id": command["command_id"], "command_status": "consumed", "handoff": command.get("handoff", "not_started"),
                      "turn_id": request_id, "turn_number": number})
            transaction.append({"Put": {"TableName": self.repository.table_name, "Item": _serialize(event),
                                        "ConditionExpression": "attribute_not_exists(event_id)"}})
        try:
            self.repository._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            existing = next((turn for turn in self.list_turns(identity.task_id) if turn["turn_id"] == request_id), None)
            if existing:
                return self.response(existing, existing=True)
            raise TaskStoreError("task turn fence changed") from None
        return self.response(turn, pending=pending - len(commands))

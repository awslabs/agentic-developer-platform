"""Task-local durable input, cancellation and process settlement (T7).

Uses T1's request/authority tables and keys. Every mutation shares the META
version fence with turn creation and completion; transport auth supplies identity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src.tasks import errors
from src.tasks.records import (
    META_SORT_KEY,
    base_item,
    command_sort_key,
    payload_digest,
    task_authority_partition,
    task_commands_partition,
    task_partition,
    task_run_grant_sort_key,
    validate_uuid,
)
from src.tasks.store import TaskStore, _is_conditional_failure, _iso, _serialize

TERMINAL = frozenset({"completed", "failed", "cancelled"})


class _TransactionConflict(errors.TaskApiError):
    def __init__(self):
        super().__init__(409, "state_conflict", "Task changed concurrently; retry the same command ID.")


def receipt(row: dict) -> dict:
    result = {
        key: row[key]
        for key in (
            "command_id",
            "kind",
            "status",
            "handoff",
            "command_sequence",
            "turn_id",
            "turn_number",
            "consumed_at",
            "authority_expires_at",
            "reason",
        )
        if key in row
    }
    return {"schema_version": "1.0", **result, "accepted_at": row["created_at"]}


class TaskCommands:
    def __init__(self, repository: TaskStore):
        self.repo = repository

    def snapshot(self, task_id: str) -> dict:
        result = self.repo.read_task(task_id)
        if result is None:
            raise errors.not_found()
        return result

    def commands(self, task_id: str) -> list[dict]:
        # At most 100 inputs plus cancellation. Query one extra page sentinel;
        # rejecting a malformed/unbounded journal is safer than missing commands.
        rows = self.repo.read_commands(task_id=task_id, limit=102)
        if len(rows) > 101:
            raise errors.state_conflict("Task command journal exceeds its fixed limit.")
        return sorted(rows, key=lambda row: int(row["command_sequence"]))

    def _put(self, row: dict) -> dict:
        return {"Put": {"TableName": self.repo.table_name, "Item": _serialize(row), "ConditionExpression": "attribute_not_exists(event_id)"}}

    def _meta(self, snapshot: dict, updates: dict, *, attempt: str | None = None) -> dict:
        from src.tasks.json_storage import encode_json_updates

        updates = encode_json_updates(snapshot, updates)
        names = {f"#f{i}": key for i, key in enumerate(updates)}
        values = {f":v{i}": value for i, value in enumerate(updates.values())}
        names.update({"#version": "version", "#state": "state"})
        values.update(
            {
                ":old_version": int(snapshot["version"]),
                ":old_state": snapshot["state"],
                ":old_events": int(snapshot.get("event_sequence", 0)),
                ":invocation": snapshot["invocation_id"],
                ":generation": int(snapshot["generation"]),
            }
        )
        condition = (
            "#version = :old_version AND #state = :old_state AND "
            "event_sequence = :old_events AND invocation_id = :invocation AND generation = :generation"
        )
        if attempt is not None:
            condition += " AND runtime_attempt_id = :attempt"
            values[":attempt"] = attempt
        return {
            "Update": {
                "TableName": self.repo.table_name,
                "Key": _serialize({"event_id": task_partition(snapshot["task_id"]), "arrived_at": META_SORT_KEY}),
                "UpdateExpression": "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(updates))),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": _serialize(values),
            }
        }

    def _write(self, items: list[dict]) -> None:
        try:
            self.repo._client.transact_write_items(TransactItems=items)
        except (ClientError, BotoCoreError) as exc:
            if _is_conditional_failure(exc):
                raise _TransactionConflict() from None
            raise errors.prerequisite_unavailable("Task transaction could not be confirmed.") from None

    def admit(self, *, task_id: str, command_id: str, kind: str, payload: dict, principal: str, tenant: str, expires_at: datetime) -> dict:
        validate_uuid(command_id, "command_id")
        if kind not in {"input", "cancel"}:
            raise errors.invalid_request("Unknown command kind.")
        snapshot = self.snapshot(task_id)
        if snapshot["scope"] != {"tenant": tenant, "canonical_principal": principal}:
            raise errors.not_found()
        digest = payload_digest({"kind": kind, "payload": payload})
        prior = self.repo._get(task_commands_partition(task_id), command_sort_key(command_id))
        if prior:
            if prior.get("command_digest") != digest:
                raise errors.TaskApiError(409, "idempotency_conflict", "Command ID has different content.")
            return receipt(prior)
        if snapshot["state"] in TERMINAL or snapshot["state"] == "cancel_requested":
            raise errors.state_conflict("Task no longer accepts commands.")
        if kind == "input" and snapshot.get("persona") in {"agent-task-claude-developer", "agent-task-codex-developer"}:
            raise errors.state_conflict("This coding runtime does not support follow-up input or steering. Cancellation remains available.")
        now = self.repo._clock()
        if expires_at.tzinfo is None or expires_at <= now:
            raise errors.disallowed_scope("Command authority is expired.")
        commands = self.commands(task_id)
        inputs = [row for row in commands if row["kind"] == "input"]
        if kind == "input":
            if len(inputs) >= 100 or sum(row["status"] == "accepted" for row in inputs) >= 10:
                raise errors.rate_limited("Task input capacity is exhausted.")
            if payload.get("reply_to") is not None:
                waiting = snapshot.get("input_request") or {}
                if payload["reply_to"] != waiting.get("input_request_id"):
                    raise errors.state_conflict("Clarification request is no longer current.")
        seq = int(snapshot.get("event_sequence", 0)) + 1
        if seq >= (10000 if kind == "cancel" else 9900):
            raise errors.rate_limited("Task event capacity is exhausted.")
        timestamp = _iso(now)
        row = base_item(
            partition=task_commands_partition(task_id), sort_key=command_sort_key(command_id), record_type="TASK_COMMANDS", scope=snapshot["scope"]
        ) | {
            "task_id": task_id,
            "command_id": command_id,
            "kind": kind,
            "payload": payload,
            "command_digest": digest,
            "author": principal,
            "authority_expires_at": _iso(expires_at),
            "command_sequence": int(snapshot.get("command_sequence", 0)) + 1,
            "status": "accepted",
            "handoff": "not_started",
            "turn_id": None,
            "turn_number": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        updates = {
            "version": int(snapshot["version"]) + 1,
            "event_sequence": seq,
            "command_sequence": row["command_sequence"],
            "updated_at": timestamp,
        }
        event_data = {"command_id": command_id, "command_status": "accepted", "handoff": "not_started"}
        if kind == "cancel":
            updates.update(state="cancel_requested", cancel_command_id=command_id)
            event_data = {"command_id": command_id, "status": "cancel_requested", "version": updates["version"]}
        event = self.repo._event_item(
            task_id=task_id,
            scope=snapshot["scope"],
            sequence=seq,
            kind="cancel.requested" if kind == "cancel" else "input.accepted",
            invocation_id=snapshot["invocation_id"],
            generation=int(snapshot["generation"]),
            timestamp=timestamp,
            data=event_data,
        )
        try:
            self._write([self._put(row), self._meta(snapshot, updates), self._put(event), *self.repo._authority_condition_checks(snapshot=snapshot)])
        except errors.TaskApiError:
            committed = self.repo._get(task_commands_partition(task_id), command_sort_key(command_id))
            if committed and committed.get("command_digest") == digest:
                return receipt(committed)
            raise
        return receipt(row)

    def cancel_unstarted(self, task_id):
        import uuid
        from types import SimpleNamespace

        for _ in range(3):
            task = self.snapshot(task_id)
            if task.get("runtime_not_started") and task["state"] == "cancelled":
                return True
            if task["state"] != "cancel_requested" or task.get("runtime_attempt_id") is not None:
                return False
            identity = SimpleNamespace(
                task_id=task_id,
                invocation_id=task["invocation_id"],
                generation=int(task["generation"]),
                runtime_attempt_id=None,
                tenant=task["scope"]["tenant"],
            )
            now = _iso(self.repo._clock())
            body = {
                "schema_version": "1.0",
                "outcome": "cancelled",
                "final_report_id": str(uuid.uuid4()),
                "child_exit": {"confirmed": False, "exit_code": None, "signal": None, "stopped_at": None},
                "result": None,
                "committed_result_refs": [],
                "error": {
                    "schema_version": "1.0",
                    "outcome": "cancelled",
                    "code": "cancelled_by_client",
                    "message": "Task cancelled before any runtime attempt started.",
                    "committed_at": now,
                    "runtime_not_started": True,
                    "child_exit_confirmed": False,
                    "recovery_required": False,
                    "provider_outcome": "not_started",
                    "total_usd": 0,
                },
            }
            try:
                self.finalize(identity, body, no_child=True)
                return True
            except errors.TaskApiError as exc:
                if exc.status != 409:
                    raise
        return False

    def _attempt(self, identity: Any) -> dict:
        snapshot = self.snapshot(identity.task_id)
        if (
            snapshot["invocation_id"] != identity.invocation_id
            or int(snapshot["generation"]) != identity.generation
            or snapshot.get("runtime_attempt_id") != identity.runtime_attempt_id
        ):
            raise errors.not_found()
        return snapshot

    def control(self, identity: Any) -> dict:
        snapshot = self._attempt(identity)
        return {
            "schema_version": "1.0",
            "task_id": identity.task_id,
            "cancel_requested": snapshot["state"] == "cancel_requested",
            "cancel_command_id": snapshot.get("cancel_command_id"),
            "pending_input_count": sum(row["kind"] == "input" and row["status"] == "accepted" for row in self.commands(identity.task_id)),
            "last_receipt_cursor": f"{identity.task_id}:{snapshot['event_sequence']}",
            "attempt_valid": True,
        }

    def _release_execution_on_finalize(self, identity, transaction):
        pk = task_authority_partition(identity.tenant)
        sk = task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation)
        grant = self.repo._get_authority(pk, sk)
        if not grant or grant.get("execution_capacity_released", False):
            return
        for index, item in enumerate(transaction):
            check = item.get("ConditionCheck")
            if not check or check.get("Key") != _serialize({"pk": pk, "sk": sk}):
                continue
            update = dict(check)
            update["UpdateExpression"] = "SET execution_capacity_released = :released"
            update["ConditionExpression"] = (
                "("
                + check["ConditionExpression"]
                + ") AND (attribute_not_exists(execution_capacity_released) OR execution_capacity_released = :not_released)"
            )
            update["ExpressionAttributeValues"] = {
                **check.get("ExpressionAttributeValues", {}),
                **_serialize({":released": True, ":not_released": False}),
            }
            transaction[index] = {"Update": update}
            for key in grant.get("execution_capacity_keys", []):
                transaction.append(
                    {
                        "Update": {
                            "TableName": self.repo.authority_table_name,
                            "Key": _serialize({"pk": key, "sk": "ACTIVE"}),
                            "UpdateExpression": "REMOVE reservations.#task ADD active_count :minus",
                            "ConditionExpression": "reservations.#task = :invocation AND active_count > :zero",
                            "ExpressionAttributeNames": {"#task": identity.task_id},
                            "ExpressionAttributeValues": _serialize({":invocation": identity.invocation_id, ":minus": -1, ":zero": 0}),
                        }
                    }
                )
            return
        raise errors.state_conflict("Task execution grant fence is missing.")

    def finalize(self, identity: Any, body: dict, *, stop_only: bool = False, no_child: bool = False, expected_version: int | None = None) -> dict:
        # A model receipt admitted before cancellation may settle after child exit.
        # Retry only rejected atomic transactions, rebuilding all fences each time.
        # Unknown write outcomes and caller-pinned versions must remain explicit.
        for attempt in range(3):
            try:
                return self._finalize_once(identity, body, stop_only=stop_only, no_child=no_child, expected_version=expected_version)
            except _TransactionConflict:
                if expected_version is not None or attempt == 2:
                    raise
        raise AssertionError("Unreachable finalization retry")

    def _finalize_once(
        self, identity: Any, body: dict, *, stop_only: bool = False, no_child: bool = False, expected_version: int | None = None
    ) -> dict:
        from datetime import timedelta

        from src.tasks.records import task_artifact_partition, task_capacity_partition, task_ops_partition, task_turns_partition
        from src.tasks.store import _deserialize

        if not no_child and isinstance(body.get("error"), dict) and "runtime_not_started" in body["error"]:
            raise errors.invalid_request("Runtime-not-started proof is gateway-owned.")
        snapshot = self._attempt(identity)
        if expected_version is not None and int(snapshot["version"]) != expected_version:
            raise errors.state_conflict("Task changed while observing termination.")
        final_digest = payload_digest(body)
        if snapshot["state"] in TERMINAL:
            if snapshot.get("finalize_digest") != final_digest:
                raise errors.state_conflict("Task already finalized with different evidence.")
            return {
                "schema_version": "1.0",
                "task_id": identity.task_id,
                "status": snapshot["state"],
                "version": int(snapshot["version"]),
                "terminal_event_id": snapshot["terminal_event_id"],
            }
        outcome = body["outcome"]
        if stop_only and outcome == "completed":
            raise errors.state_conflict("Stop-only authority cannot complete a task.")
        if outcome == "completed" and (
            snapshot["state"] == "cancel_requested" or body["child_exit"]["exit_code"] != 0 or body["child_exit"]["signal"] is not None
        ):
            raise errors.state_conflict("Cancellation or unsuccessful process exit prevents completion.")
        if outcome == "cancelled" and snapshot["state"] != "cancel_requested":
            raise errors.state_conflict("No cancellation was requested.")
        commands = self.commands(identity.task_id)
        pending = [row for row in commands if row["status"] == "accepted"]
        if outcome == "completed" and pending:
            raise errors.state_conflict("Pending input must be consumed before completion.")
        if outcome == "completed":
            from src.agentauth.task_completion_service import require_completion_receipt
            from src.tasks.store import TaskStoreError

            try:
                require_completion_receipt(self.repo, identity, snapshot)
            except TaskStoreError:
                raise errors.state_conflict("Persona completion evidence is unavailable.") from None
            from src.agentauth.task_turns import task_turn_limit

            maximum_turns = task_turn_limit(self.repo, identity.task_id)
            operations = self.repo._client.query(
                TableName=self.repo.table_name,
                KeyConditionExpression="event_id = :pk AND begins_with(arrived_at, :model)",
                ExpressionAttributeValues=_serialize({":pk": task_ops_partition(identity.task_id), ":model": "MODEL#"}),
                ConsistentRead=True,
                Limit=maximum_turns + 1,
            )
            rows = [_deserialize(row) for row in operations.get("Items", [])]
            turns = self.repo._client.query(
                TableName=self.repo.table_name,
                KeyConditionExpression="event_id = :pk",
                ExpressionAttributeValues=_serialize({":pk": task_turns_partition(identity.task_id)}),
                ConsistentRead=True,
                Limit=maximum_turns + 1,
            )
            turn_ids = {row["turn_id"] for row in map(_deserialize, turns.get("Items", []))}
            operation_turn_ids = {row.get("turn_id") for row in rows}
            if turns.get("LastEvaluatedKey") or not turn_ids or turn_ids != operation_turn_ids:
                raise errors.state_conflict("Every committed turn must have a confirmed model operation.")
            if operations.get("LastEvaluatedKey") or not rows or any(row.get("operation_status") != "confirmed" for row in rows):
                raise errors.state_conflict("Model operations are not all confirmed.")
            tools = self.repo._client.query(
                TableName=self.repo.table_name,
                KeyConditionExpression="event_id = :pk AND begins_with(arrived_at, :tool)",
                ExpressionAttributeValues=_serialize({":pk": task_ops_partition(identity.task_id), ":tool": "TOOL#"}),
                ConsistentRead=True,
                Limit=129,
            )
            tool_rows = [_deserialize(row) for row in tools.get("Items", [])]
            if tools.get("LastEvaluatedKey") or len(tool_rows) > 128 or any(row.get("operation_status") != "confirmed" for row in tool_rows):
                raise errors.state_conflict("Tool operations are not all confirmed.")
        result = body["result"]
        refs = body["committed_result_refs"]
        if outcome == "completed" and set(refs) != set(result.get("artifact_ids", [])):
            raise errors.state_conflict("Result artifacts differ from committed references.")
        if len(refs) != len(set(refs)):
            raise errors.invalid_request("Duplicate result artifacts.")
        timestamp = _iso(self.repo._clock())
        sequence = int(snapshot["event_sequence"]) + len(pending) + 1
        if sequence > 10000:
            raise errors.state_conflict("Task event budget is exhausted.")
        updates = {
            "state": outcome,
            "version": int(snapshot["version"]) + 1,
            "event_sequence": sequence,
            "updated_at": timestamp,
            "terminal_at": timestamp,
            "completed_at": timestamp,
            "outcome": outcome,
            "result": result,
            "error": body["error"],
            "child_exit": body["child_exit"],
            "finalize_digest": final_digest,
            "terminal_event_id": f"{identity.task_id}:{sequence}",
            "content_expires_at": int((self.repo._clock() + timedelta(days=30)).timestamp()),
            "expires_at": int((self.repo._clock() + timedelta(days=90)).timestamp()),
        }
        if no_child:
            if snapshot.get("runtime_attempt_id") is not None or outcome != "cancelled":
                raise errors.state_conflict("Task runtime already started.")
            updates["runtime_not_started"] = True
            updates["recovery_required"] = False
        transaction = [self._meta(snapshot, updates, attempt=identity.runtime_attempt_id)]
        if no_child:
            grant = self.repo._get_authority(
                task_authority_partition(identity.tenant),
                task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation),
            )
            if not grant or grant.get("runtime_attempt_id") is not None:
                raise errors.state_conflict("Task runtime already started.")
            values = {":task": identity.task_id, ":true": True, ":null": "NULL"}
            condition = "task_id = :task AND (attribute_not_exists(runtime_attempt_id) OR attribute_type(runtime_attempt_id, :null))"
            if grant.get("workload_uid") is None:
                condition += " AND attribute_not_exists(workload_uid)"
            else:
                condition += " AND workload_uid = :pod"
                values[":pod"] = grant["workload_uid"]
            transaction.append(
                {
                    "Update": {
                        "TableName": self.repo.authority_table_name,
                        "Key": _serialize(
                            {
                                "pk": task_authority_partition(identity.tenant),
                                "sk": task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation),
                            }
                        ),
                        "UpdateExpression": "SET runtime_start_cancelled = :true, execution_capacity_released = :true",
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": _serialize(values),
                    }
                }
            )
            if not grant.get("execution_capacity_released", False):
                for capacity_key in grant.get("execution_capacity_keys", []):
                    transaction.append(
                        {
                            "Update": {
                                "TableName": self.repo.authority_table_name,
                                "Key": _serialize({"pk": capacity_key, "sk": "ACTIVE"}),
                                "UpdateExpression": "REMOVE reservations.#task ADD active_count :minus",
                                "ConditionExpression": "reservations.#task = :invocation AND active_count > :zero",
                                "ExpressionAttributeNames": {"#task": identity.task_id},
                                "ExpressionAttributeValues": _serialize({":invocation": identity.invocation_id, ":minus": -1, ":zero": 0}),
                            }
                        }
                    )
        elif stop_only:
            # Stop-only credentials can no longer spend/report. They can settle
            # this exact persisted attempt even after policy revocation.
            transaction.append(
                {
                    "ConditionCheck": {
                        "TableName": self.repo.authority_table_name,
                        "Key": _serialize(
                            {
                                "pk": task_authority_partition(identity.tenant),
                                "sk": task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation),
                            }
                        ),
                        "ConditionExpression": (
                            "task_id = :task AND invocation_id = :invocation AND generation = :generation AND runtime_attempt_id = :attempt"
                        ),
                        "ExpressionAttributeValues": _serialize(
                            {
                                ":task": identity.task_id,
                                ":invocation": identity.invocation_id,
                                ":generation": identity.generation,
                                ":attempt": identity.runtime_attempt_id,
                            }
                        ),
                    }
                }
            )
        else:
            transaction.extend(self.repo._authority_condition_checks(snapshot=snapshot, runtime_attempt_id=identity.runtime_attempt_id))
        if not no_child and body["child_exit"]["confirmed"]:
            self._release_execution_on_finalize(identity, transaction)
        total_bytes = 0
        retained_artifacts = set(snapshot.get("artifact_ids", [])) | set(refs) | set(snapshot.get("result_artifact_ids", []))
        for artifact_id in retained_artifacts:
            artifact = self.repo._get(task_artifact_partition(artifact_id), META_SORT_KEY)
            if not artifact or artifact.get("task_id") != identity.task_id or artifact.get("scope") != snapshot["scope"]:
                raise errors.not_found()
            if artifact_id in refs:
                if artifact.get("artifact_kind") != "result":
                    raise errors.state_conflict("Result reference does not name a result artifact.")
                total_bytes += int(artifact["size_bytes"])
            transaction.append(
                {
                    "Update": {
                        "TableName": self.repo.table_name,
                        "Key": _serialize({"event_id": task_artifact_partition(artifact_id), "arrived_at": META_SORT_KEY}),
                        "UpdateExpression": "SET terminal_at = :now, expires_at = :expires",
                        "ConditionExpression": "task_id = :task AND content_sha256 = :hash",
                        "ExpressionAttributeValues": _serialize(
                            {
                                ":task": identity.task_id,
                                ":hash": artifact["content_sha256"],
                                ":now": timestamp,
                                ":expires": updates["content_expires_at"],
                            }
                        ),
                    }
                }
            )
        if total_bytes > 1048576:
            raise errors.payload_too_large("Result artifacts exceed one MiB.")
        for offset, row in enumerate(pending, 1):
            settled_status = "cancelled" if outcome == "cancelled" else "rejected"
            transaction.append(
                self._put(
                    self.repo._event_item(
                        task_id=identity.task_id,
                        scope=snapshot["scope"],
                        sequence=int(snapshot["event_sequence"]) + offset,
                        kind="command.updated",
                        invocation_id=identity.invocation_id,
                        generation=identity.generation,
                        timestamp=timestamp,
                        runtime_attempt_id=identity.runtime_attempt_id,
                        data={"command_id": row["command_id"], "command_status": settled_status, "handoff": row["handoff"]},
                    )
                )
            )
            transaction.append(
                {
                    "Update": {
                        "TableName": self.repo.table_name,
                        "Key": _serialize({"event_id": task_commands_partition(identity.task_id), "arrived_at": command_sort_key(row["command_id"])}),
                        "UpdateExpression": "SET #status = :settled, updated_at = :now, reason = :reason",
                        "ConditionExpression": "#status = :accepted",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _serialize(
                            {
                                ":settled": "cancelled" if outcome == "cancelled" else "rejected",
                                ":now": timestamp,
                                ":reason": (
                                    "Task cancelled before any runtime attempt started."
                                    if no_child
                                    else "Task process stopped before this command was consumed."
                                ),
                                ":accepted": "accepted",
                            }
                        ),
                    }
                }
            )
        transaction.append(
            {
                "Update": {
                    "TableName": self.repo.table_name,
                    "Key": _serialize({"event_id": snapshot["idempotency_partition"], "arrived_at": META_SORT_KEY}),
                    "UpdateExpression": "SET terminal_at = :now, expires_at = :expires",
                    "ConditionExpression": "task_id = :task AND request_digest = :digest",
                    "ExpressionAttributeValues": _serialize(
                        {":now": timestamp, ":expires": updates["expires_at"], ":task": identity.task_id, ":digest": snapshot["request_digest"]}
                    ),
                }
            }
        )
        transaction.append(
            {
                "Update": {
                    "TableName": self.repo.authority_table_name,
                    "Key": _serialize({"pk": task_capacity_partition(snapshot["capacity_scope_hash"]), "sk": "ACTIVE"}),
                    "UpdateExpression": "REMOVE reservations.#task ADD active_count :minus",
                    "ConditionExpression": "reservations.#task = :reservation AND active_count > :zero",
                    "ExpressionAttributeNames": {"#task": identity.task_id},
                    "ExpressionAttributeValues": _serialize({":minus": -1, ":zero": 0, ":reservation": snapshot["capacity_reservation_id"]}),
                }
            }
        )
        event = self.repo._event_item(
            task_id=identity.task_id,
            scope=snapshot["scope"],
            sequence=sequence,
            kind=f"task.{outcome}",
            invocation_id=identity.invocation_id,
            generation=identity.generation,
            runtime_attempt_id=identity.runtime_attempt_id,
            timestamp=timestamp,
            data={"status": outcome, "version": updates["version"], "outcome": outcome},
        )
        transaction.append(self._put(event))
        self._write(transaction)
        return {
            "schema_version": "1.0",
            "task_id": identity.task_id,
            "status": outcome,
            "version": updates["version"],
            "terminal_event_id": updates["terminal_event_id"],
        }

    def settlement(self, identity: Any, body: dict, *, verified_workload: bool = False) -> dict:
        """Stop-only receipt; never upgrades process evidence into provider success."""
        snapshot = self._attempt(identity)
        pk = task_authority_partition(identity.tenant)
        sk = task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation)
        grant = self.repo._get_authority(pk, sk)
        if not grant or grant.get("runtime_attempt_id") != identity.runtime_attempt_id:
            raise errors.not_found()
        evidence = dict(body["stop_evidence"])
        prior_evidence = snapshot.get("stop_evidence") or {}
        for field in ("child_exit_confirmed", "workload_terminated"):
            evidence[field] = evidence[field] or prior_evidence.get(field, False)
        stopped = evidence["child_exit_confirmed"] or evidence["workload_terminated"]
        if stopped and snapshot["state"] not in TERMINAL:
            import uuid

            outcome = "cancelled" if snapshot["state"] == "cancel_requested" else "failed"
            error = {
                "schema_version": "1.0",
                "outcome": outcome,
                "code": "cancelled_by_client" if outcome == "cancelled" else "process_failed",
                "message": "The current task process stopped without a successful final result.",
                "committed_at": _iso(self.repo._clock()),
                "child_exit_confirmed": evidence["child_exit_confirmed"],
                "recovery_required": not evidence["child_exit_confirmed"],
                "provider_outcome": "unknown",
                "total_usd": None,
            }
            # The workload proves a stop, not an exit code, provider refund, or
            # useful result. Preserve the unavailable exit-code/signal as null.
            self.finalize(
                identity,
                {
                    "schema_version": "1.0",
                    "outcome": outcome,
                    "final_report_id": str(uuid.uuid4()),
                    "child_exit": {
                        "confirmed": evidence["child_exit_confirmed"],
                        "exit_code": None,
                        "signal": None,
                        "stopped_at": evidence["observed_at"],
                    },
                    "result": None,
                    "error": error,
                    "committed_result_refs": [],
                },
                stop_only=True,
            )
            snapshot = self._attempt(identity)
            grant = self.repo._get_authority(pk, sk)
            if not grant or grant.get("runtime_attempt_id") != identity.runtime_attempt_id:
                raise errors.not_found()
        timestamp = _iso(self.repo._clock())
        # A receipt may advance unknown -> confirmed; confirmed acknowledgement
        # is monotonic and cannot be overwritten by an older retry.
        ack = snapshot.get("queue_ack_status", "pending")
        if ack != "confirmed":
            ack = body["queue_ack_status"]
        updates = {"version": int(snapshot["version"]) + 1, "updated_at": timestamp, "stop_evidence": evidence, "queue_ack_status": ack}
        if verified_workload and evidence["workload_terminated"]:
            updates["server_workload_terminated"] = True
        transaction = [self._meta(snapshot, updates, attempt=identity.runtime_attempt_id)]
        values = {":attempt": identity.runtime_attempt_id}
        condition = "runtime_attempt_id = :attempt"
        if stopped and not grant.get("execution_capacity_released", False):
            values.update({":released": True, ":false": False})
            condition += " AND (attribute_not_exists(execution_capacity_released) OR execution_capacity_released = :false)"
            transaction.append(
                {
                    "Update": {
                        "TableName": self.repo.authority_table_name,
                        "Key": _serialize({"pk": pk, "sk": sk}),
                        "UpdateExpression": "SET execution_capacity_released = :released",
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": _serialize(values),
                    }
                }
            )
            for capacity_key in grant.get("execution_capacity_keys", []):
                transaction.append(
                    {
                        "Update": {
                            "TableName": self.repo.authority_table_name,
                            "Key": _serialize({"pk": capacity_key, "sk": "ACTIVE"}),
                            "UpdateExpression": "REMOVE reservations.#task ADD active_count :minus",
                            "ConditionExpression": "reservations.#task = :invocation AND active_count > :zero",
                            "ExpressionAttributeNames": {"#task": identity.task_id},
                            "ExpressionAttributeValues": _serialize({":invocation": identity.invocation_id, ":minus": -1, ":zero": 0}),
                        }
                    }
                )
        else:
            transaction.append(
                {
                    "ConditionCheck": {
                        "TableName": self.repo.authority_table_name,
                        "Key": _serialize({"pk": pk, "sk": sk}),
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": _serialize(values),
                    }
                }
            )
        self._write(transaction)
        return {"schema_version": "1.0", "operation_status": "confirmed" if stopped else "unknown", "stop_only": True, "queue_ack_status": ack}

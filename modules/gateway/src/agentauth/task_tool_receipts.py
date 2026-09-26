"""Attempt-fenced tool receipts in the existing Task operation partition.

No route is enabled by this storage component. A trusted host must claim before
execution and settle only its own receipt. Existing claims never authorize replay.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

import rfc8785
from botocore.exceptions import ClientError

from src.agentauth.task_tool_policy import TOOL_PATTERN
from src.tasks.records import base_item, model_operation_sort_key, payload_digest, task_ops_partition, task_partition
from src.tasks.store import TaskStoreError, _serialize


class TaskToolReceipts:
    def __init__(self, repository, *, authorize, catalogue, clock=None):
        self.repository = repository
        self.authorize = authorize
        # Deployment-owned mapping: gateway tool permission -> SDK function name.
        # Neither the model nor a claim request can choose this mapping.
        self.catalogue = dict(catalogue)
        if (
            not callable(authorize)
            or len(self.catalogue) > 64
            or len(set(self.catalogue.values())) != len(self.catalogue)
            or any(
                not isinstance(tool, str)
                or not re.fullmatch(TOOL_PATTERN, tool)
                or not isinstance(name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
                for tool, name in self.catalogue.items()
            )
        ):
            raise TaskStoreError("invalid reviewed tool catalogue")
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _key(call_id):
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 200:
            raise TaskStoreError("invalid tool call identity")
        return "TOOL#" + hashlib.sha256(call_id.encode()).hexdigest()

    def read(self, task_id, call_id):
        return self.repository._get(task_ops_partition(task_id), self._key(call_id))

    def _current(self, identity, tool):
        if tool not in self.catalogue:
            raise TaskStoreError("tool catalogue permission unavailable")
        self.authorize(identity, tool)
        task = self.repository.read_task(identity.task_id)
        if not task or task.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}:
            raise TaskStoreError("tool task scope changed")
        if (task["invocation_id"], int(task["generation"]), task.get("runtime_attempt_id")) != (
            identity.invocation_id,
            identity.generation,
            identity.runtime_attempt_id,
        ):
            raise TaskStoreError("tool attempt changed")
        if task["state"] not in {"accepted", "queued", "running", "waiting_for_input"} or task["deadline_at"] <= self.clock().strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ):
            raise TaskStoreError("task no longer authorizes tool execution")
        if tool not in task.get("tool_grants", []):
            raise TaskStoreError("tool was not frozen at admission")
        self.repository.resolve_work(task["dispatch_id"], expected_kind="dispatch")
        return task

    def _fences(self, task, identity):
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
            *self.repository._authority_condition_checks(snapshot=task, runtime_attempt_id=identity.runtime_attempt_id),
        ]

    def _metadata_conflict(self, error, task):
        reasons = error.response.get("CancellationReasons", [])
        if (
            len(reasons) < 2
            or reasons[1].get("Code") != "ConditionalCheckFailed"
            or any(reason.get("Code") not in {None, "None"} for index, reason in enumerate(reasons) if index != 1)
        ):
            return False
        current = self.repository.read_task(task["task_id"])
        stable = ("state", "invocation_id", "generation", "runtime_attempt_id", "scope", "deadline_at", "grant_digest", "tool_grants")
        return bool(current) and all(current.get(key) == task.get(key) for key in stable)

    def claim(self, *, identity, turn_id, call_id, tool, arguments, _conflicts=0):
        task = self._current(identity, tool)
        try:
            canonical_arguments = rfc8785.dumps(arguments)
            if not isinstance(arguments, dict) or len(canonical_arguments) > 32768:
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise TaskStoreError("invalid tool arguments") from None
        model = self.repository._get(task_ops_partition(identity.task_id), model_operation_sort_key(turn_id))
        if (
            not model
            or model.get("operation_status") != "confirmed"
            or (model.get("invocation_id"), int(model.get("generation", 0)), model.get("runtime_attempt_id"))
            != (identity.invocation_id, identity.generation, identity.runtime_attempt_id)
        ):
            raise TaskStoreError("tool requires a confirmed model operation from this attempt")
        calls = [item for item in model.get("responses_response", {}).get("output", []) if item.get("type") == "function_call"]
        if len(calls) != 1:
            raise TaskStoreError("tool requires one serial confirmed model call")
        call = calls[0]
        if call.get("call_id") != call_id or call.get("namespace") != "mcp__adp" or call.get("name") != self.catalogue[tool]:
            raise TaskStoreError("tool does not match confirmed model call")
        try:
            if payload_digest(json.loads(call["arguments"])) != payload_digest(arguments):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise TaskStoreError("tool arguments differ from confirmed model call") from None
        binding = {
            "turn_id": turn_id,
            "call_id": call_id,
            "tool": tool,
            "arguments_json": canonical_arguments.decode("utf-8"),
            "invocation_id": identity.invocation_id,
            "generation": identity.generation,
            "runtime_attempt_id": identity.runtime_attempt_id,
        }
        digest = payload_digest(binding)
        existing = self.read(identity.task_id, call_id)
        if existing:
            if existing.get("request_digest") != digest:
                raise TaskStoreError("tool operation identity conflict")
            return existing, False
        row = base_item(partition=task_ops_partition(identity.task_id), sort_key=self._key(call_id), record_type="TASK_OPS", scope=task["scope"]) | {
            **binding,
            "task_id": identity.task_id,
            "request_digest": digest,
            "operation_status": "pending",
            "owner_token": str(uuid.uuid4()),
            "automatic_replay_permitted": False,
            "claimed_at": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        transaction = [
            {"Put": {"TableName": self.repository.table_name, "Item": _serialize(row), "ConditionExpression": "attribute_not_exists(event_id)"}},
            *self._fences(task, identity),
            {
                "ConditionCheck": {
                    "TableName": self.repository.table_name,
                    "Key": _serialize({"event_id": task_ops_partition(identity.task_id), "arrived_at": model_operation_sort_key(turn_id)}),
                    "ConditionExpression": (
                        "#status = :confirmed AND request_digest = :digest AND responses_response = :response AND runtime_attempt_id = :attempt"
                    ),
                    "ExpressionAttributeNames": {"#status": "operation_status"},
                    "ExpressionAttributeValues": _serialize(
                        {
                            ":confirmed": "confirmed",
                            ":digest": model["request_digest"],
                            ":response": model["responses_response"],
                            ":attempt": identity.runtime_attempt_id,
                        }
                    ),
                }
            },
        ]
        try:
            self.repository._client.transact_write_items(TransactItems=transaction)
            return row, True
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            existing = self.read(identity.task_id, call_id)
            if existing and existing.get("request_digest") == digest:
                return existing, False
            if _conflicts < 3 and self._metadata_conflict(error, task):
                # A rejected transaction performed no effect. Re-read current
                # authority and Task version; never repeat an uncertain send.
                return self.claim(identity=identity, turn_id=turn_id, call_id=call_id, tool=tool, arguments=arguments, _conflicts=_conflicts + 1)
            raise TaskStoreError("tool claim lost current authority") from None

    def verify_history(self, *, identity, turn_id, history):
        """Verify the complete ordered tool transcript before model continuation.

        Reads canonical turns and immutable receipts, never child-supplied turn
        lists. The caller must fence its model claim to current Task authority.
        This verifies executable history only, not semantic grounding of prose.
        """
        from src.agentauth.task_turns import TaskTurnStore

        if not isinstance(history, list) or len(history) > 256:
            raise TaskStoreError("invalid tool history")
        turns = TaskTurnStore(self.repository).list_turns(identity.task_id)
        if not turns or turns[-1]["turn_id"] != turn_id:
            raise TaskStoreError("tool history requires the latest canonical turn")
        binding = (identity.invocation_id, identity.generation, identity.runtime_attempt_id)
        seen = set()
        index = 0
        for turn in turns:
            if (turn.get("invocation_id"), int(turn.get("generation", 0)), turn.get("runtime_attempt_id")) != binding:
                raise TaskStoreError("tool history turn belongs to another attempt")
            if turn["turn_id"] == turn_id:
                break
            model = self.repository._get(task_ops_partition(identity.task_id), model_operation_sort_key(turn["turn_id"]))
            if (
                not model
                or model.get("operation_status") != "confirmed"
                or (model.get("invocation_id"), int(model.get("generation", 0)), model.get("runtime_attempt_id")) != binding
            ):
                raise TaskStoreError("tool history has an unconfirmed model turn")
            calls = [item for item in model.get("responses_response", {}).get("output", []) if item.get("type") == "function_call"]
            if len(calls) > 1:
                raise TaskStoreError("parallel tool history unavailable")
            for call in calls:
                call_id = call.get("call_id")
                if call_id in seen or index + 2 > len(history):
                    raise TaskStoreError("incomplete or duplicate tool history")
                seen.add(call_id)
                row = self.read(identity.task_id, call_id)
                if (
                    not row
                    or row.get("operation_status") != "confirmed"
                    or row.get("turn_id") != turn["turn_id"]
                    or row.get("task_id") != identity.task_id
                    or (row.get("invocation_id"), int(row.get("generation", 0)), row.get("runtime_attempt_id")) != binding
                    or row.get("automatic_replay_permitted") is not False
                    or not isinstance(row.get("content"), str)
                    or len(row["content"].encode()) > 32768
                    or type(row.get("is_error")) is not bool
                ):
                    raise TaskStoreError("tool history requires a confirmed bound receipt")
                self._current(identity, row["tool"])
                supplied_call, supplied_result = history[index : index + 2]
                index += 2
                def canonical_call(value):
                    if not isinstance(value, dict) or value.get("status") not in {None, "completed"}:
                        return None
                    normalized = {key: item for key, item in value.items() if key not in {"id", "status"}}
                    try:
                        normalized["arguments"] = rfc8785.dumps(json.loads(normalized["arguments"])).decode()
                    except (KeyError, ValueError, TypeError):
                        return None
                    return normalized
                # The SDK may omit completed status and reformat JSON. Compare
                # the same canonical arguments that authorize actual execution;
                # identity, namespace, tool, values and result remain exact.
                expected_call = canonical_call(call)
                if (
                    not isinstance(supplied_call, dict)
                    or expected_call is None or canonical_call(supplied_call) != expected_call
                    or call.get("namespace") != "mcp__adp"
                    or call.get("name") != self.catalogue[row["tool"]]
                ):
                    raise TaskStoreError("tool history call differs from confirmed model")
                try:
                    arguments_json = rfc8785.dumps(json.loads(call["arguments"])).decode()
                except (ValueError, TypeError, KeyError):
                    raise TaskStoreError("invalid model tool arguments") from None
                expected_binding = {
                    "turn_id": turn["turn_id"],
                    "call_id": call_id,
                    "tool": row["tool"],
                    "arguments_json": arguments_json,
                    "invocation_id": identity.invocation_id,
                    "generation": identity.generation,
                    "runtime_attempt_id": identity.runtime_attempt_id,
                }
                if row.get("arguments_json") != arguments_json or row.get("request_digest") != payload_digest(expected_binding):
                    raise TaskStoreError("tool history receipt digest differs")
                if (
                    not isinstance(supplied_result, dict)
                    or set(supplied_result) != {"type", "call_id", "output"}
                    or supplied_result["type"] != "function_call_output"
                    or supplied_result["call_id"] != call_id
                ):
                    raise TaskStoreError("tool history result identity differs")
                parts = supplied_result["output"]
                if (
                    not isinstance(parts, list)
                    or len(parts) != 2
                    or any(not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "input_text" for part in parts)
                    or not isinstance(parts[0]["text"], str)
                    or not re.fullmatch(r"Wall time: [0-9]{1,8}(?:\.[0-9]{1,9})? seconds\nOutput:", parts[0]["text"])
                    or parts[1]["text"] != row["content"]
                ):
                    raise TaskStoreError("tool history output differs from confirmed receipt")
        if index != len(history):
            raise TaskStoreError("tool history includes unconfirmed calls")
        return True

    def settle(self, *, identity, call_id, owner_token, status, content=None, is_error=False, _conflicts=0):
        if status not in {"confirmed", "unknown", "rejected"} or type(is_error) is not bool:
            raise TaskStoreError("invalid tool outcome")
        if (status == "confirmed" and (not isinstance(content, str) or len(content.encode()) > 32768)) or (
            status != "confirmed" and content is not None
        ):
            raise TaskStoreError("invalid tool receipt content")
        row = self.read(identity.task_id, call_id)
        if not row or row.get("owner_token") != owner_token:
            raise TaskStoreError("tool receipt owner differs")
        task = self._current(identity, row["tool"])
        if (row["invocation_id"], int(row["generation"]), row["runtime_attempt_id"]) != (
            identity.invocation_id,
            identity.generation,
            identity.runtime_attempt_id,
        ):
            raise TaskStoreError("tool receipt belongs to a different attempt")
        outcome = {"operation_status": status, "content": content, "is_error": is_error}
        if row["operation_status"] != "pending":
            if any(row.get(key) != value for key, value in outcome.items()):
                raise TaskStoreError("tool receipt is immutable")
            return row
        updated = {**row, **outcome, "completed_at": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")}
        try:
            self.repository._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.repository.table_name,
                            "Item": _serialize(updated),
                            "ConditionExpression": "#status = :pending AND owner_token = :owner AND request_digest = :digest",
                            "ExpressionAttributeNames": {"#status": "operation_status"},
                            "ExpressionAttributeValues": _serialize({":pending": "pending", ":owner": owner_token, ":digest": row["request_digest"]}),
                        }
                    },
                    *self._fences(task, identity),
                ]
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "TransactionCanceledException" and _conflicts < 3 and self._metadata_conflict(error, task):
                return self.settle(
                    identity=identity,
                    call_id=call_id,
                    owner_token=owner_token,
                    status=status,
                    content=content,
                    is_error=is_error,
                    _conflicts=_conflicts + 1,
                )
            raise TaskStoreError("tool receipt settlement could not be confirmed") from None
        return updated

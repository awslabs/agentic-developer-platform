"""Durable, owner-bound tool executions in the separately provisioned tools namespace."""

import base64
import hashlib
import json
import os
import time
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from tools_facade.credential_results import PRIVATE_DELIVERY_REQUIRED, private_result, require_generic_tool
from tools_facade.encoding import canonical
from tools_facade.knowledge_memory import MEMORY_TOOLS, completion_matches

ADMISSION_KEY = "CONTROL#admission-off"
STATELESS_READ_TOOLS = frozenset({
    "websearch___search", "knowledge___search", "knowledge___understand",
    "knowledge___impact", "knowledge___browse", "knowledge___secure",
})
SYNCHRONOUS_TOOLS = STATELESS_READ_TOOLS | MEMORY_TOOLS
ATLASSIAN_WRITES = frozenset({
    "atlassian___create_jira_issue", "atlassian___update_jira_issue",
    "atlassian___comment_jira_issue", "atlassian___transition_jira_issue",
})
WRITE_REPORT_PARTITION = "atlassian-write-report"
WRITE_READBACK_PARTITION = "atlassian-write-readback"


class ExecutionStore:
    def __init__(self, client=None, table=None, clock=None):
        self.client = client
        self.table = table
        self.clock = clock or time.time

    def _table(self):
        name = self.table or os.environ.get("ADP_TOOLS_EXECUTION_TABLE")
        if not name:
            raise HTTPException(503, "tool execution store unavailable")
        return name

    def _client(self):
        return self.client or boto3.client("dynamodb", region_name=os.environ["AWS_REGION"], config=Config(
            connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))

    def _admission_guard(self):
        return {"ConditionCheck": {
            "TableName": self._table(), "Key": {"execution_id": {"S": ADMISSION_KEY}},
            "ConditionExpression": "attribute_not_exists(execution_id)",
        }}

    def _admission_open(self):
        return not self._client().get_item(TableName=self._table(),
            Key={"execution_id": {"S": ADMISSION_KEY}}, ConsistentRead=True).get("Item")

    @staticmethod
    def _permits(item, tool):
        try:
            original = item["tool"]["S"]
            grants = json.loads(item["frozen_grants"]["S"])
            return (isinstance(grants, list) and tool in grants and
                    (tool == original or original == "code_interpreter___start" and
                     tool.startswith("code_interpreter___") or original == "browser___start" and
                     tool.startswith("browser___")))
        except (KeyError, ValueError, TypeError):
            return False

    def admit(self, execution_id: UUID | None, tool: str, decision) -> str:
        require_generic_tool(tool)
        now = int(self.clock())
        owner = decision.principal
        binding = (f"run:{decision.run_id}:{decision.workload_id}" if decision.run_id else "human")
        if decision.run_id and (not decision.workload_id or not decision.run_expires_at):
            raise HTTPException(503, "tool authority unavailable")
        if execution_id is None:
            deadline = min(now + 900, decision.run_expires_at or now + 900)
            if deadline <= now:
                raise HTTPException(403, "tool authority refused")
            identifier = str(uuid4())
            item = self._execution_item(identifier, tool, owner, binding, deadline)
            try:
                self._client().transact_write_items(TransactItems=[
                    self._admission_guard(), {"Put": {
                        "TableName": self._table(), "Item": item,
                        "ConditionExpression": "attribute_not_exists(execution_id)",
                    }},
                ])
            except (ClientError, BotoCoreError):
                try:
                    if not self._admission_open():
                        raise HTTPException(403, "tool admission disabled") from None
                except (ClientError, BotoCoreError):
                    pass
                raise HTTPException(503, "tool execution store unavailable") from None
            return identifier

        identifier = str(execution_id)
        try:
            reply = self._client().get_item(TableName=self._table(), Key={"execution_id": {"S": identifier}},
                                            ConsistentRead=True)
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None

        item = reply.get("Item", {})
        if any(item.get(field, {}).get("S") != value for field, value in (
            ("schema_version", "1.0"), ("tenant_id", owner.tenant_id), ("owner_id", owner.user_id),
            ("team_id", owner.team_id), ("binding", binding),
        )) or not self._permits(item, tool):
            raise HTTPException(404, "not found")
        native = getattr(decision, "native_admission", None)
        if native is not None and (item.get("native_test_run_id", {}).get("S") != decision.run_id
                                   or str(execution_id) != native.execution_id):
            raise HTTPException(404, "not found")
        if (item.get("state", {}).get("S") != "active" or
                int(item.get("deadline_at", {}).get("N", "0")) <= now or
                decision.run_expires_at is not None and decision.run_expires_at <= now):
            raise HTTPException(403, "tool execution closed")
        return identifier

    @staticmethod
    def _execution_item(identifier, tool, owner, binding, deadline, frozen_grants=None):
        return {
            "execution_id": {"S": identifier}, "schema_version": {"S": "1.0"},
            "tenant_id": {"S": owner.tenant_id}, "owner_id": {"S": owner.user_id},
            "team_id": {"S": owner.team_id}, "binding": {"S": binding},
            "tool": {"S": tool}, "frozen_grants": {"S": json.dumps(frozen_grants or [tool])},
            "deadline_at": {"N": str(deadline)}, "retention_ttl": {"N": str(deadline + 30 * 86400)},
            "generation": {"N": "1"}, "state": {"S": "active"},
            "cleanup_partition": {"S": "due"}, "next_cleanup_at": {"N": str(deadline)},
            "operation_tracking": {"S": "1"}, "accepted_operations": {"N": "0"},
            "unresolved_operations": {"N": "0"},
        }

    def claim(self, execution_id: UUID | None, request_id: UUID, tool: str, arguments: dict, decision,
              frozen_grants=None, ttl_seconds=None):
        require_generic_tool(tool)
        now = int(self.clock())
        owner = decision.principal
        native = getattr(decision, "native_admission", None)
        if native is not None:
            handoff = getattr(decision, "_native_handoff", None)
            from tools_facade.native_admission import NativeFacadeHandoff

            if type(handoff) is not NativeFacadeHandoff or handoff.admission is not native:
                raise HTTPException(403, "native tool handoff required")
            handoff.require_fresh()
        operation_key = f"OP#{owner.tenant_id}#{owner.user_id}#{tool}#{request_id}"
        if ttl_seconds is not None and (type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900 or
                                        tool not in {"browser___start", "code_interpreter___start"}):
            raise HTTPException(422, "invalid session lifetime")
        digest = hashlib.sha256(canonical(
            {"arguments": arguments, "ttl_seconds": ttl_seconds} if ttl_seconds is not None else arguments
        )).hexdigest()
        if tool in ATLASSIAN_WRITES:
            try:
                existing = self._client().get_item(
                    TableName=self._table(), Key={"execution_id": {"S": operation_key}}, ConsistentRead=True,
                ).get("Item")
            except (ClientError, BotoCoreError):
                raise HTTPException(503, "tool execution store unavailable") from None
            if existing:
                self.admit(UUID(existing["operation_execution_id"]["S"]), tool, decision)
                if execution_id is not None and existing["operation_execution_id"]["S"] != str(execution_id):
                    raise HTTPException(404, "not found")
                if existing.get("request_digest", {}).get("S") != digest:
                    raise HTTPException(409, "request id payload conflict")
                return operation_key, existing, False
        if ttl_seconds is not None and execution_id is not None:
            self.admit(execution_id, tool, decision)
            try:
                existing = self._client().get_item(TableName=self._table(),
                    Key={"execution_id": {"S": operation_key}}, ConsistentRead=True).get("Item", {})
            except (ClientError, BotoCoreError):
                raise HTTPException(503, "tool execution store unavailable") from None
            if not existing:
                raise HTTPException(422, "session lifetime cannot be changed")
            if existing.get("operation_execution_id", {}).get("S") != str(execution_id):
                raise HTTPException(404, "not found")
            if existing.get("request_digest", {}).get("S") != digest:
                raise HTTPException(409, "request id payload conflict")
            return operation_key, existing, False
        identifier = native.execution_id if native is not None else str(execution_id or uuid4())
        if execution_id is not None:
            self.admit(execution_id, tool, decision)
            if tool in {"browser___start", "code_interpreter___start"}:
                try:
                    root = self._client().get_item(TableName=self._table(),
                        Key={"execution_id": {"S": identifier}}, ConsistentRead=True)["Item"]
                    deadline = int(root["deadline_at"]["N"])
                except (ClientError, BotoCoreError, KeyError, ValueError):
                    raise HTTPException(503, "tool execution store unavailable") from None
        else:
            if decision.run_id and (not decision.workload_id or not decision.run_expires_at):
                raise HTTPException(503, "tool authority unavailable")
            deadline = (native.session_deadline_at if native is not None else
                        min(now + (ttl_seconds or 900), decision.run_expires_at or now + 900))
            if deadline <= now:
                raise HTTPException(403, "tool authority refused")
        operation = {
            "execution_id": {"S": operation_key}, "schema_version": {"S": "1.0"},
            "owner_id": {"S": owner.user_id}, "tenant_id": {"S": owner.tenant_id},
            "team_id": {"S": owner.team_id}, "binding": {"S":
                f"run:{decision.run_id}:{decision.workload_id}" if decision.run_id else "human"},
            "tool": {"S": tool}, "request_id": {"S": str(request_id)},
            "request_digest": {"S": digest}, "operation_execution_id": {"S": identifier},
            "receipt_id": {"S": decision.receipt_id}, "status": {"S": "pending"},
            "dispatch_intent": {"S": "claimed"}, "retention_ttl": {"N": str(now + 31 * 86400)},
        }
        if tool in ATLASSIAN_WRITES:
            connection = decision.atlassian_connection
            approval = decision.atlassian_approval
            try:
                release = json.loads(os.environ["ADP_TOOL_ADAPTER_RELEASE"])
                if (not isinstance(connection, dict)
                        or set(connection) != {"id", "revision", "site_id"}
                        or connection.get("id") != decision.connection_id
                        or not all(isinstance(value, str) and value for value in connection.values())
                        or not isinstance(arguments.get("cloudId"), str)
                        or arguments["cloudId"] != connection["site_id"]
                        or not isinstance(release, dict)
                        or not isinstance(approval, dict)
                        or set(approval) != {
                            "tool", "request_id", "payload_sha256", "policy_revision", "consent_source"
                        }
                        or approval["tool"] != tool
                        or approval["request_id"] != str(request_id)
                        or approval["payload_sha256"] != digest
                        or type(approval["policy_revision"]) is not int
                        or approval["policy_revision"] < 0
                        or approval["consent_source"] != "explicit"):
                    raise ValueError("unbound Jira write")
            except (KeyError, ValueError, TypeError):
                raise HTTPException(403, "Jira write authority unavailable") from None
            locator = arguments.get("issueIdOrKey", arguments.get("issueId"))
            operation["atlassian_binding"] = {"S": json.dumps({
                "connection": connection, "release": release, "approval": approval,
                "readback_locator": {"cloudId": connection["site_id"], "issueIdOrKey": locator}
                if isinstance(locator, str) and 0 < len(locator) <= 512 else None,
            })}
        if (tool in MEMORY_TOOLS and native is None
                and (not decision.run_id or getattr(decision, "host_generation", None) is not None)):
            operation["memory_contract"] = {"S": "host" if decision.run_id else "1"}
        if tool in {"browser___start", "code_interpreter___start"}:
            operation["execution_deadline_at"] = {"N": str(deadline)}
        if native is not None:
            operation["native_test_run_id"] = {"S": decision.run_id}
            operation["native_test_reservation_id"] = {"S": native.reservation_id}
            operation["native_test_operation_id"] = {"S": native.operation_id}
        transaction = [self._admission_guard()]
        if execution_id is None:
            execution_item = self._execution_item(identifier, tool, owner, operation["binding"]["S"],
                                                  deadline, frozen_grants)
            execution_item["original_request_id"] = {"S": str(request_id)}
            execution_item["accepted_operations"] = {"N": "1"}
            execution_item["unresolved_operations"] = {"N": "1"}
            if "memory_contract" in operation:
                execution_item["memory_contract"] = operation["memory_contract"]
            if native is not None:
                execution_item.update({
                    "native_test_run_id": {"S": decision.run_id},
                    "native_test_reservation_id": {"S": native.reservation_id},
                    "native_test_operation_id": {"S": native.operation_id},
                    "native_start_request_id": {"S": str(request_id)},
                    "native_cleanup_deadline_at": {"N": str(native.cleanup_deadline_at)},
                    "native_cleanup_attempts_remaining": {"N": str(native.cleanup_attempts)},
                })
            transaction.append({"Put": {"TableName": self._table(),
                                        "Item": execution_item,
                                        "ConditionExpression": "attribute_not_exists(execution_id)"}})
        else:
            transaction.append({"Update": {
                "TableName": self._table(), "Key": {"execution_id": {"S": identifier}},
                "UpdateExpression": "ADD accepted_operations :one, unresolved_operations :one",
                "ConditionExpression": "#state = :active AND deadline_at > :now",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":active": {"S": "active"}, ":now": {"N": str(now)},
                                              ":one": {"N": "1"}},
            }})
        transaction.append({"Put": {"TableName": self._table(), "Item": operation,
                                    "ConditionExpression": "attribute_not_exists(execution_id)"}})
        attempts = 1 if native is not None else 5
        for attempt in range(attempts):
            try:
                self._client().transact_write_items(TransactItems=transaction)
                return operation_key, operation, True
            except (ClientError, BotoCoreError):
                try:
                    existing = self._client().get_item(TableName=self._table(),
                        Key={"execution_id": {"S": operation_key}}, ConsistentRead=True).get("Item")
                    admission_open = self._admission_open() if not existing else True
                except (ClientError, BotoCoreError):
                    raise HTTPException(503, "tool execution store unavailable") from None
                if existing:
                    self.admit(UUID(existing["operation_execution_id"]["S"]), tool, decision)
                    if execution_id is not None and existing["operation_execution_id"]["S"] != identifier:
                        raise HTTPException(404, "not found")
                    if existing.get("request_digest", {}).get("S") != digest:
                        raise HTTPException(409, "request id payload conflict")
                    return operation_key, existing, False
                if not admission_open:
                    raise HTTPException(403, "tool admission disabled") from None
                if attempt + 1 < attempts:
                    time.sleep(0.01 * (attempt + 1))
        raise HTTPException(503, "tool execution store unavailable")

    def claim_native_cleanup(self, execution_id, request_id, arguments, decision):
        """Journal direct Interpreter close under the original cleanup CAS.

        This does not admit a new paid operation, renew the root deadline, or
        consume another cleanup allowance. The gateway already claimed it.
        """
        from tools_facade.native_admission import NativeCleanupHandoff

        handoff = getattr(decision, "_native_handoff", None)
        cleanup = decision.native_cleanup
        if (type(handoff) is not NativeCleanupHandoff or handoff.admission is not cleanup
                or cleanup is None or cleanup.execution_id != str(execution_id)
                or self.root_tool(execution_id, "code_interpreter___close", decision) != "code_interpreter___start"):
            raise HTTPException(403, "original native cleanup required")
        handoff.require_fresh()
        try:
            root = self._client().get_item(TableName=self._table(),
                Key={"execution_id": {"S": str(execution_id)}}, ConsistentRead=True)["Item"]
            if (root["native_test_run_id"]["S"] != decision.run_id
                    or root["native_test_reservation_id"]["S"] != cleanup.reservation_id
                    or root["native_test_operation_id"]["S"] != cleanup.original_operation_id
                    or int(root["native_cleanup_deadline_at"]["N"]) <= int(self.clock())):
                raise HTTPException(403, "original native cleanup required")
            original = self.lookup(execution_id, UUID(root["native_start_request_id"]["S"]),
                                   "code_interpreter___start", decision)
            if (original["status"]["S"] != "confirmed" or original["result_status"]["S"] != "success"
                    or original["receipt_id"]["S"] != cleanup.reservation_id
                    or original["native_test_operation_id"]["S"] != cleanup.original_operation_id):
                raise HTTPException(403, "original native start unconfirmed")
            result = json.loads(original["result_json"]["S"])
            for _ in range(5):
                if not isinstance(result, dict) or "session_id" in result:
                    break
                result = result.get("result")
            if (not isinstance(result, dict) or not isinstance(result.get("session_id"), str)
                    or arguments != {"session_id": result["session_id"]}):
                raise HTTPException(404, "not found")
            owner = decision.principal
            key = f"OP#{owner.tenant_id}#{owner.user_id}#code_interpreter___close#{request_id}"
            digest = hashlib.sha256(canonical(arguments)).hexdigest()
            operation = {
                "execution_id": {"S": key}, "schema_version": {"S": "1.0"},
                "tenant_id": root["tenant_id"], "owner_id": root["owner_id"], "team_id": root["team_id"],
                "binding": root["binding"], "tool": {"S": "code_interpreter___close"},
                "request_id": {"S": str(request_id)}, "request_digest": {"S": digest},
                "operation_execution_id": {"S": str(execution_id)}, "receipt_id": {"S": cleanup.reservation_id},
                "status": {"S": "unknown"}, "dispatch_intent": {"S": "dispatched"},
                "native_test_run_id": root["native_test_run_id"],
                "native_test_reservation_id": root["native_test_reservation_id"],
                "native_cleanup_operation_id": {"S": cleanup.operation_id},
                "cleanup_session_id": {"S": result["session_id"]},
                "retention_ttl": {"N": str(int(self.clock()) + 31 * 86400)},
            }
            handoff.require_fresh()
            try:
                self._client().transact_write_items(TransactItems=[
                    {"Update": {"TableName": self._table(), "Key": {"execution_id": {"S": str(execution_id)}},
                        "UpdateExpression": "ADD accepted_operations :one, unresolved_operations :one",
                        "ConditionExpression": ("native_test_run_id = :run AND native_test_reservation_id = :hold "
                            "AND native_test_operation_id = :original AND native_cleanup_deadline_at > :now "
                            "AND owner_id = :owner AND tenant_id = :tenant AND team_id = :team AND binding = :binding"),
                        "ExpressionAttributeValues": {":one": {"N": "1"}, ":now": {"N": str(int(self.clock()))},
                            ":run": root["native_test_run_id"], ":hold": root["native_test_reservation_id"],
                            ":original": root["native_test_operation_id"], ":owner": root["owner_id"],
                            ":tenant": root["tenant_id"], ":team": root["team_id"], ":binding": root["binding"]}}},
                    {"Put": {"TableName": self._table(), "Item": operation,
                             "ConditionExpression": "attribute_not_exists(execution_id)"}},
                ])
            except (ClientError, BotoCoreError):
                # A lost transaction acknowledgement must never dispatch. Even
                # a successful read returns status only, with no new send.
                prior = self.lookup(execution_id, request_id, "code_interpreter___close", decision)
                if prior["request_digest"]["S"] != digest:
                    raise HTTPException(409, "request id payload conflict")
                return key, prior, False
            return key, operation, True
        except (ClientError, BotoCoreError, KeyError, ValueError, TypeError):
            raise HTTPException(503, "original native cleanup unavailable") from None

    def dispatched(self, operation_key: str):
        try:
            client = self._client()
            operation = client.get_item(TableName=self._table(), Key={"execution_id": {"S": operation_key}},
                                        ConsistentRead=True).get("Item", {})
            require_generic_tool(operation.get("tool", {}).get("S"))
            identifier = operation["operation_execution_id"]["S"]
            write = operation["tool"]["S"] in ATLASSIAN_WRITES
            update_expression = "SET #status = :unknown, dispatch_intent = :dispatched"
            update_values = {":unknown": {"S": "unknown"}, ":pending": {"S": "pending"},
                             ":dispatched": {"S": "dispatched"}, ":claimed": {"S": "claimed"}}
            if write:
                update_expression += (", write_report_status = :report_pending, "
                                      "cleanup_partition = :report, next_cleanup_at = :now")
                update_values.update({":report_pending": {"S": "pending"},
                                      ":report": {"S": WRITE_REPORT_PARTITION},
                                      ":now": {"N": str(int(self.clock()))}})
            client.transact_write_items(TransactItems=[
                self._admission_guard(),
                {"ConditionCheck": {
                    "TableName": self._table(), "Key": {"execution_id": {"S": identifier}},
                    "ConditionExpression": "#state = :active AND deadline_at > :now",
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {":active": {"S": "active"},
                                                  ":now": {"N": str(int(self.clock()))}},
                }},
                {"Update": {
                    "TableName": self._table(), "Key": {"execution_id": {"S": operation_key}},
                    "UpdateExpression": update_expression,
                    "ConditionExpression": "#status = :pending AND dispatch_intent = :claimed",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": update_values,
                }},
            ])
        except KeyError:
            raise HTTPException(503, "tool execution store unavailable") from None
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                try:
                    execution = client.get_item(
                        TableName=self._table(), Key={"execution_id": {"S": identifier}},
                        ConsistentRead=True,
                    ).get("Item", {})
                except (ClientError, BotoCoreError):
                    raise HTTPException(503, "tool execution store unavailable") from None
                if execution.get("state", {}).get("S") != "active" or int(
                    execution.get("deadline_at", {}).get("N", "0")
                ) <= int(self.clock()):
                    raise HTTPException(403, "tool execution closed") from None
                try:
                    if not self._admission_open():
                        raise HTTPException(403, "tool admission disabled") from None
                except (ClientError, BotoCoreError):
                    raise HTTPException(503, "tool execution store unavailable") from None
            raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def due_write_reports(self, max_records=50):
        if type(max_records) is not int or not 1 <= max_records <= 50:
            raise ValueError("report batch must contain 1 to 50 records")
        now = int(self.clock())
        try:
            rows = self._client().query(
                TableName=self._table(), IndexName="cleanup-due",
                KeyConditionExpression="cleanup_partition = :report AND next_cleanup_at <= :now",
                ExpressionAttributeValues={":report": {"S": WRITE_REPORT_PARTITION},
                                           ":now": {"N": str(now)}},
                Limit=max_records,
            )["Items"]
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None
        return [row for row in rows if row.get("tool", {}).get("S") in ATLASSIAN_WRITES
                and row.get("write_report_status", {}).get("S") == "pending"
                and row.get("dispatch_intent", {}).get("S") == "dispatched"
                and row.get("status", {}).get("S") in {"unknown", "confirmed"}]

    def finish_write_report(self, operation, *, recorded):
        if (operation.get("tool", {}).get("S") not in ATLASSIAN_WRITES
                or operation.get("write_report_status", {}).get("S") != "pending"
                or operation.get("status", {}).get("S") not in {"unknown", "confirmed"}):
            raise ValueError("original pending Jira write report required")
        values = {":report": {"S": WRITE_REPORT_PARTITION}, ":pending": {"S": "pending"},
                  ":status": operation["status"]}
        if recorded:
            values.update({":now": {"N": str(int(self.clock()))}, ":recorded": {"S": "recorded"}})
            if operation["status"]["S"] == "unknown":
                values.update({":readback": {"S": WRITE_READBACK_PARTITION},
                               ":readback_pending": {"S": "pending"}})
        else:
            values[":retry"] = {"N": str(int(self.clock()) + 60)}
        condition = "cleanup_partition = :report AND write_report_status = :pending AND #status = :status"
        if operation["status"]["S"] == "confirmed":
            condition += " AND result_json = :result"
            values[":result"] = operation["result_json"]
        try:
            update = {"TableName": self._table(), "Key": {"execution_id": operation["execution_id"]},
                      "ConditionExpression": condition,
                      "UpdateExpression": (
                          "SET write_report_status = :recorded, write_reported_at = :now, "
                          "write_readback_status = :readback_pending, cleanup_partition = :readback, "
                          "next_cleanup_at = :now" if recorded and operation["status"]["S"] == "unknown" else
                          "SET write_report_status = :recorded, write_reported_at = :now "
                          "REMOVE cleanup_partition, next_cleanup_at" if recorded else
                          "SET next_cleanup_at = :retry"),
                      "ExpressionAttributeNames": {"#status": "status"},
                      "ExpressionAttributeValues": values}
            if recorded and operation["status"]["S"] == "confirmed":
                self._client().transact_write_items(TransactItems=[
                    {"Update": update},
                    {"Update": {"TableName": self._table(),
                                "Key": {"execution_id": operation["operation_execution_id"]},
                                "UpdateExpression": "ADD unresolved_operations :minus_one",
                                "ConditionExpression": ("operation_tracking = :tracked AND "
                                                        "unresolved_operations > :zero"),
                                "ExpressionAttributeValues": {":minus_one": {"N": "-1"},
                                                              ":tracked": {"S": "1"}, ":zero": {"N": "0"}}}},
                ])
                return self._client().get_item(TableName=self._table(), Key=update["Key"], ConsistentRead=True)["Item"]
            return self._client().update_item(**update, ReturnValues="ALL_NEW")["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                current = self._client().get_item(TableName=self._table(),
                                                  Key={"execution_id": operation["execution_id"]},
                                                  ConsistentRead=True).get("Item", {})
                if (current.get("write_report_status", {}).get("S") != "pending"
                        or current.get("status") != operation["status"]):
                    return None
            raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def due_write_readbacks(self, max_records=50):
        if type(max_records) is not int or not 1 <= max_records <= 50:
            raise ValueError("readback batch must contain 1 to 50 records")
        try:
            rows = self._client().query(
                TableName=self._table(), IndexName="cleanup-due",
                KeyConditionExpression="cleanup_partition = :partition AND next_cleanup_at <= :now",
                ExpressionAttributeValues={":partition": {"S": WRITE_READBACK_PARTITION},
                                           ":now": {"N": str(int(self.clock()))}}, Limit=max_records,
            )["Items"]
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None
        return [row for row in rows if row.get("tool", {}).get("S") in ATLASSIAN_WRITES
                and row.get("write_report_status", {}).get("S") == "recorded"
                and row.get("write_readback_status", {}).get("S") == "pending"
                and row.get("dispatch_intent", {}).get("S") == "dispatched"
                and row.get("status", {}).get("S") == "unknown"]

    def finish_write_readback(self, operation, *, performed):
        if (operation.get("tool", {}).get("S") not in ATLASSIAN_WRITES
                or operation.get("write_readback_status", {}).get("S") != "pending"
                or operation.get("status", {}).get("S") != "unknown"):
            raise ValueError("original unknown Jira readback required")
        try:
            return self._client().update_item(
                TableName=self._table(), Key={"execution_id": operation["execution_id"]},
                ConditionExpression=("cleanup_partition = :partition AND next_cleanup_at = :due "
                                     "AND write_report_status = :recorded AND write_readback_status = :pending "
                                     "AND #status = :unknown AND receipt_id = :receipt"),
                UpdateExpression="SET write_readback_evidence = :evidence, next_cleanup_at = :retry",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":partition": {"S": WRITE_READBACK_PARTITION}, ":due": operation["next_cleanup_at"],
                    ":recorded": {"S": "recorded"}, ":pending": {"S": "pending"},
                    ":unknown": {"S": "unknown"}, ":receipt": operation["receipt_id"],
                    ":evidence": {"S": "inconclusive" if performed else "unavailable"},
                    ":retry": {"N": str(int(self.clock()) + 60)},
                }, ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def root_tool(self, execution_id: UUID, tool: str, decision):
        owner = decision.principal
        binding = f"run:{decision.run_id}:{decision.workload_id}" if decision.run_id else "human"
        try:
            item = self._client().get_item(TableName=self._table(),
                Key={"execution_id": {"S": str(execution_id)}}, ConsistentRead=True).get("Item", {})
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None
        if any(item.get(field, {}).get("S") != value for field, value in (
            ("schema_version", "1.0"), ("tenant_id", owner.tenant_id), ("owner_id", owner.user_id),
            ("team_id", owner.team_id), ("binding", binding),
        )) or not self._permits(item, tool):
            raise HTTPException(404, "not found")
        return item["tool"]["S"]

    def original_operation(self, execution_id: UUID, tool: str, decision):
        original_tool = self.root_tool(execution_id, tool, decision)
        try:
            root = self._client().get_item(TableName=self._table(),
                Key={"execution_id": {"S": str(execution_id)}}, ConsistentRead=True).get("Item", {})
            request_id = root.get("original_request_id", {}).get("S")
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None
        if not request_id:
            return None
        try:
            return self.lookup(execution_id, UUID(request_id), original_tool, decision)
        except ValueError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def close(self, execution_id: UUID, tool: str, decision):
        owner = decision.principal
        binding = f"run:{decision.run_id}:{decision.workload_id}" if decision.run_id else "human"
        if (decision.run_id and (not decision.workload_id or not decision.run_expires_at)
                or decision.run_expires_at is not None and decision.run_expires_at <= int(self.clock())):
            raise HTTPException(403, "tool authority refused")
        key = {"execution_id": {"S": str(execution_id)}}

        def read_owned():
            item = self._client().get_item(TableName=self._table(), Key=key, ConsistentRead=True).get("Item", {})
            if any(item.get(field, {}).get("S") != value for field, value in (
                ("schema_version", "1.0"), ("tenant_id", owner.tenant_id),
                ("owner_id", owner.user_id), ("team_id", owner.team_id),
                ("binding", binding),
            )) or not self._permits(item, tool):
                raise HTTPException(404, "not found")
            return item

        try:
            item = read_owned()
            for _ in range(3):
                state = item.get("state", {}).get("S")
                if state != "active" and (state != "closed" or self.cleanup_proved(item)):
                    break
                now = int(self.clock())
                proof_condition = ""
                proof_values = {}
                if state == "closed":
                    # A pre-proof terminal flag is not evidence. Fence the exact
                    # owned row again, without changing its results or counters.
                    for field in ("cleanup_evidence", "cleanup_status"):
                        if field in item:
                            proof_condition += f" AND {field} = :{field}"
                            proof_values[f":{field}"] = item[field]
                        else:
                            proof_condition += f" AND attribute_not_exists({field})"
                try:
                    item = self._client().update_item(
                        TableName=self._table(), Key=key,
                        UpdateExpression=("SET #state = :closing, cleanup_status = :unknown, "
                                          "cleanup_partition = :due, next_cleanup_at = :now"),
                        ConditionExpression=("#state = :previous AND schema_version = :version "
                            "AND tenant_id = :tenant AND owner_id = :owner AND team_id = :team "
                            "AND binding = :binding AND tool = :tool AND frozen_grants = :grants"
                            + proof_condition),
                        ExpressionAttributeNames={"#state": "state"},
                        ExpressionAttributeValues={
                            ":previous": {"S": state}, ":closing": {"S": "closing"},
                            ":unknown": {"S": "unknown"}, ":now": {"N": str(now)},
                            ":due": {"S": "due"},
                            ":version": {"S": "1.0"}, ":tenant": {"S": owner.tenant_id},
                            ":owner": {"S": owner.user_id}, ":team": {"S": owner.team_id},
                            ":binding": {"S": binding}, ":tool": item["tool"],
                            ":grants": item["frozen_grants"], **proof_values,
                        }, ReturnValues="ALL_NEW",
                    )["Attributes"]
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                    item = read_owned()
            if item.get("state", {}).get("S") == "active":
                raise HTTPException(503, "tool execution store unavailable")
            return {
                "execution_id": str(execution_id), "state": item["state"]["S"],
                "cleanup_status": "confirmed" if self.cleanup_proved(item) else "unknown",
            }
        except (ClientError, BotoCoreError, KeyError):
            raise HTTPException(503, "tool execution store unavailable") from None

    def confirm_closed(self, execution_id: UUID, tool: str, decision, provider_evidence=None):
        current = self.close(execution_id, tool, decision)
        if current["cleanup_status"] == "confirmed":
            return current
        owner = decision.principal
        binding = f"run:{decision.run_id}:{decision.workload_id}" if decision.run_id else "human"
        try:
            item = self._client().get_item(TableName=self._table(),
                Key={"execution_id": {"S": str(execution_id)}}, ConsistentRead=True)["Item"]
            root_tool = item["tool"]["S"]
            if root_tool in SYNCHRONOUS_TOOLS:
                evidence = "all_operations_settled"
                if root_tool in MEMORY_TOOLS:
                    if not self.tracked_reads_settled(item):
                        return current
                    evidence = "memory_operations_completed"
                if item.get("operation_tracking", {}).get("S") == "1":
                    proof_condition = (" AND operation_tracking = :tracked AND accepted_operations > :zero "
                                       "AND unresolved_operations = :zero")
                elif self._legacy_search_settled(item):
                    evidence = "historical_operations_settled"
                    proof_condition = " AND attribute_not_exists(operation_tracking)"
                else:
                    return current
            elif self.termination_proved(item, provider_evidence):
                evidence = ("start_not_dispatched" if root_tool == "browser___start" and
                            provider_evidence.get("provider_status") == "NOT_STARTED" else "provider_terminated")
                proof_condition = ""
            else:
                return current
            values = {":closing": {"S": "closing"}, ":closed": {"S": "closed"},
                ":confirmed": {"S": "confirmed"}, ":version": {"S": "1.0"},
                ":tenant": {"S": owner.tenant_id}, ":owner": {"S": owner.user_id},
                ":team": {"S": owner.team_id}, ":binding": {"S": binding},
                ":tool": item["tool"], ":evidence": {"S": evidence},
                ":now": {"N": str(int(self.clock()))}, ":zero": {"N": "0"}, ":one": {"N": "1"}}
            if ":tracked" in proof_condition:
                values.update({":tracked": {"S": "1"}, ":zero": {"N": "0"}})
            row = self._client().update_item(
                TableName=self._table(), Key={"execution_id": {"S": str(execution_id)}},
                UpdateExpression=("SET #state = :closed, cleanup_status = :confirmed, "
                                  "cleanup_last_outcome = :confirmed, cleanup_evidence = :evidence, "
                                  "cleanup_confirmed_at = :now, cleanup_last_attempt_at = :now, "
                                  "cleanup_attempts = if_not_exists(cleanup_attempts, :zero) + :one "
                                  "REMOVE cleanup_partition, next_cleanup_at"),
                ConditionExpression=("#state = :closing AND schema_version = :version AND tenant_id = :tenant "
                                     "AND owner_id = :owner AND team_id = :team AND binding = :binding "
                                     "AND tool = :tool" + proof_condition),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except (ClientError, BotoCoreError):
            return self.close(execution_id, tool, decision)
        return {"execution_id": str(execution_id), "state": row["state"]["S"],
                "cleanup_status": row["cleanup_status"]["S"]}

    @staticmethod
    def tracked_reads_settled(item):
        try:
            admitted = (item["tool"]["S"] in STATELESS_READ_TOOLS or
                        item["tool"]["S"] in MEMORY_TOOLS and "native_test_run_id" not in item
                        and (item.get("memory_contract") == {"S": "1"} and item.get("binding") == {"S": "human"}
                             or item.get("memory_contract") == {"S": "host"}
                             and item.get("binding", {}).get("S", "").startswith("run:")))
            return (admitted and
                    item["operation_tracking"]["S"] == "1" and
                    int(item["accepted_operations"]["N"]) > 0 and
                    item["unresolved_operations"]["N"] == "0")
        except (KeyError, ValueError, TypeError):
            return False

    @staticmethod
    def cleanup_proved(item):
        root = item.get("tool", {}).get("S")
        evidence = item.get("cleanup_evidence", {}).get("S")
        return (item.get("state", {}).get("S") == "closed" and
                item.get("cleanup_status", {}).get("S") == "confirmed" and
                ((evidence == "all_operations_settled" and ExecutionStore.tracked_reads_settled(item)) or
                 (root in MEMORY_TOOLS and evidence == "memory_operations_completed" and
                  ExecutionStore.tracked_reads_settled(item)) or
                 (root == "websearch___search" and evidence == "historical_operations_settled") or
                 (root in {"browser___start", "code_interpreter___start"} and
                  evidence == "provider_terminated") or
                 (root == "browser___start" and evidence == "start_not_dispatched")))

    @staticmethod
    def termination_proved(item, evidence):
        root = item.get("tool", {}).get("S")
        return (root in {"browser___start", "code_interpreter___start"} and
                isinstance(evidence, dict) and
                evidence.get("execution_id") == item["execution_id"]["S"] and
                evidence.get("status") == "confirmed" and evidence.get("termination_confirmed") is True and
                (root != "browser___start" or evidence.get("provider_status") == "TERMINATED" or
                 (evidence.get("provider_status") == "NOT_STARTED" and
                  evidence.get("cleanup_proof") == "start_not_dispatched")))

    def fence_due(self):
        now = int(self.clock())
        key = None
        while True:
            options = {
                "TableName": self._table(), "IndexName": "cleanup-due",
                "KeyConditionExpression": "cleanup_partition = :due AND next_cleanup_at <= :now",
                "ExpressionAttributeValues": {":due": {"S": "due"}, ":now": {"N": str(now)}},
                "Limit": 50,
            }
            if key:
                options["ExclusiveStartKey"] = key
            try:
                page = self._client().query(**options)
            except (ClientError, BotoCoreError):
                raise HTTPException(503, "tool execution store unavailable") from None
            for item in page["Items"]:
                if item.get("state", {}).get("S") != "active" or int(item["deadline_at"]["N"]) > now:
                    continue
                try:
                    self._client().update_item(
                        TableName=self._table(), Key={"execution_id": item["execution_id"]},
                        UpdateExpression="SET #state = :closing, cleanup_status = :unknown, next_cleanup_at = :retry",
                        ConditionExpression="#state = :active AND deadline_at <= :now",
                        ExpressionAttributeNames={"#state": "state"},
                        ExpressionAttributeValues={
                            ":closing": {"S": "closing"}, ":unknown": {"S": "unknown"},
                            ":active": {"S": "active"}, ":now": {"N": str(now)},
                            ":retry": {"N": str(now + 60)},
                        },
                    )
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise HTTPException(503, "tool execution store unavailable") from None
                except BotoCoreError:
                    raise HTTPException(503, "tool execution store unavailable") from None
            key = page.get("LastEvaluatedKey")
            if not key:
                return

    def due_cleanup(self, max_records: int = 50):
        if not 1 <= max_records <= 50:
            raise ValueError("cleanup batch must contain 1 to 50 records")
        now = int(self.clock())
        try:
            return self._client().query(
                TableName=self._table(), IndexName="cleanup-due",
                KeyConditionExpression="cleanup_partition = :due AND next_cleanup_at <= :now",
                ExpressionAttributeValues={":due": {"S": "due"}, ":now": {"N": str(now)}},
                Limit=max_records,
            )["Items"]
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None

    def repair_terminal_due(self, item):
        evidence = item.get("cleanup_evidence", {}).get("S")
        proved = self.cleanup_proved(item)
        now = int(self.clock())
        if proved:
            expression = "REMOVE cleanup_partition, next_cleanup_at"
            condition = "#state = :closed AND cleanup_status = :confirmed AND cleanup_evidence = :proof"
            values = {":closed": {"S": "closed"}, ":confirmed": {"S": "confirmed"},
                      ":proof": {"S": evidence}}
        else:
            expression = "SET #state = :closing, cleanup_status = :unknown, next_cleanup_at = :retry"
            condition = "#state = :closed AND "
            condition += "cleanup_evidence = :bad" if evidence else "attribute_not_exists(cleanup_evidence)"
            values = {":closed": {"S": "closed"}, ":closing": {"S": "closing"},
                      ":unknown": {"S": "unknown"}, ":retry": {"N": str(now + 60)}}
            if evidence:
                values[":bad"] = {"S": evidence}
        values[":now"] = {"N": str(now)}
        # Do not let a stale scheduler snapshot refence a row that an owned
        # direct close has already proved, or a different immutable identity.
        for field in ("schema_version", "tenant_id", "owner_id", "team_id", "binding", "tool",
                      "frozen_grants", "generation", *(("cleanup_status",) if not proved else ())):
            if field in item:
                condition += f" AND {field} = :saved_{field}"
                values[f":saved_{field}"] = item[field]
            else:
                condition += f" AND attribute_not_exists({field})"
        try:
            self._client().update_item(
                TableName=self._table(), Key={"execution_id": item["execution_id"]},
                UpdateExpression=expression,
                ConditionExpression=condition + " AND next_cleanup_at <= :now",
                ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def resume_cleanup(self, item):
        now = int(self.clock())
        state = item.get("state", {}).get("S")
        if state not in {"active", "closing"}:
            return None
        prior = int(item.get("cleanup_attempts", {}).get("N", "0"))
        if item.get("native_test_run_id"):
            try:
                deadline = int(item["native_cleanup_deadline_at"]["N"])
                limit = int(item["native_cleanup_attempts_remaining"]["N"])
                if not 1 <= limit <= 32 or now >= deadline - 90 or prior >= limit:
                    self.expire_native_cleanup(item)
                    return None
            except (KeyError, ValueError, TypeError):
                self.expire_native_cleanup(item)
                return None
        expression = ("SET next_cleanup_at = :retry, cleanup_last_attempt_at = :now, "
                      "cleanup_last_outcome = :unknown, "
                      "cleanup_attempts = if_not_exists(cleanup_attempts, :zero) + :one")
        condition = "#state = :closing AND next_cleanup_at <= :now AND "
        condition += ("cleanup_attempts = :prior" if "cleanup_attempts" in item
                      else "attribute_not_exists(cleanup_attempts)")
        values = {":closing": {"S": "closing"}, ":now": {"N": str(now)},
                  ":retry": {"N": str(now + min(60 * 2 ** min(prior, 1), 120))},
                  ":unknown": {"S": "unknown"}, ":one": {"N": "1"}, ":zero": {"N": "0"}}
        if "cleanup_attempts" in item:
            values[":prior"] = {"N": str(prior)}
        if state == "active":
            expression = "SET #state = :closing, cleanup_status = :unknown, next_cleanup_at = :retry"
            condition = "#state = :active AND deadline_at <= :now AND next_cleanup_at <= :now"
            values = {":active": {"S": "active"}, ":closing": {"S": "closing"},
                      ":unknown": {"S": "unknown"}, ":now": {"N": str(now)},
                      ":retry": {"N": str(now + 60)}}
        try:
            return self._client().update_item(
                TableName=self._table(), Key={"execution_id": item["execution_id"]},
                UpdateExpression=expression, ConditionExpression=condition,
                ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def expire_native_cleanup(self, item):
        """Stop automatic continuation; preserve the row and unknown liability."""
        try:
            self._client().update_item(
                TableName=self._table(), Key={"execution_id": item["execution_id"]},
                UpdateExpression=("SET #state = :closing, cleanup_status = :unknown, "
                                  "cleanup_last_outcome = :exhausted REMOVE cleanup_partition, next_cleanup_at"),
                ConditionExpression="native_test_run_id = :run AND #state = :state",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":run": item["native_test_run_id"], ":state": item["state"],
                    ":closing": {"S": "closing"}, ":unknown": {"S": "unknown"},
                    ":exhausted": {"S": "native_allowance_exhausted"}},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def cleanup_outcome(self, item, outcome):
        try:
            self._client().update_item(
                TableName=self._table(), Key={"execution_id": item["execution_id"]},
                UpdateExpression="SET cleanup_last_outcome = :outcome",
                ConditionExpression="#state = :closing AND cleanup_attempts = :attempt",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":closing": {"S": "closing"},
                    ":attempt": item["cleanup_attempts"], ":outcome": {"S": outcome}},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None

    def confirm_due_provider(self, item, evidence):
        if not self.termination_proved(item, evidence):
            return False
        try:
            self._client().update_item(
                TableName=self._table(), Key={"execution_id": item["execution_id"]},
                UpdateExpression=("SET #state = :closed, cleanup_status = :confirmed, "
                                  "cleanup_last_outcome = :confirmed, cleanup_evidence = :evidence, "
                                  "cleanup_confirmed_at = :now REMOVE cleanup_partition, next_cleanup_at"),
                ConditionExpression="#state = :closing AND tool = :tool AND cleanup_attempts = :attempt",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":closing": {"S": "closing"}, ":closed": {"S": "closed"},
                    ":confirmed": {"S": "confirmed"},
                    ":evidence": {"S": "start_not_dispatched" if item["tool"]["S"] == "browser___start" and
                                  evidence.get("provider_status") == "NOT_STARTED" else "provider_terminated"},
                    ":now": {"N": str(int(self.clock()))}, ":tool": item["tool"],
                    ":attempt": item["cleanup_attempts"]},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None
        return True

    def confirm_due_search(self, item):
        # The historical scan fallback remains specific to the original Search
        # contract. Knowledge may close only with durable tracked operations.
        if item.get("tool", {}).get("S") not in SYNCHRONOUS_TOOLS:
            return False
        memory = item.get("tool", {}).get("S") in MEMORY_TOOLS
        if memory and not self.tracked_reads_settled(item):
            return False
        tracked = item.get("operation_tracking", {}).get("S") == "1"
        if not tracked and not self._legacy_search_settled(item):
            return False
        proof = ("operation_tracking = :tracked AND accepted_operations > :zero "
                 "AND unresolved_operations = :zero" if tracked else
                 "attribute_not_exists(operation_tracking)")
        evidence = "all_operations_settled" if tracked else "historical_operations_settled"
        if memory:
            evidence = "memory_operations_completed"
        try:
            self._client().update_item(
                TableName=self._table(), Key={"execution_id": item["execution_id"]},
                UpdateExpression=("SET #state = :closed, cleanup_status = :confirmed, "
                                  "cleanup_last_outcome = :confirmed, cleanup_evidence = :evidence, "
                                  "cleanup_confirmed_at = :now "
                                  "REMOVE cleanup_partition, next_cleanup_at"),
                ConditionExpression=f"#state = :closing AND tool = :search AND {proof}",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":closing": {"S": "closing"}, ":closed": {"S": "closed"},
                    ":confirmed": {"S": "confirmed"}, ":search": item["tool"],
                    ":evidence": {"S": evidence}, ":now": {"N": str(int(self.clock()))},
                    **({":tracked": {"S": "1"}, ":zero": {"N": "0"}} if tracked else {})},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise HTTPException(503, "tool execution store unavailable") from None
        except BotoCoreError:
            raise HTTPException(503, "tool execution store unavailable") from None
        return True

    def _legacy_search_settled(self, item):
        now = int(self.clock())
        try:
            retention = int(item["retention_ttl"]["N"])
        except (KeyError, ValueError, TypeError):
            return False
        if (item.get("state", {}).get("S") != "closing" or
                item.get("tool", {}).get("S") != "websearch___search" or
                "operation_tracking" in item or retention <= now):
            return False
        identifier = item["execution_id"]["S"]
        cursor = None
        found = 0
        seen = set()
        for _ in range(10):
            options = {"TableName": self._table(), "ConsistentRead": True, "Limit": 100,
                       "FilterExpression": "operation_execution_id = :execution",
                       "ExpressionAttributeValues": {":execution": {"S": identifier}}}
            if cursor:
                options["ExclusiveStartKey"] = cursor
            try:
                page = self._client().scan(**options)
            except (ClientError, BotoCoreError):
                raise HTTPException(503, "tool execution store unavailable") from None
            for operation in page.get("Items", []):
                try:
                    if (operation["execution_id"]["S"] in seen or
                            any(operation[field]["S"] != item[field]["S"] for field in
                                ("schema_version", "tenant_id", "owner_id", "team_id", "binding", "tool")) or
                            operation["status"]["S"] != "confirmed" or
                            operation["result_status"]["S"] != "success" or
                            operation["dispatch_intent"]["S"] != "dispatched" or
                            "result_json" not in operation or
                            int(operation["retention_ttl"]["N"]) <= retention):
                        return False
                    seen.add(operation["execution_id"]["S"])
                except (KeyError, ValueError, TypeError):
                    return False
                found += 1
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return found > 0
        return False

    def stateless_ready(self, execution_id: UUID):
        try:
            item = self._client().get_item(TableName=self._table(),
                Key={"execution_id": {"S": str(execution_id)}}, ConsistentRead=True).get("Item", {})
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None
        if item.get("operation_tracking", {}).get("S") != "1":
            return self._legacy_search_settled(item)
        return self.tracked_reads_settled(item)

    def confirmed(self, operation_key: str, result):
        try:
            client = self._client()
            operation = client.get_item(TableName=self._table(),
                Key={"execution_id": {"S": operation_key}}, ConsistentRead=True).get("Item", {})
            require_generic_tool(operation.get("tool", {}).get("S"))
            tool_result = result.get("result")
            failed = "error" in result or (isinstance(tool_result, dict) and tool_result.get("isError") is True)
            write = operation.get("tool", {}).get("S") in ATLASSIAN_WRITES
            if write and (failed or not isinstance(tool_result, dict) or
                          not isinstance(tool_result.get("structuredContent"), dict) and
                          not isinstance(tool_result.get("content"), list)):
                raise HTTPException(503, "Jira write outcome unverified")
            if (operation.get("tool", {}).get("S") in MEMORY_TOOLS and not failed
                    and not completion_matches(operation, result)):
                raise HTTPException(503, "knowledge memory completion unavailable")
            outcome = "error" if failed else "success"
            identifier = operation["operation_execution_id"]["S"]
            execution = client.get_item(TableName=self._table(),
                Key={"execution_id": {"S": identifier}}, ConsistentRead=True).get("Item", {})
            if (operation.get("tool", {}).get("S") in MEMORY_TOOLS
                    and operation.get("binding") != execution.get("binding")):
                raise HTTPException(503, "knowledge memory completion unavailable")
            update_expression = "SET #status = :confirmed, result_status = :outcome, result_json = :result"
            update_values = {":confirmed": {"S": "confirmed"}, ":unknown": {"S": "unknown"},
                             ":dispatched": {"S": "dispatched"}, ":outcome": {"S": outcome},
                             ":result": {"S": json.dumps(result)}}
            if write:
                update_expression += (", write_report_status = :report_pending, "
                                      "cleanup_partition = :report, next_cleanup_at = :now")
                update_values.update({":report_pending": {"S": "pending"},
                                      ":report": {"S": WRITE_REPORT_PARTITION},
                                      ":now": {"N": str(int(self.clock()))}})
            update = {"TableName": self._table(), "Key": {"execution_id": {"S": operation_key}},
                "UpdateExpression": update_expression,
                "ConditionExpression": "#status = :unknown AND dispatch_intent = :dispatched",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": update_values}
            if outcome == "success" and not write and execution.get("operation_tracking", {}).get("S") == "1":
                client.transact_write_items(TransactItems=[
                    {"Update": update},
                    {"Update": {"TableName": self._table(), "Key": {"execution_id": {"S": identifier}},
                        "UpdateExpression": "ADD unresolved_operations :minus_one",
                        "ConditionExpression": "operation_tracking = :tracked AND unresolved_operations > :zero",
                        "ExpressionAttributeValues": {":minus_one": {"N": "-1"}, ":tracked": {"S": "1"},
                                                      ":zero": {"N": "0"}}}},
                ])
                reply = client.get_item(TableName=self._table(), Key={"execution_id": {"S": operation_key}},
                                        ConsistentRead=True)["Item"]
            else:
                reply = client.update_item(**update, ReturnValues="ALL_NEW")["Attributes"]
        except (ClientError, BotoCoreError, KeyError):
            raise HTTPException(503, "tool execution store unavailable") from None
        return reply

    def lookup(self, execution_id: UUID, request_id: UUID, tool: str, decision):
        owner = decision.principal
        binding = f"run:{decision.run_id}:{decision.workload_id}" if decision.run_id else "human"
        if (decision.run_id and (not decision.workload_id or not decision.run_expires_at)
                or decision.run_expires_at is not None and decision.run_expires_at <= int(self.clock())):
            raise HTTPException(403, "tool authority refused")
        operation_key = f"OP#{owner.tenant_id}#{owner.user_id}#{tool}#{request_id}"
        try:
            client = self._client()
            operation = client.get_item(TableName=self._table(), Key={"execution_id": {"S": operation_key}},
                                        ConsistentRead=True).get("Item", {})
            execution = client.get_item(TableName=self._table(), Key={"execution_id": {"S": str(execution_id)}},
                                        ConsistentRead=True).get("Item", {})
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None
        if (any(operation.get(field, {}).get("S") != value for field, value in (
                ("schema_version", "1.0"), ("tenant_id", owner.tenant_id), ("owner_id", owner.user_id),
                ("team_id", owner.team_id), ("binding", binding), ("tool", tool),
                ("request_id", str(request_id)), ("operation_execution_id", str(execution_id)),
            )) or any(execution.get(field, {}).get("S") != value for field, value in (
                ("schema_version", "1.0"), ("tenant_id", owner.tenant_id), ("owner_id", owner.user_id),
                ("team_id", owner.team_id), ("binding", binding),
            )) or not self._permits(execution, tool) or
                int(operation.get("retention_ttl", {}).get("N", "0")) <= int(self.clock()) or
                int(execution.get("retention_ttl", {}).get("N", "0")) <= int(self.clock())):
            raise HTTPException(404, "not found")
        return operation

    def recover_request(self, request_id: UUID, tool: str, arguments: dict, decision,
                        ttl_seconds=None, execution_id=None):
        """Resolve only a retained original; this path cannot claim or dispatch."""
        owner = decision.principal
        key = f"OP#{owner.tenant_id}#{owner.user_id}#{tool}#{request_id}"
        try:
            operation = self._client().get_item(TableName=self._table(),
                Key={"execution_id": {"S": key}}, ConsistentRead=True).get("Item", {})
            identifier = UUID(operation["operation_execution_id"]["S"])
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool execution store unavailable") from None
        except (KeyError, ValueError, TypeError):
            raise HTTPException(404, "not found") from None
        operation = self.lookup(identifier, request_id, tool, decision)
        digest = hashlib.sha256(canonical(
            {"arguments": arguments, "ttl_seconds": ttl_seconds} if ttl_seconds is not None else arguments
        )).hexdigest()
        if (operation.get("request_digest", {}).get("S") != digest or
                execution_id is not None and str(identifier) != str(execution_id)):
            raise HTTPException(404, "not found")
        return operation

    @staticmethod
    def result_chunk(operation, offset: int, length: int):
        if private_result(operation):
            raise HTTPException(409, PRIVATE_DELIVERY_REQUIRED)
        if operation.get("status", {}).get("S") != "confirmed" or "result_json" not in operation:
            raise HTTPException(404, "not found")
        data = operation["result_json"]["S"].encode("utf-8")
        content_type = "application/json"
        if operation.get("tool", {}).get("S") == "code_interpreter___file":
            try:
                receipt = json.loads(data)["result"]["result"]
                artifact = receipt["artifact"]
                data = base64.b64decode(receipt["result"]["data_base64"], validate=True)
                if (artifact["byte_length"] != len(data) or
                        artifact["content_sha256"] != hashlib.sha256(data).hexdigest()):
                    raise ValueError("file artifact mismatch")
                content_type = artifact["content_type"]
            except (KeyError, ValueError, TypeError, AttributeError):
                raise HTTPException(503, "Interpreter artifact unavailable") from None
        if offset >= len(data) or length < 1 or length > 8192:
            raise HTTPException(416, "invalid artifact range")
        chunk = data[offset:offset + length]
        return {
            "artifact_id": "result", "content_type": content_type, "offset": offset,
            "length": len(chunk), "total_length": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "data_base64": base64.b64encode(chunk).decode("ascii"),
        }

    @staticmethod
    def receipt(operation):
        response = {
            "execution_id": operation["operation_execution_id"]["S"],
            "request_id": operation["request_id"]["S"],
            "receipt_id": operation["receipt_id"]["S"],
            "root_human_id": operation["owner_id"]["S"],
            "status": operation["status"]["S"],
        }
        if "atlassian_binding" in operation:
            original = json.loads(operation["atlassian_binding"]["S"])
            response.update({
                "operation_id": operation["execution_id"]["S"],
                "tool": operation["tool"]["S"],
                "payload_sha256": operation["request_digest"]["S"],
                "site_id": original["connection"]["site_id"],
                "backend_generation": original["release"]["backend_generation"],
                "report_status": operation.get("write_report_status", {}).get("S", "pending"),
            })
        if response["status"] == "confirmed":
            if private_result(operation):
                return {**response, "result_status": "restricted", "result_unavailable": PRIVATE_DELIVERY_REQUIRED}
            response["result_status"] = operation["result_status"]["S"]
            response["result"] = json.loads(operation["result_json"]["S"])
        return response

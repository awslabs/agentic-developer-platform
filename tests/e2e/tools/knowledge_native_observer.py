"""Read independent original native/mediator and facade operation records.

This observer proves agreement of those actual stores. It does not turn an
absent record into zero backend calls, assert index content, or settle billing.
"""

import hashlib
import json
import math
import re
from copy import deepcopy
from uuid import UUID

from tests.e2e.tools.scenarios import FixtureUnavailable


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class NativeKnowledgeOperationObserver:
    def __init__(
        self,
        *,
        client,
        authority_table,
        execution_table,
        reference,
        release_sha256,
        target_binding_sha256,
    ):
        if (
            any(
                not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", value)
                for value in (authority_table, execution_table)
            )
            or any(
                not re.fullmatch(r"[a-f0-9]{64}", value)
                for value in (release_sha256, target_binding_sha256)
            )
            or client.meta.config.retries.get("total_max_attempts") != 1
        ):
            raise FixtureUnavailable(
                "exact single-attempt original-operation observer binding required"
            )
        self.client, self.authority_table, self.execution_table = (
            client,
            authority_table,
            execution_table,
        )
        self.reference = reference
        self.release_sha256, self.target_binding_sha256 = (
            release_sha256,
            target_binding_sha256,
        )

    def row(self, table, key):
        item = self.client.get_item(TableName=table, Key=key, ConsistentRead=True).get(
            "Item"
        )
        if not isinstance(item, dict):
            raise FixtureUnavailable("original operation evidence is unavailable")
        return item

    def document(self, key, kind):
        row = self.row(self.authority_table, key)
        raw = row.get("document", {}).get("S")
        if (
            row.get("kind", {}).get("S") != kind
            or not isinstance(raw, str)
            or len(raw.encode()) > 262144
        ):
            raise FixtureUnavailable("original operation evidence is invalid")
        value = json.loads(raw)
        if canonical(value) != raw:
            raise FixtureUnavailable("original operation evidence is invalid")
        return value

    def observe(self, *, operation_id, request_id, tool, arguments):
        try:
            if any(
                str(UUID(value)) != value
                for value in (operation_id, request_id, self.reference.run_id)
            ) or tool not in {
                "knowledge___" + name
                for name in ("search", "understand", "impact", "browse", "secure")
            }:
                raise ValueError
            tenant, run = self.reference.tenant_id, self.reference.run_id

            def key(suffix):
                return {"pk": {"S": "TENANT#" + tenant}, "sk": {"S": suffix}}

            approval = self.document(
                key("TOOLS_TEST_APPROVAL#" + run), "native_test_approval_v1"
            )
            designation, job = approval["designation"], approval["job"]
            if (
                approval["scope"] != "all_in"
                or approval["run_id"] != run
                or designation["tenant_id"] != tenant
                or approval["revision"] != self.reference.approval_revision
                or designation["deployment_id"] != self.reference.deployment_id
                or job["sha"] != self.reference.source_revision
                or job["workflow_sha"] != self.reference.definition_revision
                or job["run_id"] != self.reference.github_run_id
                or job["run_attempt"] != self.reference.github_run_attempt
            ):
                raise ValueError
            raw = self.row(
                self.authority_table, key("TOOLS_TEST_OP#" + run + "#" + operation_id)
            )
            if (
                not isinstance(raw.get("document", {}).get("S"), str)
                or len(raw["document"]["S"].encode()) > 262144
            ):
                raise ValueError
            original = json.loads(raw["document"]["S"])
            binding = original["binding"]
            if (
                canonical(original) != raw["document"]["S"]
                or original["operation_id"] != operation_id
                or original["run_id"] != run
                or original["tenant_id"] != tenant
                or original["binding_digest"] != digest(binding)
                or raw["binding_digest"]["S"] != digest(binding)
                or raw["version"]["N"] != str(original["version"])
                or raw["owner_token"]["S"] != original["owner_token"]
                or raw["status"]["S"] != original["status"]
                or original["status"] != "dispatch_started"
                or original["reservation_confirmed"] is not True
                or any(
                    binding[field] != expected
                    for field, expected in {
                        "tenant_id": tenant,
                        "run_id": run,
                        "operation_id": operation_id,
                        "user_id": designation["user_id"],
                        "subject_id": designation["subject_id"],
                        "team_id": designation["team_id"],
                        "department_id": designation["department_id"],
                        "credential_id": approval["credential_id"],
                        "authority_revision": approval["revision"],
                        "meter_epoch": approval["meter_epoch"],
                        "runtime_id": "github-actions:" + digest(job),
                        "source_sha256": digest(
                            {
                                "repository_id": job["repository_id"],
                                "sha": job["sha"],
                                "workflow_sha": job["workflow_sha"],
                            }
                        ),
                    }.items()
                )
            ):
                raise ValueError
            body = {
                "schema_version": "1.0",
                "action": "call",
                "tool": tool,
                "arguments": arguments,
                "request_id": request_id,
                "execution_id": None,
                "ttl_seconds": None,
            }
            effect = [
                item
                for item in approval["effects"]
                if item["effect_key"] == binding["route"]
            ]
            quote = original["quote"]
            authority = self.document(key("TOOLS_TEST_RUN#" + run), "tools_test_run_v1")
            reservation = "tools-test-effect:" + digest(
                {"tenant_id": tenant, "run_id": run, "operation_id": operation_id}
            )
            if (
                binding["request_sha256"] != digest(body)
                or len(effect) != 1
                or effect[0]["action"] != "call"
                or effect[0]["tool"] != tool.replace("___", ".", 1)
                or effect[0]["source_sha256"] != self.release_sha256
                or effect[0]["contract_sha256"] != quote["source_contract_sha256"]
                or quote["request_sha256"] != digest(body)
                or original["reservation_id"] != reservation
                or quote["authority_sha256"] != digest(authority)
                or any(
                    authority[field] != binding[field]
                    for field in (
                        "tenant_id",
                        "run_id",
                        "user_id",
                        "subject_id",
                        "team_id",
                        "department_id",
                        "credential_id",
                        "source_sha256",
                        "runtime_id",
                        "meter_epoch",
                    )
                )
                or authority["revision"] != approval["revision"]
                or authority["effects"] != approval["effects"]
                or authority["scope"] != "all_in"
                or authority["cap_usd"] != "20"
            ):
                raise ValueError
            dispatch, received = (
                original["facade_dispatch"],
                original["native_knowledge_dispatch"],
            )
            execution_id = dispatch["execution_id"]
            if (
                str(UUID(execution_id)) != execution_id
                or dispatch["new_execution"] is not True
                or received["status"] != "response_received"
                or received["request_sha256"] != digest(body)
                or received["target_binding_sha256"] != self.target_binding_sha256
                or any(
                    type(received[field]) not in {int, float}
                    or not math.isfinite(received[field])
                    for field in ("claimed_at", "received_at")
                )
                or not approval["approved_at"]
                <= received["claimed_at"]
                <= received["received_at"]
                < dispatch["session_deadline_at"]
                <= approval["approved_at"] + 300
            ):
                raise ValueError
            index = self.document(
                key("TOOLS_TEST_EXECUTION#" + run + "#" + execution_id),
                "native_test_execution_binding_v1",
            )
            if index != {
                "run_id": run,
                "operation_id": operation_id,
                "execution_id": execution_id,
            }:
                raise ValueError
            facade_key = (
                "OP#"
                + tenant
                + "#"
                + designation["user_id"]
                + "#"
                + tool
                + "#"
                + request_id
            )
            facade = self.row(self.execution_table, {"execution_id": {"S": facade_key}})
            execution = self.row(
                self.execution_table, {"execution_id": {"S": execution_id}}
            )
            common = {
                "schema_version": "1.0",
                "tenant_id": tenant,
                "owner_id": designation["user_id"],
                "team_id": designation["team_id"],
                "tool": tool,
                "binding": "run:" + run + ":native-test-" + approval["credential_id"],
                "native_test_run_id": run,
                "native_test_reservation_id": reservation,
            }
            if (
                any(
                    row.get(field, {}).get("S") != value
                    for row in (facade, execution)
                    for field, value in common.items()
                )
                or any(
                    facade.get(field, {}).get("S") != value
                    for field, value in {
                        "execution_id": facade_key,
                        "operation_execution_id": execution_id,
                        "request_id": request_id,
                        "receipt_id": reservation,
                        "status": "confirmed",
                        "result_status": "success",
                        "dispatch_intent": "dispatched",
                    }.items()
                )
                or execution.get("execution_id", {}).get("S") != execution_id
                or facade.get("request_digest", {}).get("S")
                != hashlib.sha256(
                    json.dumps(
                        arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
            ):
                raise ValueError
            answer = json.loads(facade["result_json"]["S"])
            if answer.get("jsonrpc") != "2.0" or received["rpc_id_sha256"] != digest(
                answer["id"]
            ):
                raise ValueError
            # The interceptor adds the original receipt to _meta after Door.
            # Compare both legitimate representations of an otherwise empty
            # original _meta object; no payload field can be silently ignored.
            raw_answer = deepcopy(answer)
            metadata = raw_answer["result"]["_meta"]
            if metadata.pop("adp_receipt_id") != reservation:
                raise ValueError
            hashes = {digest(raw_answer)}
            if not metadata:
                del raw_answer["result"]["_meta"]
                hashes.add(digest(raw_answer))
            if received["response_sha256"] not in hashes:
                raise ValueError
            cleanup = "pending"
            if (
                execution.get("state", {}).get("S") == "closed"
                or execution.get("cleanup_status", {}).get("S") == "confirmed"
            ):
                if (
                    execution.get("state", {}).get("S") != "closed"
                    or execution.get("cleanup_status", {}).get("S") != "confirmed"
                    or execution.get("cleanup_evidence", {}).get("S")
                    != "all_operations_settled"
                    or execution.get("operation_tracking", {}).get("S") != "1"
                    or int(execution.get("accepted_operations", {}).get("N", "0")) <= 0
                    or execution.get("unresolved_operations", {}).get("N") != "0"
                    or not approval["approved_at"]
                    <= int(execution["cleanup_confirmed_at"]["N"])
                    <= dispatch["cleanup_deadline_at"]
                    <= approval["approved_at"] + 600
                ):
                    raise ValueError
                cleanup = "confirmed"
            return {
                "source": "independent-original-operation-stores",
                "run_id": run,
                "native_operation_id": operation_id,
                "operation_ref": request_id,
                "execution_id": execution_id,
                "receipt_id": reservation,
                "tool": tool,
                "request_sha256": digest(body),
                "door_response_sha256": received["response_sha256"],
                "facade_response_sha256": digest(answer),
                "cleanup_status": cleanup,
                "backend_invocation_count": None,
                "index_verified": False,
                "billing_complete": False,
                "governance_complete": False,
            }
        except Exception:
            raise FixtureUnavailable(
                "independent original Knowledge operation is unconfirmed"
            ) from None

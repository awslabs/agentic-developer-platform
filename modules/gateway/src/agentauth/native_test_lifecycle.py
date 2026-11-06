"""Original-owner cleanup and honest retained-accounting status.

Cleanup consumes the original effect's finite allowance. It never creates a
quote, meter or reservation, and revoking new-work authority does not transfer
cleanup ownership. A terminal provider receipt cannot settle the financial hold.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_effect_quotes import NativeEffectQuote
from src.agentauth.native_test_enrollment import ISSUER, OperatorLocalApproval, capability_principal_binding, is_effect_approval
from src.agentauth.native_test_facade_contract import ATLASSIAN_CALLS, KNOWLEDGE_READS
from src.agentauth.test_run_admission import TestRunAdmissionService, _target_evidence, _uuid
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest
from src.budget.reservations import ReservationTarget

CLEANUP_SEND_SECONDS = 90
MAX_STATUS_READS = 3


class NativeTestLifecycle:
    def __init__(self, *, enrollment, budget, execution_table, execution_client):
        if not execution_table or budget._get_reservations() is not enrollment.reservations:
            raise AdmissionRefusedError("native_test_lifecycle_unavailable")
        self.enrollment, self.store, self.budget = enrollment, enrollment.store, budget
        self.execution_table, self.execution_client = execution_table, execution_client
        self.clock = enrollment.clock

    async def original_owner(self, context, credential, *, run_id=None):
        """Read retained original identity without reinstating revoked dispatch."""
        try:
            prefix, credential_id, secret = credential.split(".")
            if prefix != "adpt1" or str(UUID(credential_id)) != credential_id or len(secret) != 43:
                raise ValueError
            capability = await run_in_threadpool(self.store.capability, context.org_id, credential_id)
            approval = await run_in_threadpool(self.store.approval, context.org_id, capability["run_id"])
            self.enrollment.require_login(context, approval, self.clock())
            if type(approval) is OperatorLocalApproval:
                await self.enrollment.current_operator_owner(context, approval)
            if (
                not is_effect_approval(approval)
                or (run_id is not None and approval.run_id != _uuid(run_id))
                or capability["issuer"] != ISSUER
                or capability["audience"] != approval.audience
                or capability["status"] not in {"active", "revoked"}
                or capability["credential_id"] != approval.credential_id
                or capability["approval_revision"] != approval.revision
                or capability["deployment_id"] != self.enrollment.deployment_id
                or any(capability.get(key) != value for key, value in capability_principal_binding(approval).items())
                or capability["authority_digest"] != digest(approval.authority())
                or not hmac.compare_digest(capability["token_digest"], hashlib.sha256(credential.encode()).hexdigest())
                or self.clock().timestamp() >= approval.meter_expires_at
            ):
                raise ValueError
            authority = await run_in_threadpool(self.store.authority, context.org_id, approval.run_id)
            if authority != approval.authority():
                raise ValueError
            return approval
        except AdmissionRefusedError:
            raise
        except Exception:
            raise AdmissionRefusedError("native_test_original_owner_refused") from None

    async def _original(self, approval, execution_id):
        _uuid(execution_id)
        key = self.store.key(approval.principal.tenant_id, "EXECUTION", approval.run_id + "#" + execution_id)
        binding = await run_in_threadpool(self.store.document, key, "native_test_execution_binding_v1")
        try:
            if binding["execution_id"] != execution_id or binding["run_id"] != approval.run_id:
                raise ValueError
            operation = await run_in_threadpool(self.store.read_operation, approval.principal.tenant_id, approval.run_id, binding["operation_id"])
            quote = NativeEffectQuote.from_evidence(operation["quote"])
            registrations = [effect for effect in approval.effects if effect.effect_key == operation["binding"]["route"]]
            if (
                len(registrations) != 1
                or quote.authority_sha256 != digest(approval.authority())
                or quote.source_contract_sha256 != registrations[0].contract_sha256
                or quote.request_sha256 != operation["binding"]["request_sha256"]
                or operation["reservation_confirmed"] is not True
                or operation["status"] not in {"admitted", "dispatch_started", "unknown"}
                or operation["facade_dispatch"]["execution_id"] != execution_id
                or operation["facade_dispatch"]["new_execution"] is not True
                or operation["facade_dispatch"]["cleanup_attempts"] != quote.cleanup_attempts
                or operation["facade_dispatch"]["cleanup_deadline_at"] > approval.approved_at + 600
            ):
                raise ValueError
            return operation, quote, registrations[0]
        except (KeyError, ValueError, TypeError):
            raise AdmissionRefusedError("native_test_original_execution_refused") from None

    async def _funding(self, approval, operation, amount):
        # Check the original full reservation, never headroom or a new reserve.
        service = TestRunAdmissionService(store=self.store, budget=self.budget, clock=self.clock)
        try:
            targets = [ReservationTarget(**{**value, "headroom_usd": Decimal(value["headroom_usd"])}) for value in operation["targets"]]
            expected = _target_evidence(service._target(approval.authority()))
            expected.pop("ttl_seconds")
            actual = [_target_evidence(target) for target in targets]
            for evidence in actual:
                evidence.pop("ttl_seconds")
            if (
                not targets
                or expected not in actual
                or [target.key() for target in targets] != operation["target_keys"]
                or len({target.key() for target in targets}) != len(targets)
                or any(target.org_id != approval.principal.tenant_id for target in targets)
            ):
                raise ValueError
            return min([await service._verify_hold(operation["reservation_id"], amount, target) for target in targets])
        except Exception:
            raise AdmissionRefusedError("native_test_original_funding_unknown") from None

    def _claim_cleanup(self, approval, operation, cleanup_operation_id, deadline, hold_deadline):
        now = self.clock().timestamp()
        if now >= min(deadline - CLEANUP_SEND_SECONDS, hold_deadline):
            raise AdmissionRefusedError("native_test_cleanup_horizon_exhausted")
        tenant, run = approval.principal.tenant_id, approval.run_id
        key = self.store.key(tenant, "CLEANUP", run + "#" + cleanup_operation_id)
        existing = self.store.document(key, "native_test_cleanup_attempt_v1")
        binding = {
            "run_id": run,
            "operation_id": cleanup_operation_id,
            "original_operation_id": operation["operation_id"],
            "execution_id": operation["facade_dispatch"]["execution_id"],
            "reservation_id": operation["reservation_id"],
        }
        if existing is not None:
            if any(existing.get(name) != value for name, value in binding.items()):
                raise AdmissionRefusedError("native_test_cleanup_operation_conflict")
            return {**existing, "replay": True}
        used = operation.get("native_cleanup_attempts_used", 0)
        if type(used) is not int or not 0 <= used < operation["facade_dispatch"]["cleanup_attempts"]:
            raise AdmissionRefusedError("native_test_cleanup_attempts_exhausted")
        updated = {**operation, "native_cleanup_attempts_used": used + 1, "version": operation["version"] + 1}
        attempt = {
            **binding,
            "attempt": used + 1,
            "status": "dispatch_started",
            "started_at": now,
            "dispatch_deadline_at": min(deadline - CLEANUP_SEND_SECONDS, hold_deadline),
        }
        self.store.transaction(
            [
                self.store.guard(self.store.authority_key(tenant, run), "tools_test_run_v1", approval.authority()),
                self.store.put(key, "native_test_cleanup_attempt_v1", attempt),
                {
                    "Put": {
                        "TableName": self.store.table,
                        "Item": self.store._item(updated),
                        "ConditionExpression": "document = :original",
                        "ExpressionAttributeValues": {":original": {"S": canonical(operation)}},
                    }
                },
            ]
        )
        if self.clock().timestamp() >= attempt["dispatch_deadline_at"]:
            raise AdmissionRefusedError("native_test_cleanup_horizon_exhausted")
        return {**attempt, "replay": False}

    async def cleanup(self, *, context, credential, operation_id, execution_id, tool):
        _uuid(operation_id)
        approval = await self.original_owner(context, credential)
        return await self._cleanup(approval, operation_id, execution_id, tool, context.expires_at.timestamp())

    async def _cleanup(self, approval, operation_id, execution_id, tool, login_deadline):
        operation, quote, registration = await self._original(approval, execution_id)
        if tool != registration.tool.replace(".", "___", 1):
            raise AdmissionRefusedError("native_test_original_execution_refused")
        hold_deadline = await self._funding(approval, operation, quote.total_usd)
        attempt = await run_in_threadpool(
            self._claim_cleanup,
            approval,
            operation,
            operation_id,
            operation["facade_dispatch"]["cleanup_deadline_at"],
            min(hold_deadline, login_deadline),
        )
        return approval, attempt

    async def authorize_cleanup(self, **kwargs):
        approval, attempt = await self.cleanup(**kwargs)
        return self._cleanup_decision(approval, attempt)

    async def authorize_atlassian_receipt(self, *, context, credential, operation_id, body):
        approval = await self.original_owner(context, credential)
        try:
            operation = await run_in_threadpool(self.store.read_operation, approval.principal.tenant_id, approval.run_id, _uuid(operation_id))
            execution_id = operation["facade_dispatch"]["execution_id"]
            original, _, registration = await self._original(approval, execution_id)
            retained = original["native_atlassian_request"]
            arguments_sha256 = hashlib.sha256(
                json.dumps(body.arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest()
            if (
                original != operation
                or body.action != "recover"
                or body.tool not in ATLASSIAN_CALLS
                or body.tool != registration.tool.replace(".", "___", 1)
                or retained["tool"] != body.tool
                or retained["request_id"] != str(body.request_id)
                or retained["arguments_sha256"] != arguments_sha256
                or body.execution_id is not None
                and str(body.execution_id) != execution_id
                or body.ttl_seconds is not None
                or self.clock().timestamp() >= operation["facade_dispatch"]["cleanup_deadline_at"]
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AdmissionRefusedError("native_test_original_receipt_refused") from None
        await run_in_threadpool(self._claim_status_read, approval)
        return {
            "schema_version": "1.0",
            "allowed": True,
            "receipt_id": operation["reservation_id"],
            "principal": {
                "user_id": approval.principal.user_id,
                "root_human_id": approval.principal.user_id,
                "tenant_id": approval.principal.tenant_id,
                "team_id": approval.principal.team_id,
            },
            "run_id": approval.run_id,
            "workload_id": "native-test-" + approval.credential_id,
            "run_expires_at": min(approval.cleanup_expires_at, operation["facade_dispatch"]["cleanup_deadline_at"]),
            "native_receipt_read": {
                "tool": body.tool,
                "request_id": str(body.request_id),
                "execution_id": execution_id,
                "original_operation_id": operation_id,
            },
        }

    async def reconcile_cleanup(self, *, tenant_id, run_id, operation_id, execution_id, tool):
        _uuid(run_id)
        _uuid(operation_id)
        approval = await run_in_threadpool(self.store.approval, tenant_id, run_id)
        authority = await run_in_threadpool(self.store.authority, tenant_id, run_id)
        if not is_effect_approval(approval) or authority != approval.authority() or approval.deployment_id != self.enrollment.deployment_id:
            raise AdmissionRefusedError("native_test_original_owner_refused")
        approval, attempt = await self._cleanup(approval, operation_id, execution_id, tool, approval.meter_expires_at)
        return self._cleanup_decision(approval, attempt)

    @staticmethod
    def _cleanup_decision(approval, attempt):
        if attempt["replay"]:
            return {"schema_version": "1.0", "native_status": attempt}
        return {
            "schema_version": "1.0",
            "allowed": True,
            "receipt_id": attempt["reservation_id"],
            "principal": {
                "user_id": approval.principal.user_id,
                "root_human_id": approval.principal.user_id,
                "tenant_id": approval.principal.tenant_id,
                "team_id": approval.principal.team_id,
            },
            "run_id": approval.run_id,
            "workload_id": "native-test-" + approval.credential_id,
            "run_expires_at": min(approval.cleanup_expires_at, approval.approved_at + 600),
            "native_cleanup": attempt,
        }

    def _operations(self, approval):
        try:
            tenant, run = approval.principal.tenant_id, approval.run_id
            page = self.store.client.query(
                TableName=self.store.table,
                KeyConditionExpression="pk = :tenant AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={":tenant": {"S": "TENANT#" + tenant}, ":prefix": {"S": "TOOLS_TEST_OP#" + run + "#"}},
                ConsistentRead=True,
                Limit=128,
            )
            if page.get("LastEvaluatedKey") or not page.get("Items"):
                raise ValueError
            operations = []
            for row in page["Items"]:
                document = json.loads(row["document"]["S"])
                operation = self.store.read_operation(tenant, run, document["operation_id"])
                if operation != document:
                    raise ValueError
                operations.append(operation)
            return operations
        except Exception:
            raise AdmissionRefusedError("native_test_accounting_inventory_unknown") from None

    def _execution_status(self, approval, operation):
        dispatch = operation["facade_dispatch"]
        result = {
            "execution_id": dispatch["execution_id"],
            "original_operation_id": operation["operation_id"],
            "reservation_id": operation["reservation_id"],
            "quoted_usd": operation.get("quote", {}).get("total_usd"),
            "contract_sha256": operation.get("quote", {}).get("source_contract_sha256"),
            "request_sha256": operation["binding"]["request_sha256"],
            "session_deadline_at": dispatch["session_deadline_at"],
            "cleanup_deadline_at": dispatch["cleanup_deadline_at"],
            "cleanup_attempts_used": operation.get("native_cleanup_attempts_used", 0),
            "cleanup_attempts_limit": dispatch["cleanup_attempts"],
            "cleanup_status": "pending" if self.clock().timestamp() < dispatch["cleanup_deadline_at"] else "unknown",
            "cleanup_confirmed_at": None,
            "cleanup_evidence_sha256": None,
        }
        try:
            registrations = [effect for effect in approval.effects if effect.effect_key == operation["binding"]["route"]]
            if len(registrations) != 1 or registrations[0].contract_sha256 != operation["quote"]["source_contract_sha256"]:
                raise ValueError
            row = self.execution_client.get_item(
                TableName=self.execution_table, Key={"execution_id": {"S": dispatch["execution_id"]}}, ConsistentRead=True
            ).get("Item")
            if row is None:
                return result
            expected = {
                "schema_version": "1.0",
                "execution_id": dispatch["execution_id"],
                "tool": registrations[0].tool.replace(".", "___", 1),
                "tenant_id": approval.principal.tenant_id,
                "owner_id": approval.principal.user_id,
                "team_id": approval.principal.team_id,
                "binding": "run:" + approval.run_id + ":native-test-" + approval.credential_id,
                "native_test_run_id": approval.run_id,
                "native_test_reservation_id": operation["reservation_id"],
            }
            if any(row.get(key, {}).get("S") != value for key, value in expected.items()):
                raise ValueError
            root, evidence = row["tool"]["S"], row.get("cleanup_evidence", {}).get("S")
            proved = (
                root in KNOWLEDGE_READS | ATLASSIAN_CALLS | {"websearch___search"}
                and evidence == "all_operations_settled"
                and row.get("operation_tracking", {}).get("S") == "1"
                and int(row.get("accepted_operations", {}).get("N", "0")) > 0
                and row.get("unresolved_operations", {}).get("N") == "0"
            ) or (root in {"browser___start", "code_interpreter___start"} and evidence == "provider_terminated")
            if row.get("state", {}).get("S") == "closed" or row.get("cleanup_status", {}).get("S") == "confirmed":
                confirmed_at = int(row["cleanup_confirmed_at"]["N"])
                if (
                    not proved
                    or row.get("state", {}).get("S") != "closed"
                    or row.get("cleanup_status", {}).get("S") != "confirmed"
                    or not approval.approved_at <= confirmed_at <= dispatch["cleanup_deadline_at"] <= approval.approved_at + 600
                ):
                    raise ValueError
                result.update(cleanup_status="confirmed", cleanup_confirmed_at=confirmed_at, cleanup_evidence_sha256=digest(row))
        except Exception:
            result["cleanup_status"] = "unknown"
        return result

    async def status(self, *, context, credential, run_id):
        approval = await self.original_owner(context, credential, run_id=run_id)
        await run_in_threadpool(self._claim_status_read, approval)
        result = {
            "schema_version": "1.0",
            "run_id": approval.run_id,
            "run_started_at": approval.approved_at,
            "principal": {"user_id": approval.principal.user_id, "tenant_id": approval.principal.tenant_id},
            "cap_usd": "20",
            "funding_status": "unknown",
            "accounting_status": "unknown",
            "held_usd": None,
            "settled_usd": None,
            "governance_complete": False,
            "executions": [],
        }
        try:
            operations = await run_in_threadpool(self._operations, approval)
            result["executions"] = [
                await run_in_threadpool(self._execution_status, approval, operation)
                for operation in operations
                if operation.get("facade_dispatch", {}).get("new_execution") is True
            ]
            quoted = Decimal("0")
            for operation in operations:
                amount = Decimal(operation["quote"]["total_usd"])
                if not amount.is_finite() or amount <= 0 or operation["reservation_confirmed"] is not True:
                    raise ValueError
                await self._funding(approval, operation, amount)
                quoted += amount
            service = TestRunAdmissionService(store=self.store, budget=self.budget, clock=self.clock)
            snapshot = await self.enrollment.reservations.snapshot(service._target(approval.authority()))
            if snapshot is None or snapshot.total_usd != quoted or quoted > 20:
                raise ValueError
            # No source in this slice can establish complete billed quantities.
            # Preserve all holds and expose the incomplete governance gate.
            result.update(funding_status="held", accounting_status="pending", held_usd=str(quoted), settled_usd="0")
        except Exception:
            pass
        return result

    def _claim_status_read(self, approval):
        """Consume one original setup-funded read before its inventory work."""
        key = self.store.key(approval.principal.tenant_id, "STATUS", approval.run_id)
        previous = self.store.document(key, "native_test_status_reads_v1")
        used = previous.get("attempts") if previous is not None else 0
        if (
            type(used) is not int
            or not 0 <= used < MAX_STATUS_READS
            or previous is not None
            and previous != {"run_id": approval.run_id, "attempts": used}
        ):
            raise AdmissionRefusedError("native_test_status_reads_exhausted")
        update = self.store.put(key, "native_test_status_reads_v1", {"run_id": approval.run_id, "attempts": used + 1})
        if previous is not None:
            update["Put"]["ConditionExpression"] = "document = :original"
            update["Put"]["ExpressionAttributeValues"] = {":original": {"S": canonical(previous)}}
        self.store.transaction(
            [
                self.store.guard(self.store.authority_key(approval.principal.tenant_id, approval.run_id), "tools_test_run_v1", approval.authority()),
                update,
            ]
        )

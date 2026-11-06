"""Durable one-shot HTTP transfer of the original all-in effect reservation.

The facade must consume the private proof before its first paid local effect.
Only the digest is persisted. Replays return journal status without the proof;
a lost dispatch acknowledgement cannot be retried into a provider submission.
Composition stays unavailable until the full lifecycle runtime is installed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_effect_admission import NativeTestEffectAdmission
from src.agentauth.native_test_enrollment import is_effect_approval
from src.agentauth.native_test_facade_contract import (
    ATLASSIAN_CALLS,
    STATEFUL_STARTS,
    FacadeEffectQuoteAdapter,
    InterpreterStepCostContract,
    NativeFacadeCall,
)
from src.agentauth.test_run_admission import AdmissionReceipt, PreparedModelAdmission, _receipt
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest
from src.budget.reservations import ReservationTarget
from src.shared.schemas.auth import TokenContext


def native_interpreter_request_key(store, tenant, user_id, request_id):
    return store.key(tenant, "REQUEST", digest({"user_id": user_id, "request_id": request_id}))


def _priced_hold(prepared):
    try:
        quoted = prepared.receipt.quoted_usd
        reserved = prepared.receipt.reserved_usd
        cap = prepared._authority["cap_usd"]
        if (
            cap != "20"
            or not isinstance(quoted, str)
            or quoted != reserved
            or not (Decimal("0") < Decimal(quoted) <= Decimal(cap))
            or Decimal(quoted) != prepared._quote.total_usd
        ):
            raise ValueError
    except (AttributeError, InvalidOperation, KeyError, TypeError, ValueError):
        raise AdmissionRefusedError("native_test_facade_price_unavailable") from None
    return quoted, reserved, cap


class _FacadeEffectAdmission(NativeTestEffectAdmission):
    def __init__(self, *, secret=None, contract=None, run_started_at=None, **kwargs):
        super().__init__(**kwargs)
        self.secret, self.contract = secret, contract
        self.run_started_at = run_started_at

    def _new_operation(self, operation, authority, body):
        call = NativeFacadeCall.parse(body)
        if self.secret is None or self.contract is None or type(self.run_started_at) is not int:
            raise AdmissionRefusedError("native_test_facade_preparation_refused")
        now = int(self.clock().timestamp())
        # Existing provider contracts require UUID4 syntax. Derive those bits
        # from the original run/operation instead of accepting a caller ID.
        execution_id = call.execution_id or str(
            UUID(bytes=hashlib.sha256((authority["run_id"] + ":native-facade:" + operation["operation_id"]).encode()).digest()[:16], version=4)
        )
        return {
            **operation,
            **(
                {
                    "native_atlassian_request": {
                        "tool": call.tool,
                        "request_id": call.request_id,
                        "arguments_sha256": hashlib.sha256(
                            json.dumps(call.arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
                        ).hexdigest(),
                        "approved_sha256": call.approved_digest(),
                    }
                }
                if call.tool in ATLASSIAN_CALLS
                else {}
            ),
            **(
                {
                    "interpreter_request": {
                        "run_id": authority["run_id"],
                        "request_id": call.request_id,
                        "user_id": authority["user_id"],
                        "execution_id": execution_id,
                        "operation_id": operation["operation_id"],
                        "tool": call.tool,
                    }
                }
                if type(self.contract) is InterpreterStepCostContract and call.action != "list"
                else {}
            ),
            "facade_dispatch": {
                "proof_sha256": hashlib.sha256(self.secret.encode()).hexdigest(),
                "execution_id": execution_id,
                "new_execution": call.action == "call" and call.execution_id is None,
                "stateful_session": call.tool in STATEFUL_STARTS and call.execution_id is None,
                "session_deadline_at": min(
                    now + (call.ttl_seconds or self.contract.session_seconds), authority["dispatch_expires_at"], self.run_started_at + 300
                ),
                "cleanup_deadline_at": min(self.run_started_at + self.contract.cleanup_seconds, authority["cleanup_expires_at"]),
                "cleanup_attempts": self.contract.cleanup_attempts,
                **({"interpreter_step": self.contract.interpreter_step} if type(self.contract) is InterpreterStepCostContract else {}),
            },
        }

    def _claim_operation(self, authority, operation, now):
        items = []
        if type(self.contract) is InterpreterStepCostContract:
            step = self.contract.interpreter_step
            dispatch = operation["facade_dispatch"]
            if dispatch.get("interpreter_step") != step:
                raise AdmissionRefusedError("native_test_interpreter_step_refused")
            items.append(
                self.store.put(
                    self.store.key(authority["tenant_id"], "INTERPRETER_STEP", authority["run_id"] + "#" + step),
                    "native_test_interpreter_step_v1",
                    {
                        "run_id": authority["run_id"],
                        "step": step,
                        "operation_id": operation["operation_id"],
                        "execution_id": dispatch["execution_id"],
                        "binding_digest": operation["binding_digest"],
                    },
                )
            )
            if step != "list":
                request = operation["interpreter_request"]
                if step == "start":
                    items.append(
                        self.store.put(
                            self.store.key(authority["tenant_id"], "EXECUTION_OWNER", dispatch["execution_id"]),
                            "native_test_interpreter_execution_v1",
                            {
                                "run_id": authority["run_id"],
                                "execution_id": dispatch["execution_id"],
                                "user_id": authority["user_id"],
                                "operation_id": operation["operation_id"],
                            },
                        )
                    )
                request_key = native_interpreter_request_key(self.store, authority["tenant_id"], authority["user_id"], request["request_id"])
                if step in {"execute_status", "result"}:
                    original_request = self.store.document(request_key, "native_test_interpreter_request_v1")
                    if (
                        not original_request
                        or original_request.get("run_id") != authority["run_id"]
                        or original_request.get("execution_id") != dispatch["execution_id"]
                        or original_request.get("tool") != "code_interpreter___execute"
                    ):
                        raise AdmissionRefusedError("native_test_original_interpreter_execute_required")
                    original_execute = self.store.read_operation(authority["tenant_id"], authority["run_id"], original_request["operation_id"])
                    if (
                        not original_execute
                        or original_execute.get("status") != "dispatch_started"
                        or original_execute.get("reservation_confirmed") is not True
                        or original_execute.get("interpreter_request") != original_request
                    ):
                        raise AdmissionRefusedError("native_test_original_interpreter_execute_required")
                    items.append(self.store.guard(request_key, "native_test_interpreter_request_v1", original_request))
                    items.append(
                        {
                            "ConditionCheck": {
                                "TableName": self.store.table,
                                "Key": self.store.operation_key(authority["tenant_id"], authority["run_id"], original_execute["operation_id"]),
                                "ConditionExpression": "document = :original",
                                "ExpressionAttributeValues": {":original": self.store._item(original_execute)["document"]},
                            }
                        }
                    )
                else:
                    items.append(self.store.put(request_key, "native_test_interpreter_request_v1", request))
            if step not in {"list", "start"}:
                # Claiming a child requires the permanent original session slot
                # from this run. Unknown/closed sessions never reset the slot.
                key = {"pk": {"S": "TENANT#" + authority["tenant_id"]}, "sk": {"S": "TOOLS_TEST_SESSION#" + authority["run_id"]}}
                try:
                    session = self.store._read(key)
                    original = self.store.read_operation(authority["tenant_id"], authority["run_id"], session["operation_id"]["S"])
                    if (
                        session["execution_id"]["S"] != dispatch["execution_id"]
                        or session.get("tool", {}).get("S") != "code_interpreter___start"
                        or original["status"] != "dispatch_started"
                        or original.get("reservation_confirmed") is not True
                        or original["facade_dispatch"].get("interpreter_step") != "start"
                        or original["facade_dispatch"]["execution_id"] != dispatch["execution_id"]
                    ):
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    raise AdmissionRefusedError("native_test_original_interpreter_start_required") from None
                items.append(
                    {
                        "ConditionCheck": {
                            "TableName": self.store.table,
                            "Key": key,
                            "ConditionExpression": "execution_id = :execution AND operation_id = :original AND tool = :tool",
                            "ExpressionAttributeValues": {
                                ":execution": {"S": dispatch["execution_id"]},
                                ":original": session["operation_id"],
                                ":tool": {"S": "code_interpreter___start"},
                            },
                        }
                    }
                )
                items.append(
                    {
                        "ConditionCheck": {
                            "TableName": self.store.table,
                            "Key": self.store.operation_key(authority["tenant_id"], authority["run_id"], original["operation_id"]),
                            "ConditionExpression": "document = :original",
                            "ExpressionAttributeValues": {":original": {"S": self.store._item(original)["document"]["S"]}},
                        }
                    }
                )
        if operation["facade_dispatch"]["new_execution"]:
            items.append(
                self.store.put(
                    self.store.key(authority["tenant_id"], "EXECUTION", authority["run_id"] + "#" + operation["facade_dispatch"]["execution_id"]),
                    "native_test_execution_binding_v1",
                    {
                        "run_id": authority["run_id"],
                        "operation_id": operation["operation_id"],
                        "execution_id": operation["facade_dispatch"]["execution_id"],
                    },
                )
            )
        if operation["facade_dispatch"]["stateful_session"]:
            # One session for the whole original test, shared by browser and
            # interpreter. Failure or closure never replenishes this allowance.
            items += [
                {
                    "Put": {
                        "TableName": self.store.table,
                        "Item": {
                            "pk": {"S": "TENANT#" + authority["tenant_id"]},
                            "sk": {"S": "TOOLS_TEST_SESSION#" + authority["run_id"]},
                            "operation_id": {"S": operation["operation_id"]},
                            "execution_id": {"S": operation["facade_dispatch"]["execution_id"]},
                            **(
                                {"tool": {"S": "code_interpreter___start"}} if operation["facade_dispatch"].get("interpreter_step") == "start" else {}
                            ),
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            ]
        return self.store.claim(authority, operation, now, claim_items=items)

    async def consume(self, *, context, credential, operation_id, body, proof):
        identity, authority = await self._authority(context, credential)
        operation = await run_in_threadpool(self.store.read_operation, identity.tenant_id, identity.run_id, operation_id)
        try:
            if (
                not isinstance(proof, str)
                or len(proof) != 43
                or not hmac.compare_digest(operation["facade_dispatch"]["proof_sha256"], hashlib.sha256(proof.encode()).hexdigest())
                or operation["status"] != "admitted"
                or operation.get("native_cleanup_attempts_used", 0) != 0
            ):
                raise ValueError
            quote = self._restore_quote(operation["quote"])
            targets = [ReservationTarget(**{**target, "headroom_usd": Decimal(target["headroom_usd"])}) for target in operation["targets"]]
            target_key = self._target(authority).key()
            budget_context = TokenContext(
                user_id=identity.subject_id,
                org_id=identity.tenant_id,
                team_id=identity.team_id,
                department_id=identity.department_id,
                account_type="human",
                expires_at=context.expires_at,
            )
            budget_context._budget_admission_targets = targets
            budget_context._policy_flow_target = next(target for target in targets if target.key() == target_key)
            budget_context._policy_request_id = operation["reservation_id"]
            budget_context._policy_estimated_cost = quote.total_usd
            self._bind_quote(budget_context, quote, body, self.effect_key, authority)
            prepared = PreparedModelAdmission(_receipt(operation, replay=False), authority, operation, budget_context, quote)
        except (KeyError, ValueError, TypeError, StopIteration):
            raise AdmissionRefusedError("native_test_facade_dispatch_refused") from None
        receipt = await self.begin_effect_dispatch(prepared, context=context, credential=credential, body=body)
        return {
            "schema_version": "1.0",
            "operation_id": operation_id,
            "reservation_id": receipt.reservation_id,
            "status": "dispatch_started",
            "dispatch_deadline_at": budget_context._native_test_dispatch_deadline,
        }


class NativeFacadeAdmission:
    """Server-owned gateway/facade adapter; not a lifecycle readiness claim."""

    def __init__(self, *, enrollment, budget):
        self.enrollment, self.budget = enrollment, budget

    async def _service(self, *, context, credential, body, secret=None):
        call = NativeFacadeCall.parse(body)
        identity = await self.enrollment.verify(context, credential)
        approval = await run_in_threadpool(self.enrollment.store.approval, identity.tenant_id, identity.run_id)
        if not is_effect_approval(approval):
            raise AdmissionRefusedError("native_test_all_in_credential_unavailable")
        matches = []
        for registration in approval.effects:
            permission = call.tool.replace("___", ".", 1) if call.action in {"call", "recover"} else None
            if registration.action != call.action or (permission is not None and registration.tool != permission):
                continue
            source = FacadeEffectQuoteAdapter(
                store=self.enrollment.store,
                deployment_id=approval.deployment_id,
                contract_id=registration.contract_id,
                contract_sha256=registration.contract_sha256,
                source_sha256=registration.source_sha256,
            )
            _, contract = await run_in_threadpool(source._read, self.enrollment.clock())
            if source._request_permitted(body, contract):
                matches.append((registration, source, contract))
        if len(matches) != 1:
            raise AdmissionRefusedError("native_test_facade_contract_refused")
        registration, source, contract = matches[0]
        return approval, _FacadeEffectAdmission(
            effect_key=registration.effect_key,
            store=self.enrollment.store,
            budget=self.budget,
            identity_verifier=self.enrollment,
            quote_adapter=source,
            clock=self.enrollment.clock,
            secret=secret,
            contract=contract,
            run_started_at=approval.approved_at,
        )

    async def prepare(self, *, context, credential, operation_id, body):
        proof = secrets.token_urlsafe(32)
        approval, service = await self._service(context=context, credential=credential, body=body, secret=proof)
        prepared = await service.prepare_effect(context=context, credential=credential, operation_id=operation_id, body=body)
        if isinstance(prepared, AdmissionReceipt):
            return {"schema_version": "1.0", "native_status": asdict(prepared)}
        quoted_usd, reserved_usd, run_cap_usd = _priced_hold(prepared)
        dispatch = prepared._operation["facade_dispatch"]
        decision = {
            "schema_version": "1.0",
            "allowed": True,
            "receipt_id": prepared.receipt.reservation_id,
            "principal": {
                "user_id": approval.principal.user_id,
                "root_human_id": approval.principal.user_id,
                "tenant_id": approval.principal.tenant_id,
                "team_id": approval.principal.team_id,
            },
            "run_id": approval.run_id,
            "workload_id": "native-test-" + approval.credential_id,
            "run_expires_at": approval.dispatch_expires_at,
            "native_admission": {
                "operation_id": operation_id,
                "proof": proof,
                "reservation_id": prepared.receipt.reservation_id,
                "quoted_usd": quoted_usd,
                "reserved_usd": reserved_usd,
                "run_cap_usd": run_cap_usd,
                "request_sha256": hashlib.sha256(body).hexdigest(),
                **{key: value for key, value in dispatch.items() if key not in {"proof_sha256", "stateful_session", "interpreter_step"}},
                "frozen_grants": sorted({effect.tool.replace(".", "___", 1) for effect in approval.effects if effect.action == "call"}),
            },
        }
        if NativeFacadeCall.parse(body).action == "list":
            from src.agentauth.native_test_atlassian_runtime import NativeAtlassianRuntime
            from src.agentauth.native_test_interpreter_runtime import NativeInterpreterRuntime
            from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeRuntime
            from src.internal.tools_authorize import ToolDescription

            try:
                bridge = self.enrollment._effect_runtime
                if type(bridge) in {NativeKnowledgeRuntime, NativeInterpreterRuntime, NativeAtlassianRuntime}:
                    bridge.coverage()
                    catalogue = (await run_in_threadpool(bridge.catalogue.read, approval))["facade_tools"]
                else:
                    policy = await run_in_threadpool(
                        self.enrollment.policy_store.get, tenant_id=approval.principal.tenant_id, owner_id=approval.principal.user_id
                    )
                    catalogue = policy["catalog"]
                if not isinstance(catalogue, list) or len(catalogue) > 64:
                    raise ValueError
                tools = [ToolDescription.model_validate(value) for value in catalogue]
                decision["tools"] = [tool.model_dump(by_alias=True) for tool in tools if tool.name.replace("___", ".", 1) in approval.tools]
            except (ValueError, TypeError, KeyError):
                raise AdmissionRefusedError("native_test_catalogue_unavailable") from None
        return decision

    async def dispatch(self, *, context, credential, operation_id, body, proof):
        _, service = await self._service(context=context, credential=credential, body=body)
        return await service.consume(context=context, credential=credential, operation_id=operation_id, body=body, proof=proof)

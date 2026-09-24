"""One durable, budgeted Messages invocation per canonical Task turn."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from src.agentauth.task_budget import task_budget
from src.agentauth.task_model_binding import resolve_task_model
from src.agentauth.task_turns import TaskTurnStore
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.pricing_decisions import price_completed_usage
from src.chat_logging.service import ChatLoggingService
from src.orchestration.provider_quotes import confirm_quote_spendable, quote_request
from src.proxy.bedrock_signing import bedrock_destination_signer
from src.proxy.pricing_capture import PricingCapture
from src.shared.config import get_settings
from src.tasks.records import base_item, model_operation_sort_key, payload_digest, task_ops_partition, task_partition
from src.tasks.store import TaskStoreError, _serialize
from src.usage.service import UsageService

logger = logging.getLogger(__name__)

_RECEIPT_FIELDS = {"schema_version", "task_id", "turn_id", "operation_status", "handoff", "request_digest",
                   "model_id", "claimed_at", "completed_at", "usage", "reservation_status", "automatic_replay_permitted",
                   "content", "stop_reason"}


async def invoke_task_messages(db, *, identity, binding, target, request, operation_id):
    credentials = None
    if not target.is_platform:
        credentials = await bedrock_destination_signer.get_credentials(db, target, user_id=identity.canonical_principal)
    kwargs = {"region_name": target.region or get_settings().aws_region,
              "config": Config(connect_timeout=5, read_timeout=120, retries={"total_max_attempts": 1})}
    if credentials:
        kwargs.update(aws_access_key_id=credentials.access_key_id, aws_secret_access_key=credentials.secret_access_key,
                      aws_session_token=credentials.session_token)
    client = boto3.client("bedrock-runtime", **kwargs)
    capture = PricingCapture(request_id=operation_id, original_model=binding["model_id"])
    capture.forwarded(client, binding["model_id"])
    body = json.dumps({"anthropic_version": "bedrock-2023-05-31", **request}, separators=(",", ":"))

    def invoke():
        response = client.invoke_model(modelId=binding["model_id"], contentType="application/json", accept="application/json", body=body)
        raw = response["body"].read(65537)
        response["body"].close()
        if len(raw) > 65536:
            raise TaskStoreError("model response exceeds task frame bound")
        return json.loads(raw), response

    document, metadata = await run_in_threadpool(invoke)
    content = document.get("content")
    if (not isinstance(content, list) or not content or any(not isinstance(block, dict) or set(block) != {"type", "text"}
            or block["type"] != "text" or not isinstance(block["text"], str) for block in content)):
        raise TaskStoreError("task provider returned unsupported content")
    usage = document.get("usage", {})
    if any(type(usage.get(field)) is not int or usage[field] < 0 for field in ("input_tokens", "output_tokens")):
        raise TaskStoreError("task provider usage unavailable")
    if usage["output_tokens"] > request["max_tokens"]:
        raise TaskStoreError("task provider output exceeded bound")
    capture.response(document, metadata)
    if not capture.provider_request_id or document.get("stop_reason") not in {"end_turn", "max_tokens", "stop_sequence"}:
        raise TaskStoreError("task provider completion receipt unavailable")
    decision = await price_completed_usage(request_id=operation_id, org_id=identity.tenant,
        raw_usage=capture.raw_usage, evidence=capture.routing, api_format="anthropic")
    return {"content": content, "stop_reason": document.get("stop_reason"),
            "usage": {key: usage[key] for key in ("input_tokens", "output_tokens")},
            "price": decision, "provider_request_id": capture.provider_request_id}


async def write_task_usage_event(*, context, identity, binding, decision, usage, latency_ms):
    """Await the existing budget-tracker transport; persist no conversation text."""
    logger_service = ChatLoggingService()
    if not logger_service.enabled:
        raise TaskStoreError("task usage settlement transport unavailable")
    timestamp = datetime.now(UTC)
    document = logger_service._build_chat_log(request_id=decision.request_id, timestamp=timestamp,
        org_id=identity.tenant, user_id=context.user_id, team_id=context.team_id, root_human_id="",
        account_type="service", model=binding["model_id"], api_format="anthropic", latency_ms=latency_ms,
        scrubbed_request={}, scrubbed_response={"model": binding["model_id"], "usage": usage},
        scrub_level="basic", total_redactions=0, pii_types_found=[], patterns_matched=[], headers_scrubbed=[],
        pricing_decision=decision.to_dict())
    written = await logger_service._get_s3_writer().write_log(log_data=document.model_dump(mode="json"),
        org_id=identity.tenant, user_id=context.user_id, request_id=decision.request_id, timestamp=timestamp)
    if not written:
        raise TaskStoreError("task usage settlement event unavailable")


class TaskModel:
    def __init__(self, repository, *, db, budget=None, readiness=resolve_task_model,
                 provider=invoke_task_messages, enforcement=None, usage_writer=None, event_writer=write_task_usage_event, clock=None):
        self.repository, self.db = repository, db
        self.budget = budget or task_budget(repository)
        self.readiness, self.provider = readiness, provider
        self.enforcement = enforcement or BudgetEnforcementService(db_session=db)
        self.usage_writer = usage_writer or UsageService(db).log_request
        self.event_writer = event_writer
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def receipt(operation):
        return {name: value for name, value in operation.items() if name in _RECEIPT_FIELDS}

    def _read(self, task_id, turn_id):
        return self.repository._get(task_ops_partition(task_id), model_operation_sort_key(turn_id))

    def _current(self, identity):
        task = self.repository.read_task(identity.task_id)
        if task is None or (task["invocation_id"], int(task["generation"]), task.get("runtime_attempt_id")) != (
                identity.invocation_id, identity.generation, identity.runtime_attempt_id):
            raise TaskStoreError("model attempt changed")
        if task["state"] not in {"accepted", "queued", "running", "waiting_for_input"}:
            raise TaskStoreError("task cannot invoke a model")
        if task["deadline_at"] <= self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"):
            raise TaskStoreError("task model deadline elapsed")
        self.repository.resolve_work(task["dispatch_id"], expected_kind="dispatch")
        return task

    def _claim(self, *, identity, turn_id, digest, model_id):
        task = self._current(identity)
        if not any(turn["turn_id"] == turn_id for turn in TaskTurnStore(self.repository).list_turns(identity.task_id)):
            raise TaskStoreError("model turn has not been committed")
        operation = base_item(partition=task_ops_partition(identity.task_id), sort_key=model_operation_sort_key(turn_id),
            record_type="TASK_OPS", scope=task["scope"]) | {
            "task_id": identity.task_id, "turn_id": turn_id, "operation_status": "pending", "handoff": "not_started",
            "request_digest": digest, "model_id": model_id, "claimed_at": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "automatic_replay_permitted": False, "usage": None, "reservation_status": "unknown",
            "owner_token": str(uuid.uuid4()), "invocation_id": identity.invocation_id, "generation": identity.generation,
            "runtime_attempt_id": identity.runtime_attempt_id}
        transaction = [{"Put": {"TableName": self.repository.table_name, "Item": _serialize(operation),
                                 "ConditionExpression": "attribute_not_exists(event_id)"}},
            {"Update": {"TableName": self.repository.table_name,
                "Key": _serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
                "UpdateExpression": "SET #version = :next",
                "ConditionExpression": "#version = :version AND runtime_attempt_id = :attempt",
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": _serialize({":next": int(task["version"]) + 1, ":version": int(task["version"]),
                                                         ":attempt": identity.runtime_attempt_id})}}]
        transaction.extend(self.repository._authority_condition_checks(snapshot=task, runtime_attempt_id=identity.runtime_attempt_id))
        try:
            self.repository._client.transact_write_items(TransactItems=transaction)
            return operation, True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            existing = self._read(identity.task_id, turn_id)
            if existing and existing["request_digest"] == digest:
                return existing, False
            raise TaskStoreError("model operation claim refused") from None

    def _save(self, operation, **updates):
        updated = {**operation, **updates}
        # Provider evidence belongs to the claimed operation even if cancellation
        # revoked live authority while the provider was processing it. It cannot
        # change task outcome or authorize another provider call.
        for _ in range(3):
            task = self.repository.read_task(operation["task_id"])
            try:
                self.repository._client.transact_write_items(TransactItems=[
                    {"Put": {"TableName": self.repository.table_name, "Item": _serialize(updated),
                        "ConditionExpression": "owner_token = :owner AND request_digest = :digest AND operation_status = :previous",
                        "ExpressionAttributeValues": _serialize({":owner": operation["owner_token"], ":digest": operation["request_digest"],
                                                                 ":previous": operation["operation_status"]})}},
                    {"Update": {"TableName": self.repository.table_name,
                        "Key": _serialize({"event_id": task_partition(operation["task_id"]), "arrived_at": "META"}),
                        "UpdateExpression": "SET #version = :next", "ConditionExpression": "#version = :version",
                        "ExpressionAttributeNames": {"#version": "version"},
                        "ExpressionAttributeValues": _serialize({":version": int(task["version"]), ":next": int(task["version"]) + 1})}},
                ])
                return updated
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
        raise TaskStoreError("model receipt persistence unavailable")

    async def execute(self, *, identity, turn_id, request_digest, request):
        if payload_digest(request) != request_digest:
            raise TaskStoreError("model request digest mismatch")
        task = await run_in_threadpool(self._current, identity)
        existing = await run_in_threadpool(self._read, identity.task_id, turn_id)
        if existing:
            if existing["request_digest"] != request_digest:
                raise TaskStoreError("model turn already has another request")
            return self.receipt(existing)
        grant = await run_in_threadpool(self.repository._get_authority, "TENANT#" + identity.tenant,
                                       f"TASK_RUN#{identity.invocation_id}#GEN#{identity.generation:010d}")
        if request["max_tokens"] > int(grant["limits"]["max_output_tokens_per_turn"]):
            raise TaskStoreError("task model output bound exceeded")
        binding, policy, target = await self.readiness(self.db, tenant=identity.tenant, principal=identity.canonical_principal,
            deadline=datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00")),
            expected_policy_version=grant["model_binding"]["model_policy_version"], include_context=True)
        if binding != grant["model_binding"]:
            raise TaskStoreError("task model binding changed")
        operation, owned = await run_in_threadpool(self._claim, identity=identity, turn_id=turn_id,
                                                  digest=request_digest, model_id=binding["model_id"])
        if not owned:
            return self.receipt(operation)
        started = time.monotonic()
        sent = False
        reserved = False
        context = policy.context
        try:
            quote_body = json.dumps({"model": binding["model_id"], **request}, separators=(",", ":")).encode()
            quote = await quote_request(quote_body, "/v1/messages")
            budget_target = replace(self.budget._target(scope="task:" + identity.task_id, cap=grant["limits"]["max_usd"]), entity_type="run")
            await self.budget._initialize(budget_target)
            context._budget_enforcement_enabled = True
            context._policy_flow_target = budget_target
            context._policy_quote, context._policy_estimated_cost, context._policy_request_id = quote, quote.total_usd, turn_id
            verdict = await self.enforcement.check_budget_hierarchy(context, quote.total_usd, request_id=turn_id)
            if not verdict.allowed:
                raise TaskStoreError("model budget refused")
            reserved = True
            if await confirm_quote_spendable(quote) is not None:
                raise TaskStoreError("model quote expired")
            await run_in_threadpool(self._current, identity)
            operation = await run_in_threadpool(self._save, operation, reservation_status="reserved", handoff="prepared")
            sent = True  # Every failure from this point conservatively retains the upper bound.
            result = await self.provider(self.db, identity=identity, binding=binding, target=target, request=request, operation_id=turn_id)
            decision = result["price"]
            measured_cost = Decimal(decision.ledger_cost_usd)
            # Missing pricing facts retain the full upper bound, never a guessed
            # zero. The stored pricing decision explicitly records its confidence.
            charged = measured_cost if decision.confidence == "verified" else quote.total_usd
            if charged > quote.total_usd or charged < 0:
                raise TaskStoreError("provider price exceeds reserved bound")
            receipt_usage = {**result["usage"], "estimated_usd": float(charged)}
            operation = await run_in_threadpool(self._save, operation, operation_status="confirmed", handoff="confirmed",
                completed_at=self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"), usage=receipt_usage,
                content=result["content"], stop_reason=result["stop_reason"], reservation_status="reserved",
                pricing_decision=decision.to_dict(), provider_request_id=result["provider_request_id"])
            if decision.confidence != "verified":
                return self.receipt(operation)  # Exact usage exists; unknown price retains the full hold.
            await self.usage_writer(context=context, model=binding["model_id"],
                input_tokens=result["usage"]["input_tokens"], output_tokens=result["usage"]["output_tokens"],
                cost_usd=charged, latency_ms=int((time.monotonic() - started) * 1000), status_code=200,
                request_id=turn_id, agent_run_id=identity.invocation_id, pricing_decision=decision,
                provider_request_id=result["provider_request_id"], bedrock_account_id=target.account_id,
                destination_region=target.region or get_settings().aws_region)
            await self.event_writer(context=context, identity=identity, binding=binding, decision=decision,
                usage=result["usage"], latency_ms=int((time.monotonic() - started) * 1000))
            operation = await run_in_threadpool(self._save, operation, usage_logged=True)
            await self.enforcement.reconcile_reservation(context, turn_id, binding["model_id"],
                result["usage"]["input_tokens"], result["usage"]["output_tokens"], actual_cost_usd=charged, usage_known=True)
            await self.budget.verify_settlement(turn_id, charged, [budget_target])
            operation = await run_in_threadpool(self._save, operation, reservation_status="settled")
            return self.receipt(operation)
        except Exception as exc:
            logger.warning("Task model operation interrupted", extra={"task_id": identity.task_id, "turn_id": turn_id,
                "exception_type": type(exc).__name__, "provider_may_have_started": sent})
            if operation["operation_status"] == "confirmed":
                return self.receipt(operation)  # Never discard a durable provider receipt.
            if reserved and not sent:
                await self.enforcement.reconcile_reservation(
                    context, turn_id, binding["model_id"], 0, 0, actual_cost_usd=Decimal(0), usage_known=True)
            operation = await run_in_threadpool(self._save, operation,
                operation_status="unknown" if sent else "rejected", handoff="unknown" if sent else "not_started",
                reservation_status="unknown" if sent else ("reserved" if reserved else "not_reserved"), usage=None)
            return self.receipt(operation)

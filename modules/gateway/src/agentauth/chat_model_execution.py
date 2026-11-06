"""Scoped chat model handoff using the gateway's provider and accounting services."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import rfc8785
from starlette.concurrency import run_in_threadpool

from src.agentauth.agentcore_recall import RECALLED_CONTEXT_END, RECALLED_CONTEXT_START
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_model_journal import ChatModelJournal
from src.agentauth.chat_model_json import canonical_model_json
from src.agentauth.chat_model_provider import invoke_chat_messages
from src.agentauth.model_policy import _resolve_active_allowlist_policy
from src.agentauth.task_budget import TaskBudget
from src.agentauth.task_model import write_task_usage_event
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.enforcement_settings import BudgetAccountingGap
from src.orchestration.provider_quotes import confirm_quote_spendable, quote_request
from src.proxy.bedrock_routing import bedrock_routing_resolver
from src.proxy.model_resolver import production_model_resolver
from src.shared.config import get_settings
from src.usage.service import UsageService

logger = logging.getLogger(__name__)

# Bounded, operator-facing text of the exception that ended a rejected or unknown
# operation; never the request, a token or an identity (#8048).
ERROR_DETAIL_MAX_LENGTH = 300

_RECEIPT_FIELDS = frozenset(
    {
        "run_id",
        "session_id",
        "operation_id",
        "request_digest",
        "model_id",
        "lease_generation",
        "status",
        "handoff",
        "reservation_status",
        "usage",
        "automatic_replay_permitted",
        "content",
        "stop_reason",
        "error_code",
        "error_class",
        "error_detail",
    }
)


@dataclass(frozen=True)
class CrewModelIdentity:
    run_id: str
    root_human_id: str


async def resolve_chat_provider(db, launch, model_id):
    policy = await _resolve_active_allowlist_policy(
        db,
        tenant_id=launch.tenant_id,
        principal_kind="human",
        principal_id=launch.user_id,
        expires_at=datetime.fromtimestamp(launch.expires_at, UTC),
        require_hierarchy=True,
    )
    production_model_resolver(get_settings()).check_model_access(model_id, policy.context)
    if "anthropic." not in model_id:
        raise ChatAuthorizationUnavailableError("chat model transport unsupported")
    target = await bedrock_routing_resolver.resolve(db, policy.context, user_id=policy.routing_user_id)
    return policy.context, target


async def record_chat_accounting_gap(db, operation_id, scope):
    if await db.get(BudgetAccountingGap, operation_id) is None:
        db.add(BudgetAccountingGap(request_id=operation_id, scope_key=scope))
        await db.commit()


class ChatModelExecution:
    def __init__(
        self,
        authority,
        db,
        *,
        readiness=resolve_chat_provider,
        provider=invoke_chat_messages,
        enforcement=None,
        usage_writer=None,
        event_writer=write_task_usage_event,
        gap_writer=record_chat_accounting_gap,
        clock=None,
    ):
        self.authority, self.db = authority, db
        self.journal = ChatModelJournal(authority)
        self.readiness, self.provider = readiness, provider
        self.enforcement = enforcement or BudgetEnforcementService(db_session=db)
        self.usage_writer = usage_writer or UsageService(db).log_request
        self.event_writer = event_writer
        self.gap_writer = gap_writer
        self.clock = clock or (lambda: int(datetime.now(UTC).timestamp()))

    @staticmethod
    def receipt(operation):
        return {field: value for field, value in operation.items() if field in _RECEIPT_FIELDS}

    async def _save(self, launch, operation, *, authorize=False, **updates):
        result = await run_in_threadpool(
            self.journal.transition,
            launch,
            operation,
            now=self.clock(),
            authorize=authorize,
            **updates,
        )
        if result is None:
            raise ChatAuthorizationRefusedError("chat model operation changed")
        return result

    async def _settle(self, context, operation_id, model_id, usage, amount):
        await self.enforcement.reconcile_reservation(
            context,
            operation_id,
            model_id,
            usage["input_tokens"],
            usage["output_tokens"],
            actual_cost_usd=amount,
            usage_known=True,
        )
        if context._budget_enforcement_enabled:
            targets = context._budget_admission_targets
            if targets is None:
                raise ChatAuthorizationUnavailableError("chat model accounting unavailable")
            await TaskBudget(self.authority.store, reservations=self.enforcement._get_reservations()).verify_settlement(
                operation_id,
                amount,
                targets,
            )

    async def _invoke(self, *, authorize, on_event, **kwargs):
        async def emit(event):
            await authorize()
            if on_event is not None:
                await on_event(event)

        provider = asyncio.create_task(self.provider(**kwargs, on_event=emit))
        try:
            while not provider.done():
                done, _ = await asyncio.wait({provider}, timeout=0.25)
                if not done:
                    await authorize()
            return await provider
        finally:
            if not provider.done():
                provider.cancel()
                try:
                    await provider
                except asyncio.CancelledError:
                    pass

    async def execute(self, *, launch, operation_id, model_id, request, authorize, on_event=None, crew_identity=None, recalled_request=None):
        if bool(launch.session_run_id) != (crew_identity is not None) or (
            crew_identity is not None and crew_identity.root_human_id != launch.user_id
        ):
            raise ChatAuthorizationRefusedError("chat model crew identity refused")
        try:
            encoded = canonical_model_json(request)
        except rfc8785.CanonicalizationError as error:
            raise ChatAuthorizationRefusedError("chat model request is not canonical JSON") from error
        if len(encoded) > 65536:
            raise ChatAuthorizationRefusedError("chat model request exceeds frame bound")
        request = json.loads(encoded)
        if recalled_request is None:
            provider_request = request
        else:
            try:
                provider_encoded = canonical_model_json(recalled_request)
            except rfc8785.CanonicalizationError as error:
                raise ChatAuthorizationRefusedError("recalled model request is not canonical JSON") from error
            if len(provider_encoded) > 65536:
                raise ChatAuthorizationRefusedError("recalled model request exceeds frame bound")
            provider_request = json.loads(provider_encoded)
            original_messages = request.get("messages")
            recalled_messages = provider_request.get("messages")
            if (
                {key: value for key, value in provider_request.items() if key != "messages"}
                != {key: value for key, value in request.items() if key != "messages"}
                or not isinstance(original_messages, list)
                or not isinstance(recalled_messages, list)
                or len(original_messages) != len(recalled_messages)
                or not original_messages
                or not isinstance(original_messages[-1], dict)
                or not isinstance(recalled_messages[-1], dict)
                or original_messages[:-1] != recalled_messages[:-1]
                or original_messages[-1].get("role") != "user"
                or {key: value for key, value in original_messages[-1].items() if key != "content"}
                != {key: value for key, value in recalled_messages[-1].items() if key != "content"}
            ):
                raise ChatAuthorizationRefusedError("recalled model request changed protocol")
            original_content = original_messages[-1].get("content")
            recalled_content = recalled_messages[-1].get("content")
            if (
                isinstance(original_content, str)
                and original_content
                and isinstance(recalled_content, str)
                and recalled_content.endswith(original_content)
            ):
                prefix = recalled_content[: -len(original_content)]
            elif (
                isinstance(original_content, list)
                and isinstance(recalled_content, list)
                and len(recalled_content) == len(original_content) + 1
                and recalled_content[1:] == original_content
                and isinstance(recalled_content[0], dict)
                and set(recalled_content[0]) == {"type", "text"}
                and recalled_content[0]["type"] == "text"
            ):
                prefix = recalled_content[0]["text"]
            else:
                raise ChatAuthorizationRefusedError("recalled model request changed user turn")
            if (
                not isinstance(prefix, str)
                or not prefix.startswith(RECALLED_CONTEXT_START)
                or not prefix.endswith(f"\n{RECALLED_CONTEXT_END}\n\n")
                or len(prefix) > 1400 + len(RECALLED_CONTEXT_END) + 3
            ):
                raise ChatAuthorizationRefusedError("recalled model context invalid")
        operation = await run_in_threadpool(
            self.journal.claim,
            launch,
            operation_id=operation_id,
            request_digest=hashlib.sha256(encoded).hexdigest(),
            model_id=model_id,
            now=self.clock(),
        )
        if operation["status"] != "pending":
            return self.receipt(operation)
        owned = await run_in_threadpool(
            self.journal.transition,
            launch,
            operation,
            now=self.clock(),
            authorize=True,
            status="running",
        )
        if owned is None:
            raise ChatAuthorizationRefusedError("chat model operation already claimed")
        operation = owned
        accounting_id = "chat-" + hashlib.sha256(f"{launch.run_id}\0{operation_id}".encode()).hexdigest()
        sent = reserved = False
        context = None
        started = time.monotonic()
        try:
            context, target = await self.readiness(self.db, launch, model_id)
            from src.products.work_scope import attach_session_run

            context._project_attribution = await attach_session_run(
                self.db,
                tenant_id=launch.tenant_id,
                user_id=launch.user_id,
                session_id=launch.session_id,
                run_id=crew_identity.run_id if crew_identity is not None else launch.run_id,
            )
            if crew_identity is not None:
                context._crew_root_human_id = crew_identity.root_human_id
                context._crew_root_run_id = crew_identity.run_id
            binding = {"model_id": model_id, "transport": "anthropic_messages"}
            quote = await quote_request(json.dumps({"model": model_id, **provider_request}, separators=(",", ":")).encode(), "/v1/messages")
            context._policy_quote = quote
            posture = await self.enforcement.prepare_enforcement_context(context, launch.run_id)
            if posture is not None and not posture.allowed:
                raise ChatAuthorizationRefusedError("chat model budget unavailable")
            operation = await self._save(
                launch,
                operation,
                accounting_id=accounting_id,
                reserved_usd=str(quote.total_usd),
                reservation_status="pending",
            )
            reserved = True
            verdict = await self.enforcement.check_budget_hierarchy(context, quote.total_usd, request_id=accounting_id, run_id=launch.run_id)
            if not verdict.allowed:
                reserved = False
                raise ChatAuthorizationRefusedError("chat model budget refused")
            if context._budget_admission_targets is None:
                raise ChatAuthorizationUnavailableError("chat model accounting unavailable")
            if context._budget_enforcement_enabled and context._budget_admission_targets:
                refusal = await self.enforcement._reserve_or_degrade(
                    accounting_id,
                    quote.total_usd,
                    [target for target in context._budget_admission_targets if target.entity_type != "project"],
                    strict=True,
                )
                if refusal is not None:
                    raise ChatAuthorizationUnavailableError("chat model reservation unavailable")
            if await confirm_quote_spendable(quote) is not None:
                raise ChatAuthorizationUnavailableError("chat model quote expired")
            await authorize()
            operation = await self._save(
                launch,
                operation,
                authorize=True,
                handoff="prepared",
                reservation_status="reserved",
                budget_scope=context._budget_observation_scope or "global",
                reservation_targets=[{**asdict(target), "headroom_usd": str(target.headroom_usd)} for target in context._budget_admission_targets],
            )
            sent = True
            identity = SimpleNamespace(tenant=launch.tenant_id, canonical_principal=f"human:{launch.user_id}")
            result = await self._invoke(
                db=self.db,
                identity=identity,
                binding=binding,
                target=target,
                request=provider_request,
                operation_id=accounting_id,
                authorize=authorize,
                on_event=on_event,
            )
            decision, usage = result["price"], result["usage"]
            if not result.get("provider_request_id") or any(
                type(usage.get(key)) is not int or usage[key] < 0 for key in ("input_tokens", "output_tokens")
            ):
                raise ChatAuthorizationUnavailableError("chat model usage unavailable")
            measured = Decimal(decision.ledger_cost_usd)
            charged = measured if decision.confidence == "verified" else quote.maximum_cost_usd
            if not charged.is_finite() or charged < 0 or charged > quote.maximum_cost_usd:
                raise ChatAuthorizationUnavailableError("chat model cost exceeds reservation")
            operation = await self._save(
                launch,
                operation,
                status="confirmed",
                handoff="confirmed",
                usage={**usage, "estimated_usd": str(charged)},
                content=result["content"],
                stop_reason=result["stop_reason"],
                pricing_decision=decision.to_dict(),
                provider_request_id=result["provider_request_id"],
            )
            if decision.confidence != "verified":
                await self.gap_writer(self.db, accounting_id, operation["budget_scope"])
                return self.receipt(operation)
            latency_ms = int((time.monotonic() - started) * 1000)
            await self.usage_writer(
                context=context,
                model=model_id,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=charged,
                latency_ms=latency_ms,
                status_code=200,
                request_id=accounting_id,
                agent_run_id=crew_identity.run_id if crew_identity is not None else launch.run_id,
                pricing_decision=decision,
                provider_request_id=result["provider_request_id"],
                bedrock_account_id=target.account_id,
                destination_region=target.region or get_settings().aws_region,
            )
            await self.event_writer(context=context, identity=identity, binding=binding, decision=decision, usage=usage, latency_ms=latency_ms)
            operation = await self._save(launch, operation, usage_logged=True)
            await self._settle(context, accounting_id, model_id, usage, charged)
            operation = await self._save(launch, operation, reservation_status="settled")
            return self.receipt(operation)
        except BaseException as error:
            if sent and not operation.get("usage_logged"):
                await self.gap_writer(self.db, accounting_id, operation["budget_scope"])
                durable = await run_in_threadpool(self.journal._read, launch.run_id, operation_id)
                if durable and durable.get("status") == "confirmed" and durable.get("request_digest") == operation["request_digest"]:
                    operation = durable
            if operation["status"] != "confirmed":
                reservation_status = "unknown" if reserved else "not_reserved"
                if reserved and not sent:
                    try:
                        await self._settle(context, accounting_id, model_id, {"input_tokens": 0, "output_tokens": 0}, Decimal(0))
                        reservation_status = "released"
                    except Exception:
                        pass
                error_code = "model_outcome_unknown" if sent else "model_admission_refused"
                error_class, error_detail = type(error).__name__, str(error)[:ERROR_DETAIL_MAX_LENGTH]
                # The receipt below is a normal 200 for the sandbox, so this is the only place the
                # refusal can be named for operators: class and bounded message, never the request,
                # a token or an identity. Logged before the journal write so a lost write cannot
                # hide the cause (#8048).
                logger.warning(
                    "Chat model outcome unknown" if sent else "Chat model admission refused",
                    extra={
                        "run_id": launch.run_id,
                        "operation_id": operation_id,
                        "model_id": model_id,
                        "error_code": error_code,
                        "error_class": error_class,
                        "error_detail": error_detail,
                    },
                )
                operation = await self._save(
                    launch,
                    operation,
                    status="unknown" if sent else "rejected",
                    handoff="unknown" if sent else "not_started",
                    reservation_status=reservation_status,
                    error_code=error_code,
                    error_class=error_class,
                    error_detail=error_detail,
                )
            if not isinstance(error, Exception):
                raise
            return self.receipt(operation)

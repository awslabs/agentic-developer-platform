"""Per-launch model authority for a registered chat root and verified chat pod."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime
from functools import lru_cache, partial
from typing import Literal

from anyio.to_thread import run_sync
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from src.agentauth.agentcore_recall import AgentCoreRecall, RecallUnavailableError, insert_recalled_context, recallable_user_message
from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_answer_evidence import AnswerEvidence
from src.agentauth.chat_capability import (
    ChatAuthorizationRefusedError,
    ChatAuthorizationUnavailableError,
    ChatModelPolicyUnavailableError,
    Identifier,
    require_sandbox_run,
)
from src.agentauth.chat_data_routes import bearer, contract_errors
from src.agentauth.chat_data_routes import runtime as sandbox_runtime
from src.agentauth.chat_delivery import ChatDeliveryRelay
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_model_execution import ChatModelExecution, CrewModelIdentity
from src.agentauth.chat_model_stream import MEDIA_TYPE, model_stream_response
from src.agentauth.chat_persona_prompt import RenderedPrompt, render_system_prompt
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.agentauth.chat_user_turn import load_user_turn, verify_user_turn
from src.agentauth.external_roots import root_store
from src.agentauth.model_policy import CHAT_POSTURE_RUNTIME, ChatModelPolicyPosture, chat_model_policy_posture
from src.agentauth.model_policy_keys import model_policy_keys as verification_keys
from src.agentauth.routes import AgentRuntime, ModelDecisionRequest, ModelPolicyWithheldError, resolved_model_response
from src.agentauth.store import AuthorityStoreError
from src.agentauth.task_runtime_routes import SdkRequest
from src.agentauth.workload import WORKLOAD_HEADER, KubernetesWorkloadVerifier, WorkloadRefusedError
from src.orchestration.chat_data_migration import _owns_context_row
from src.proxy.bedrock_principal import read_routing_run
from src.shared.database import get_db

logger = logging.getLogger(__name__)


class ChatModelRequest(ModelDecisionRequest):
    invocation_id: str = Field(min_length=1, max_length=128)
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


@lru_cache(maxsize=1)
def chat_runtime() -> AgentRuntime:
    return AgentRuntime(store=root_store(), workloads=KubernetesWorkloadVerifier.in_cluster(chat=True))


router = APIRouter(tags=["agent-authority"])


@router.post("/internal/v1/agent/chat/model-decision")
async def chat_model_decision(
    body: ChatModelRequest,
    request: Request,
    runtime: AgentRuntime = Depends(chat_runtime),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        pod = await run_in_threadpool(runtime.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
        now = datetime.now(UTC)
        # Read the protected pointer before binding; a chat pod cannot consume a
        # developer dispatch simply by learning its invocation and digest.
        lookup = await run_in_threadpool(runtime.store._read, f"INVOCATION#{body.invocation_id}", "DISPATCH")
        tenant = (lookup or {}).get("tenant_id", {}).get("S", "")
        if not tenant or (lookup or {}).get("envelope_digest") != {"S": body.envelope_digest}:
            raise BootstrapRefusedError("chat root unavailable")
        grant = await run_in_threadpool(runtime.store.live_grant, invocation_id=body.invocation_id, tenant_id=tenant, attempt=1, now=now)
        if grant.authority.kind != "chat_event":
            raise BootstrapRefusedError("chat root unavailable")
        record = await run_in_threadpool(runtime.store.bind, invocation_id=body.invocation_id, digest=body.envelope_digest, pod=pod, now=now)
        # Chat has no issue-work claim. Its grant is deliberately self-monitoring
        # only and this route cannot dispatch or control repository work.
        result = await resolved_model_response(
            db=db,
            runtime=runtime,
            record=record,
            grant=grant,
            nonce=body.nonce,
            client_contract=body.model_policy_contract,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (BootstrapRefusedError, WorkloadRefusedError):
        raise HTTPException(404, "not found") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None


class SandboxModelRequest(ModelDecisionRequest):
    run_id: Identifier
    session_id: Identifier


class SandboxModelKeysRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    session_id: Identifier


async def _sandbox_launch(
    request: Request,
    token: str,
    services,
    run_id: str,
    session_id: str,
    operation: str = "model.invoke",
    *,
    crew_run: bool = False,
):
    authority, capabilities = services
    pod = await run_in_threadpool(authority.verify_workload, request.headers.get(WORKLOAD_HEADER, ""))
    if pod.namespace != "adp-gateway-agents" or pod.service_account not in {"adp-chat-sandbox", "adp-crew-pod"}:
        raise WorkloadRefusedError("chat sandbox identity refused")
    now = int(datetime.now(UTC).timestamp())
    launch = await run_in_threadpool(capabilities.verify, token, run_id=run_id, session_id=session_id, operation=operation, now=now)
    require_sandbox_run(pod, launch.session_run_id or launch.run_id)
    if pod.uid != launch.sandbox_uid or pod.image_digest != launch.image_digest:
        raise WorkloadRefusedError("chat sandbox binding refused")
    crew_identity = None
    if crew_run:
        asserted = request.headers.get("x-agent-runid")
        if not launch.session_run_id:
            if asserted:
                raise ChatAuthorizationRefusedError("chat crew run refused")
        else:
            if not asserted:
                raise ChatAuthorizationRefusedError("chat crew run required")
            try:
                row = await read_routing_run(
                    asserted,
                    expected_sandbox_uid=pod.uid,
                    expected_session_run_id=launch.session_run_id,
                )
            except Exception as exc:
                raise ChatAuthorizationUnavailableError("chat crew run unavailable") from exc
            if (
                not row
                or row.get("kind") != "crew"
                or row.get("status") != "active"
                or row.get("is_human_rooted") is not True
                or row.get("tenant_id") != launch.tenant_id
                or row.get("user_id") != launch.user_id
                or row.get("root_human_id") != launch.user_id
                or row.get("session_id") != launch.session_id
                or row.get("lease_generation") != launch.lease_generation
                or row.get("target_kind") != "eks-pod"
                or row.get("target_ref") != pod.name
            ):
                raise ChatAuthorizationRefusedError("chat crew run refused")
            crew_identity = CrewModelIdentity(run_id=asserted, root_human_id=row["root_human_id"])
    return authority, launch, now, crew_identity


@router.post("/v1/chat/model/keys")
@contract_errors
async def sandbox_model_keys(
    body: SandboxModelKeysRequest,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
) -> JSONResponse:
    await _sandbox_launch(request, token, services, body.run_id, body.session_id)
    return await verification_keys()


# Reasons that describe a transient inability to decide (store, posture or verifier
# outage) rather than a policy refusal. Only these invite the sandbox to retry.
_RETRYABLE_POLICY_REASONS = frozenset(
    {
        "authority_store_unavailable",
        "decision_unavailable",
        "model_validation_unavailable",
        "runtime_posture_unavailable",
        "snapshot_unavailable",
    }
)
_POLICY_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


def _policy_reason(value: object, fallback: str) -> str:
    """Only a stable snake_case reason code leaves the gateway; never free text."""
    return value if isinstance(value, str) and _POLICY_REASON.fullmatch(value) else fallback


# The body posture the chat route expects for each configured posture (#7997): the
# enforced path keeps the registry's live ``enforcing`` value, the two relaxed
# postures are named as such by model_policy so the sandbox can tell them apart.
_CHAT_POSTURE_BODY: dict[ChatModelPolicyPosture, str] = {"enforced": "enforcing", "advisory": "advisory", "off": "off"}


def _policy_refusal_reason(
    result: object, policy: object, decision: object, posture: ChatModelPolicyPosture = "enforced", *, launch_user_id: object = None
) -> str:
    """Name the first check the live decision failed, in the order the route refuses them.

    ``principal_mismatch`` (#8010) names a decision that is otherwise sound but was
    issued for a different human than the launch's owner; ``decision_unbound`` is
    kept for every remaining binding failure (run, tenant, lease).
    """
    enforced = posture == "enforced"
    if not isinstance(policy, dict):
        return "policy_missing"
    if policy.get("status") != "proposed":
        return _policy_reason(policy.get("reason"), "policy_unavailable")
    if enforced and policy.get("posture_verified") is not True:
        return "posture_unverified"
    if policy.get("posture") != _CHAT_POSTURE_BODY[posture]:
        return "posture_not_enforcing" if enforced else "posture_mismatch"
    if not isinstance(decision, dict):
        return "decision_missing"
    if decision.get("runtime_posture") != CHAT_POSTURE_RUNTIME[posture]:
        return "posture_not_enforcing" if enforced else "posture_mismatch"
    if not isinstance(decision.get("resolved_model_id"), str) or not decision["resolved_model_id"]:
        return "model_unresolved"
    if not policy.get("assertion") or not (isinstance(result, dict) and result.get("assertion")):
        return "assertion_missing"
    if decision.get("principal_kind") != "human" or decision.get("principal_id") != launch_user_id:
        return "principal_mismatch"
    return "decision_unbound"


def _execution_persona(store, launch) -> str | None:
    """Best-effort persona for the refusal body; a store failure here must not mask the reason."""
    try:
        raw = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
        persona = raw.get("persona", {}).get("S")
    except Exception:
        return None
    return persona if isinstance(persona, str) and 0 < len(persona) <= 128 else None


async def _model_policy_unavailable(
    authority, launch, *, reason: str, model_id: object = None, persona: object = None, principal_id: object = None
) -> ChatModelPolicyUnavailableError:
    if not (isinstance(persona, str) and persona):
        persona = await run_in_threadpool(_execution_persona, authority.store, launch)
    resolved = model_id if isinstance(model_id, str) and 0 < len(model_id) <= 256 else None
    # Ids only (#8010): both sides of a principal_mismatch are needed to see which
    # users row the policy bound; no token or assertion ever reaches the log.
    decision_principal_id = principal_id if isinstance(principal_id, str) and 0 < len(principal_id) <= 255 else None
    error = ChatModelPolicyUnavailableError(reason, persona=persona, model_id=resolved, retryable=reason in _RETRYABLE_POLICY_REASONS)
    logger.warning(
        "Chat model policy decision unavailable",
        extra={
            "run_id": launch.run_id,
            "session_id": launch.session_id,
            "tenant_id": launch.tenant_id,
            "persona": persona,
            "model_id": resolved,
            "reason": reason,
            "retryable": error.retryable,
            "decision_principal_id": decision_principal_id,
            "launch_user_id": launch.user_id,
        },
    )
    return error


@router.post("/v1/chat/model/decision")
@contract_errors
async def sandbox_model_decision(
    body: SandboxModelRequest,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    authority, launch, now, _ = await _sandbox_launch(request, token, services, body.run_id, body.session_id)
    _, capabilities = services
    # Read once per turn so the decision is produced and checked under one posture.
    posture = chat_model_policy_posture()
    try:
        record = await run_in_threadpool(authority.store.authority.load_execution, invocation_id=launch.run_id, tenant_id=launch.tenant_id)
        grant = await run_in_threadpool(
            authority.store.live_grant,
            invocation_id=launch.run_id,
            tenant_id=launch.tenant_id,
            attempt=launch.attempt,
            now=datetime.fromtimestamp(now, UTC),
        )
    except AuthorityStoreError:
        raise await _model_policy_unavailable(authority, launch, reason="authority_store_unavailable") from None
    if (
        record is None
        or record.current_attempt != launch.attempt
        or record.tenant_id != launch.tenant_id
        or grant.grant_id != launch.grant_id
        or grant.revocation_epoch != launch.grant_epoch
        or grant.authority.kind != "chat_event"
    ):
        raise BootstrapRefusedError("chat model authority refused")
    try:
        result = await resolved_model_response(
            db=db,
            runtime=AgentRuntime(store=authority.store, workloads=authority.workloads),
            record=record,
            grant=grant,
            nonce=body.nonce,
            client_contract=body.model_policy_contract,
            response_context={"lease_generation": launch.lease_generation},
            chat_posture=posture,
        )
    except ModelPolicyWithheldError as exc:
        # The worker route keeps its 409; the sandbox needs the reason to end the turn visibly.
        raise await _model_policy_unavailable(authority, launch, reason=_policy_reason(exc.reason, "policy_unavailable")) from None
    except AuthorityStoreError:
        raise await _model_policy_unavailable(authority, launch, reason="authority_store_unavailable") from None
    response = result.get("result") if isinstance(result, dict) else None
    policy = response.get("model_policy") if isinstance(response, dict) else None
    decision = policy.get("decision") if isinstance(policy, dict) else None
    # Under ``enforced`` only a verified, enforcing decision is usable.  Under
    # ``advisory``/``off`` (#7997) the body must still be a proposal produced for
    # that same posture, bound to this run, tenant, owner and lease, and signed;
    # ``posture_verified`` may be false, which is what an advisory decision issued
    # despite a rejected invocability check looks like.  ``not_permitted`` never
    # reaches here as a proposal in any posture (model_policy refuses it).
    enforced = posture == "enforced"
    if (
        not isinstance(decision, dict)
        or policy.get("posture") != _CHAT_POSTURE_BODY[posture]
        or (enforced and policy.get("posture_verified") is not True)
        or policy.get("status") != "proposed"
        or decision.get("runtime_posture") != CHAT_POSTURE_RUNTIME[posture]
        or decision.get("invocation_id") != launch.run_id
        or decision.get("tenant_id") != launch.tenant_id
        or decision.get("principal_kind") != "human"
        or decision.get("principal_id") != launch.user_id
        or not isinstance(response.get("context"), dict)
        or response["context"].get("lease_generation") != launch.lease_generation
        or not isinstance(decision.get("resolved_model_id"), str)
        or not decision["resolved_model_id"]
        or not policy.get("assertion")
        or not result.get("assertion")
    ):
        raise await _model_policy_unavailable(
            authority,
            launch,
            reason=_policy_refusal_reason(result, policy, decision, posture, launch_user_id=launch.user_id),
            model_id=decision.get("resolved_model_id") if isinstance(decision, dict) else None,
            persona=decision.get("persona") if isinstance(decision, dict) else None,
            principal_id=decision.get("principal_id") if isinstance(decision, dict) else None,
        )
    if policy.get("posture_verified") is not True:
        # The audit trail (signed response + journal) already records the decision;
        # the log line is what AC-01 reads to see that chat answered on advice.
        logger.warning(
            "Chat model policy decision accepted without verified evidence",
            extra={
                "run_id": launch.run_id,
                "session_id": launch.session_id,
                "tenant_id": launch.tenant_id,
                "persona": decision.get("persona"),
                "model_id": decision["resolved_model_id"],
                "chat_posture": posture,
                "rejection_reason": _policy_reason(policy.get("rejection_reason"), "unverified"),
            },
        )
    await run_in_threadpool(
        capabilities.verify,
        token,
        run_id=launch.run_id,
        session_id=launch.session_id,
        operation="model.invoke",
        now=int(datetime.now(UTC).timestamp()),
    )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


class SandboxProviderRequest(SdkRequest):
    max_tokens: int = Field(ge=1, le=10000, strict=True)


class SandboxModelInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    session_id: Identifier
    operation_id: Identifier
    request: SandboxProviderRequest
    deliver_response: bool = Field(default=False, strict=True)
    # Source coverage gathered from this turn's tool calls (#8113); delivered to the owner's
    # screen as the AG-UI `answer_evidence` event alongside the streamed reply.
    evidence: AnswerEvidence | None = None


class SandboxToolEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    session_id: Identifier
    operation_id: Identifier
    tool_call_id: Identifier
    name: Identifier
    args: dict


class SandboxCrewTextEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    session_id: Identifier
    operation_id: Identifier
    sequence: int = Field(ge=0, le=16380)
    event_type: Literal["start", "content", "end"]
    delta: str = Field(default="", max_length=1024)


@router.post("/v1/chat/model/crew-events")
@contract_errors
async def sandbox_crew_text_event(
    body: SandboxCrewTextEvent,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
) -> JSONResponse:
    if (
        not body.operation_id.startswith("crew_")
        or (body.event_type == "content") != bool(body.delta)
        or (body.event_type != "content" and body.delta)
        or len(body.delta.encode()) > 4096
    ):
        raise HTTPException(422, detail={"error": "invalid_crew_event"})
    authority, launch, _, crew_identity = await _sandbox_launch(
        request,
        token,
        services,
        body.run_id,
        body.session_id,
        crew_run=True,
    )
    if crew_identity is None:
        raise ChatAuthorizationRefusedError("chat crew event refused")

    async def authorize():
        _, current, _, current_crew = await _sandbox_launch(
            request,
            token,
            services,
            body.run_id,
            body.session_id,
            crew_run=True,
        )
        if current != launch or current_crew != crew_identity:
            raise ChatAuthorizationRefusedError("chat crew lease changed")
        await run_in_threadpool(_accepted_turn, authority, launch)

    relay = await ChatDeliveryRelay.create(authority, launch, body.operation_id, authorize)
    relay.sequence = body.sequence
    message_id = "chat_" + relay.stream_id
    if body.event_type == "start":
        if body.sequence != 0:
            raise HTTPException(422, detail={"error": "invalid_crew_event"})
        await relay.publish({"event_type": "RUN_STARTED", "threadId": relay.delivery.thread_id, "runId": relay.delivery.task_id})
        await relay.publish({"event_type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"})
    elif body.event_type == "content":
        if body.sequence < 2:
            raise HTTPException(422, detail={"error": "invalid_crew_event"})
        await relay.publish({"event_type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": body.delta})
    else:
        if body.sequence < 3:
            raise HTTPException(422, detail={"error": "invalid_crew_event"})
        await relay.publish({"event_type": "TEXT_MESSAGE_END", "messageId": message_id})
    return JSONResponse({"accepted": True}, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/model/tool-events")
@contract_errors
async def sandbox_tool_event(
    body: SandboxToolEvent,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
) -> JSONResponse:
    args_json = json.dumps(body.args, ensure_ascii=False, separators=(",", ":"))
    if len(args_json.encode()) > 16_000:
        raise HTTPException(422, detail={"error": "chat_tool_event_too_large"})
    authority, launch, _, crew_identity = await _sandbox_launch(
        request,
        token,
        services,
        body.run_id,
        body.session_id,
        crew_run=True,
    )
    if crew_identity is None or body.operation_id != f"tool_{body.tool_call_id}":
        raise ChatAuthorizationRefusedError("chat tool event refused")

    async def authorize():
        _, current, _, current_crew = await _sandbox_launch(
            request,
            token,
            services,
            body.run_id,
            body.session_id,
            crew_run=True,
        )
        if current != launch or current_crew != crew_identity:
            raise ChatAuthorizationRefusedError("chat tool lease changed")
        await run_in_threadpool(_accepted_turn, authority, launch)

    relay = await ChatDeliveryRelay.create(authority, launch, body.operation_id, authorize)
    await relay.publish({"event_type": "TOOL_CALL_START", "toolCallId": body.tool_call_id, "toolCallName": body.name})
    await relay.publish({"event_type": "TOOL_CALL_ARGS", "toolCallId": body.tool_call_id, "delta": args_json})
    await relay.publish({"event_type": "TOOL_CALL_END", "toolCallId": body.tool_call_id})
    return JSONResponse({"accepted": True}, headers={"Cache-Control": "no-store"})


def agentcore_recall():
    keys = ("ADP_AGENTCORE_MEMORY_ID", "ADP_AGENTCORE_MEMORY_STRATEGY_ID", "ADP_AGENTCORE_MEMORY_REGION", "ADP_AGENTCORE_NAMESPACE_KEY")
    memory_id, strategy_id, region, secret = (os.environ.get(key, "") for key in keys)
    if not any((memory_id, strategy_id, region, secret)):
        return None
    if not all((memory_id, strategy_id, region, secret)) or (os.environ.get("AWS_REGION") and os.environ["AWS_REGION"] != region):
        raise ChatAuthorizationUnavailableError("memory target unconfigured")
    try:
        key = bytes.fromhex(secret)
    except ValueError:
        raise ChatAuthorizationUnavailableError("memory namespace key invalid") from None
    if len(key) < 32:
        raise ChatAuthorizationUnavailableError("memory namespace key invalid")
    import boto3

    return AgentCoreRecall(
        boto3.client(
            "bedrock-agentcore", region_name=region, config=Config(connect_timeout=0.2, read_timeout=0.25, retries={"total_max_attempts": 1})
        ),
        memory_id=memory_id,
        strategy_id=strategy_id,
        namespace_key=key,
    )


RECALL_TIMEOUT_SECONDS = 0.35


def recall_outage(model_request):
    if os.environ.get("ADP_AGENTCORE_RECALL_OUTAGE_POLICY") == "degrade":
        return model_request, "unavailable"
    raise ChatAuthorizationUnavailableError("memory recall unavailable")


async def recalled_model_request(body, authority, capabilities, token, launch, model_request):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RECALL_TIMEOUT_SECONDS
    try:
        provider = await asyncio.wait_for(run_sync(agentcore_recall, abandon_on_cancel=True), timeout=RECALL_TIMEOUT_SECONDS)
    except (RecallUnavailableError, TimeoutError):
        return recall_outage(model_request)
    if provider is None:
        return model_request, "unavailable"
    now = int(datetime.now(UTC).timestamp())
    await run_in_threadpool(capabilities.verify, token, run_id=launch.run_id, session_id=launch.session_id, operation="memory.search", now=now)
    header = await run_in_threadpool(
        lambda: authority.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
    )
    owner = (launch.tenant_id, launch.team_id, launch.user_id)
    if not header or not _owns_context_row(header, owner):
        raise ChatAuthorizationUnavailableError("memory session ownership unavailable")
    if header.get("memoryPrivacy") != "shared" or header.get("memoryConsent") is not True:
        return model_request, "skipped"
    reference, _ = await run_in_threadpool(_accepted_turn, authority, launch)
    message = await run_in_threadpool(
        lambda: authority.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": f"msg#{reference}"}, ConsistentRead=True).get(
            "Item"
        )
    )
    if not message or not _owns_context_row(message, owner) or message.get("role") != "user":
        raise ChatAuthorizationUnavailableError("memory user turn unavailable")
    if message.get("parts") or not isinstance(message.get("content"), str) or not message["content"].strip():
        return model_request, "skipped"
    if not recallable_user_message(model_request["messages"][-1]):
        return model_request, "skipped"
    try:
        recalled = await asyncio.wait_for(
            run_sync(
                partial(provider.retrieve, capabilities, token, authority=authority, run_id=body.run_id, query=message["content"], now=now),
                abandon_on_cancel=True,
            ),
            timeout=max(0, deadline - loop.time()),
        )
    except (RecallUnavailableError, TimeoutError):
        await run_in_threadpool(capabilities.verify, token, run_id=launch.run_id, session_id=launch.session_id, operation="memory.search", now=now)
        current = await run_in_threadpool(
            lambda: authority.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
        )
        if not current or not _owns_context_row(current, owner):
            raise ChatAuthorizationUnavailableError("memory session ownership unavailable")
        if current.get("memoryPrivacy") != "shared" or current.get("memoryConsent") is not True:
            return model_request, "skipped"
        return recall_outage(model_request)
    current = await run_in_threadpool(
        lambda: authority.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
    )
    if not current or not _owns_context_row(current, owner) or current.get("memoryPrivacy") != "shared" or current.get("memoryConsent") is not True:
        return model_request, "skipped"
    augmented = insert_recalled_context(model_request, recalled["context"])
    return augmented, "skipped" if augmented is model_request and recalled["context"] else recalled["status"]


@router.post("/v1/chat/model/invoke")
@contract_errors
async def sandbox_model_invoke(
    body: SandboxModelInvocation,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
    db: AsyncSession = Depends(get_db),
) -> Response:
    authority, launch, _, crew_identity = await _sandbox_launch(request, token, services, body.run_id, body.session_id, crew_run=True)
    selected = await sandbox_model_decision(
        SandboxModelRequest(run_id=body.run_id, session_id=body.session_id, nonce=secrets.token_hex(32), model_policy_contract=1),
        request,
        token,
        services,
        db,
    )
    result = json.loads(selected.body)["result"]
    if result["context"]["lease_generation"] != launch.lease_generation:
        raise ChatAuthorizationRefusedError("chat model lease changed")

    async def authorize():
        _, current, _, current_crew = await _sandbox_launch(request, token, services, body.run_id, body.session_id, crew_run=True)
        if current != launch or current_crew != crew_identity:
            raise ChatAuthorizationRefusedError("chat model lease changed")
        await run_in_threadpool(_accepted_turn, authority, launch)

    base_request = body.request.model_dump(exclude_none=True)
    model_request, memory_coverage = await recalled_model_request(body, authority, services[1], token, launch, base_request)
    recalled_kwargs = {"recalled_request": model_request} if model_request is not base_request else {}
    relay = await ChatDeliveryRelay.create(authority, launch, body.operation_id, authorize) if body.deliver_response else None
    if relay is not None and body.evidence is not None:
        relay.evidence = body.evidence.document()

    if request.headers.get("accept") == MEDIA_TYPE:
        await authorize()
        response = model_stream_response(
            ChatModelExecution(authority, db),
            crew_identity=crew_identity,
            launch=launch,
            operation_id=body.operation_id,
            model_id=result["model_policy"]["decision"]["resolved_model_id"],
            request=base_request,
            authorize=authorize,
            **recalled_kwargs,
            on_event=relay.model_event if relay else None,
        )

        response.headers["X-ADP-Memory-Coverage"] = memory_coverage
        return response
    receipt = await ChatModelExecution(authority, db).execute(
        crew_identity=crew_identity,
        launch=launch,
        operation_id=body.operation_id,
        model_id=result["model_policy"]["decision"]["resolved_model_id"],
        request=base_request,
        authorize=authorize,
        **recalled_kwargs,
        on_event=relay.model_event if relay else None,
    )
    await authorize()
    return JSONResponse(receipt, headers={"Cache-Control": "no-store", "X-ADP-Memory-Coverage": memory_coverage})


class SandboxNextTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    session_id: Identifier


def _accepted_turn(authority, launch):
    store = authority.store
    execution = store.authority.load_execution(invocation_id=launch.run_id, tenant_id=launch.tenant_id)
    dispatch = store._read(f"INVOCATION#{launch.run_id}", "DISPATCH") or {}
    if (
        execution is None
        or execution.current_attempt != launch.attempt
        or execution.repo != f"chat/{launch.session_id}"
        or dispatch.get("tenant_id") != {"S": launch.tenant_id}
    ):
        raise ChatAuthorizationRefusedError("accepted chat turn unavailable")
    reference = load_user_turn(store, execution, dispatch.get("envelope_digest", {}).get("S", ""))
    if reference is None:
        raise ChatAuthorizationUnavailableError("trusted user input unavailable")
    table = authority.context_table
    receipt = table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": f"turn#{launch.run_id}"}, ConsistentRead=True).get("Item")
    message_ref = "user_" + hashlib.sha256(launch.run_id.encode()).hexdigest()
    if (
        not receipt
        or receipt.get("status") != "accepted"
        or receipt.get("runId") != launch.run_id
        or receipt.get("leaseGeneration") != launch.lease_generation
        or receipt.get("ref") != message_ref
        or not _owns_context_row(receipt, (launch.tenant_id, launch.team_id, launch.user_id))
    ):
        raise ChatAuthorizationRefusedError("accepted chat turn unavailable")
    verify_user_turn(table, launch, reference)
    return message_ref, reference


def _record_prompt_revision(store, launch, rendered: RenderedPrompt, now: int) -> None:
    """Record the gateway-rendered prompt revision on the run's ``EXEC`` record (#8113).

    Idempotent per revision so repeated ``turn/next`` reads in a persistent session do
    not churn the record that every other chat transaction fences on. A record that
    cannot be written is an unavailable turn, not a silently unaudited one.
    """
    raw = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
    fields = rendered.record()
    if all(raw.get(name) == {"S": value} for name, value in fields.items()):
        return
    try:
        store.client.update_item(
            TableName=store.table,
            Key=_encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{launch.run_id}"}),
            UpdateExpression="SET " + ", ".join(f"{name} = :{name}" for name in fields) + ", chat_prompt_rendered_at = :now",
            ConditionExpression="#status = :active AND current_attempt = :attempt",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=_encoded(
                {**{f":{name}": value for name, value in fields.items()}, ":now": now, ":active": "active", ":attempt": launch.attempt}
            ),
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ChatAuthorizationRefusedError("chat prompt record refused") from None
        raise ChatAuthorizationUnavailableError("chat prompt record unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat prompt record unavailable") from None


def _rendered_turn_prompt(store, launch, now: int) -> RenderedPrompt:
    rendered = render_system_prompt(_execution_persona(store, launch))
    _record_prompt_revision(store, launch, rendered, now)
    return rendered


@router.post("/v1/chat/turn/next")
@contract_errors
async def sandbox_turn_next(
    body: SandboxNextTurnRequest,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
) -> JSONResponse:
    authority, launch, now, _ = await _sandbox_launch(request, token, services, body.run_id, body.session_id, "turn.next")
    message_ref, protected = await run_in_threadpool(_accepted_turn, authority, launch)
    _, capabilities = services
    history = ChatHistoryStore(authority.context_table, capabilities)
    result = await run_in_threadpool(history.get_messages, token, run_id=launch.run_id, session_id=launch.session_id, ids=[message_ref], now=now)
    if (
        result["status"] != "ok"
        or len(result["entries"]) != 1
        or result["entries"][0]["ref"] != message_ref
        or envelope_digest(result["entries"][0]["message"]) != protected["message_digest"]
    ):
        raise ChatAuthorizationUnavailableError("accepted chat turn history unavailable")
    sequence = None
    session = await run_in_threadpool(
        ChatSessionMailbox(authority.context_table).state,
        session_id=launch.session_id,
        owner=(launch.tenant_id, launch.team_id, launch.user_id),
        now=now,
    )
    if session["mode"] == "persistent":
        sequence = await run_in_threadpool(
            ChatSessionMailbox(authority.context_table).initial_cursor,
            session_id=launch.session_id,
            owner=(launch.tenant_id, launch.team_id, launch.user_id),
            run_id=launch.run_id,
            sandbox_uid=launch.sandbox_uid,
            generation=launch.lease_generation,
            message=result["entries"][0]["message"]["content"],
            now=now,
        )
    rendered = await run_in_threadpool(_rendered_turn_prompt, authority.store, launch, now)
    await run_in_threadpool(
        capabilities.verify, token, run_id=launch.run_id, session_id=launch.session_id, operation="turn.next", now=int(datetime.now(UTC).timestamp())
    )
    return JSONResponse(
        {
            "run_id": launch.run_id,
            "session_id": launch.session_id,
            "lease_generation": launch.lease_generation,
            "turn": result["entries"][0],
            "system_prompt": rendered.system_prompt,
            "prompt_revision": rendered.prompt_revision,
            **({"session_sequence": sequence} if sequence is not None else {}),
        },
        headers={"Cache-Control": "no-store"},
    )

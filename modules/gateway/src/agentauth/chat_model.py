"""Per-launch model authority for a registered chat root and verified chat pod."""

import hashlib
import json
import secrets
from datetime import UTC, datetime
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier, require_sandbox_run
from src.agentauth.chat_data_routes import bearer, contract_errors
from src.agentauth.chat_data_routes import runtime as sandbox_runtime
from src.agentauth.chat_delivery import ChatDeliveryRelay
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_model_execution import ChatModelExecution
from src.agentauth.chat_model_stream import MEDIA_TYPE, model_stream_response
from src.agentauth.chat_user_turn import load_user_turn, verify_user_turn
from src.agentauth.external_roots import root_store
from src.agentauth.model_policy_keys import model_policy_keys as verification_keys
from src.agentauth.routes import AgentRuntime, ModelDecisionRequest, resolved_model_response
from src.agentauth.store import AuthorityStoreError
from src.agentauth.task_runtime_routes import SdkRequest
from src.agentauth.workload import WORKLOAD_HEADER, KubernetesWorkloadVerifier, WorkloadRefusedError
from src.orchestration.chat_data_migration import _owns_context_row
from src.shared.database import get_db


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


async def _sandbox_launch(request: Request, token: str, services, run_id: str, session_id: str, operation: str = "model.invoke"):
    authority, capabilities = services
    pod = await run_in_threadpool(authority.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
    if pod.namespace != "adp-gateway-agents" or pod.service_account != "adp-chat-sandbox":
        raise WorkloadRefusedError("chat sandbox identity refused")
    now = int(datetime.now(UTC).timestamp())
    launch = await run_in_threadpool(capabilities.verify, token, run_id=run_id, session_id=session_id, operation=operation, now=now)
    require_sandbox_run(pod, launch.run_id)
    if pod.uid != launch.sandbox_uid or pod.image_digest != launch.image_digest:
        raise WorkloadRefusedError("chat sandbox binding refused")
    return authority, launch, now


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


@router.post("/v1/chat/model/decision")
@contract_errors
async def sandbox_model_decision(
    body: SandboxModelRequest,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    authority, launch, now = await _sandbox_launch(request, token, services, body.run_id, body.session_id)
    _, capabilities = services
    record = await run_in_threadpool(authority.store.authority.load_execution, invocation_id=launch.run_id, tenant_id=launch.tenant_id)
    grant = await run_in_threadpool(
        authority.store.live_grant,
        invocation_id=launch.run_id,
        tenant_id=launch.tenant_id,
        attempt=launch.attempt,
        now=datetime.fromtimestamp(now, UTC),
    )
    if (
        record is None
        or record.current_attempt != launch.attempt
        or record.tenant_id != launch.tenant_id
        or grant.grant_id != launch.grant_id
        or grant.revocation_epoch != launch.grant_epoch
        or grant.authority.kind != "chat_event"
    ):
        raise BootstrapRefusedError("chat model authority refused")
    result = await resolved_model_response(
        db=db,
        runtime=AgentRuntime(store=authority.store, workloads=authority.workloads),
        record=record,
        grant=grant,
        nonce=body.nonce,
        client_contract=body.model_policy_contract,
        response_context={"lease_generation": launch.lease_generation},
    )
    response = result.get("result") if isinstance(result, dict) else None
    policy = response.get("model_policy") if isinstance(response, dict) else None
    decision = policy.get("decision") if isinstance(policy, dict) else None
    if (
        not isinstance(decision, dict)
        or policy.get("posture") != "enforcing"
        or policy.get("posture_verified") is not True
        or policy.get("status") != "proposed"
        or decision.get("runtime_posture") != "enforcing"
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
        raise ChatAuthorizationUnavailableError("chat model policy unavailable")
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


@router.post("/v1/chat/model/invoke")
@contract_errors
async def sandbox_model_invoke(
    body: SandboxModelInvocation,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
    db: AsyncSession = Depends(get_db),
) -> Response:
    authority, launch, _ = await _sandbox_launch(request, token, services, body.run_id, body.session_id)
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
        _, current, _ = await _sandbox_launch(request, token, services, body.run_id, body.session_id)
        if current != launch:
            raise ChatAuthorizationRefusedError("chat model lease changed")
        await run_in_threadpool(_accepted_turn, authority, launch)

    relay = await ChatDeliveryRelay.create(authority, launch, body.operation_id, authorize) if body.deliver_response else None
    if request.headers.get("accept") == MEDIA_TYPE:
        await authorize()
        return model_stream_response(
            ChatModelExecution(authority, db),
            launch=launch,
            operation_id=body.operation_id,
            model_id=result["model_policy"]["decision"]["resolved_model_id"],
            request=body.request.model_dump(exclude_none=True),
            authorize=authorize,
            on_event=relay.model_event if relay else None,
        )

    receipt = await ChatModelExecution(authority, db).execute(
        launch=launch,
        operation_id=body.operation_id,
        model_id=result["model_policy"]["decision"]["resolved_model_id"],
        request=body.request.model_dump(exclude_none=True),
        authorize=authorize,
        on_event=relay.model_event if relay else None,
    )
    await authorize()
    return JSONResponse(receipt, headers={"Cache-Control": "no-store"})


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


@router.post("/v1/chat/turn/next")
@contract_errors
async def sandbox_turn_next(
    body: SandboxNextTurnRequest,
    request: Request,
    token: str = Depends(bearer),
    services=Depends(sandbox_runtime),
) -> JSONResponse:
    authority, launch, now = await _sandbox_launch(request, token, services, body.run_id, body.session_id, "turn.next")
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
    await run_in_threadpool(
        capabilities.verify, token, run_id=launch.run_id, session_id=launch.session_id, operation="turn.next", now=int(datetime.now(UTC).timestamp())
    )
    return JSONResponse(
        {"run_id": launch.run_id, "session_id": launch.session_id, "lease_generation": launch.lease_generation, "turn": result["entries"][0]},
        headers={"Cache-Control": "no-store"},
    )

"""Per-launch model authority for a registered chat root and verified chat pod."""

from datetime import UTC, datetime
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.external_roots import root_store
from src.agentauth.routes import AgentRuntime, ModelDecisionRequest, resolved_model_response
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, KubernetesWorkloadVerifier, WorkloadRefusedError
from src.shared.database import get_db


class ChatModelRequest(ModelDecisionRequest):
    invocation_id: str = Field(min_length=1, max_length=128)
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


@lru_cache(maxsize=1)
def chat_runtime() -> AgentRuntime:
    return AgentRuntime(store=root_store(), workloads=KubernetesWorkloadVerifier.in_cluster(chat=True))


# The edge requires IAM. TokenReview additionally authenticates the exact pod,
# image and service account here; chat needs no broad internal-plane registry scope.
router = APIRouter(prefix="/internal/v1/agent/chat", tags=["agent-authority"])


@router.post("/model-decision")
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

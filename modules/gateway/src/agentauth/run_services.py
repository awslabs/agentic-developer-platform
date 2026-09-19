"""Run-bound replacements for shared credentials in coding workers (#5195).

The gateway owns service keys. Callers authenticate with both their run credential
and current TokenReview-bound workload; service identities come only from protected
execution/grant records. Nothing in a worker body or its environment selects a tenant,
root human, correlation chain, signing input, or downstream credential.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError

router = APIRouter(
    prefix="/internal/v1/agent/self",
    tags=["agent-authority"],
    dependencies=[Depends(require_agent_transport)],
)

MARKER_KEY_ENV = "ADP_MARKER_SIGNING_KEY"
_REFUSALS = (BootstrapRefusedError, ExecutionStateError, CredentialError, WorkloadRefusedError)


class OwnRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def live_context(request: Request, runtime: AgentRuntime):
    """Recheck the credential after the awaited flow/policy read.

    A changed authority snapshot invalidates that read. No stale credential or
    withdrawn grant can become a signing authority merely because it passed the
    first check before a slow dependency.
    """
    credential = request.headers.get(CREDENTIAL_HEADER, "")
    workload = request.headers.get(WORKLOAD_HEADER, "")
    if not credential or not workload:
        raise HTTPException(404, "not found")
    try:
        first = await run_in_threadpool(runtime.authenticate, credential, workload)
        await runtime.validate_flow(first[2], first[3])
        current = await run_in_threadpool(runtime.authenticate, credential, workload)
        if first[1:] != current[1:]:
            raise HTTPException(404, "not found")
        return current
    except _REFUSALS:
        raise HTTPException(404, "not found") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None


@dataclass(frozen=True)
class MarkerIdentity:
    correlation_id: str
    root_human_id: str
    is_human_rooted: str
    invocation_id: str
    chain_depth: str

    def canonical(self) -> str:
        return ":".join((self.correlation_id, self.root_human_id, self.is_human_rooted, self.invocation_id, self.chain_depth))

    def signed_fields(self, key: str) -> dict[str, str]:
        signature = hmac.new(key.encode("utf-8"), self.canonical().encode("utf-8"), hashlib.sha256).digest()
        return {
            "correlation_id": self.correlation_id,
            "root_human_id": self.root_human_id,
            "is_human_rooted": self.is_human_rooted,
            "invocation_id": self.invocation_id,
            "chain_depth": self.chain_depth,
            "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
        }


def marker_identity(record, grant, execution: dict | None) -> MarkerIdentity:
    """Resolve exactly this authenticated execution's immutable provenance."""
    if (
        not execution
        or not record.flow_id
        or grant.flow_id != record.flow_id
        or grant.principal != record.principal
        or grant.tenant_id != record.tenant_id
        or grant.authority.org_id != record.tenant_id
    ):
        raise HTTPException(404, "not found")
    # Do not infer a depth from an events row writable by coding workers.
    raw_depth = execution.get("chain_depth", {}).get("N")
    if not isinstance(raw_depth, str) or not raw_depth.isdecimal() or not 0 <= int(raw_depth) <= grant.max_chain_depth:
        raise HTTPException(503, "run service identity unavailable")
    values = (record.flow_id, grant.authority.human_id, record.invocation_id)
    if any(not isinstance(value, str) or not value or len(value) > 255 or any(char.isspace() or char in "<>" for char in value) for value in values):
        raise HTTPException(503, "run service identity unavailable")
    return MarkerIdentity(
        correlation_id=record.flow_id,
        root_human_id=grant.authority.human_id,
        is_human_rooted="false" if grant.authority.kind == "service_policy" else "true",
        invocation_id=record.invocation_id,
        chain_depth=str(int(raw_depth)),
    )


@router.post("/marker")
async def own_marker(body: OwnRunRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)) -> JSONResponse:
    initial = await live_context(request, runtime)
    record = initial[2]
    try:
        execution = await run_in_threadpool(runtime.store._read, f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None
    # The protected read may wait; verify its subject is still the current run.
    current = await live_context(request, runtime)
    if initial[1:] != current[1:]:
        raise HTTPException(404, "not found")
    identity = marker_identity(current[2], current[3], execution)
    config = os.environ if runtime.env is None else runtime.env
    key = config.get(MARKER_KEY_ENV, "")
    if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 8192:
        raise HTTPException(503, "marker signing unavailable")
    return JSONResponse(identity.signed_fields(key), headers={"Cache-Control": "no-store"})

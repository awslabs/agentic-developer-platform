"""IAM-authenticated routes for a run's own status and control registration (#5028).

Mounted under the same ``/internal`` proxy as :mod:`src.agentauth.routes` and
reusing its transport dependency and its ``AgentRuntime``, so there is exactly one
credential system, one workload verifier and one protected store behind every
agent-facing write.

## Two proofs per request, and why one is not enough

Every route here requires **both**:

- ``X-Adp-Run-Credential`` — the HMAC-verified run credential, which says *which
  invocation and attempt* is calling. SigV4 alone cannot: every agent worker
  assumes the same platform role.
- ``X-Adp-Workload-Token`` — a projected Kubernetes token with the dedicated
  bootstrap audience, verified through TokenReview against live pod facts, which
  says *which pod* is presenting that credential.

The workload token is re-verified on every request rather than trusted from
bootstrap. A credential that leaked out of its pod is otherwise indistinguishable
from the pod itself for the rest of its TTL; requiring a token only the pod's own
projected volume can produce closes that. It also supplies the pod IP that becomes
the control destination, so the destination is never a request field.

## Refusals are uniform

Every authorization failure — bad credential, superseded attempt, cancelled run,
wrong pod, another run's row — returns the same 404. A caller able to tell those
apart learns whether a run it named exists and how many attempts it has had.
Malformed *own* input (an unsupported status, a protected field) returns 400,
which is safe because it is only reachable after the caller has been
authenticated as itself.
"""

from __future__ import annotations

import os
from functools import lru_cache

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.composition import build_authorization_service
from src.agentauth.execution import ExecutionStateError
from src.agentauth.registration import (
    AgentRegistrationService,
    RegistrationRefusedError,
)
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError

# ``/self``, not bare ``/internal/v1/agent``, and the segment is load-bearing
# twice over.
#
# 1. ``routes.py`` already serves ``POST /control/{run_id}/{action}`` for the
#    *coordinator* path. A registration route at ``/control/registration/clear``
#    matches that pattern with ``run_id="registration"``, and since that router is
#    included first it wins — the clear handler here is simply never reached.
#    Verified by request, not by reading the patterns.
# 2. ``routes.py`` also serves ``GET /status`` for a coordinator reading *another*
#    run's state. This module's ``/status`` is a worker writing its *own*. Distinct
#    methods on one path would route correctly but leave the two authorization
#    models one typo apart.
#
# ``/self`` names the actual distinction: everything here acts on the caller's own
# run, identified from its credential, never on a run named in the request.
router = APIRouter(
    prefix="/internal/v1/agent/self",
    tags=["agent-authority"],
    dependencies=[Depends(require_agent_transport)],
)

# Bound on the free-text values a status write may carry, applied by pydantic
# before the service truncates to its per-field limits. Two bounds rather than
# one so an oversized body is refused at parse time instead of silently trimmed.
_MAX_FIELD_CHARS = 8192


class StatusRequest(BaseModel):
    """A status transition for the caller's own run.

    ``extra="forbid"`` is the load-bearing line. It makes an unrecognized field a
    422 rather than a silently ignored one, which is what stops a caller from
    appearing to set ``tenant_id`` or ``control_address``; the service rejects
    those names again by allowlist, because a model is easy to widen and the
    service is where the security statement belongs.

    Note what is absent: no ``event_id``, no ``arrived_at``, no ``tenant_id``. The
    row key is derived from the protected execution record, so there is nothing
    here for a caller to point at another run's row.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)
    summary: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)
    transcript_key: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)
    session_id: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)
    token_mode: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)
    error_message: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)
    skip_reason: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)
    stop_reason: str | None = Field(default=None, max_length=_MAX_FIELD_CHARS)

    def fields(self) -> dict[str, str]:
        return {name: value for name, value in self.model_dump(exclude={"status"}).items() if isinstance(value, str) and value}


class RegisterControlRequest(BaseModel):
    """Register this pod's control listener.

    Only the token and its expiry are supplied, because only the pod can mint the
    token it will honour. The address is the verified pod's IP and the port is
    pinned gateway configuration — a caller-supplied address is precisely the
    control-channel redirect this path exists to remove.
    """

    model_config = ConfigDict(extra="forbid")

    control_token: str = Field(min_length=32, max_length=256)
    control_token_expires_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ClearControlRequest(BaseModel):
    """Remove this attempt's control registration at teardown."""

    model_config = ConfigDict(extra="forbid")

    control_generation: int = Field(ge=1, le=1_000_000)


class RenewControlRequest(RegisterControlRequest):
    control_generation: int = Field(ge=1)
    expected_epoch: int = Field(ge=1)
    rotation_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class RegistrationRuntime:
    """Binds the registration service to the per-request verified pod.

    Separate from :class:`~src.agentauth.routes.AgentRuntime` because the workload
    verifier and the protected store are shared while the events-table writer is
    not, and reusing ``AgentRuntime`` for both would put an events-table client on
    the bootstrap path that does not need one.
    """

    def __init__(self, *, service: AgentRegistrationService, runtime: AgentRuntime) -> None:
        self.service = service
        self.runtime = runtime

    def _verified(self, request: Request):
        # Verified per request, not carried from bootstrap: see the module
        # docstring on why a credential alone cannot identify the presenting pod.
        return self.runtime.workloads.verify(request.headers.get(WORKLOAD_HEADER, ""))

    def credential(self, request: Request) -> str:
        token = request.headers.get(CREDENTIAL_HEADER, "")
        if not token:
            raise RegistrationRefusedError("not found")
        return token

    async def validate_live(self, request: Request):
        context = await run_in_threadpool(self.runtime.authenticate, self.credential(request), request.headers.get(WORKLOAD_HEADER, ""))
        await self.runtime.validate_flow(context[2], context[3])
        return context[0]

    def status(self, request: Request, body: StatusRequest, *, pod=None) -> dict:
        self.service.record_status(
            credential_token=self.credential(request),
            pod=pod or self._verified(request),
            status=body.status,
            fields=body.fields(),
        )
        return {"recorded": True}

    def register(self, request: Request, body: RegisterControlRequest, *, pod=None) -> dict:
        registration = self.service.register_control(
            credential_token=self.credential(request),
            pod=pod or self._verified(request),
            token=body.control_token,
            token_expires_at=body.control_token_expires_at,
        )
        return {
            "control_generation": registration.generation,
            "control_address": registration.address,
            "control_port": registration.port,
        }

    def clear(self, request: Request, body: ClearControlRequest) -> dict:
        self.service.clear_control(
            credential_token=self.credential(request),
            pod=self._verified(request),
            generation=body.control_generation,
        )
        return {"cleared": True}

    def renew(self, request: Request, body: RenewControlRequest, *, pod=None) -> dict:
        return self.service.renew_control(
            credential_token=self.credential(request),
            pod=pod or self._verified(request),
            generation=body.control_generation,
            expected_epoch=body.expected_epoch,
            rotation_id=body.rotation_id,
            token=body.control_token,
            token_expires_at=body.control_token_expires_at,
        )

    def renewal_state(self, request: Request, body: ClearControlRequest, *, pod=None) -> dict:
        return self.service.control_registration_state(
            credential_token=self.credential(request),
            pod=pod or self._verified(request),
            generation=body.control_generation,
        )


@lru_cache(maxsize=1)
def get_registration_runtime() -> RegistrationRuntime:
    """Process-wide registration runtime.

    Cached for the boto3 clients underneath, which are expensive to build and hold
    no request state. Nothing authorization-relevant is cached: the credential, the
    workload token, the execution record and the grant are all resolved fresh per
    request, which is what keeps revocation bounded by the credential TTL rather
    than by the process lifetime.

    Reuses ``get_agent_runtime`` so the enablement flag, the authority table and
    the credential-key requirement are checked in exactly one place.
    """
    runtime = get_agent_runtime()
    authority_table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
    events_table = os.environ.get("WEBHOOK_EVENTS_TABLE", "")
    if not events_table:
        # No default. A wrong events table would mean writing a run's status into
        # another environment's row, and a dev-shaped default is how that happens
        # silently in production.
        raise HTTPException(503, "agent authority is not configured")
    client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    try:
        service = AgentRegistrationService(
            policy=build_authorization_service(authority_table=authority_table, events_table=events_table),
            authority_table=authority_table,
            events_table=events_table,
            dynamodb_client=client,
        )
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority is not configured") from None
    return RegistrationRuntime(service=service, runtime=runtime)


async def _call(handler, request: Request, body, *, live_runtime: RegistrationRuntime | None = None) -> JSONResponse:
    """Run a registration handler, collapsing every refusal to one shape."""
    try:
        if live_runtime is not None:
            pod = await live_runtime.validate_live(request)
            result = await run_in_threadpool(handler, request, body, pod=pod)
        else:
            result = await run_in_threadpool(handler, request, body)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except RegistrationRefusedError as exc:
        # 400 only for the caller's own malformed input, which is reachable only
        # after it authenticated as itself. Everything else is an indistinguishable
        # 404 — see the module docstring.
        if str(exc) in {"unsupported status", "unsupported field", "invalid control token", "invalid control token expiry", "invalid generation"}:
            raise HTTPException(400, str(exc)) from None
        if str(exc) == "control already registered":
            # 409: the work was already admitted under a different listener
            # identity, so retrying is wrong rather than merely unauthorized.
            raise HTTPException(409, "control already registered") from None
        raise HTTPException(404, "not found") from None
    except (WorkloadRefusedError, BootstrapRefusedError, CredentialError, ExecutionStateError):
        raise HTTPException(404, "not found") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None


@router.post("/status")
async def record_status(
    body: StatusRequest,
    request: Request,
    runtime: RegistrationRuntime = Depends(get_registration_runtime),
) -> JSONResponse:
    response = await _call(runtime.status, request, body, live_runtime=runtime if body.status == "in_progress" else None)
    # The status service has verified the pod, credential and current attempt,
    # and committed a protected terminal report before releasing ownership.
    from src.agentauth.run_credential import verify_credential
    from src.orchestration.work_admission import enabled, worker_checkpoint
    from src.orchestration.work_claims import ReleaseReason, WorkClaimError

    if enabled():
        caller = verify_credential(runtime.credential(request), env=runtime.runtime.env)
        try:
            await worker_checkpoint(
                org_id=caller.tenant_id,
                invocation_id=caller.invocation_id,
                terminal=body.status != "in_progress",
                store=runtime.runtime.store,
                reason=ReleaseReason.COMPLETED if body.status == "completed" else ReleaseReason.FAILED,
            )
        except WorkClaimError:
            raise HTTPException(409, "work ownership refused") from None
    return response


@router.post("/control/registration")
async def register_control(
    body: RegisterControlRequest,
    request: Request,
    runtime: RegistrationRuntime = Depends(get_registration_runtime),
) -> JSONResponse:
    return await _call(runtime.register, request, body, live_runtime=runtime)


@router.post("/control/registration/clear")
async def clear_control(
    body: ClearControlRequest,
    request: Request,
    runtime: RegistrationRuntime = Depends(get_registration_runtime),
) -> JSONResponse:
    return await _call(runtime.clear, request, body)


@router.post("/control/registration/renew")
async def renew_control(
    body: RenewControlRequest,
    request: Request,
    runtime: RegistrationRuntime = Depends(get_registration_runtime),
) -> JSONResponse:
    return await _call(runtime.renew, request, body, live_runtime=runtime)


@router.post("/control/registration/state")
async def control_registration_state(
    body: ClearControlRequest,
    request: Request,
    runtime: RegistrationRuntime = Depends(get_registration_runtime),
) -> JSONResponse:
    return await _call(runtime.renewal_state, request, body, live_runtime=runtime)

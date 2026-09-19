"""IAM transport plus verified workload identity for delegated agent requests.

Mounted under the existing API Gateway /internal proxy, which preserves this
prefix and overwrites X-Caller-Identity using the SigV4 principal. The shared
internal API key is deliberately insufficient for every route in this module.
"""

from __future__ import annotations

import inspect
import logging
import os
from datetime import UTC, datetime
from functools import lru_cache, partial

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.adapter import CREDENTIAL_HEADER, MAX_COMMAND_BODY_BYTES, AgentControlAdapter
from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, issue_bound_credential
from src.agentauth.composition import build_authorization_service, build_control_adapter
from src.agentauth.dispatch import FAN_OUT_CAPABILITY, FAN_OUT_CAPABILITY_FIELD, DispatchRequest, DispatchService
from src.agentauth.execution import ExecutionStateError, evaluate_execution_state
from src.agentauth.grants import LIVE_CONTROL_ACTIONS, AgentAction
from src.agentauth.model_policy import MODEL_POLICY_CONTRACT_VERSION
from src.agentauth.policy import PolicyError
from src.agentauth.revalidation import RevalidationRequest, revalidate_command
from src.agentauth.run_credential import CredentialError, verify_credential
from src.agentauth.store import AuthorityStoreError
from src.agentauth.waves import WaveRequest
from src.agentauth.workload import WORKLOAD_HEADER, KubernetesWorkloadVerifier, WorkloadRefusedError
from src.internal.auth_deps import verify_internal_or_irsa
from src.orchestration.work_claims import WorkClaimError
from src.shared.database import get_db

logger = logging.getLogger("bedrockgateway.agentauth.routes")


async def require_agent_transport(request: Request) -> None:
    caller = request.headers.get("X-Caller-Identity")
    if not caller:
        raise HTTPException(403, "forbidden")
    await verify_internal_or_irsa(request, x_caller_identity=caller, x_internal_api_key=None)


router = APIRouter(prefix="/internal/v1/agent", tags=["agent-authority"], dependencies=[Depends(require_agent_transport)])


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invocation_id: str = Field(min_length=1, max_length=128)
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The model-policy contract this client can actually consume.  Absent means
    #: a previously shipped client: it cannot honour an enforcing decision, so it
    #: is admitted only under a verified non-enforcing posture.  Declared by the
    #: client rather than inferred, because the gateway cannot otherwise tell a
    #: capable worker from one that will ignore the payload — and it is a
    #: *capability* claim only, never authority: everything it could unlock is
    #: still decided by the gateway from its own committed state.
    model_policy_contract: int | None = Field(default=None, ge=1, le=64, strict=True)


class AgentRuntime:
    def __init__(
        self,
        *,
        store: BootstrapStore,
        workloads: KubernetesWorkloadVerifier,
        env: dict[str, str] | None = None,
        adapter: AgentControlAdapter | None = None,
        dispatcher: DispatchService | None = None,
    ) -> None:
        self.store = store
        self.workloads = workloads
        self.env = env
        self._adapter = adapter
        self._dispatcher = dispatcher

    @property
    def adapter(self) -> AgentControlAdapter:
        if self._adapter is None:
            self._adapter = build_control_adapter(authority_table=self.store.table, dynamodb_client=self.store.client, env=self.env)
        return self._adapter

    def authenticate(self, credential_token: str, workload_token: str):
        """Verify individual pod, current execution and human authority per call."""
        pod = self.workloads.verify(workload_token)
        now = datetime.now(UTC)
        caller = verify_credential(credential_token, now=now, env=self.env)
        record = self.store.authority.load_execution(invocation_id=caller.invocation_id, tenant_id=caller.tenant_id)
        evaluate_execution_state(
            record=record,
            invocation_id=caller.invocation_id,
            tenant_id=caller.tenant_id,
            attempt=caller.attempt,
            credential_epoch=caller.credential_epoch,
            presented_workload_binding=pod.uid,
            now=now,
        )
        grant = self.store.live_grant(invocation_id=caller.invocation_id, tenant_id=caller.tenant_id, attempt=caller.attempt, now=now)
        return pod, caller, record, grant

    async def validate_flow(self, record, grant):
        """Validate live engine authority, returning the verified graph assignment.

        Issue #4898: returns the `GraphAttribution` for an execution bound to one
        graph node, or None when there is no single owning node (a coordinator, or
        a non-engine authority kind). Callers that only care whether the request
        is authorized can keep ignoring the result — refusal is still an
        exception.
        """
        if grant.authority.kind in {"github_event", "service_policy"}:
            return None
        if grant.authority.kind != "gate_decision":
            raise BootstrapRefusedError("unsupported authority source")
        from src.agentauth.engine import validate_engine_authority
        from src.shared.database import get_session_factory

        execution = await run_in_threadpool(self.store._read, f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
        try:
            async with get_session_factory()() as session:
                return await validate_engine_authority(session=session, execution=execution or {}, grant=grant, store=self.store)
        except Exception:
            raise BootstrapRefusedError("engine authority unavailable") from None

    async def enroll_coordinator(self, record, grant):
        from src.agentauth.coordinator import bind_coordinator, resolve_coordinator_assignment
        from src.shared.database import get_session_factory

        config = os.environ if self.env is None else self.env
        repo = config.get("BG_ORCH_DISPATCH_REPO", "")
        if grant.authority.kind != "github_event" or not repo:
            return record, grant
        execution = await run_in_threadpool(self.store._read, f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
        if not execution or execution.get("persona", {}).get("S") not in {"operations", "aidlc"}:
            return record, grant
        try:
            async with get_session_factory()() as session:
                assignment = await resolve_coordinator_assignment(session=session, execution=execution, grant=grant, configured_repo=repo)
                if assignment is None:
                    return record, grant
                await run_in_threadpool(bind_coordinator, store=self.store, record=record, grant=grant, assignment=assignment)
        except (BootstrapRefusedError, AuthorityStoreError):
            raise
        except Exception:
            raise BootstrapRefusedError("coordinator assignment unavailable") from None
        record = await run_in_threadpool(self.store.authority.load_execution, invocation_id=record.invocation_id, tenant_id=record.tenant_id)
        grant = await run_in_threadpool(
            self.store.live_grant,
            invocation_id=record.invocation_id,
            tenant_id=record.tenant_id,
            attempt=record.current_attempt,
            now=datetime.now(UTC),
        )
        return record, grant

    def dispatch(self, body: DispatchRequest, credential_token: str, workload_token: str, *, context=None, fan_out_cleared: bool = False) -> dict:
        pod, _, _, _ = context or self.authenticate(credential_token, workload_token)
        return self.dispatcher.dispatch(body=body, credential_token=credential_token, workload_binding=pod.uid, fan_out_cleared=fan_out_cleared)

    async def resolve_fan_out(self, body, context) -> bool:
        """Resolve the #5365 repository fan-out clearance for a root coordinator.

        Only asked when the stored grant already carries the server-written
        capability, so an ordinary issue-scoped run costs no query. Any failure to
        resolve returns False, which leaves the caller pinned to its launch issue
        rather than widened on an unproven fact.
        """
        from src.agentauth.coordinator import resolve_repository_fan_out
        from src.shared.database import get_session_factory

        record, grant = context[2], context[3]
        raw_grant = await run_in_threadpool(self.store._read, f"TENANT#{record.tenant_id}", f"GRANT#{grant.principal}")
        if not raw_grant or raw_grant.get(FAN_OUT_CAPABILITY_FIELD) != {"S": FAN_OUT_CAPABILITY}:
            return False
        execution = await run_in_threadpool(self.store._read, f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
        if not execution:
            return False
        try:
            async with get_session_factory()() as session:
                config = os.environ if self.env is None else self.env
                return await resolve_repository_fan_out(
                    session=session,
                    execution=execution,
                    grant=grant,
                    target_repo=body.target.repo,
                    target_issue=body.target.issue,
                    orchestration_repo=config.get("BG_ORCH_DISPATCH_REPO", ""),
                )
        except Exception:
            return False

    @property
    def dispatcher(self):
        if self._dispatcher is None:
            self._dispatcher = DispatchService(
                store=self.store,
                policy=build_authorization_service(authority_table=self.store.table, dynamodb_client=self.store.client, env=self.env),
                queue_url=os.environ.get("AGENT_DISPATCH_QUEUE_URL", ""),
                events_table=os.environ.get("WEBHOOK_EVENTS_TABLE", ""),
                sqs=boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1")),
            )
        return self._dispatcher

    async def dispatch_request(self, body, credential_token, workload_token, *, context):
        if context[3].authority.kind != "gate_decision":
            cleared = await self.resolve_fan_out(body, context)
            return await run_in_threadpool(partial(self.dispatch, body, credential_token, workload_token, context=context, fan_out_cleared=cleared))
        from src.agentauth.graph_dispatch import dispatch_graph
        from src.shared.database import get_session_factory

        config = os.environ if self.env is None else self.env
        return await dispatch_graph(
            service=self.dispatcher,
            session_factory=get_session_factory(),
            body=body,
            credential_token=credential_token,
            workload_binding=context[0].uid,
            configured_repo=config.get("BG_ORCH_DISPATCH_REPO", ""),
        )

    async def bind_wave(self, body, credential_token, workload_token, *, context):
        from src.agentauth.waves import register_wave
        from src.shared.database import get_session_factory

        config = os.environ if self.env is None else self.env
        return await register_wave(
            service=self.dispatcher,
            session_factory=get_session_factory(),
            body=body,
            credential_token=credential_token,
            workload_binding=context[0].uid,
            configured_repo=config.get("BG_ORCH_DISPATCH_REPO", ""),
        )

    async def revalidate(self, body, credential_token, workload_token, *, context):
        return await revalidate_command(self, body, context=context)

    def status(self, run_id: str, credential_token: str, workload_token: str, *, context=None) -> dict:
        pod, _, _, _ = context or self.authenticate(credential_token, workload_token)
        return self.adapter.status(credential_token=credential_token, target_run_id=run_id, presented_workload_binding=pod.uid).to_public_dict()

    def control(self, run_id: str, action: AgentAction, body: bytes, credential_token: str, workload_token: str, *, context=None) -> None:
        pod, _, _, _ = context or self.authenticate(credential_token, workload_token)
        self.adapter.prepare_command(
            credential_token=credential_token,
            target_run_id=run_id,
            action=action,
            request_body=body,
            presented_workload_binding=pod.uid,
        )
        # This runtime ships no command implementation. Enabling a policy verb
        # alone must never return success without actually forwarding its effect.
        raise PolicyError(501, f"{action.value} is not implemented in this deployment")

    def bootstrap(self, body: BootstrapRequest, token: str) -> dict:
        pod = self.workloads.verify(token)
        from src.orchestration.work_admission import admit_deferred_bootstrap, enabled

        if enabled():
            from anyio import from_thread

            from_thread.run(admit_deferred_bootstrap, self.store, body.invocation_id, body.envelope_digest)
        now = datetime.now(UTC)
        record = self.store.bind(invocation_id=body.invocation_id, digest=body.envelope_digest, pod=pod, now=now)
        return issue_bound_credential(record, now=now, env=self.env)


@lru_cache(maxsize=1)
def get_agent_runtime() -> AgentRuntime:
    if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        raise HTTPException(503, "agent authority is not enabled")
    table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
    if not table or not os.environ.get("AGENT_RUN_CREDENTIAL_KEY"):
        raise HTTPException(503, "agent authority is not configured")
    try:
        return AgentRuntime(
            store=BootstrapStore(table_name=table, dynamodb_client=boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))),
            workloads=KubernetesWorkloadVerifier.in_cluster(),
        )
    except (AuthorityStoreError, WorkloadRefusedError, OSError):
        raise HTTPException(503, "agent authority is not configured") from None


def _refuse_unconsumable_model_policy(
    model_policy: dict,
    *,
    client_contract: int | None,
    invocation_id: str,
) -> None:
    """Withhold run authority when this client cannot honour the live policy.

    The mixed-version case the gateway has to own.  A worker built before the
    enforcing contract existed ignores the ``model_policy`` payload entirely and
    launches on its legacy model assignment.  Issuing it an ordinary bound
    credential would mean the platform believed it was enforcing while that run
    quietly did whatever it used to — enforcement bypassed by version skew, with
    nothing in the evidence to show it.  Refusing here is the only place that can
    prevent it: the decision to withhold authority belongs to the gateway, which
    alone knows the committed posture.

    Three refusals, and none of them is a fallback:

    * an ``enforcing`` posture (proposal or refusal) for a client that does not
      declare the contract — it would not act on either;
    * an unverified posture on a run that *is* enrolled in the policy — the
      platform does not know what it is enforcing, so it cannot know this client
      is safe to admit;
    * an enforcing *refusal* for any client — under enforcement an unavailable
      proposal must stop the run, not let it continue on legacy.

    A verified ``report_only``/``disabled`` posture admits every client, including
    an unavailable proposal: that is exactly the current live configuration and
    legacy behaviour is correct there.  Raised as 409 rather than 404 because the
    request is well-formed and authorized — the conflict is between the client's
    capability and the platform's posture, and an operator needs to see that
    difference.  No detail about the posture leaves the gateway in the message.

    There is deliberately no exception here for a run whose snapshot is missing
    or unbound.  An earlier revision admitted those two reason codes on the theory
    that "no snapshot" meant "not enrolled, nothing to enforce".  That was wrong
    twice: the posture is a property of the persona's registered compatibility
    class, so it is established regardless of the snapshot, and missing snapshot
    material is a *policy failure* that enforcement has to catch rather than
    proof enforcement does not apply.  Any run reaching this function with an
    unverified posture is now genuinely a case where the platform cannot tell
    what it is enforcing.  Bootstrap compatibility comes from establishing the
    posture correctly upstream, not from admitting unverified runs here.
    """
    posture = model_policy.get("posture")
    verified = model_policy.get("posture_verified") is True
    if not verified:
        logger.warning(
            "Withholding run authority: live model policy posture unverified",
            extra={"invocation_id": invocation_id, "reason": model_policy.get("reason")},
        )
        raise HTTPException(409, "model policy unavailable")
    if posture != "enforcing":
        return
    if model_policy.get("status") != "proposed":
        logger.warning(
            "Withholding run authority: enforcing posture with no admissible proposal",
            extra={"invocation_id": invocation_id, "reason": model_policy.get("reason")},
        )
        raise HTTPException(409, "model policy unavailable")
    if client_contract is None or client_contract < MODEL_POLICY_CONTRACT_VERSION:
        logger.warning(
            "Withholding run authority: client cannot consume an enforcing decision",
            extra={"invocation_id": invocation_id, "client_contract": client_contract},
        )
        raise HTTPException(409, "model policy contract unsupported")


@router.post("/bootstrap")
async def bootstrap(
    body: BootstrapRequest,
    request: Request,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        result = await run_in_threadpool(runtime.bootstrap, body, request.headers.get(WORKLOAD_HEADER, ""))
        caller = verify_credential(result["credential"], env=runtime.env)
        record = await run_in_threadpool(runtime.store.authority.load_execution, invocation_id=caller.invocation_id, tenant_id=caller.tenant_id)
        grant = await run_in_threadpool(
            runtime.store.live_grant, invocation_id=caller.invocation_id, tenant_id=caller.tenant_id, attempt=caller.attempt, now=datetime.now(UTC)
        )
        record, grant = await runtime.enroll_coordinator(record, grant)
        await runtime.validate_flow(record, grant)
        from src.orchestration.work_admission import worker_checkpoint

        await worker_checkpoint(org_id=record.tenant_id, invocation_id=record.invocation_id, store=runtime.store)
        result = issue_bound_credential(record, now=datetime.now(UTC), env=runtime.env)
        from src.agentauth.model_policy import bootstrap_model_policy_live

        model_policy = await bootstrap_model_policy_live(
            db,
            store=runtime.store,
            record=record,
            grant=grant,
            env=runtime.env,
        )
        _refuse_unconsumable_model_policy(
            model_policy,
            client_contract=body.model_policy_contract,
            invocation_id=record.invocation_id,
        )
        result["model_policy"] = model_policy
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except WorkClaimError as exc:
        if exc.code == "work_waiting":
            raise HTTPException(425, "work ownership pending", headers={"Retry-After": "10"}) from None
        raise HTTPException(404, "not found") from None
    except (BootstrapRefusedError, WorkloadRefusedError):
        raise HTTPException(404, "not found") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None


async def _agent_call(request: Request, runtime: AgentRuntime, method, *args) -> JSONResponse:
    context = None

    def audit(outcome, status):
        target = args[0] if args else None
        action = "dispatch" if isinstance(target, DispatchRequest) else "monitor"
        if isinstance(target, DispatchRequest):
            target = f"{target.target.repo}#{target.target.issue}"
        elif isinstance(target, WaveRequest):
            action, target = "bind_wave", f"{target.repo}/{target.epic_ref}/{target.wave_ref}"
        elif isinstance(target, RevalidationRequest):
            action, target = "revalidate_" + target.action.value, context[1].invocation_id if context else None
        elif len(args) > 1 and isinstance(args[1], AgentAction):
            action = args[1].value
        logger.info(
            "Agent request outcome",
            extra={
                "principal": context[1].principal if context else "unverified",
                "authority_reference_id": context[3].authority.reference_id if context else None,
                "target": target,
                "action": action,
                "outcome": outcome,
                "response_status": status,
            },
        )

    try:
        context = await run_in_threadpool(runtime.authenticate, request.headers.get(CREDENTIAL_HEADER, ""), request.headers.get(WORKLOAD_HEADER, ""))
        await runtime.validate_flow(context[2], context[3])
        call_args = (*args, request.headers.get(CREDENTIAL_HEADER, ""), request.headers.get(WORKLOAD_HEADER, ""))
        if inspect.iscoroutinefunction(method):
            result = await method(*call_args, context=context)
        else:
            result = await run_in_threadpool(method, *call_args, context=context)
        audit("allowed", 202 if isinstance(args[0], DispatchRequest) else 200)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (BootstrapRefusedError, WorkloadRefusedError, CredentialError, ExecutionStateError):
        audit("refused", 404)
        raise HTTPException(404, "not found") from None
    except PolicyError as exc:
        audit("refused", exc.status_code)
        raise HTTPException(exc.status_code, exc.detail) from None
    except AuthorityStoreError:
        audit("unavailable", 503)
        raise HTTPException(503, "agent authority unavailable") from None


@router.get("/status")
async def status(request: Request, run: str, runtime: AgentRuntime = Depends(get_agent_runtime)) -> JSONResponse:
    if not run or len(run) > 128:
        raise HTTPException(404, "not found")
    return await _agent_call(request, runtime, runtime.status, run)


@router.post("/dispatch", status_code=202)
async def dispatch(body: DispatchRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)) -> JSONResponse:
    response = await _agent_call(request, runtime, runtime.dispatch_request, body)
    response.status_code = 202
    return response


@router.post("/waves")
async def bind_wave(body: WaveRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)) -> JSONResponse:
    return await _agent_call(request, runtime, runtime.bind_wave, body)


@router.post("/revalidate")
async def revalidate(body: RevalidationRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)) -> JSONResponse:
    return await _agent_call(request, runtime, runtime.revalidate, body)


@router.post("/control/{run_id}/{action}")
async def control(request: Request, run_id: str, action: AgentAction, runtime: AgentRuntime = Depends(get_agent_runtime)) -> JSONResponse:
    if not run_id or len(run_id) > 128 or action not in LIVE_CONTROL_ACTIONS:
        raise HTTPException(404, "not found")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_COMMAND_BODY_BYTES:
            raise HTTPException(413, "command body is too large")
    return await _agent_call(request, runtime, runtime.control, run_id, action, bytes(body))

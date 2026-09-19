"""Bind model traffic to the current protected pod before budget resolution.

Policy requests buffer and replay the original body frames for quoting; response
streaming is unchanged. The registry's protected worker identity requires run
authentication even when legacy budget binding is disabled or in shadow mode.
"""

from __future__ import annotations

import os
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.grants import (
    AUTHORITY_GATE_DECISION,
    AUTHORITY_REPLAN_REQUEST,
    AUTHORITY_SERVICE_POLICY,
    RECOGNIZED_AUTHORITY_KINDS,
)
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError
from src.orchestration.provider_quotes import QuoteRefusedError, quote_request, revalidate_quote
from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.logging import get_logger

logger = get_logger(__name__)


class ModelPolicyRefusedError(Exception):
    def __init__(self, decision):
        self.decision = decision


class AgentModelIdentityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(ENFORCED_PATHS):
            await self.app(scope, receive, send)
            return
        context = scope.get("state", {}).get("token_context")
        if context is None or context.auth_source != "iam" or context.user_id != "authority-worker":
            await self.app(scope, receive, send)
            return

        from src.agentauth.routes import get_agent_runtime
        from src.budget.run_binding import RunBinding
        from src.orchestration.work_admission import worker_checkpoint
        from src.orchestration.work_claims import WorkClaimError
        from src.shared.database import get_session_factory
        from src.shared.identity.resolver import UnresolvableUserEntityError, resolve_root_user_entity_id

        request = Request(scope)
        try:
            runtime = get_agent_runtime()
            _, caller, record, grant = await run_in_threadpool(
                runtime.authenticate,
                request.headers.get(CREDENTIAL_HEADER, ""),
                request.headers.get(WORKLOAD_HEADER, ""),
            )
            attribution = await runtime.validate_flow(record, grant)
            await worker_checkpoint(org_id=caller.tenant_id, invocation_id=caller.invocation_id, store=runtime.store)
            if request.headers.get("X-Agent-RunId", caller.invocation_id) != caller.invocation_id:
                raise BootstrapRefusedError("model run assertion mismatch")
            if request.headers.get("X-Agent-OrgId", caller.tenant_id) != caller.tenant_id:
                raise BootstrapRefusedError("model tenant assertion mismatch")
            # Recognized-authority handling (#4529). Previously this block keyed on two
            # inequalities — `!= "service_policy"` for root resolution and
            # `== "gate_decision"` for policy admission — so an authority kind nobody
            # had taught this path about reached the provider with no policy check, no
            # budget binding, and `flow_id` dropped from its run binding. That last
            # part is the quiet one: spend still happened, it just was not attributed
            # to the flow that caused it, so it could not be seen or capped.
            if grant.authority.kind not in RECOGNIZED_AUTHORITY_KINDS:
                raise BootstrapRefusedError("unsupported authority source")
            root = grant.authority.human_id
            if grant.authority.kind != AUTHORITY_SERVICE_POLICY:
                async with get_session_factory()() as session:
                    root = await resolve_root_user_entity_id(session, caller.tenant_id, root)
                    if grant.authority.kind in {AUTHORITY_GATE_DECISION, AUTHORITY_REPLAN_REQUEST}:
                        from src.orchestration.flow_meter import meter_target
                        from src.orchestration.policy_admission import load_in_force_policy
                        from src.orchestration.runtime_policy import authorize_worker_credential

                        execution = await run_in_threadpool(runtime.store._read, f"TENANT#{caller.tenant_id}", f"EXEC#{caller.invocation_id}")
                        inputs = await load_in_force_policy(session, org_id=caller.tenant_id, flow_id=grant.flow_id)
                        # An authoring run has no graph node, so the assignment-level
                        # check below does not apply to it and `authorize_worker_credential`
                        # refuses its kind outright. Metering still must: the ruling is
                        # that a replan author works under "existing applicable budget
                        # constraints", and an authoring job that could spend outside its
                        # flow's accepted budget would be a way to spend a flow's money
                        # without touching the flow. So this kind skips the assignment
                        # check and keeps the budget binding below.
                        if grant.authority.kind == AUTHORITY_GATE_DECISION:
                            decision = await authorize_worker_credential(
                                session, execution=execution or {}, grant=grant, broker_path="model", inputs=inputs
                            )
                            if not decision.permitted:
                                raise ModelPolicyRefusedError(decision)
                        if inputs.policy is not None:
                            if os.environ.get("BUDGET_ENFORCEMENT_ENABLED", "true").lower() != "true":
                                raise AuthorityStoreError("policy budget enforcement unavailable")
                            context._policy_flow_target = meter_target(org_id=caller.tenant_id, flow_id=grant.flow_id, policy=inputs.policy)
            if context._policy_flow_target is not None:
                # Buffer only policy-governed requests for a bounded
                # quote, then replay every original ASGI frame. In
                # particular, the upstream JSON is never reserialized.
                frames, chunks, size = [], [], 0
                while True:
                    frame = await receive()
                    if frame["type"] != "http.request":
                        return
                    frames.append(frame)
                    chunk = frame.get("body", b"")
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise BootstrapRefusedError("policy request exceeds the bounded input size")
                    chunks.append(chunk)
                    if not frame.get("more_body", False):
                        break
                body = b"".join(chunks)
                try:
                    quote = await quote_request(body, scope["path"])
                except QuoteRefusedError as exc:
                    # A refusal is a value, not a cost. There is no estimate, no
                    # default model price and no client token count to fall back
                    # to, so nothing is forwarded upstream.
                    logger.info(
                        "Bounded provider quote refused",
                        extra={"reason": exc.refusal.reason, "capability": exc.refusal.capability, "principal": caller.invocation_id},
                    )
                    raise BootstrapRefusedError("bounded provider quote unavailable") from None
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise BootstrapRefusedError("bounded provider quote unavailable") from None
                context._policy_quote = quote
                context._policy_estimated_cost = quote.total_usd
                remaining_frames = iter(frames)
                upstream_receive = receive

                async def replay():
                    frame = next(remaining_frames, None)
                    return frame if frame is not None else await upstream_receive()

                receive = replay
                # Upload time cannot extend a credential, grant, or policy. Check
                # again after the body is available, immediately before spending.
                _, caller, record, grant = await run_in_threadpool(
                    runtime.authenticate, request.headers.get(CREDENTIAL_HEADER, ""), request.headers.get(WORKLOAD_HEADER, "")
                )
                # Reauthentication re-proves the assignment against live SQL, so
                # this later result supersedes the pre-upload one: it is the state
                # immediately before spending.
                attribution = await runtime.validate_flow(record, grant)
                async with get_session_factory()() as session:
                    # Same kind test as the pre-upload check, and it must stay the same
                    # one: this is that check repeated after the body arrived, not a
                    # different policy. An authoring run is re-proved by the
                    # `authenticate` + `validate_flow` pair immediately above, which is
                    # the live re-proof available for a kind that owns no graph node.
                    if grant.authority.kind == AUTHORITY_GATE_DECISION:
                        decision = await authorize_worker_credential(session, execution=execution or {}, grant=grant, broker_path="model")
                        if not decision.permitted:
                            raise ModelPolicyRefusedError(decision)
                # The quote is evidence about specific bytes priced at a specific
                # published revision. Re-verify that binding here — after upload
                # and reauthentication, immediately before the reservation — so a
                # rate generation that rolled over, or any divergence between the
                # quoted and forwarded bytes, requotes instead of spending.
                try:
                    await revalidate_quote(quote, body, scope["path"])
                except QuoteRefusedError as exc:
                    logger.info(
                        "Bounded provider quote no longer binds this request",
                        extra={"reason": exc.refusal.reason, "capability": exc.refusal.capability, "principal": caller.invocation_id},
                    )
                    raise BootstrapRefusedError("bounded provider quote unavailable") from None
                # Client IDs are trace hints, not spend idempotency keys. Every
                # separate upstream submission gets its own reservation id.
                context._policy_request_id = str(uuid4())
                scope.setdefault("state", {})["request_id"] = context._policy_request_id
            # Authenticated registry org remains __platform__. Only attribution
            # and budget binding use the protected run's tenant and principal.
            context.attributed_org_id = caller.tenant_id
            context._protected_run_binding = RunBinding(
                run_id=caller.invocation_id,
                correlation_id=grant.flow_id or caller.invocation_id,
                tenant_id=caller.tenant_id,
                user_id=root,
                root_human_id=root,
                is_human_rooted=grant.authority.kind != AUTHORITY_SERVICE_POLICY,
                # Both engine kinds bind their flow, so an authoring run's spend is
                # attributed to the flow it was commissioned to amend rather than
                # floating free of it. Dropping `flow_id` here is what previously made
                # a non-`gate_decision` run's cost invisible to flow-level accounting.
                flow_id=grant.flow_id if grant.authority.kind in {AUTHORITY_GATE_DECISION, AUTHORITY_REPLAN_REQUEST} else None,
            )
            # Issue #4898: attach the verified graph assignment so the shared
            # usage writer can persist `usage_logs.graph_address`. Captured HERE —
            # before the request reaches the provider — so the value metering
            # later reads is the one that was actually proven for this call, not a
            # re-resolution of a node that may by then have completed.
            #
            # The equality checks are a binding assertion, not a second
            # authorization: `attribution` was composed inside
            # `validate_engine_authority` from the same `grant`/`caller` that
            # authorized this request, so agreement is expected. Requiring it
            # anyway means any future path that could return another run's or
            # tenant's assignment yields a NULL address instead of a
            # cross-attributed charge. A mismatch declines attribution; it never
            # denies the call, because attribution is reporting and must not be
            # able to break a model request.
            if (
                attribution is not None
                and attribution.org_id == caller.tenant_id
                and attribution.run_id == caller.invocation_id
                and attribution.address
            ):
                context._graph_attribution = attribution
        except ModelPolicyRefusedError as exc:
            from src.orchestration.execution_policy import DenyReason

            reason = exc.decision.reason
            status = 503 if reason in {DenyReason.SPEND_UNKNOWN, DenyReason.BUDGET_UNAVAILABLE} else 403
            error = "execution_policy_refused"
            if reason is DenyReason.SPEND_LIMIT_EXCEEDED:
                status, error = 402, "budget_exceeded"
            logger.info("Model policy refused", extra={"principal": caller.invocation_id, "flow_id": grant.flow_id, "reason": reason.value})
            await JSONResponse({"error": error, "reason": reason.value, "scope": "flow"}, status_code=status)(scope, receive, send)
            return
        except (BootstrapRefusedError, ExecutionStateError, CredentialError, WorkloadRefusedError, WorkClaimError, UnresolvableUserEntityError):
            await JSONResponse({"error": "worker_identity_refused"}, status_code=403)(scope, receive, send)
            return
        except (AuthorityStoreError, BotoCoreError, ClientError, HTTPException):
            await JSONResponse({"error": "worker_identity_unavailable"}, status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)

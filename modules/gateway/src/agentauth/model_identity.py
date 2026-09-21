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


async def _known_shared_model_run(session, run_id: str) -> bool:
    """Lookup the asserted run only to detect a missing required capability.

    Headers never confer authority. A match can only deny the tokenless request;
    authenticated assignment and policy checks below are required to grant access.
    """
    from sqlalchemy import select

    from src.orchestration.models import OrchestrationAcceptedPlan
    from src.orchestration.policy_admission import load_in_force_policy
    from src.orchestration.run_reports import OrchestrationRunReport

    assignment = await session.get(OrchestrationRunReport, run_id)
    if assignment is None:
        return False
    inputs = await load_in_force_policy(session, org_id=assignment.org_id, flow_id=assignment.flow_id)
    if inputs.policy is not None or inputs.refusal is not None:
        return True
    plans = await session.scalars(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == assignment.org_id,
            OrchestrationAcceptedPlan.flow_id == assignment.flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    for plan in plans:
        marker = (plan.plan_document or {}).get("execution_continuation")
        if isinstance(marker, dict) and marker.get("mode") == "shared_worker_role":
            return True
    return False


def _assert_report_headers(request, assignment):
    if request.headers.get("X-Agent-RunId", assignment.run_id) != assignment.run_id:
        raise BootstrapRefusedError("model run assertion mismatch")
    if request.headers.get("X-Agent-OrgId", assignment.org_id) != assignment.org_id:
        raise BootstrapRefusedError("model tenant assertion mismatch")


def _bind_report_context(context, assignment, principal, node, flow):
    from src.budget.run_binding import RunBinding
    from src.orchestration.dispatch import GraphAttribution, graph_address

    context.attributed_org_id = assignment.org_id
    context._protected_run_binding = RunBinding(
        run_id=assignment.run_id,
        correlation_id=assignment.flow_id,
        tenant_id=assignment.org_id,
        user_id=principal,
        root_human_id=principal,
        is_human_rooted=True,
        flow_id=assignment.flow_id,
    )
    context._graph_attribution = GraphAttribution(
        org_id=assignment.org_id,
        flow_id=assignment.flow_id,
        node_id=assignment.node_id,
        node_attempt=assignment.attempt,
        address=graph_address(node, flow_slug=flow.slug),
        run_id=assignment.run_id,
    )


class AgentModelIdentityMiddleware:
    def __init__(self, app):
        self.app = app

    async def _shared_worker(self, scope, receive, send, context):
        """Bind authenticated platform-run traffic to the accepted flow budget.

        A shared administrator role is not provider-side isolation. A supplied
        capability never falls back; a known governed run without one is denied.
        Unrelated legacy traffic retains its existing identity/budget behavior.
        """
        from src.orchestration.flow_meter import meter_target
        from src.orchestration.review_cycle import CycleBlockedError
        from src.orchestration.run_reports import REPORT_HEADER, RunReportError, authenticate_report_assignment
        from src.orchestration.shared_policy import authorize_shared_model
        from src.shared.database import get_session_factory

        request = Request(scope)
        credential = request.headers.get(REPORT_HEADER, "")
        asserted_run = request.headers.get("X-Agent-RunId")
        if REPORT_HEADER not in request.headers:
            if asserted_run:
                try:
                    async with get_session_factory()() as session:
                        governed = await _known_shared_model_run(session, asserted_run)
                except Exception:
                    await JSONResponse({"error": "worker_identity_unavailable"}, status_code=503)(scope, receive, send)
                    return
                if governed:
                    await JSONResponse(
                        {"error": "worker_identity_refused", "reason": "report_credential_required"},
                        status_code=403,
                    )(scope, receive, send)
                    return
            await self.app(scope, receive, send)
            return
        try:
            async with get_session_factory()() as session:
                assignment = await authenticate_report_assignment(session, credential)
                _assert_report_headers(request, assignment)
                from src.orchestration.models import OrchestrationFlow, OrchestrationNode
                from src.orchestration.policy_admission import load_in_force_policy

                inputs = await load_in_force_policy(session, org_id=assignment.org_id, flow_id=assignment.flow_id)
                legacy_report = inputs.policy is None and inputs.refusal is None and not await _known_shared_model_run(session, assignment.run_id)
                if legacy_report:
                    # Reporting ships independently from continuation activation.
                    # A valid legacy assignment keeps ordinary budgets, without
                    # inventing a policy or trusting caller-supplied attribution.
                    node = await session.get(OrchestrationNode, assignment.node_id)
                    flow = await session.get(OrchestrationFlow, assignment.flow_id)
                    actor = assignment.dispatch_metadata.get("actor")
                    principal = actor.get("user_id") if isinstance(actor, dict) else None
                    if (
                        assignment.terminal_receipt is not None
                        or not isinstance(principal, str)
                        or not principal
                        or node is None
                        or node.state not in {"running", "awaiting_merge"}
                        or flow is None
                        or flow.state not in {"pending", "running"}
                    ):
                        raise BootstrapRefusedError("legacy model assignment is not active or attributable")
                else:
                    # A present or malformed policy cannot silently become legacy.
                    policy, principal, node, flow = await authorize_shared_model(session, assignment)
                    policy_snapshot = policy.model_dump(mode="json")
                identity = (assignment.run_id, assignment.org_id, assignment.flow_id, assignment.node_id, assignment.attempt, principal)
            if legacy_report:
                _bind_report_context(context, assignment, principal, node, flow)
            else:
                if os.environ.get("BUDGET_ENFORCEMENT_ENABLED", "true").lower() != "true":
                    raise AuthorityStoreError("policy budget enforcement unavailable")

                # Same bounded quote and byte-for-byte replay contract as the
                # protected worker branch; existing budget middleware reserves it.
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
                quote = await quote_request(body, scope["path"])
                # Upload time cannot extend a run or preserve an obsolete policy.
                async with get_session_factory()() as session:
                    assignment = await authenticate_report_assignment(session, credential)
                    policy, principal, node, flow = await authorize_shared_model(session, assignment)
                    _assert_report_headers(request, assignment)
                    if identity != (assignment.run_id, assignment.org_id, assignment.flow_id, assignment.node_id, assignment.attempt, principal):
                        raise BootstrapRefusedError("model assignment changed during upload")
                    if policy_snapshot != policy.model_dump(mode="json"):
                        raise BootstrapRefusedError("model policy changed during upload")
                if os.environ.get("BUDGET_ENFORCEMENT_ENABLED", "true").lower() != "true":
                    raise AuthorityStoreError("policy budget enforcement unavailable")
                await revalidate_quote(quote, body, scope["path"])

                _bind_report_context(context, assignment, principal, node, flow)
                context._policy_flow_target = meter_target(org_id=assignment.org_id, flow_id=assignment.flow_id, policy=policy)
                context._policy_quote = quote
                context._policy_estimated_cost = quote.total_usd
                context._policy_request_id = str(uuid4())
                scope.setdefault("state", {})["request_id"] = context._policy_request_id
                remaining_frames = iter(frames)
                upstream_receive = receive

                async def replay():
                    frame = next(remaining_frames, None)
                    return frame if frame is not None else await upstream_receive()

                receive = replay
        except CycleBlockedError as exc:
            unavailable = exc.reason in {"budget_unavailable", "spend_unknown", "budget_enforcement_unavailable"}
            exhausted = exc.reason == "spend_limit_exceeded"
            await JSONResponse(
                {"error": "budget_exceeded" if exhausted else "execution_policy_refused", "reason": exc.reason, "scope": "flow"},
                status_code=402 if exhausted else 503 if unavailable else 403,
            )(scope, receive, send)
            return
        except RunReportError as exc:
            await JSONResponse(
                {"error": "worker_identity_unavailable" if exc.retryable else "worker_identity_refused", "reason": exc.code},
                status_code=503 if exc.retryable else 403,
            )(scope, receive, send)
            return
        except (BootstrapRefusedError, QuoteRefusedError):
            await JSONResponse({"error": "worker_identity_refused"}, status_code=403)(scope, receive, send)
            return
        except Exception:
            logger.warning("Shared worker model identity or budget could not be verified")
            await JSONResponse({"error": "worker_identity_unavailable"}, status_code=503)(scope, receive, send)
            return
        # The capability belongs to this authentication boundary, not to provider
        # headers or chat/request logging downstream.
        scope["headers"] = [(name, value) for name, value in scope.get("headers", []) if name.lower() != REPORT_HEADER.lower().encode()]
        await self.app(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(ENFORCED_PATHS):
            await self.app(scope, receive, send)
            return
        context = scope.get("state", {}).get("token_context")
        if context is not None and context.auth_source == "iam" and context.user_id == "scaledjob-worker":
            await self._shared_worker(scope, receive, send, context)
            return
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
                        if inputs.refusal is not None:
                            raise ModelPolicyRefusedError(inputs.refusal)
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
                    elif grant.authority.kind == AUTHORITY_REPLAN_REQUEST:
                        current_inputs = await load_in_force_policy(session, org_id=caller.tenant_id, flow_id=grant.flow_id)
                        if current_inputs.refusal is not None:
                            raise ModelPolicyRefusedError(current_inputs.refusal)
                        if current_inputs.plan_version != inputs.plan_version or current_inputs.policy != inputs.policy:
                            raise BootstrapRefusedError("authoring model policy changed during upload")
                        if os.environ.get("BUDGET_ENFORCEMENT_ENABLED", "true").lower() != "true":
                            raise AuthorityStoreError("policy budget enforcement unavailable")
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
            # Issue #5426: attach only the protected PMM-06 snapshot projection.
            # This is report-only evidence and therefore degrades to None; it can
            # neither deny the provider call nor be supplied by the worker.
            from src.usage.model_policy_evidence import HEADER, protected_usage_attribution

            context._persona_usage_attribution = await run_in_threadpool(
                protected_usage_attribution,
                store=runtime.store,
                record=record,
                decision_id=request.headers.get(HEADER),
                approving_human_id=grant.authority.human_id or None,
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

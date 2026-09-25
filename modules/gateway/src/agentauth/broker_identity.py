"""Bind every credential broker request to its authenticated worker.

The broker's invocation_id lookup alone is not authentication: workers share
IRSA. Keep the existing credential/repository authorization after this check.
"""

from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError
from src.internal.credential_authorization import BROKER_CAPABILITIES, require_broker_capability

BROKER_PATHS = frozenset({*BROKER_CAPABILITIES, "/internal/v1/github-installation-token"})
logger = logging.getLogger(__name__)


def worker_tenant(request: Request) -> str | None:
    grant = getattr(request.state, "agent_broker_grant", None)
    return grant.tenant_id if grant is not None else None


async def verify_broker_worker(request: Request) -> None:
    # Local import avoids the transport dependency's composition cycle.
    from src.agentauth.routes import get_agent_runtime
    from src.shared.config import get_settings

    context = None
    request.state.agent_user_credential_authority = None
    request.state.agent_credential_binding = None
    request.state.agent_installation_binding = None
    request.state.agent_broker_grant = None
    request.state.agent_authorized_action = None
    for attribute in ("agent_github_permissions", "agent_github_not_after"):
        if hasattr(request.state, attribute):
            delattr(request.state, attribute)
    try:
        if request.url.path == "/internal/v1/user-credentials" and request.method == "GET":
            body = dict(request.query_params)
        else:
            body = await request.json()
        runtime = get_agent_runtime()
        context = await run_in_threadpool(
            runtime.authenticate,
            request.headers.get(CREDENTIAL_HEADER, ""),
            request.headers.get(WORKLOAD_HEADER, ""),
        )
        from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION

        if context[3].authority.kind == AUTHORITY_PAID_DOMAIN_OPERATION:
            raise BootstrapRefusedError("paid domain worker has no broker authority")
        await runtime.validate_flow(context[2], context[3])
        caller = context[1]
        if not isinstance(body, dict) or body.get("invocation_id") != caller.invocation_id:
            raise BootstrapRefusedError("broker invocation mismatch")
        if request.url.path != "/internal/v1/github-installation-token":
            require_broker_capability(request)
        execution = await run_in_threadpool(runtime.store._read, f"TENANT#{caller.tenant_id}", f"EXEC#{caller.invocation_id}")
        if not execution:
            raise BootstrapRefusedError("broker execution unavailable")
        from src.orchestration.runtime_policy import WorkerCredentialDecision, authorize_worker_credential
        from src.shared.database import get_session_factory

        async with get_session_factory()() as session:
            decision = await authorize_worker_credential(
                session, execution=execution, grant=context[3], broker_path=request.url.path, credential_request=body
            )
        if not decision.permitted:
            logger.info(
                "Worker credential policy refused",
                extra={"principal": caller.principal, "reason": decision.reason.value, "action": request.url.path},
            )
            raise BootstrapRefusedError("worker credential policy refused")
        if isinstance(decision, WorkerCredentialDecision):
            request.state.agent_authorized_action = decision.action
            request.state.agent_github_permissions = decision.permissions
            request.state.agent_github_not_after = decision.not_after
            request.state.agent_user_credential_authority = decision if decision.provider_permissions else None
        if request.url.path == "/internal/v1/github-installation-token":
            repo = f"{body.get('repo_owner', '')}/{body.get('repo_name', '')}"
            if execution.get("repo") != {"S": repo} or execution.get("installation_id", {}).get("N") != str(body.get("installation_id")):
                raise BootstrapRefusedError("broker repository mismatch")
            from src.internal.credential_binding import InstallationBinding

            # Issue #5663 (A09): carry the repository this run is bound to, taken
            # from the execution row just compared above — never from the body. The
            # route re-asserts it on the default path, so both paths bind the same
            # fact and a protected caller is not silently exempted from the check.
            request.state.agent_installation_binding = InstallationBinding(
                tenant_id=caller.tenant_id,
                installation_id=int(body["installation_id"]),
                repo=execution.get("repo", {}).get("S") or None,
            )
        else:
            # Exact trusted row, never a caller-selected partition/query result.
            # No configuration permits a body-user fallback.
            settings = get_settings()
            reply = await run_in_threadpool(
                runtime.store.client.get_item,
                TableName=settings.webhook_events_table,
                Key={"event_id": {"S": caller.invocation_id}, "arrived_at": execution["arrived_at"]},
                ConsistentRead=True,
                ProjectionExpression="authorized_user_id",
            )
            user_id = reply.get("Item", {}).get("authorized_user_id", {}).get("S")
            if (
                not user_id
                or body.get("user_id") != user_id
                or user_id != context[3].authority.human_id
                or caller.tenant_id != context[3].authority.org_id
            ):
                raise BootstrapRefusedError("broker user mismatch")
            from src.internal.credential_binding import BindingResult

            request.state.agent_credential_binding = BindingResult(
                resolved_user_id=user_id,
                from_registry=True,
                drift_detected=False,
                body_user_id=user_id,
                invocation_id=caller.invocation_id,
                tenant_id=caller.tenant_id,
            )
        request.state.agent_broker_grant = context[3]
    except (BootstrapRefusedError, WorkloadRefusedError, CredentialError, ExecutionStateError, ValueError, KeyError):
        logger.info("Worker broker refused", extra={"principal": context[1].principal if context else "unverified", "action": request.url.path})
        raise HTTPException(404, "not found") from None
    except (AuthorityStoreError, ClientError, BotoCoreError):
        raise HTTPException(503, "agent authority unavailable") from None


async def verify_selected_user_credential(request: Request, credential, *, revalidate: bool = False) -> None:
    """Bind the endpoint's actual selection and repeat live checks before effects."""
    authority = getattr(request.state, "agent_user_credential_authority", None)
    if authority is None:
        return
    if revalidate:
        await verify_broker_worker(request)
        authority = getattr(request.state, "agent_user_credential_authority", None)
    if authority is None or authority.credential_id != credential.id or authority.credential_secret_arn != credential.secret_arn:
        raise HTTPException(404, "not found")


def user_credential_audit(request: Request) -> dict:
    authority = getattr(request.state, "agent_user_credential_authority", None)
    if authority is None:
        return {}
    return {
        "credential_permission_mode": "user_configured",
        "credential_lifetime": "provider_managed",
        "execution_policy_id": authority.policy_id,
        "accepted_plan_version": authority.plan_version,
    }

"""Bind legacy credential brokers to the authenticated worker when enabled.

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

BROKER_PATHS = frozenset(
    {
        "/internal/v1/github-installation-token",
        "/internal/v1/credential-assume-role",
        "/internal/v1/credential-raw-read",
        "/internal/v1/proxy-request",
        "/internal/v1/credential-materialize",
        "/internal/v1/user-credentials",
        "/internal/v1/worker-task-credentials",
    }
)
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
    request.state.agent_authorized_action = None
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
        required_scope = {
            "/internal/v1/credential-raw-read": "credential:raw-read",
            "/internal/v1/credential-materialize": "credential:materialize",
        }.get(request.url.path)
        if required_scope:
            # Header scopes remain the client's requested operation. Only the
            # registered IAM identity can grant that capability to a worker.
            identity = getattr(request.state, "token_context", None)
            if required_scope not in (getattr(identity, "credential_scopes", None) or []):
                raise BootstrapRefusedError("worker credential capability unavailable")
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

            request.state.agent_installation_binding = InstallationBinding(tenant_id=caller.tenant_id, installation_id=int(body["installation_id"]))
        else:
            # Exact trusted row, never a caller-selected partition/query result.
            # The legacy broker may be in shadow mode, so explicitly disallow
            # its fallback to body_user_id when the authoritative user is absent.
            settings = get_settings()
            reply = await run_in_threadpool(
                runtime.store.client.get_item,
                TableName=settings.webhook_events_table,
                Key={"event_id": {"S": caller.invocation_id}, "arrived_at": execution["arrived_at"]},
                ConsistentRead=True,
                ProjectionExpression="authorized_user_id",
            )
            user_id = reply.get("Item", {}).get("authorized_user_id", {}).get("S")
            if not user_id or body.get("user_id") != user_id:
                raise BootstrapRefusedError("broker user mismatch")
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

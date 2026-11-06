"""Bind every credential broker request to its authenticated worker.

The broker's invocation_id lookup alone is not authentication: workers share
IRSA. Keep the existing credential/repository authorization after this check.
"""

from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError
from src.internal.credential_authorization import BROKER_CAPABILITIES, require_broker_capability
from src.shared.models.vault import UserCredential

BROKER_PATHS = frozenset({*BROKER_CAPABILITIES, "/internal/v1/github-installation-token"})
logger = logging.getLogger(__name__)


def worker_tenant(request: Request) -> str | None:
    grant = getattr(request.state, "agent_broker_grant", None)
    authority = getattr(request.state, "agent_tools_credential_authority", None)
    if authority is not None:
        return authority.tenant_id
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
        from src.agentauth.grants import AUTHORITY_GITHUB_EVENT, AUTHORITY_PAID_DOMAIN_OPERATION

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
        dispatch_envelope = None
        if (
            context[3].authority.kind == AUTHORITY_GITHUB_EVENT
            and request.url.path == "/internal/v1/github-installation-token"
            and body.get("identity") == "review"
        ):
            from src.agentauth.human_review_identity import original_review_dispatch

            dispatch_envelope = await run_in_threadpool(
                original_review_dispatch, runtime.store, pod_uid=context[0].uid, invocation_id=caller.invocation_id, execution=execution
            )
        from src.orchestration.runtime_policy import WorkerCredentialDecision, authorize_worker_credential
        from src.shared.database import get_session_factory

        async with get_session_factory()() as session:
            decision = await authorize_worker_credential(
                session,
                execution=execution,
                grant=context[3],
                broker_path=request.url.path,
                credential_request=body,
                dispatch_envelope=dispatch_envelope,
            )
        if not decision.permitted:
            logger.info(
                "Worker credential policy refused",
                extra={"principal": caller.principal, "reason": decision.reason.value, "action": request.url.path},
            )
            raise BootstrapRefusedError("worker credential policy refused")
        if isinstance(decision, WorkerCredentialDecision):
            request.state.agent_authorized_action = decision.action
            # Protected persona of this execution; the token route uses it to
            # let a reviewer-owned repair continuation keep its reviewer identity.
            request.state.agent_execution_persona = execution.get("persona", {}).get("S")
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
            recorded_repository_id = execution.get("provider_repository_id", {}).get("N")
            request.state.agent_installation_binding = InstallationBinding(
                tenant_id=caller.tenant_id,
                installation_id=int(body["installation_id"]),
                repo=execution.get("repo", {}).get("S") or None,
                # #9550: the repository id the trusted producer recorded, for the
                # Product-scoped token set; never read from the body.
                repository_id=int(recorded_repository_id) if recorded_repository_id and str(recorded_repository_id).isdecimal() else None,
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


async def verify_selected_user_credential(request: Request, credential, *, db: AsyncSession, revalidate: bool = False) -> None:
    """Bind the endpoint's actual selection and repeat live checks before effects."""
    # Personal vault consent applies to every agent authority, including webhook
    # and service-policy runs without an accepted engine policy. Shared credentials
    # retain their existing owner-scope policy; the personal consent endpoint cannot
    # manage team, organization, or domain-app credentials.
    if credential.user_id is not None and getattr(credential, "agent_consent", False) is not True:
        raise HTTPException(403, {"error": "not_consented"})

    tools_authority = getattr(request.state, "agent_tools_credential_authority", None)
    if tools_authority is not None and revalidate:
        from src.internal.tools_credential_auth import verify_tools_credential_request

        await verify_tools_credential_request(request)
        tools_authority = getattr(request.state, "agent_tools_credential_authority", None)
        if tools_authority is None:
            raise HTTPException(403, {"error": "not_consented"})
    if tools_authority is not None:
        if (
            credential.id not in tools_authority.credential_ids
            or credential.org_id != tools_authority.tenant_id
            or credential.user_id != tools_authority.user_id
        ):
            raise HTTPException(403, {"error": "not_consented"})
    else:
        had_policy_authority = getattr(request.state, "agent_user_credential_authority", None) is not None
        if revalidate and (had_policy_authority or getattr(request.state, "agent_broker_grant", None) is not None):
            await verify_broker_worker(request)
        authority = getattr(request.state, "agent_user_credential_authority", None)
        if authority is None and had_policy_authority:
            raise HTTPException(404, "not found")
        if authority is not None and (authority.credential_id != credential.id or authority.credential_secret_arn != credential.secret_arn):
            raise HTTPException(404, "not found")

    if credential.user_id is not None and revalidate:
        # Select the column after the asynchronous authority checks, not the ORM
        # object: its identity-map snapshot may predate revocation during an effect.
        consent = await db.scalar(
            select(UserCredential.agent_consent).where(
                UserCredential.id == credential.id,
                UserCredential.user_id == credential.user_id,
                UserCredential.org_id == credential.org_id,
                UserCredential.secret_arn == credential.secret_arn,
            )
        )
        if consent is not True:
            raise HTTPException(403, {"error": "not_consented"})


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

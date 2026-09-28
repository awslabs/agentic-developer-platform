"""Resolve an authenticated worker's exact server-owned origin record."""

from __future__ import annotations

from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError


@dataclass(frozen=True)
class ServerRunIdentity:
    invocation_id: str
    tenant_id: str
    actor_user_id: str
    correlation_id: str
    root_human_id: str
    is_human_rooted: bool
    parent_invocation_id: str | None
    triggered_by: str | None = None


async def verified_run_identity(request: Request) -> ServerRunIdentity:
    from src.agentauth.routes import get_agent_runtime
    from src.shared.config import get_settings

    try:
        runtime = get_agent_runtime()
        context = await run_in_threadpool(runtime.authenticate, request.headers.get(CREDENTIAL_HEADER, ""), request.headers.get(WORKLOAD_HEADER, ""))
        caller, grant = context[1], context[3]
        await runtime.validate_flow(context[2], grant)
        execution = await run_in_threadpool(runtime.store._read, f"TENANT#{caller.tenant_id}", f"EXEC#{caller.invocation_id}")
        if not execution or not execution.get("arrived_at"):
            raise BootstrapRefusedError("origin unavailable")
        reply = await run_in_threadpool(
            runtime.store.client.get_item,
            TableName=get_settings().webhook_events_table,
            Key={"event_id": {"S": caller.invocation_id}, "arrived_at": execution["arrived_at"]},
            ConsistentRead=True,
            ProjectionExpression=(
                "tenant_id, actor_user_id, user_id, correlation_id, root_human_id, is_human_rooted, parent_invocation_id, triggered_by"
            ),
        )
        row = reply.get("Item", {})
        tenant = row.get("tenant_id", {}).get("S")
        actor = row.get("actor_user_id", {}).get("S") or row.get("user_id", {}).get("S")
        correlation = row.get("correlation_id", {}).get("S")
        rooted = row.get("is_human_rooted", {}).get("BOOL")
        root = row.get("root_human_id", {}).get("S", "")
        if (
            tenant != caller.tenant_id
            or tenant != grant.tenant_id
            or not actor
            or not correlation
            or not isinstance(rooted, bool)
            or (rooted and (not root or root != grant.authority.human_id))
            or grant.authority.org_id != tenant
        ):
            raise BootstrapRefusedError("origin binding unavailable")
        return ServerRunIdentity(
            caller.invocation_id,
            tenant,
            actor,
            correlation,
            root,
            rooted,
            row.get("parent_invocation_id", {}).get("S"),
            row.get("triggered_by", {}).get("S"),
        )
    except (BootstrapRefusedError, WorkloadRefusedError, CredentialError, ExecutionStateError, ValueError, KeyError, TypeError):
        raise HTTPException(403, "authenticated run identity required") from None
    except (AuthorityStoreError, ClientError, BotoCoreError):
        raise HTTPException(503, "run identity unavailable") from None

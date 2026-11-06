"""Resolve the person whose Bedrock routing governs a model request.

Authentication and spend attribution stay unchanged. Hosted workers may use a
person's destination only through a live, server-written run capability, using
the same tenant verification as run binding. Budget shadow/degraded state is
never credential authority.
"""

import asyncio
import time
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.budget.run_binding import RunBindingError, RunBindingResolver, verify_row_matches_caller
from src.orchestration.intake_wiring import _get_context_table
from src.shared.config import get_settings
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext


class BedrockRoutingIdentityError(BedrockGatewayError):
    def __init__(self, reason: str, *, unavailable: bool = False):
        super().__init__(
            "bedrock_routing_identity_unavailable",
            "ADP could not verify the run owner for Bedrock account routing. Retry the run or contact your platform administrator.",
            503 if unavailable else 403,
            {"reason": reason},
        )


async def routing_user(db: AsyncSession, subject: str) -> User | None:
    """The signed-in user or server-recorded run owner, independent of workspace."""
    return await db.scalar(select(User).where(or_(User.id == subject, User.cognito_sub == subject)))


async def read_routing_run(run_id: str, *, expected_sandbox_uid: str | None = None, expected_session_run_id: str | None = None) -> dict | None:
    settings = get_settings()
    resolver = RunBindingResolver(table_name=settings.webhook_events_table, aws_region=settings.aws_region)
    # Credential authority needs current lifecycle state, not the budget cache's
    # run-lifetime snapshot. Keep the blocking SDK read off the event loop.
    row = await resolver.read_current(run_id)
    if row is None or row.get("kind") != "crew":
        return row
    if row.get("status") != "active":
        return None
    if row.get("target_kind") == "ec2-instance":
        return row
    table = _get_context_table()
    if table is None:
        raise RuntimeError("crew lease context unavailable")
    header = await asyncio.to_thread(
        lambda: table.get_item(Key={"PK": f"session#{row.get('session_id')}", "SK": "header"}, ConsistentRead=True).get("Item")
    )
    lease = (header or {}).get("chatLease")
    now = time.time()
    if (
        not header
        or header.get("status") != "active"
        or header.get("sessionMode") != "persistent"
        or header.get("sessionState") in {"ending", "ended", "recovering"}
        or header.get("tenantId") != row.get("tenant_id")
        or header.get("ownerUserId") != row.get("root_human_id")
        or header.get("ownerUserId") != row.get("user_id")
        or not isinstance(lease, dict)
        or lease.get("generation") != row.get("lease_generation")
        or not lease.get("sandbox_uid")
        or not lease.get("run_id")
        or (expected_sandbox_uid is not None and lease.get("sandbox_uid") != expected_sandbox_uid)
        or (expected_session_run_id is not None and lease.get("run_id") != expected_session_run_id)
        or (isinstance(lease.get("expires_at"), bool) or not isinstance(lease.get("expires_at"), int | Decimal))
        or lease["expires_at"] <= now
        or (isinstance(header.get("ttl"), bool) or not isinstance(header.get("ttl"), int | Decimal))
        or header["ttl"] <= now
    ):
        return None
    return row


async def resolve_routing_principal(db: AsyncSession, context: TokenContext, *, agent_run_id: str | None = None) -> tuple[TokenContext, str | None]:
    subject = context.user_id
    hosted = (
        context.account_type == "service"
        and context.auth_source == "iam"
        and (context.scope in {"internal", "platform"} or context.requires_host_identity)
    )
    if hosted:
        if not agent_run_id:
            raise BedrockRoutingIdentityError("missing_run_id")
        try:
            row = await read_routing_run(agent_run_id)
        except Exception as exc:
            raise BedrockRoutingIdentityError("run_lookup_failed", unavailable=True) from exc
        if context.requires_host_identity and (row is None or row.get("kind") != "crew" or row.get("target_kind") != "ec2-instance"):
            raise BedrockRoutingIdentityError("host_run_required")
        if row is not None and row.get("kind") == "crew" and row.get("target_kind") == "ec2-instance":
            from src.cloud_desktop.host_identity import current_host

            try:
                generation = row.get("lease_generation")
                expires_at = row.get("lease_expires_at")
                valid = (
                    row.get("status") == "active"
                    and row.get("executor_scope") == "session"
                    and isinstance(generation, int | Decimal)
                    and not isinstance(generation, bool)
                    and generation == int(generation)
                    and isinstance(expires_at, int | Decimal)
                    and not isinstance(expires_at, bool)
                    and expires_at > time.time()
                    and row.get("root_human_id") == row.get("user_id")
                    and row.get("target_ref") == context._dev_box_instance_id
                    and row.get("host_role_arn") == context._dev_box_role_arn
                    and context._dev_box_role_arn is not None
                    and await current_host(
                        db,
                        box_id=row.get("session_id"),
                        generation=int(generation),
                        run_id=agent_run_id,
                        provider_instance_id=row.get("target_ref"),
                        user_id=row.get("user_id"),
                        org_id=row.get("tenant_id"),
                        deployment_id=row.get("deployment_id"),
                    )
                    is not None
                )
            except Exception as exc:
                raise BedrockRoutingIdentityError("host_lookup_failed", unavailable=True) from exc
            if not valid:
                raise BedrockRoutingIdentityError("host_fenced")
        if row is None:
            raise BedrockRoutingIdentityError("unknown_run")
        # A Dev Box host authenticates as the platform's guest agent (org
        # ``__platform__``), never as a tenant member, so its attributed tenant can
        # never equal the owner's tenant on the run row. The host identity above
        # (instance id, role ARN, live host) is the proof for an ``ec2-instance`` run;
        # the tenant it is bound to is the row's server-written one. Every other
        # hosted caller keeps asserting its own tenant (#8223 cause 32b, dev
        # 2026-10-21: every box model call answered ``tenant_mismatch`` → 403).
        host_verified = row.get("kind") == "crew" and row.get("target_kind") == "ec2-instance" and context._dev_box_role_arn is not None
        caller_org_id = str(row.get("tenant_id") or "") if host_verified else context.attributed_org_id
        try:
            binding = verify_row_matches_caller(run_id=agent_run_id, row=row, caller_user_id=context.user_id, caller_org_id=caller_org_id)
        except RunBindingError as exc:
            raise BedrockRoutingIdentityError(exc.reason) from exc
        if not row.get("status"):
            raise BedrockRoutingIdentityError("missing_run_status")
        if binding.is_human_rooted is False:
            # An explicitly service-rooted job has no person's rule to inherit.
            # It keeps its authenticated service routing, never a supplied user.
            return context, None
        if binding.is_human_rooted is not True or not binding.root_human_id:
            raise BedrockRoutingIdentityError("missing_run_owner")
        subject = binding.root_human_id
    elif context.account_type != "human":
        return context, None

    user = await routing_user(db, subject)
    if user is None:
        if hosted:
            raise BedrockRoutingIdentityError("unknown_run_owner")
        return context, None
    if user.user_kind != "human":
        raise BedrockRoutingIdentityError("owner_is_not_human")
    if hosted and row.get("kind") == "crew":
        if user.id != binding.root_human_id or user.org_id != binding.tenant_id:
            raise BedrockRoutingIdentityError("run_owner_mismatch")
        context._crew_root_human_id = user.id
        context._crew_root_run_id = agent_run_id
        if row.get("target_kind") == "ec2-instance":
            context._dev_box_host_run_id = agent_run_id
    # The person's own hierarchy also governs cloud calls made in another repo
    # workspace. Do not modify the original authentication/attribution context.
    return context.model_copy(update={"user_id": user.id, "org_id": user.org_id, "team_id": user.team_id}), user.id

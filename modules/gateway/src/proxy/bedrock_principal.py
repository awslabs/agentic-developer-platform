"""Resolve the person whose Bedrock routing governs a model request.

Authentication and spend attribution stay unchanged. Hosted workers may use a
person's destination only through a live, server-written run capability, using
the same tenant verification as run binding. Budget shadow/degraded state is
never credential authority.
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.budget.run_binding import RunBindingError, RunBindingResolver, verify_row_matches_caller
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


async def read_routing_run(run_id: str) -> dict | None:
    settings = get_settings()
    resolver = RunBindingResolver(table_name=settings.webhook_events_table, aws_region=settings.aws_region)
    # Credential authority needs current lifecycle state, not the budget cache's
    # run-lifetime snapshot. Keep the blocking SDK read off the event loop.
    return await resolver.read_current(run_id)


async def resolve_routing_principal(db: AsyncSession, context: TokenContext, *, agent_run_id: str | None = None) -> tuple[TokenContext, str | None]:
    subject = context.user_id
    hosted = context.account_type == "service" and context.auth_source == "iam" and context.scope in {"internal", "platform"}
    if hosted:
        if not agent_run_id:
            raise BedrockRoutingIdentityError("missing_run_id")
        try:
            row = await read_routing_run(agent_run_id)
        except Exception as exc:
            raise BedrockRoutingIdentityError("run_lookup_failed", unavailable=True) from exc
        if row is None:
            raise BedrockRoutingIdentityError("unknown_run")
        try:
            binding = verify_row_matches_caller(run_id=agent_run_id, row=row, caller_user_id=context.user_id, caller_org_id=context.attributed_org_id)
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
    # The person's own hierarchy also governs cloud calls made in another repo
    # workspace. Do not modify the original authentication/attribution context.
    return context.model_copy(update={"user_id": user.id, "org_id": user.org_id, "team_id": user.team_id}), user.id

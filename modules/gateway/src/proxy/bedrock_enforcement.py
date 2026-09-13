"""Resolve and sign for the Bedrock account selected by a saved routing rule.

Routing is always enforced. The former environment switch and per-organization
opt-in were rollout controls; stale values no longer override a saved mapping.
No mapping means platform credentials. A selected destination that cannot serve
an invocation fails closed with account details and remediation.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.proxy.bedrock_routing import BedrockTarget, bedrock_routing_resolver
from src.proxy.bedrock_signing import DestinationCredentials, bedrock_destination_signer
from src.shared.schemas.auth import TokenContext


@dataclass(frozen=True)
class RoutingDecision:
    """What the proxy should sign this request with, and what to record for it.

    ``credentials is None`` means "use the ambient platform client" — the case for
    every call without a usable matching mapping.

    ``target`` is carried so the settlement path can record the account **actually**
    used without resolving a second time. A default decision with no target is used
    before resolution completes and by callers that only capture routing for audit.
    Keeping the resolved target avoids walking the routing rules twice.
    """

    target: BedrockTarget | None = None
    credentials: DestinationCredentials | None = None

    @property
    def is_enforced(self) -> bool:
        return self.target is not None


async def resolve_routing_decision(context: TokenContext) -> RoutingDecision:
    """Honor the authenticated principal's saved mapping on every request.

    The resolver selects person, primary team, organization, or platform, in that
    order. A selected destination must serve the request or raise an actionable
    error; neither an unavailable destination nor a failed database read may send
    the request to the platform account. The former rollout flags are retired.
    """
    from src.shared.database import get_session_factory

    session_factory = get_session_factory()
    async with session_factory() as session:
        target = await bedrock_routing_resolver.resolve(session, context)

        # No applicable mapping: use ambient platform credentials and record
        # that this was the resolved destination. No AssumeRole call is needed.
        if target.is_platform:
            return RoutingDecision(target=target)

        user_id = await bedrock_routing_resolver.resolve_canonical_user_id(session, context)
        credentials = await bedrock_destination_signer.get_credentials(
            session,
            target,
            # Session tag only, for CloudTrail attribution in the destination account.
            # Canonical `users.id`, never the Cognito sub in `context.user_id` — the
            # repo's #4647 id-namespace contract, and what the connect flow's
            # UserSessionTag is set to. Falls back to the raw principal when there is
            # no canonical row (service accounts), which is an honest audit value
            # rather than an empty tag.
            user_id=user_id or context.user_id or "unknown",
        )
        return RoutingDecision(target=target, credentials=credentials)

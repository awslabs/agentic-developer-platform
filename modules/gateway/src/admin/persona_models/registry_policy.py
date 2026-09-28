"""Registry model restrictions shared by HTTP routes and the scheduled engine.

Keep this service independent of route imports: the tick has no web-session
signing key and must resolve the same policy without initializing web auth.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.agent_registry_service import AgentRegistryService
from src.shared.models.persona_models import ServicePrincipalAlias

logger = logging.getLogger("bedrockgateway.persona_models.registry_policy")


async def resolve_managed_service_restriction_policy(
    db: AsyncSession,
    *,
    org_id: str,
    canonical_service_principal_id: str,
    registry_service: AgentRegistryService | None = None,
) -> tuple[list[list[str]], str | None]:
    """Resolve all active Agent Registry restrictions for an administered principal.

    A canonical principal can have several aliases.  Because one saved mapping
    governs all of them, the caller may select only a model admitted by every
    active Agent Registry alias.  Missing/disabled registry rows or a registry
    read failure are contradictory policy, so the operation fails closed.
    """
    alias_ids = list(
        await db.scalars(
            select(ServicePrincipalAlias.alias_id).where(
                ServicePrincipalAlias.org_id == org_id,
                ServicePrincipalAlias.canonical_service_principal_id == canonical_service_principal_id,
                ServicePrincipalAlias.alias_source == "agent_registry",
                ServicePrincipalAlias.is_active == True,  # noqa: E712
            )
        )
    )
    if not alias_ids:
        return [], None

    service = registry_service or AgentRegistryService()
    entries_by_name: dict[str, list] = {}
    last_key: str | None = None
    try:
        while True:
            page = await service.list_agents(org_id=org_id, page_size=100, last_key=last_key)
            for entry in page.items:
                entries_by_name.setdefault(entry.agent_name, []).append(entry)
            last_key = page.last_key
            if not last_key:
                break
    except Exception:
        logger.warning(
            "service_principal_model_policy_unavailable org=%s principal=%s",
            org_id,
            canonical_service_principal_id,
            exc_info=True,
        )
        return [], "not_permitted"

    pattern_sets: list[list[str]] = []
    for alias_id in alias_ids:
        entries = entries_by_name.get(alias_id, [])
        if not entries or any(entry.status != "active" for entry in entries):
            logger.warning(
                "service_principal_model_policy_incoherent org=%s principal=%s alias=%s matches=%s",
                org_id,
                canonical_service_principal_id,
                alias_id,
                len(entries),
            )
            return [], "not_permitted"
        pattern_sets.extend([list(entry.allowed_models) for entry in entries])

    return pattern_sets, None

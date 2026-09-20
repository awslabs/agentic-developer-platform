"""Persona-model catalogue routes — Issue #5420 (PMM-03).

Read-only catalogue endpoints under ``/me/persona-models``.  Design note §6.0:
these routes exist on PMM-02's router (one router, one prefix), and the
catalogue is a route on it — not a parallel surface.

Since PMM-02 has not yet merged, this module creates the router.
When PMM-02 lands, the router ownership transfers there and this module's
routes are added to it — the PR states which happened.

``/me/...`` means "derived from your token, takes no target" — the same
convention as ``/me/bedrock-routing/selection`` (self_routes.py:99-105).
No ``/api`` prefix: CloudFront strips the first ``/api`` before the origin
(#4330, guarded by tests/test_route_prefix_convention.py).

Routes:
  - GET /me/persona-models/catalog          → persona catalogue
  - GET /me/persona-models/catalog?persona_key=X → model catalogue for X

Alias resolution is NOT a separate route.  It is part of the validation
response (§6.3) and the save path, not a standalone endpoint.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.agent_registry_service import AgentRegistryService
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.persona_models import ServicePrincipalAlias
from src.shared.schemas.auth import TokenContext

from . import catalogue_service as service
from .catalogue_schemas import (
    ModelCatalogueResponse,
    PersonaCatalogueResponse,
)
from .self_routes import get_persona_model_current_user

logger = logging.getLogger("bedrockgateway.persona_models.routes")

router = APIRouter(prefix="/me/persona-models", tags=["persona-models"])


# ``TokenContext.account_type`` uses the auth layer's vocabulary ("human" /
# "service").  The persona-models vocabulary is canonical "human" /
# "service_account" (PMM-02's DB CHECK constraint and R1).  Translate at this
# boundary rather than letting two spellings of the same concept spread.
_ACCOUNT_TYPE_TO_PRINCIPAL_KIND: dict[str, str] = {
    "human": "human",
    "service": "service_account",
}


def principal_policy_inputs(current_user: TokenContext) -> tuple[str, str | None]:
    """Server-resolved principal identity for gate 4 (operator round-4 item 4).

    Returns ``(principal_kind, canonical_principal_id)``.  Both come from the
    authenticated token — never from a request parameter, because a caller
    must not be able to ask "what may *that* principal select" (§6.2).

    ``canonical_principal_id`` prefers PMM-02's ``canonical_service_principal_id``
    once that field exists on the token.  Until PMM-02 merges, the attribute is
    absent and ``getattr`` yields ``None``; for a service_account that makes
    gate 4 fail closed, which is the correct pre-merge behaviour — R1 requires
    a canonical ID to own a preference, and a mutable name is not a substitute.

    An unrecognized ``account_type`` maps to ``service_account``, the stricter
    of the two: an unknown principal kind must not be treated as a human.
    """
    kind = _ACCOUNT_TYPE_TO_PRINCIPAL_KIND.get(current_user.account_type, "service_account")
    if kind == "human":
        # A human's canonical identity is their authenticated subject.
        return kind, current_user.user_id or None
    return kind, getattr(current_user, "canonical_service_principal_id", None) or None


def self_service_restriction_policy(current_user: TokenContext) -> tuple[list[list[str]], str | None]:
    """Return the authenticated service caller's registry-owned model policy.

    IAM authentication already resolved the exact Agent Registry row by role
    ARN.  Reusing that server-owned value avoids a second DynamoDB lookup and,
    critically, never accepts policy text from the request.  A missing value on
    an Agent Registry caller is an incoherent auth context and fails closed.
    """
    if current_user.account_type != "service" or current_user.canonical_alias_source != "agent_registry":
        return [], None
    if current_user.registered_allowed_models is None:
        return [], "not_permitted"
    return [list(current_user.registered_allowed_models)], None


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


async def resolve_effective_destination(
    db: AsyncSession,
    current_user: TokenContext,
    *,
    routing_user_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the caller's effective Bedrock destination for evidence lookup.

    Returns ``(account_id, region)``, either part of which may be ``None``.
    Evidence is keyed on the destination, so an incomplete destination can
    only ever yield "unproven" — never a permissive default.

    Three behaviours here are deliberate, each from operator review round 4:

    **The platform account is preserved.**  ``_platform_target()`` returns the
    configured platform account with ``region=None``, because the routing
    ladder has no per-request region to report.  The previous implementation
    required *both* parts and so discarded a perfectly good account id, which
    made destination-specific evidence unreachable on the default path — the
    path most deployments are on.  The runtime region (``settings.aws_region``)
    is the region an ambient-IRSA call actually lands in, so pairing it with
    the configured platform account reconstructs the real destination rather
    than inventing one.

    **A mapped rung's region wins.**  When the ladder resolved an explicit
    mapping that names a region, that region is authoritative over the
    process default.

    **Failures are distinguished, not flattened.**  A resolver fault (the
    database is unreachable) and a configuration gap (no platform account is
    set) are different operator problems with different fixes.  Collapsing
    both into a silent null destination — as a bare ``except Exception`` did —
    makes a real misconfiguration indistinguishable from normal operation, so
    each is logged with its own event name.

    Never raises: the catalogue is a read, and §6.3 requires a read to return
    a value.  A resolution failure degrades to an unproven catalogue, which
    is the fail-closed outcome.
    """
    settings = get_settings()
    configured_platform_account = settings.platform_bedrock_account_id or None

    try:
        # The module singleton, not a fresh instance: the existence-gate cache
        # lives on the instance, and bedrock_routing.py is explicit that a
        # per-request instance "would defeat it entirely", costing one query
        # per call. Constructing one here reintroduced exactly that regression.
        from src.proxy.bedrock_routing import bedrock_routing_resolver

        target = await bedrock_routing_resolver.resolve(
            db,
            current_user,
            user_id=routing_user_id,
        )
    except Exception:
        logger.warning(
            "catalogue_destination_resolver_failed user=%s",
            current_user.user_id,
            exc_info=True,
        )
        return None, None

    account_id = target.account_id or (configured_platform_account if target.is_platform else None)
    # An explicitly mapped region outranks the process default.
    region = target.region or (settings.aws_region or None)

    if account_id is None:
        # Not a resolver fault: the ladder worked and reported "no account".
        # On the platform rung that means platform_bedrock_account_id is unset,
        # which is a configuration gap an operator can close.
        logger.warning(
            "catalogue_destination_unconfigured user=%s rung=%s platform_account_set=%s",
            current_user.user_id,
            target.rung,
            configured_platform_account is not None,
        )
        return None, None

    logger.debug(
        "catalogue_destination_resolved user=%s rung=%s account=%s region=%s region_source=%s",
        current_user.user_id,
        target.rung,
        account_id,
        region,
        "mapping" if target.region else "runtime_default",
    )
    return account_id, region


@router.get("/catalog")
async def get_catalogue(
    current_user: Annotated[TokenContext, Depends(get_persona_model_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    persona_key: Annotated[str | None, Query(description="Filter by persona key for model catalogue.")] = None,
) -> PersonaCatalogueResponse | ModelCatalogueResponse:
    """Read the persona or model catalogue.

    Without ``persona_key``: returns the persona catalogue — all registered
    personas with their compatibility class and configurability.

    With ``persona_key``: returns the selectable-model catalogue filtered
    by that persona's compatibility class and the caller's effective
    destination.

    **Takes no target parameter.** The destination is resolved server-side
    from the caller's identity (§6.2).  A caller cannot ask "what may that
    principal select".

    **Never triggers a probe.** All reads serve recorded evidence only (R3).
    """
    if persona_key is None:
        # Persona catalogue
        personas = service.build_persona_catalogue()
        return PersonaCatalogueResponse(personas=personas)

    # Model catalogue for a specific persona
    from src.admin.persona_models.catalogue import persona_compatibility_class

    compat_class = persona_compatibility_class(persona_key)
    if compat_class is None:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "unknown_persona",
                "message": f"'{persona_key}' is not a registered persona.",
            },
        )

    principal_kind, canonical_principal_id = principal_policy_inputs(current_user)
    restriction_pattern_sets, policy_unavailable_reason = self_service_restriction_policy(current_user)
    account_id, region = await resolve_effective_destination(
        db,
        current_user,
        routing_user_id="" if principal_kind == "service_account" else None,
    )

    from .dispatch_selection import mapping_enabled

    models = await service.build_model_catalogue(
        db,
        persona_key=persona_key,
        account_id=account_id,
        region=region,
        principal_kind=principal_kind,
        canonical_principal_id=canonical_principal_id,
        service_restriction_pattern_sets=restriction_pattern_sets,
        policy_unavailable_reason=policy_unavailable_reason,
        # No explicit organization policy means the versioned platform
        # baseline.  Service restrictions are supplied separately above.
        tenant_allowed_patterns=None,
        require_evidence=not mapping_enabled(),
    )

    return ModelCatalogueResponse(
        tenant_id=current_user.org_id,
        persona_key=persona_key,
        compatibility_class=compat_class,
        models=models,
    )

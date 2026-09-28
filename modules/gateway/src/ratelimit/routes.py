"""
Rate limiting API routes.

Issue #133: Security Fix - All rate limit routes now require valid Cognito JWT authentication.
The get_token_context function has been updated to use proper Cognito JWT validation
via src.auth.dependencies.get_current_user instead of relying on middleware to populate
request.state.token_context.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from src.auth.dependencies import get_current_user  # Issue #133: Real Cognito JWT auth
from src.shared.schemas.auth import TokenContext

from .models import EntityType, RateLimitConfigRequest, RateLimitConfigResponse, RateLimitStatusResponse
from .service import RateLimitService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ratelimits", tags=["ratelimits"])

# Global service instance (will be injected via dependency)
_rate_limit_service: RateLimitService | None = None


def get_rate_limit_service() -> RateLimitService:
    """Get the rate limit service instance."""
    global _rate_limit_service
    if _rate_limit_service is None:
        _rate_limit_service = RateLimitService()
    return _rate_limit_service


def set_rate_limit_service(service: RateLimitService) -> None:
    """Set the rate limit service instance (for testing)."""
    global _rate_limit_service
    _rate_limit_service = service


# Issue #133: CRITICAL SECURITY FIX
# The old get_token_context that relied on request.state.token_context being
# populated by middleware has been replaced with proper Cognito JWT validation.
# Use get_current_user from src.auth.dependencies for all authentication.


def require_admin(context: TokenContext) -> TokenContext:
    """Require admin privileges."""
    if not context.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return context


@router.get("", response_model=list[RateLimitConfigResponse])
async def list_rate_limits(
    entity_type: Annotated[str | None, Query(description="Filter by entity type")] = None,
    service: RateLimitService = Depends(get_rate_limit_service),
    context: TokenContext = Depends(get_current_user),  # Issue #133: Real Cognito JWT auth
) -> list[RateLimitConfigResponse]:
    """
    List all configured rate limits for the organization.

    Requires admin privileges.
    """
    require_admin(context)

    # Get all rate limits for the org
    # Note: This is a simplified implementation
    # In production, you'd query from persistent storage
    results = []

    # For now, return configured limits from memory
    for key, limits in service._rate_limits.items():
        parts = key.split(":")
        if len(parts) >= 3:
            e_type, e_id, org_id = parts[0], parts[1], parts[2]
            if org_id == context.org_id:
                if entity_type is None or e_type == entity_type:
                    results.append(
                        RateLimitConfigResponse(
                            entity_type=e_type,
                            entity_id=e_id,
                            org_id=org_id,
                            rpm=limits.get("rpm"),
                            tpm=limits.get("tpm"),
                            concurrent_requests=limits.get("concurrent_requests"),
                            burst_size=limits.get("burst_size"),
                        )
                    )

    return results


@router.get("/{entity_type}/{entity_id}", response_model=RateLimitConfigResponse)
async def get_rate_limits(
    entity_type: Annotated[str, Path(description="Entity type (organization, department, team, user, service_account)")],
    entity_id: Annotated[str, Path(description="Entity ID")],
    service: RateLimitService = Depends(get_rate_limit_service),
    context: TokenContext = Depends(get_current_user),  # Issue #133: Real Cognito JWT auth
) -> RateLimitConfigResponse:
    """
    Get rate limit configuration for a specific entity.

    Requires admin privileges.
    """
    require_admin(context)

    try:
        e_type = EntityType(entity_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {entity_type}")

    result = await service.get_limits(e_type, entity_id, context.org_id)

    if not result:
        # Return defaults
        is_service = e_type == EntityType.SERVICE_ACCOUNT
        defaults = service._get_limits_for_entity(e_type, entity_id, context.org_id, is_service)
        return RateLimitConfigResponse(
            entity_type=entity_type,
            entity_id=entity_id,
            org_id=context.org_id,
            rpm=defaults.get("rpm"),
            tpm=defaults.get("tpm"),
            concurrent_requests=defaults.get("concurrent"),
        )

    return result


@router.put("/{entity_type}/{entity_id}", response_model=RateLimitConfigResponse)
async def configure_rate_limits(
    entity_type: Annotated[str, Path(description="Entity type (organization, department, team, user, service_account)")],
    entity_id: Annotated[str, Path(description="Entity ID")],
    config: RateLimitConfigRequest,
    service: RateLimitService = Depends(get_rate_limit_service),
    context: TokenContext = Depends(get_current_user),  # Issue #133: Real Cognito JWT auth
) -> RateLimitConfigResponse:
    """
    Configure rate limits for a specific entity.

    Requires admin privileges.
    """
    require_admin(context)

    try:
        e_type = EntityType(entity_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {entity_type}")

    # Validate that at least one limit is being set
    if config.rpm is None and config.tpm is None and config.concurrent_requests is None:
        raise HTTPException(status_code=400, detail="At least one rate limit must be specified")

    result = await service.configure_limits(e_type, entity_id, context.org_id, config)

    logger.info(f"Rate limits configured for {entity_type}/{entity_id} by {context.user_id}")

    return result


@router.delete("/{entity_type}/{entity_id}", status_code=204)
async def delete_rate_limits(
    entity_type: Annotated[str, Path(description="Entity type")],
    entity_id: Annotated[str, Path(description="Entity ID")],
    service: RateLimitService = Depends(get_rate_limit_service),
    context: TokenContext = Depends(get_current_user),  # Issue #133: Real Cognito JWT auth
) -> None:
    """
    Delete rate limit configuration for a specific entity.

    After deletion, default limits will apply.
    Requires admin privileges.
    """
    require_admin(context)

    try:
        e_type = EntityType(entity_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {entity_type}")

    deleted = await service.delete_limits(e_type, entity_id, context.org_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Rate limit configuration not found")

    logger.info(f"Rate limits deleted for {entity_type}/{entity_id} by {context.user_id}")


@router.get("/{entity_type}/{entity_id}/status", response_model=RateLimitStatusResponse)
async def get_rate_limit_status(
    entity_type: Annotated[str, Path(description="Entity type")],
    entity_id: Annotated[str, Path(description="Entity ID")],
    service: RateLimitService = Depends(get_rate_limit_service),
    context: TokenContext = Depends(get_current_user),  # Issue #133: Real Cognito JWT auth
) -> RateLimitStatusResponse:
    """
    Get current rate limit status for a specific entity.

    Shows current usage against limits.
    Requires admin privileges or self-access.
    """

    try:
        e_type = EntityType(entity_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {entity_type}")

    # Allow self-access for users checking their own status
    is_self_access = (e_type == EntityType.USER or e_type == EntityType.SERVICE_ACCOUNT) and entity_id == context.user_id

    if not context.is_admin and not is_self_access:
        raise HTTPException(status_code=403, detail="Admin privileges required or can only access own status")

    is_service = e_type == EntityType.SERVICE_ACCOUNT
    return await service.get_status(e_type, entity_id, context.org_id, is_service)

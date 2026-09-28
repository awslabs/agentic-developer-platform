"""
FastAPI routes for authentication endpoints.

Issue #133: Security Fix - The /auth/exchange endpoint has been DISABLED by default.
This endpoint accepted raw AWS credentials (access key + secret key) which is a
credential exposure risk. Use Cognito JWT authentication instead.

This module provides REST API endpoints for:
- Credential exchange (POST /auth/exchange) - DISABLED BY DEFAULT
- Service account management
- Token validation and revocation
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_db
from src.shared.exceptions import (
    BedrockGatewayError,
)
from src.shared.schemas.auth import AuthExchangeRequest, AuthExchangeResponse

from .auth_service import AuthService
from .middleware import get_current_user_context
from .schemas import ServiceAccountCreate, ServiceAccountListResponse, ServiceAccountResponse, ServiceAccountUpdate
from .service_account_service import ServiceAccountService

logger = logging.getLogger(__name__)

# Create router with prefix and tags
router = APIRouter(prefix="/auth", tags=["authentication"])

# Initialize services
auth_service = AuthService()
service_account_service = ServiceAccountService()

# Issue #133: Feature flag to control deprecated /auth/exchange endpoint
# DISABLED BY DEFAULT for security. Set BG_ENABLE_LEGACY_AUTH_EXCHANGE=true to enable.
# WARNING: Enabling this endpoint exposes a credential exchange risk.
ENABLE_LEGACY_AUTH_EXCHANGE = os.environ.get("BG_ENABLE_LEGACY_AUTH_EXCHANGE", "false").lower() == "true"


@router.post(
    "/exchange",
    response_model=AuthExchangeResponse,
    summary="[DEPRECATED] Exchange AWS credentials for Bedrock Gateway token",
    description="""
    **DEPRECATED**: This endpoint is deprecated as of Issue #119.
    Use Cognito JWT authentication instead:
    - Human users: Use PKCE flow with bg-cognito-auth.sh
    - Agents/Services: Use client_credentials flow with Cognito App Client

    Legacy: Exchange AWS temporary credentials (from AWS SSO or assumed role) for a Bedrock Gateway API token.

    This endpoint supports:
    - Human user authentication via AWS SSO (US-1.4)
    - Service account authentication via IAM roles (US-1.6)

    The provided AWS credentials are validated using AWS STS GetCallerIdentity,
    then mapped to internal organization/department/team structures.
    """,
    deprecated=True,
    responses={
        200: {"description": "Authentication successful, token returned"},
        401: {"description": "Invalid AWS credentials"},
        403: {
            "description": "Access denied - unknown organization or unregistered service account",
            "content": {
                "application/json": {
                    "examples": {
                        "unknown_organization": {
                            "summary": "Unknown Organization (US-9.2)",
                            "value": {
                                "error": "unknown_organization",
                                "message": ("AWS account 123456789012 is not registered with any organization. Contact your platform administrator."),
                            },
                        },
                        "unregistered_service_account": {
                            "summary": "Unregistered Service Account (US-9.5)",
                            "value": {
                                "error": "unregistered_service_account",
                                "message": (
                                    "IAM role arn:aws:iam::123456789012:"
                                    "role/my-role is not registered as a "
                                    "service account. Contact your org "
                                    "administrator."
                                ),
                            },
                        },
                    }
                }
            },
        },
        500: {"description": "Internal server error"},
    },
)
async def exchange_credentials(request: AuthExchangeRequest, db: AsyncSession = Depends(get_db)) -> AuthExchangeResponse:
    """Exchange AWS credentials for a Bedrock Gateway token.

    Issue #133: DISABLED BY DEFAULT for security.
    This endpoint accepts raw AWS credentials which is a credential exposure risk.
    Use Cognito JWT authentication instead (Issue #119).
    """
    # Issue #133: Check if this deprecated endpoint is enabled
    if not ENABLE_LEGACY_AUTH_EXCHANGE:
        logger.warning("Attempt to use disabled /auth/exchange endpoint")
        raise HTTPException(
            status_code=410,  # 410 Gone - resource no longer available
            detail={
                "error": "endpoint_disabled",
                "message": (
                    "The /auth/exchange endpoint is deprecated and disabled. "
                    "Use Cognito JWT authentication instead: "
                    "- Human users: Use PKCE flow with bg-cognito-auth.sh "
                    "- Agents/Services: Use client_credentials flow with Cognito App Client"
                ),
            },
        )

    try:
        logger.info("Processing credential exchange request")
        logger.warning("DEPRECATED: /auth/exchange endpoint is being used. Migrate to Cognito JWT.")

        # Use the auth service to handle the exchange
        response = await auth_service.exchange_credentials(request, db)

        logger.info(f"Credential exchange successful for account type: {response.account_type}")
        return response

    except BedrockGatewayError as e:
        # Convert specific business errors to HTTP exceptions
        logger.warning(f"Authentication failed: {e.message}")
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message, **(e.details or {})})
    except Exception as e:
        logger.error(f"Unexpected error in credential exchange: {e}")
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "An unexpected error occurred during authentication"})


@router.get(
    "/me",
    summary="Get current user information",
    description="""
    Returns information about the currently authenticated user.
    Works with both Cognito OAuth tokens and legacy gateway tokens.
    """,
    responses={
        200: {"description": "User information returned"},
        401: {"description": "Invalid or expired token"},
    },
)
async def get_current_user(token_context=Depends(get_current_user_context)) -> dict:
    """Get current user information from the authenticated token."""
    return {
        "user_id": token_context.user_id,
        "org_id": token_context.org_id,
        "department_id": token_context.department_id,
        "team_id": token_context.team_id,
        "account_type": token_context.account_type,
        "is_admin": token_context.is_admin,
        "expires_at": token_context.expires_at.isoformat() if token_context.expires_at else None,
    }


@router.get(
    "/login-options",
    summary="Public login-page configuration",
    description="""
    Public, unauthenticated read of login-page configuration for the SPA.

    Returns whether "Sign in with GitHub" is actually wired on this deployment
    (Issue #2746). On a fresh deploy the broker OAuth secret holds a placeholder
    client_id, so the login page uses this to disable the GitHub button until an
    administrator registers the deployment's GitHub App.

    Returns ONLY a boolean — never secret material, ARNs, or error detail. Check
    failures resolve to ``false`` (never a 5xx) so the login page always renders.
    """,
    responses={
        200: {"description": "Login options returned (github_login_enabled boolean)"},
    },
)
async def login_options() -> dict:
    """Return public login-page configuration (Issue #2746)."""
    from src.admin.connections.service import is_github_login_enabled

    return {"github_login_enabled": await is_github_login_enabled()}


@router.post(
    "/logout",
    summary="Logout current user",
    description="Log out the current user by revoking their token (for legacy tokens).",
    responses={
        200: {"description": "Logout successful"},
        401: {"description": "Invalid or expired token"},
    },
)
async def logout(token_context=Depends(get_current_user_context), db: AsyncSession = Depends(get_db)) -> dict:
    """Logout the current user."""
    try:
        logger.info(f"User logout: {token_context.user_id}")
        # For Cognito tokens, we don't need to do anything server-side
        # The frontend handles clearing the tokens and redirecting to Cognito logout
        # For legacy tokens, we would revoke them here
        return {"message": "Logout successful"}

    except Exception as e:
        logger.error(f"Logout error: {e}")
        # Still return success - client should clear tokens regardless
        return {"message": "Logout successful"}


@router.post(
    "/revoke",
    summary="Revoke current token",
    description="Revoke the current authentication token to log out.",
    responses={
        200: {"description": "Token revoked successfully"},
        401: {"description": "Invalid or expired token"},
        500: {"description": "Internal server error"},
    },
)
async def revoke_token(token_context=Depends(get_current_user_context), db: AsyncSession = Depends(get_db)) -> dict:
    """Revoke the current authentication token."""
    try:
        # Extract the token from the request
        # Note: This would need to be implemented in the middleware
        # For now, we'll assume the token is available in the context

        await auth_service.revoke_token("current_token", db)  # TODO: Get actual token

        logger.info(f"Token revoked for user: {token_context.user_id}")
        return {"message": "Token revoked successfully"}

    except Exception as e:
        logger.error(f"Failed to revoke token: {e}")
        raise HTTPException(status_code=500, detail={"error": "revocation_failed", "message": "Failed to revoke token"})


# Service Account Management Routes (US-1.5)


@router.post(
    "/service-accounts",
    response_model=ServiceAccountResponse,
    summary="Create a new service account",
    description="""
    Create a new service account registration (US-1.5).

    This allows organizations to register IAM roles as service accounts
    for automated agent authentication (M2M).
    """,
    responses={
        201: {"description": "Service account created successfully"},
        400: {"description": "Invalid request data"},
        409: {"description": "Service account already exists"},
        403: {"description": "Insufficient permissions"},
    },
)
async def create_service_account(
    service_account_data: ServiceAccountCreate, token_context=Depends(get_current_user_context), db: AsyncSession = Depends(get_db)
) -> ServiceAccountResponse:
    """Create a new service account."""
    try:
        # Check admin permissions
        if not token_context.is_admin:
            raise HTTPException(
                status_code=403, detail={"error": "insufficient_permissions", "message": "Admin privileges required to create service accounts"}
            )

        response = await service_account_service.create_service_account(service_account_data, token_context.org_id, db)

        logger.info(f"Service account created: {response.name}")
        return response

    except HTTPException:
        raise
    except BedrockGatewayError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message})
    except Exception as e:
        logger.error(f"Failed to create service account: {e}")
        raise HTTPException(status_code=500, detail={"error": "creation_failed", "message": "Failed to create service account"})


@router.get(
    "/service-accounts",
    response_model=ServiceAccountListResponse,
    summary="List service accounts",
    description="List service accounts with optional filtering and pagination.",
)
async def list_service_accounts(
    department_id: str | None = Query(None, description="Filter by department ID"),
    team_id: str | None = Query(None, description="Filter by team ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> ServiceAccountListResponse:
    """List service accounts."""
    try:
        response = await service_account_service.list_service_accounts(
            token_context.org_id, db, department_id=department_id, team_id=team_id, page=page, page_size=page_size
        )

        return response

    except Exception as e:
        logger.error(f"Failed to list service accounts: {e}")
        raise HTTPException(status_code=500, detail={"error": "list_failed", "message": "Failed to list service accounts"})


@router.get(
    "/service-accounts/{service_account_id}",
    response_model=ServiceAccountResponse,
    summary="Get service account details",
    description="Get details of a specific service account.",
)
async def get_service_account(
    service_account_id: str = Path(..., description="Service account ID"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> ServiceAccountResponse:
    """Get service account details."""
    try:
        response = await service_account_service.get_service_account(service_account_id, token_context.org_id, db)

        return response

    except BedrockGatewayError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message})
    except Exception as e:
        logger.error(f"Failed to get service account {service_account_id}: {e}")
        raise HTTPException(status_code=500, detail={"error": "get_failed", "message": "Failed to get service account"})


@router.put(
    "/service-accounts/{service_account_id}",
    response_model=ServiceAccountResponse,
    summary="Update service account",
    description="Update an existing service account.",
)
async def update_service_account(
    service_account_id: str = Path(..., description="Service account ID"),
    update_data: ServiceAccountUpdate = ...,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> ServiceAccountResponse:
    """Update service account."""
    try:
        # Check admin permissions
        if not token_context.is_admin:
            raise HTTPException(
                status_code=403, detail={"error": "insufficient_permissions", "message": "Admin privileges required to update service accounts"}
            )

        response = await service_account_service.update_service_account(service_account_id, update_data, token_context.org_id, db)

        logger.info(f"Service account updated: {service_account_id}")
        return response

    except HTTPException:
        raise
    except BedrockGatewayError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message})
    except Exception as e:
        logger.error(f"Failed to update service account {service_account_id}: {e}")
        raise HTTPException(status_code=500, detail={"error": "update_failed", "message": "Failed to update service account"})


@router.delete(
    "/service-accounts/{service_account_id}",
    summary="Delete service account",
    description="Delete a service account registration.",
    responses={
        200: {"description": "Service account deleted successfully"},
        404: {"description": "Service account not found"},
        403: {"description": "Insufficient permissions"},
    },
)
async def delete_service_account(
    service_account_id: str = Path(..., description="Service account ID"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete service account."""
    try:
        # Check admin permissions
        if not token_context.is_admin:
            raise HTTPException(
                status_code=403, detail={"error": "insufficient_permissions", "message": "Admin privileges required to delete service accounts"}
            )

        await service_account_service.delete_service_account(service_account_id, token_context.org_id, db)

        logger.info(f"Service account deleted: {service_account_id}")
        return {"message": "Service account deleted successfully"}

    except HTTPException:
        raise
    except BedrockGatewayError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message})
    except Exception as e:
        logger.error(f"Failed to delete service account {service_account_id}: {e}")
        raise HTTPException(status_code=500, detail={"error": "delete_failed", "message": "Failed to delete service account"})


# Admin endpoints


@router.post(
    "/admin/cleanup-tokens", summary="Clean up expired tokens", description="Administrative endpoint to clean up expired tokens from the database."
)
async def cleanup_expired_tokens(token_context=Depends(get_current_user_context), db: AsyncSession = Depends(get_db)) -> dict:
    """Clean up expired tokens."""
    try:
        # Check admin permissions
        if not token_context.is_admin:
            raise HTTPException(
                status_code=403, detail={"error": "insufficient_permissions", "message": "Admin privileges required for token cleanup"}
            )

        count = await auth_service.cleanup_expired_tokens(db)

        return {"message": "Token cleanup completed", "tokens_cleaned": count}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token cleanup failed: {e}")
        raise HTTPException(status_code=500, detail={"error": "cleanup_failed", "message": "Token cleanup failed"})


@router.post(
    "/admin/revoke-user-tokens/{user_id}",
    summary="Revoke all tokens for a user",
    description="Administrative endpoint to revoke all tokens for a specific user or service account.",
)
async def revoke_all_user_tokens(
    user_id: str = Path(..., description="User or service account ID"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Revoke all tokens for a user."""
    try:
        # Check admin permissions
        if not token_context.is_admin:
            raise HTTPException(
                status_code=403, detail={"error": "insufficient_permissions", "message": "Admin privileges required for bulk token revocation"}
            )

        count = await auth_service.revoke_all_user_tokens(user_id, token_context.org_id, db)

        return {"message": "User tokens revoked", "tokens_revoked": count}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to revoke user tokens: {e}")
        raise HTTPException(status_code=500, detail={"error": "revocation_failed", "message": "Failed to revoke user tokens"})

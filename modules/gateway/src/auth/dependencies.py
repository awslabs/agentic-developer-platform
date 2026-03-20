"""
Shared authentication dependencies for FastAPI routes.

Issue #133: Security Fix - Proper Cognito JWT authentication for all protected routes.
Issue #144: Added timing instrumentation for auth segment.

This module provides reusable FastAPI dependencies for:
- Cognito JWT token validation
- User context extraction from tokens
- Admin privilege verification

Usage:
    from src.auth.dependencies import get_current_user, require_admin

    @router.get("/protected")
    async def protected_route(user: TokenContext = Depends(get_current_user)):
        return {"user_id": user.user_id}
"""

import logging
import time
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, Header, HTTPException, Request

from src.shared.config import get_settings
from src.shared.schemas.auth import TokenContext
from src.shared.timing import get_timings

from .cognito_jwt import CognitoJWTValidator, CognitoTokenClaims

logger = logging.getLogger(__name__)

# Global Cognito validator instance (lazy initialized)
_cognito_validator: CognitoJWTValidator | None = None


def _get_cognito_validator() -> CognitoJWTValidator | None:
    """Get or create the Cognito JWT validator.

    Returns None if Cognito is not configured.
    """
    global _cognito_validator
    settings = get_settings()

    if not settings.cognito_user_pool_id:
        return None

    if _cognito_validator is None:
        try:
            _cognito_validator = CognitoJWTValidator()
        except ValueError as e:
            logger.warning(f"Cognito validator not available: {e}")
            return None

    return _cognito_validator


def _cognito_claims_to_context(claims: CognitoTokenClaims) -> TokenContext:
    """
    Convert Cognito token claims to TokenContext.

    Issue #133: Supports both human PKCE tokens and agent client_credentials tokens.

    Args:
        claims: Validated Cognito token claims

    Returns:
        TokenContext: Token context for authorization decisions
    """
    # Determine account type from custom claim
    account_type = claims.account_type or "human"

    # Determine if user is admin based on role or groups
    is_admin = (
        claims.role == "platform_admin"
        or claims.role == "admin"
        or claims.role == "org_admin"
        or "admins" in claims.cognito_groups
        or "platform-admins" in claims.cognito_groups
    )

    # For service accounts, use client_id as user_id
    user_id = claims.sub
    if account_type == "service" and not claims.username:
        user_id = claims.client_id or claims.sub

    return TokenContext(
        user_id=user_id,
        org_id=claims.org_id or "",
        team_id=claims.team_id or "",
        department_id=claims.department_id or "",
        account_type=account_type,
        is_admin=is_admin,
        expires_at=datetime.fromtimestamp(claims.exp, UTC),
    )


async def get_current_user(
    request: Request,
    authorization: str = Header(None, alias="Authorization"),
) -> TokenContext:
    """
    FastAPI dependency to get current user context from Cognito JWT token.

    Issue #133: Replaces the hardcoded mock get_current_user() that was
    returning is_admin=True for all requests.
    Issue #144: Instrumented with timing for 'auth' segment.

    This dependency:
    1. Extracts JWT token from Authorization header
    2. Validates the token against Cognito JWKS
    3. Extracts user context from token claims
    4. Returns TokenContext for authorization decisions

    Supports both:
    - Human user tokens (from PKCE/authorization code flow)
    - Agent tokens (from client_credentials flow)

    Args:
        request: FastAPI Request object (injected automatically)
        authorization: Authorization header value (e.g., "Bearer <token>")

    Returns:
        TokenContext: Current user/service account context

    Raises:
        HTTPException: If token is invalid, expired, or missing
    """
    # Issue #144: Start timing auth segment
    auth_start = time.monotonic()

    try:
        # Issue #260: Check for IAM identity first (AWS_IAM auth via /agent/* path)
        from src.shared.config import get_settings

        settings = get_settings()
        if settings.trust_apigw_headers:
            caller_identity = request.headers.get("x-caller-identity", "")
            if caller_identity:
                from src.auth.agent_registry import (
                    agent_entry_to_token_context,
                    get_agent_registry_service,
                    parse_assumed_role_arn,
                )

                role_arn = parse_assumed_role_arn(caller_identity)
                if role_arn:
                    registry = get_agent_registry_service()
                    entry = registry.get_agent_by_role_arn(role_arn)
                    if entry:
                        return agent_entry_to_token_context(entry)
                    # Role not in registry — return minimal context for unregistered IAM callers
                    # This allows the onboarding endpoint to be called by any valid IAM identity
                    # The endpoint itself controls what operations are allowed
                    return TokenContext(
                        user_id=role_arn.split("/")[-1],
                        org_id="",
                        team_id="",
                        department_id="",
                        account_type="service",
                        is_admin=False,
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                        auth_source="iam",
                    )

        # Check if authorization header is present
        if not authorization:
            raise HTTPException(
                status_code=401,
                detail={"error": "missing_token", "message": "Authorization header required"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Validate header format
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_token_format", "message": "Authorization header must be 'Bearer <token>'"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = authorization[7:]  # Remove "Bearer " prefix

        # Get Cognito validator
        validator = _get_cognito_validator()
        if validator is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "auth_not_configured", "message": "Cognito authentication is not configured"},
            )

        try:
            # Validate token against Cognito JWKS
            claims = validator.validate_token(token)

            # Convert claims to TokenContext
            context = _cognito_claims_to_context(claims)

            logger.debug(f"Token validated for user: {context.user_id}, is_admin: {context.is_admin}")
            return context

        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=401,
                detail={"error": "token_expired", "message": "Token has expired"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid Cognito token: {e}")
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_token", "message": "Invalid or malformed token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        except Exception as e:
            logger.error(f"Unexpected error validating Cognito token: {e}")
            raise HTTPException(
                status_code=500,
                detail={"error": "authentication_error", "message": "Authentication failed due to internal error"},
            )
    finally:
        # Issue #144: Record auth timing regardless of success/failure
        elapsed_ms = (time.monotonic() - auth_start) * 1000
        try:
            timings = get_timings(request)
            timings.record("auth", elapsed_ms)
        except Exception:
            pass  # Don't let timing errors break auth


async def get_current_user_from_request(request: Request) -> TokenContext:
    """
    Alternative dependency that extracts token from request object.

    Useful for routes that need access to the full request object.

    Args:
        request: FastAPI Request object

    Returns:
        TokenContext: Current user context

    Raises:
        HTTPException: If token is invalid, expired, or missing
    """
    authorization = request.headers.get("Authorization")
    return await get_current_user(request=request, authorization=authorization)


def require_admin(current_user: TokenContext = Depends(get_current_user)) -> TokenContext:
    """
    FastAPI dependency to require admin privileges.

    Use this dependency for routes that should only be accessible to admins.

    Args:
        current_user: Current user context from get_current_user

    Returns:
        TokenContext: User context (if admin)

    Raises:
        HTTPException: If user lacks admin privileges
    """
    if not current_user.is_admin:
        logger.warning(f"Access denied for non-admin user: {current_user.user_id}")
        raise HTTPException(
            status_code=403,
            detail={"error": "insufficient_permissions", "message": "Admin privileges required"},
        )

    return current_user

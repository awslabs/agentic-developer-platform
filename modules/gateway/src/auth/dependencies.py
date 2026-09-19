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
from datetime import UTC, datetime

import jwt
from fastapi import Depends, Header, HTTPException, Request

from src.shared.config import get_settings
from src.shared.schemas.auth import TokenContext
from src.shared.timing import get_timings

from .cognito_jwt import CognitoJWTValidator, CognitoTokenClaims

logger = logging.getLogger(__name__)

# Global Cognito validator instance (lazy initialized)
_cognito_validator: CognitoJWTValidator | None = None


def _stamp_trusted_alias_source(ctx: TokenContext) -> TokenContext:
    """Stamp the canonical trusted alias source on a service-caller context.

    Issue #5419 (PMM-02). Each validated auth path maps to exactly one alias
    source. The source is stamped here so the preference dependency can resolve
    the caller using a single exact ``(alias_source, alias_id)`` query — never a
    multi-source search that could cross namespaces.

    The mapping:
    - IAM / Agent Registry (``auth_source="iam"``) → ``agent_registry``
    - Cognito client_credentials (``auth_source="jwt"``, ``account_type="service"``)
      → ``cognito_m2m``

    ``sa_registration``, ``eventbridge``, and ``github_actions`` are registrable
    alias sources but have no self-auth adapter.  When an adapter exists, this
    function gets a new branch.  Until then, those callers have no trusted source
    and the preference dependency refuses them cleanly.
    """
    if ctx.account_type != "service":
        return ctx

    if ctx.auth_source == "iam":
        return ctx.model_copy(update={"canonical_alias_source": "agent_registry"})
    if ctx.auth_source == "jwt":
        return ctx.model_copy(update={"canonical_alias_source": "cognito_m2m"})

    return ctx


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
    Issue #5419: For service accounts (client_credentials flow), ``user_id`` is
    the validated ``client_id`` — never ``sub``.  Cognito client_credentials tokens
    always carry ``client_id``; its absence on a service token is an incoherent
    shape that must be refused, not guessed at.

    The previous code checked ``not claims.username`` to decide whether to use
    ``client_id``, but ``_parse_claims`` fills ``username`` with ``sub`` when the
    payload has no ``username`` key (which is always the case for
    client_credentials tokens).  That made the branch unreachable, so ``user_id``
    was always ``sub``.  The PMM dependency resolves ``cognito_m2m`` aliases
    using ``user_id``, so it searched for ``sub`` instead of the registered
    ``client_id`` — and no alias ever matched.

    Args:
        claims: Validated Cognito token claims

    Returns:
        TokenContext: Token context for authorization decisions

    Raises:
        HTTPException: If account_type is "service" but client_id is missing
    """
    # Determine account type from custom claim
    account_type = claims.account_type or "human"

    # Determine if user is admin based on role or groups.
    # NOTE: is_admin means PLATFORM admin. It must NOT include "org_admin",
    # which is an organization-scoped role assigned to every tenant's own
    # administrator. get_user_role() maps is_admin=True to PLATFORM_ADMIN with
    # no org scope, so admitting org_admin here would let any tenant admin act
    # across all organizations. This predicate is kept identical to the copies
    # in auth/middleware.py and auth/auth_service.py.
    is_admin = (
        claims.role == "platform_admin" or claims.role == "admin" or "admins" in claims.cognito_groups or "platform-admins" in claims.cognito_groups
    )

    # For service accounts (client_credentials flow), always use validated
    # client_id.  This is the identifier the alias registry is keyed on:
    # cognito_m2m aliases are registered with client_id, so resolution must
    # look up client_id, not sub.
    user_id = claims.sub
    if account_type == "service":
        if not claims.client_id:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "incoherent_service_token",
                    "message": "Service account token is missing client_id claim.",
                },
            )
        user_id = claims.client_id

    return TokenContext(
        user_id=user_id,
        org_id=claims.org_id or "",
        team_id=claims.team_id or "",
        department_id=claims.department_id or "",
        account_type=account_type,
        cognito_username=claims.username or "",
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
        # get_settings is imported at module scope (it was redundantly re-imported
        # here, which shadowed the module attribute and made this branch untestable).
        settings = get_settings()
        if settings.trust_apigw_headers:
            caller_identity = request.headers.get("x-caller-identity", "")
            if caller_identity:
                # Issue #3985: X-Caller-Identity presence is TERMINAL.
                #
                # This header is only meaningful when injected by API Gateway's
                # AWS_IAM integration; a client-supplied value is a forgery
                # attempt. So once it is present we either resolve it to a
                # registered agent or reject — we never fall through to the JWT
                # branch (which would let an attacker use a bogus ARN to reach
                # the JWT path) and we never fabricate a context for an ARN that
                # is absent from the registry.
                #
                # The previous behavior minted an authenticated `service`
                # TokenContext with org_id="" for ANY parseable ARN, which let a
                # caller assert an arbitrary user_id across the ~18 routers
                # behind this dependency. This now mirrors
                # middleware.extract_iam_identity_from_headers, which has always
                # raised UnregisteredServiceAccountError for the same case.
                from src.auth.agent_registry import (
                    agent_entry_to_token_context,
                    get_agent_registry_service,
                    parse_assumed_role_arn,
                )

                role_arn = parse_assumed_role_arn(caller_identity)
                if not role_arn:
                    logger.warning("Rejecting request: unparseable X-Caller-Identity ARN")
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": "invalid_caller_identity",
                            "message": "X-Caller-Identity is not a valid assumed-role ARN.",
                        },
                    )

                registry = get_agent_registry_service()
                entry = registry.get_agent_by_role_arn(role_arn)
                if not entry:
                    logger.warning("Rejecting request: IAM role not in agent registry")
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": "agent_not_registered",
                            "message": "Agent not registered. Contact your org administrator.",
                        },
                    )

                ctx = agent_entry_to_token_context(entry)
                # Issue #5419 (PMM-02): stamp the trusted alias source so the
                # preference dependency can resolve using an exact-source query.
                ctx = _stamp_trusted_alias_source(ctx)
                return ctx

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

            # Issue #5419 (PMM-02): stamp trusted alias source for service callers
            if context.account_type == "service":
                context = _stamp_trusted_alias_source(context)

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
        except HTTPException:
            # Preserve intentional HTTP status codes (e.g. the 401 from
            # _cognito_claims_to_context for incoherent service tokens).
            # Without this re-raise, the generic handler below converts them
            # to 500 — which hides the actionable refusal from the caller.
            raise
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

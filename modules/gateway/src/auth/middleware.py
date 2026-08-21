"""
Authentication middleware for FastAPI requests.

This module provides middleware and dependency functions for:
- Request authentication via JWT tokens (Cognito JWT or legacy gateway tokens)
- Token validation and context extraction
- Authorization checks
- Security headers and CORS handling

Issue #119: Unified Cognito JWT Auth
- All clients (browser, CLI, agents) now use Cognito JWT tokens
- Backend validates JWT against Cognito JWKS
- Custom claims (org_id, team_id, etc.) extracted from token
"""

import logging
from datetime import UTC, datetime

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import Settings, get_settings
from src.shared.database import get_db
from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.schemas.auth import TokenContext

from .auth_service import AuthService
from .cognito_jwt import CognitoJWTValidator, CognitoTokenClaims
from .exceptions import TokenValidationError

logger = logging.getLogger(__name__)

# HTTP Bearer token scheme for OpenAPI documentation
# Use auto_error=False to return 401 (not 403) for missing credentials
security = HTTPBearer(
    scheme_name="Bearer Token",
    description="Cognito JWT access token (from PKCE flow or client_credentials)",
    auto_error=False,
)

# Global auth service instance
auth_service = AuthService()

# Global Cognito validator instance (lazy initialized)
_cognito_validator: CognitoJWTValidator | None = None


def get_cognito_validator() -> CognitoJWTValidator | None:
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


async def get_current_user_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(security), db: AsyncSession = Depends(get_db)
) -> TokenContext:
    """
    FastAPI dependency to get current user context from JWT token.

    This dependency:
    1. Extracts JWT token from Authorization header
    2. Validates the token signature and expiration
    3. Checks token against database (not revoked)
    4. Returns TokenContext for authorization decisions

    Args:
        credentials: HTTP Authorization credentials (None if not provided)
        db: Database session

    Returns:
        TokenContext: Current user/service account context

    Raises:
        HTTPException: If token is missing, invalid, expired, or revoked
    """
    # Check for missing credentials (returns 401, not 403)
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_token", "message": "Authorization header required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        token = credentials.credentials

        # Validate token using auth service
        token_context = await auth_service.validate_token(token, db)

        logger.debug(f"Token validated for user: {token_context.user_id}")
        return token_context

    except TokenValidationError as e:
        logger.warning(f"Token validation failed: {e.message}")
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "message": "Invalid, expired, or revoked token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        logger.error(f"Unexpected error during token validation: {e}")
        raise HTTPException(status_code=500, detail={"error": "authentication_error", "message": "Authentication failed due to internal error"})


async def get_optional_user_context(request: Request, db: AsyncSession = Depends(get_db)) -> TokenContext | None:
    """
    FastAPI dependency to optionally get user context.

    This dependency allows endpoints to work with or without authentication.
    If a valid token is provided, it returns the context. Otherwise, returns None.

    Args:
        request: FastAPI request object
        db: Database session

    Returns:
        Optional[TokenContext]: User context if authenticated, None otherwise
    """
    try:
        # Extract token from Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Validate token
        token_context = await auth_service.validate_token(token, db)
        return token_context

    except Exception:
        # Silently ignore authentication errors for optional auth
        return None


def require_admin_privileges(token_context: TokenContext = Depends(get_current_user_context)) -> TokenContext:
    """
    FastAPI dependency to require admin privileges.

    Args:
        token_context: Current user context

    Returns:
        TokenContext: User context (if admin)

    Raises:
        HTTPException: If user lacks admin privileges
    """
    if not token_context.is_admin:
        logger.warning(f"Access denied for non-admin user: {token_context.user_id}")
        raise HTTPException(status_code=403, detail={"error": "insufficient_permissions", "message": "Admin privileges required"})

    return token_context


def require_service_account(token_context: TokenContext = Depends(get_current_user_context)) -> TokenContext:
    """
    FastAPI dependency to require service account authentication.

    Args:
        token_context: Current user context

    Returns:
        TokenContext: Service account context

    Raises:
        HTTPException: If not authenticated as service account
    """
    if token_context.account_type != "service":
        logger.warning(f"Access denied for non-service account: {token_context.user_id}")
        raise HTTPException(status_code=403, detail={"error": "service_account_required", "message": "Service account authentication required"})

    return token_context


def require_human_user(token_context: TokenContext = Depends(get_current_user_context)) -> TokenContext:
    """
    FastAPI dependency to require human user authentication.

    Args:
        token_context: Current user context

    Returns:
        TokenContext: Human user context

    Raises:
        HTTPException: If not authenticated as human user
    """
    if token_context.account_type != "human":
        logger.warning(f"Access denied for non-human account: {token_context.user_id}")
        raise HTTPException(status_code=403, detail={"error": "human_user_required", "message": "Human user authentication required"})

    return token_context


def require_organization_access(required_org_id: str, token_context: TokenContext = Depends(get_current_user_context)) -> TokenContext:
    """
    FastAPI dependency to require access to a specific organization.

    Args:
        required_org_id: Required organization ID
        token_context: Current user context

    Returns:
        TokenContext: User context (if has org access)

    Raises:
        HTTPException: If user lacks access to the organization
    """
    if token_context.org_id != required_org_id:
        logger.warning(
            f"Organization access denied: user {token_context.user_id} in org {token_context.org_id} tried to access org {required_org_id}"
        )
        raise HTTPException(status_code=403, detail={"error": "organization_access_denied", "message": "Access denied to this organization"})

    return token_context


async def auth_middleware(request: Request, call_next):
    """
    Authentication middleware for FastAPI.

    This middleware adds security headers and handles authentication
    for all requests (except excluded paths).

    Args:
        request: FastAPI request
        call_next: Next middleware/handler

    Returns:
        Response with security headers
    """
    # Add security headers
    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )

    # CORS headers (configure as needed for your environment)
    if request.method == "OPTIONS":
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "86400"

    return response


# Issue #260: Agent path prefix for AWS_IAM authenticated requests
# Requests to /agent/* come through API Gateway with AWS_IAM auth
# API Gateway strips the /agent prefix before forwarding to the backend
AGENT_PATH_PREFIX = "/agent"

# =============================================================================
# API Gateway Header-Based Authentication (Issue #240 — removed by #3985)
# =============================================================================
#
# Issue #240 introduced a Lambda authorizer that validated the caller and then
# asserted the resulting identity to this service via X-Auth-Source + X-Agent-*
# headers, which extract_api_gateway_context() turned into a full TokenContext.
#
# Issue #3985 removed that function and both of its call sites. The Lambda
# authorizer has been deprecated and unattached for some time (see
# infra/modules/lambda-authorizer/main.tf), and /{proxy+} is authorization NONE,
# so nothing trusted was setting these headers — but the code still believed
# them, which let any client that could reach the pod mint an identity for an
# arbitrary org_id/user_id by setting X-Auth-Source: iam.
#
# Identity now comes only from:
#   1. AWS_IAM SigV4 via API Gateway -> X-Caller-Identity -> agent registry
#      lookup (extract_iam_identity_from_headers below), or
#   2. a Cognito JWT.
#
# Do NOT reintroduce a header that is trusted purely on presence. The only
# remaining X-Agent-* header with any authority is X-Agent-OrgId, and it is
# honored solely as a tenant-attribution override for callers already
# authenticated via (1) whose registry entry has scope == "internal" (#747).
API_GATEWAY_HEADER_ORG_ID = "X-Agent-OrgId"

# =============================================================================
# Issue #260: AWS_IAM Auth Headers from API Gateway
# =============================================================================
# When API Gateway uses AWS_IAM authorization type, it populates these headers
# with the IAM identity of the caller. We use these to look up the agent in
# DynamoDB instead of relying on the Lambda authorizer.

# API Gateway context headers for IAM identity
# The exact header name depends on API Gateway configuration
# Common formats:
# - X-Amzn-Iam-User-Arn: The IAM user/role ARN
# - X-Amzn-Requestcontext: JSON-encoded request context (includes identity)
API_GATEWAY_HEADER_IAM_USER_ARN = "X-Amzn-Iam-User-Arn"
API_GATEWAY_HEADER_REQUEST_CONTEXT = "X-Amzn-Requestcontext"
# API Gateway also passes identity in the request context via integration request mapping
API_GATEWAY_HEADER_CALLER_IDENTITY = "X-Caller-Identity"


def extract_iam_identity_from_headers(request: Request) -> TokenContext | None:
    """
    Extract IAM identity from API Gateway AWS_IAM auth headers.

    Issue #260: When requests come through /agent/* path with AWS_IAM authorization,
    API Gateway validates the SigV4 signature and populates identity headers.
    We extract the caller's IAM role ARN and look up the agent in DynamoDB.

    The IAM identity is passed via integration request mapping template that
    maps $context.identity.userArn to a header.

    Args:
        request: FastAPI Request object with headers

    Returns:
        TokenContext if IAM identity found and agent is registered, None otherwise
    """
    import json

    headers = request.headers

    # Try multiple header sources for the IAM identity
    user_arn = None

    # 1. Check X-Caller-Identity header (set via integration request mapping)
    caller_identity = headers.get(API_GATEWAY_HEADER_CALLER_IDENTITY, "").strip()
    if caller_identity:
        user_arn = caller_identity
        logger.debug(f"Found IAM identity in X-Caller-Identity: {user_arn}")

    # 2. Check X-Amzn-Iam-User-Arn header
    if not user_arn:
        iam_user_arn = headers.get(API_GATEWAY_HEADER_IAM_USER_ARN, "").strip()
        if iam_user_arn:
            user_arn = iam_user_arn
            logger.debug(f"Found IAM identity in X-Amzn-Iam-User-Arn: {user_arn}")

    # 3. Check X-Amzn-Requestcontext header (JSON-encoded context)
    if not user_arn:
        request_context = headers.get(API_GATEWAY_HEADER_REQUEST_CONTEXT, "").strip()
        if request_context:
            try:
                import base64

                # Request context might be base64 encoded
                try:
                    decoded = base64.b64decode(request_context).decode("utf-8")
                    context = json.loads(decoded)
                except Exception:
                    context = json.loads(request_context)

                # Extract userArn from identity
                identity = context.get("identity", {})
                user_arn = identity.get("userArn", "")
                if user_arn:
                    logger.debug(f"Found IAM identity in X-Amzn-Requestcontext: {user_arn}")
            except Exception as e:
                logger.debug(f"Failed to parse X-Amzn-Requestcontext: {e}")

    if not user_arn:
        logger.debug("No IAM identity found in request headers")
        return None

    # Import here to avoid circular imports
    from src.auth.agent_registry import (
        agent_entry_to_token_context,
        get_agent_registry_service,
        parse_assumed_role_arn,
    )

    # Parse the assumed-role ARN to get the IAM role ARN
    role_arn = parse_assumed_role_arn(user_arn)
    if not role_arn:
        logger.warning(f"Could not parse IAM role ARN from: {user_arn}")
        return None

    # Look up the agent in DynamoDB
    service = get_agent_registry_service()
    agent_entry = service.get_agent_by_role_arn(role_arn)
    if not agent_entry:
        logger.warning(f"Agent not found in registry for role: {role_arn}")
        from src.shared.exceptions import UnregisteredServiceAccountError

        raise UnregisteredServiceAccountError(role_arn)

    # Build TokenContext from agent entry
    token_context = agent_entry_to_token_context(agent_entry)

    # Issue #747: Accept X-Agent-OrgId override for internal-scope agents.
    # Internal agents (e.g. scaledjob-worker) pass the triggering tenant's
    # org_id via this header so usage_logs attribute calls to the right tenant.
    if agent_entry.get("scope") == "internal":
        override_org_id = headers.get(API_GATEWAY_HEADER_ORG_ID, "").strip()
        if override_org_id:
            token_context.org_id = override_org_id
            logger.debug(f"Internal agent org_id overridden to: {override_org_id}")

    logger.info(f"IAM auth successful: agent={agent_entry['agent_name']}, org={token_context.org_id}, team={agent_entry['team_id']}")
    return token_context


class TokenContextMiddleware:
    """ASGI middleware that extracts Cognito JWT and sets request.state.token_context.

    Two auth paths, tried in this order:
    1. API Gateway IAM auth (X-Caller-Identity -> agent registry lookup), for the
       AWS_IAM /agent/{proxy+} route — when BG_TRUST_APIGW_HEADERS=true (#260)
    2. Cognito JWT auth (Authorization / X-Api-Key header) — default

    Issue #3985 removed a third path (#240's X-Auth-Source / X-Agent-* Lambda
    authorizer headers). It minted a TokenContext from headers alone, with no
    registry lookup or signature, so any client that could reach the pod could
    assert an arbitrary org_id — including through the public CloudFront edge.

    This runs before BudgetEnforcementMiddleware and RateLimitEnforcementMiddleware
    so they can access the authenticated user context.

    Only processes proxy paths that require enforcement. Non-proxy paths
    (admin, health, etc.) are passed through without auth extraction.
    Auth failures are silently skipped — the route handler will reject
    unauthenticated requests with a proper 401.
    """

    def __init__(self, app):
        self.app = app
        self._settings = None

    def _get_settings(self) -> "Settings":
        """Lazy load settings to avoid circular imports."""
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not any(path.startswith(p) for p in ENFORCED_PATHS):
            await self.app(scope, receive, send)
            return

        # Build a minimal Request to read headers
        request = Request(scope, receive, send)
        settings = self._get_settings()

        # Issue #260: Check for IAM identity first (AWS_IAM auth via /agent/* path)
        # This takes priority over other auth methods when IAM headers are present
        if settings.trust_apigw_headers:
            # Check for IAM identity headers (set by API Gateway AWS_IAM auth)
            caller_identity = request.headers.get(API_GATEWAY_HEADER_CALLER_IDENTITY, "")
            iam_user_arn = request.headers.get(API_GATEWAY_HEADER_IAM_USER_ARN, "")
            if caller_identity or iam_user_arn:
                try:
                    from src.shared.timing import get_timings

                    timings = get_timings(request)
                    with timings.time_segment("auth"):
                        token_context = extract_iam_identity_from_headers(request)
                    if token_context:
                        scope.setdefault("state", {})["token_context"] = token_context
                        request.state.token_context = token_context
                        await self.app(scope, receive, send)
                        return
                except HTTPException:
                    # IAM auth failed — re-raise to reject request
                    raise
                except Exception as e:
                    # Check if it's an unregistered service account error
                    from src.shared.exceptions import UnregisteredServiceAccountError

                    if isinstance(e, UnregisteredServiceAccountError):
                        logger.warning("IAM auth failed: agent not registered")
                        raise HTTPException(
                            status_code=403,
                            detail={"error": "agent_not_registered", "message": "Agent not registered. Contact your org administrator."},
                        )
                    logger.debug(f"IAM auth extraction failed: {e}, falling back to other auth methods")

        # Issue #240's X-Auth-Source / X-Agent-* branch was removed by #3985 —
        # see the header-constants block above. A request that would previously
        # have been authenticated here now falls through to Cognito JWT auth.

        # Fall back to Cognito JWT auth
        authorization = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")

        if authorization and not authorization.startswith("Bearer "):
            # x-api-key value — wrap it
            if not authorization.startswith("Bearer"):
                authorization = f"Bearer {authorization}"

        if authorization:
            try:
                from src.shared.timing import get_timings

                timings = get_timings(request)
                with timings.time_segment("auth"):
                    token_context = await validate_cognito_jwt(authorization)
                scope.setdefault("state", {})["token_context"] = token_context
                # Also set on request.state for downstream middleware
                request.state.token_context = token_context
            except Exception:
                # Auth failed — let the route handler deal with it
                pass

        await self.app(scope, receive, send)


class AuthenticationError(Exception):
    """Custom authentication error for middleware."""

    def __init__(self, message: str, status_code: int = 401):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def create_token_context_dependency(require_auth: bool = True):
    """
    Factory function to create token context dependencies with different requirements.

    Args:
        require_auth: Whether authentication is required

    Returns:
        Dependency function
    """
    if require_auth:
        return get_current_user_context
    else:
        return get_optional_user_context


# =============================================================================
# Unified Cognito JWT Validation (Issue #119)
# =============================================================================


async def validate_cognito_jwt(authorization: str) -> TokenContext:
    """
    Validate a Cognito JWT access token and extract user context.

    This is the primary validation function for the unified auth flow.
    It validates the JWT against Cognito JWKS and extracts custom claims
    injected by the Pre Token Generation Lambda.

    Issue #119: Used by all routes (proxy + admin) for authentication.

    Args:
        authorization: Full Authorization header value (e.g., "Bearer <token>")

    Returns:
        TokenContext: User/service account context from token claims

    Raises:
        HTTPException: If token is invalid, expired, or missing required claims
    """
    # Extract token from Authorization header
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_token", "message": "Authorization header required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token_format", "message": "Authorization header must be 'Bearer <token>'"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:]  # Remove "Bearer " prefix

    # Get Cognito validator
    validator = get_cognito_validator()
    if validator is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "auth_not_configured", "message": "Cognito authentication is not configured"},
        )

    try:
        # Validate token against Cognito JWKS
        claims = validator.validate_token(token)

        # Convert claims to TokenContext
        return _cognito_claims_to_context(claims)

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


def _cognito_claims_to_context(claims: CognitoTokenClaims) -> TokenContext:
    """
    Convert Cognito token claims to TokenContext.

    Issue #119: Supports both human and service account tokens.

    Args:
        claims: Validated Cognito token claims

    Returns:
        TokenContext: Token context for authorization decisions
    """
    # Determine account type from custom claim
    account_type = claims.account_type or "human"

    # Determine if user is admin based on role or groups
    is_admin = (
        claims.role == "platform_admin" or claims.role == "admin" or "admins" in claims.cognito_groups or "platform-admins" in claims.cognito_groups
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


async def get_cognito_user_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> TokenContext:
    """
    FastAPI dependency for Cognito-only JWT validation.

    Issue #119: This dependency only validates Cognito JWT tokens.
    Use this for routes that should only accept Cognito tokens
    (no fallback to legacy gateway tokens).

    Args:
        credentials: HTTP Authorization credentials (None if not provided)

    Returns:
        TokenContext: Current user/service account context

    Raises:
        HTTPException: If token is missing, invalid, expired, or not a Cognito token
    """
    # Check for missing credentials (returns 401, not 403)
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_token", "message": "Authorization header required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await validate_cognito_jwt(f"Bearer {credentials.credentials}")


# Pre-configured dependencies for common use cases
authenticated_user = Depends(get_current_user_context)
optional_user = Depends(get_optional_user_context)
admin_user = Depends(require_admin_privileges)
service_account_user = Depends(require_service_account)
human_user_only = Depends(require_human_user)

# Issue #119: Cognito-only authentication dependency
cognito_user = Depends(get_cognito_user_context)

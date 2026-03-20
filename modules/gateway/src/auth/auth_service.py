"""
AuthService implementation for the Bedrock Gateway Authentication module.

This module implements the IAuthService interface and provides the core authentication
functionality including AWS STS integration, token management, and tenant resolution.

Now supports both:
- Legacy AWS credential exchange (for service accounts and CLI)
- Cognito OAuth 2.0 JWT validation (for web UI authentication)
"""

from datetime import UTC, datetime

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.exceptions import InvalidCredentialsError, UnknownOrganizationError, UnregisteredServiceAccountError
from src.shared.interfaces.auth import IAuthService
from src.shared.logging import get_logger
from src.shared.metrics import emit_auth_exchange_count
from src.shared.schemas.auth import AuthExchangeRequest, AuthExchangeResponse, TokenContext

from .cognito_jwt import CognitoJWTValidator, CognitoTokenClaims
from .exceptions import AuthenticationError, AuthorizationError, TokenValidationError
from .sts_client import STSClient
from .tenant_resolver import TenantResolver
from .token_manager import TokenManager

logger = get_logger(__name__)


class AuthService(IAuthService):
    """
    Core authentication service implementing the IAuthService interface.

    This service orchestrates the authentication flow:
    1. Validates AWS credentials via STS
    2. Resolves tenant information (org/department/team)
    3. Generates and stores secure tokens
    4. Validates tokens for subsequent requests
    5. Handles token revocation

    Supports:
    - Human user authentication via AWS SSO (US-1.4)
    - Service account registration and authentication (US-1.5, US-1.6)
    - Unknown organization handling (US-9.2)
    - Unregistered service account handling (US-9.5)
    """

    def __init__(
        self,
        sts_client: STSClient | None = None,
        tenant_resolver: TenantResolver | None = None,
        token_manager: TokenManager | None = None,
        cognito_validator: CognitoJWTValidator | None = None,
    ):
        """
        Initialize the authentication service.

        Args:
            sts_client: AWS STS client for credential validation
            tenant_resolver: Service for resolving tenant information
            token_manager: Service for token management
            cognito_validator: Cognito JWT validator for OAuth tokens
        """
        self.sts_client = sts_client or STSClient()
        self.tenant_resolver = tenant_resolver or TenantResolver()
        settings = get_settings()

        if token_manager is None:
            token_manager = TokenManager(settings.token_secret_key)
        self.token_manager = token_manager

        # Initialize Cognito validator if configured
        self._cognito_validator = cognito_validator
        self._cognito_enabled = bool(settings.cognito_user_pool_id and settings.cognito_client_id)

    async def exchange_credentials(self, request: AuthExchangeRequest, db: AsyncSession) -> AuthExchangeResponse:
        """
        Exchange AWS credentials for a Bedrock Gateway token.

        This implements the core authentication flow:
        1. Validate AWS credentials using STS GetCallerIdentity
        2. Resolve caller identity to tenant information
        3. Generate and store a JWT token
        4. Return the token with metadata

        Args:
            request: AWS credentials to exchange
            db: Database session

        Returns:
            AuthExchangeResponse: Generated token and metadata

        Raises:
            InvalidCredentialsError: If AWS credentials are invalid
            UnknownOrganizationError: If AWS account is not registered
            UnregisteredServiceAccountError: If service account is not registered
            AuthenticationError: If authentication fails for any other reason
        """
        try:
            logger.debug("Starting credential exchange process")

            # Step 1: Validate AWS credentials using STS
            try:
                caller_identity = await self.sts_client.get_caller_identity(
                    aws_access_key_id=request.aws_access_key_id,
                    aws_secret_access_key=request.aws_secret_access_key,
                    aws_session_token=request.aws_session_token,
                )
                logger.debug(f"AWS credentials validated for account: {caller_identity.account}")

            except Exception as e:
                logger.warning(f"AWS credential validation failed: {e}")
                raise InvalidCredentialsError("Invalid AWS credentials provided")

            # Step 2: Resolve tenant information
            try:
                tenant_info = await self.tenant_resolver.resolve_tenant(caller_identity, db)
                logger.debug(f"Resolved tenant: {tenant_info.org_name} / {tenant_info.account_type}")

            except Exception as e:
                # Tenant resolver raises specific exceptions (UnknownOrganizationError, etc.)
                # that are automatically converted to proper HTTP responses
                logger.warning(f"Tenant resolution failed: {e}")
                raise

            # Step 3: Generate JWT token
            try:
                token, expires_at = self.token_manager.generate_token(tenant_info)
                logger.debug(f"Generated token for entity: {tenant_info.entity_id}")

            except Exception as e:
                logger.error(f"Token generation failed: {e}")
                raise AuthenticationError("Failed to generate authentication token")

            # Step 4: Store token in database for tracking and revocation
            try:
                token_id = await self.token_manager.store_token(token, tenant_info, expires_at, db)
                logger.debug(f"Stored token with ID: {token_id}")

            except Exception as e:
                logger.error(f"Token storage failed: {e}")
                # Continue anyway - token is still valid even if storage fails
                # This allows the system to be resilient to database issues

            # Step 5: Return successful authentication response
            response = AuthExchangeResponse(
                token=token,
                expires_at=expires_at,
                user_id=tenant_info.entity_id,
                org_id=tenant_info.org_id,
                team_id=tenant_info.team_id,
                department_id=tenant_info.department_id,
                account_type=tenant_info.account_type,
            )

            # Emit success metric
            emit_auth_exchange_count(
                org_id=tenant_info.org_id,
                account_type=tenant_info.account_type,
                success=True,
            )

            logger.info(
                "Authentication successful",
                extra={
                    "account_type": tenant_info.account_type,
                    "entity_id": tenant_info.entity_id,
                    "org_name": tenant_info.org_name,
                },
            )

            return response

        except (InvalidCredentialsError, AuthenticationError, UnknownOrganizationError, UnregisteredServiceAccountError):
            # Emit failure metric
            emit_auth_exchange_count(
                org_id="unknown",
                account_type="unknown",
                success=False,
            )
            # Re-raise authentication and authorization errors as-is
            raise
        except Exception as e:
            # Emit failure metric
            emit_auth_exchange_count(
                org_id="unknown",
                account_type="unknown",
                success=False,
            )
            # Catch any unexpected errors and wrap them
            logger.error(
                "Unexpected error during credential exchange",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise AuthenticationError(f"Authentication failed: {str(e)}")

    async def validate_token(self, token: str, db: AsyncSession) -> TokenContext:
        """
        Validate a JWT token and return context information.

        Supports two types of tokens:
        1. Cognito JWT tokens (from OAuth 2.0 flow) - validated via JWKS
        2. Legacy gateway tokens (from credential exchange) - validated via database

        Args:
            token: JWT token to validate
            db: Database session

        Returns:
            TokenContext: Token context for the authenticated request

        Raises:
            TokenValidationError: If token validation fails
        """
        try:
            logger.debug("Validating token")

            # Try Cognito validation first if enabled
            if self._cognito_enabled:
                try:
                    cognito_context = await self._validate_cognito_token(token)
                    if cognito_context:
                        logger.debug(f"Cognito token validated for user: {cognito_context.user_id}")
                        return cognito_context
                except TokenValidationError:
                    # Cognito validation failed, try legacy validation
                    logger.debug("Cognito validation failed, trying legacy token validation")

            # Fall back to legacy token validation
            token_context = await self.token_manager.validate_token(token, db)

            logger.debug(f"Token validated for user: {token_context.user_id}")
            return token_context

        except TokenValidationError:
            # Re-raise token validation errors as-is
            raise
        except Exception as e:
            logger.error(f"Unexpected error during token validation: {e}")
            raise TokenValidationError(f"Token validation failed: {str(e)}")

    async def _validate_cognito_token(self, token: str) -> TokenContext | None:
        """
        Validate a Cognito JWT token.

        Args:
            token: JWT token to validate

        Returns:
            TokenContext if valid, None if not a Cognito token

        Raises:
            TokenValidationError: If token is a Cognito token but invalid
        """
        try:
            # Get or create validator
            if self._cognito_validator is None:
                self._cognito_validator = CognitoJWTValidator()

            # Validate the token
            claims = self._cognito_validator.validate_token(token)

            # Convert Cognito claims to TokenContext
            return self._cognito_claims_to_context(claims)

        except jwt.InvalidTokenError as e:
            # Check if this looks like a Cognito token (RS256 signed)
            # If so, it's an invalid Cognito token
            try:
                header = jwt.get_unverified_header(token)
                if header.get("alg") == "RS256":
                    raise TokenValidationError(f"Invalid Cognito token: {e}")
            except Exception:
                pass

            # Not a Cognito token, return None to try legacy validation
            return None

    def _cognito_claims_to_context(self, claims: CognitoTokenClaims) -> TokenContext:
        """
        Convert Cognito token claims to TokenContext.

        Issue #119: Updated to support both human and service account tokens.
        The account_type is now determined from custom:account_type claim
        injected by the Pre Token Generation Lambda.

        Args:
            claims: Validated Cognito token claims

        Returns:
            TokenContext: Token context for authorization
        """
        # Determine account type from custom claim or infer from token
        # Service accounts (client_credentials) have custom:account_type = "service"
        account_type = claims.account_type or "human"

        # Determine if user is admin based on role or groups
        is_admin = claims.role == "platform_admin" or claims.role == "admin" or "admins" in claims.cognito_groups

        # For service accounts, use client_id as user_id if sub is empty
        user_id = claims.sub
        if account_type == "service" and not claims.username:
            # client_credentials tokens use client_id as the subject
            user_id = claims.client_id or claims.sub

        logger.debug(f"Cognito token validated: user_id={user_id}, org_id={claims.org_id}, account_type={account_type}, is_admin={is_admin}")

        return TokenContext(
            user_id=user_id,
            org_id=claims.org_id or "",
            team_id=claims.team_id or "",
            department_id=claims.department_id or "",
            account_type=account_type,
            is_admin=is_admin,
            expires_at=datetime.fromtimestamp(claims.exp, UTC),
        )

    async def revoke_token(self, token: str, db: AsyncSession) -> None:
        """
        Revoke a JWT token.

        This method marks the token as revoked in the database,
        preventing its future use.

        Args:
            token: JWT token to revoke
            db: Database session

        Raises:
            TokenValidationError: If token revocation fails
        """
        try:
            logger.debug("Revoking token")

            success = await self.token_manager.revoke_token(token, db)
            if success:
                logger.debug("Token revoked successfully")
            else:
                logger.warning("Token not found or already revoked")

        except Exception as e:
            logger.error(f"Failed to revoke token: {e}")
            raise TokenValidationError(f"Token revocation failed: {str(e)}")

    async def revoke_all_user_tokens(self, entity_id: str, org_id: str, db: AsyncSession) -> int:
        """
        Revoke all tokens for a specific user or service account.

        This is useful for:
        - User logout from all sessions
        - Service account key rotation
        - Security incidents requiring immediate access revocation

        Args:
            entity_id: User or service account ID
            org_id: Organization ID
            db: Database session

        Returns:
            int: Number of tokens revoked

        Raises:
            AuthorizationError: If token revocation fails
        """
        try:
            logger.debug(f"Revoking all tokens for entity: {entity_id}")

            count = await self.token_manager.revoke_all_user_tokens(entity_id, org_id, db)

            logger.info(f"Revoked {count} tokens for entity: {entity_id}")
            return count

        except Exception as e:
            logger.error(f"Failed to revoke user tokens for {entity_id}: {e}")
            raise AuthorizationError(f"Token revocation failed: {str(e)}")

    async def cleanup_expired_tokens(self, db: AsyncSession) -> int:
        """
        Clean up expired tokens from the database.

        This method should be called periodically (e.g., via a scheduled job)
        to remove expired tokens and keep the database clean.

        Args:
            db: Database session

        Returns:
            int: Number of tokens cleaned up
        """
        try:
            logger.debug("Starting expired token cleanup")

            count = await self.token_manager.cleanup_expired_tokens(db)

            if count > 0:
                logger.info(f"Cleaned up {count} expired tokens")
            else:
                logger.debug("No expired tokens to clean up")

            return count

        except Exception as e:
            logger.error(f"Token cleanup failed: {e}")
            # Don't raise exception for cleanup failures - this is a background operation
            return 0

    def get_token_info_without_validation(self, token: str) -> dict | None:
        """
        Extract token information without validation.

        This is useful for logging and debugging purposes.
        Should NOT be used for authorization decisions.

        Args:
            token: JWT token

        Returns:
            Optional[dict]: Token claims or None if parsing fails
        """
        return self.token_manager.extract_claims_without_verification(token)

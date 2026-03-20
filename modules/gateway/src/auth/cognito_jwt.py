"""
Cognito JWT Token Validator

This module provides JWT validation for AWS Cognito tokens using JWKS.
It validates the token signature, issuer, audience, and expiration.
"""

import logging
from typing import Any

import jwt
from jwt import PyJWKClient, PyJWKClientError
from pydantic import BaseModel

from src.shared.config import get_settings

logger = logging.getLogger(__name__)


class CognitoTokenClaims(BaseModel):
    """Validated claims from a Cognito access token.

    Supports both:
    - Human user tokens (from PKCE/authorization code flow)
    - Agent tokens (from client_credentials flow)

    The Pre Token Generation Lambda (V2) injects custom:* claims into access tokens.
    """

    sub: str  # User ID (subject) - for client_credentials, this is the client_id
    iss: str  # Issuer URL
    client_id: str  # Cognito client ID
    token_use: str  # 'access' or 'id'
    scope: str | None = None  # OAuth2 scopes (e.g., "bedrockgw/invoke")
    auth_time: int = 0  # Not present in client_credentials tokens
    exp: int
    iat: int
    jti: str = ""
    username: str = ""  # Not present in client_credentials tokens

    # Cognito groups (for user tokens)
    cognito_groups: list[str] = []

    # Custom attributes - injected by Pre Token Generation Lambda (Issue #119)
    email: str | None = None
    name: str | None = None
    org_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    role: str | None = None
    account_type: str | None = None  # "human" or "service"
    agent_name: str | None = None  # For agent tokens


class CognitoJWTValidator:
    """
    Validates JWT tokens issued by AWS Cognito.

    Supports both:
    - Human user tokens (from PKCE/authorization code flow)
    - Agent tokens (from client_credentials flow)

    Issue #119: Unified Cognito JWT Auth

    Features:
    - Fetches and caches JWKS (JSON Web Key Set) from Cognito
    - Validates token signature using RS256 algorithm
    - Validates issuer, audience, and expiration claims
    - Extracts user attributes from token claims
    - Accepts tokens from any App Client in the same User Pool
    """

    def __init__(
        self,
        user_pool_id: str | None = None,
        client_id: str | None = None,
        allowed_client_ids: list[str] | None = None,
        region: str | None = None,
    ):
        """
        Initialize the JWT validator.

        Args:
            user_pool_id: Cognito User Pool ID (defaults to config)
            client_id: Primary Cognito Client ID (defaults to config)
            allowed_client_ids: Additional allowed client IDs (e.g., agent clients)
            region: AWS region (defaults to config)
        """
        settings = get_settings()

        self.user_pool_id = user_pool_id or settings.cognito_user_pool_id
        self.client_id = client_id or settings.cognito_client_id
        self.region = region or settings.aws_region

        if not self.user_pool_id:
            raise ValueError("Cognito User Pool ID must be configured")

        # Build list of allowed client IDs
        # Issue #119: Accept tokens from any App Client in the same User Pool
        # When allowed_client_ids is None (default), accept any client_id
        # When allowed_client_ids is an empty list [], accept any client_id
        # When allowed_client_ids has values, only accept those client_ids
        self.allowed_client_ids: set[str] = set()
        if allowed_client_ids is not None:
            self.allowed_client_ids.update(allowed_client_ids)
        # Note: We no longer automatically add settings.cognito_client_id to the allowed list
        # This allows both human (PKCE) and agent (client_credentials) tokens to be accepted

        # Build Cognito URLs
        self.issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"
        self.jwks_url = f"{self.issuer}/.well-known/jwks.json"

        # Initialize JWKS client (with caching)
        self._jwk_client: PyJWKClient | None = None

    @property
    def jwk_client(self) -> PyJWKClient:
        """Lazy initialization of JWKS client."""
        if self._jwk_client is None:
            self._jwk_client = PyJWKClient(
                self.jwks_url,
                cache_keys=True,
                lifespan=3600,  # Cache keys for 1 hour
            )
        return self._jwk_client

    def validate_token(self, token: str) -> CognitoTokenClaims:
        """
        Validate a Cognito JWT token.

        Args:
            token: JWT token string

        Returns:
            CognitoTokenClaims: Validated token claims

        Raises:
            jwt.InvalidTokenError: If token validation fails
        """
        try:
            # Get the signing key from JWKS
            signing_key = self.jwk_client.get_signing_key_from_jwt(token)

            # Decode and validate the token
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": False,  # Cognito access tokens use client_id claim instead
                    "require": ["exp", "iss", "sub", "token_use"],
                },
            )

            # Validate token_use claim (must be 'access' for API auth)
            token_use = payload.get("token_use")
            if token_use not in ["access", "id"]:
                raise jwt.InvalidTokenError(f"Invalid token_use: {token_use}")

            # Validate client_id claim (for access tokens)
            # Issue #119: Accept any client from the user pool if allowed_client_ids is empty
            if token_use == "access":
                token_client_id = payload.get("client_id", "")
                if self.allowed_client_ids and token_client_id not in self.allowed_client_ids:
                    logger.warning(f"Token client_id {token_client_id} not in allowed list")
                    raise jwt.InvalidTokenError("Token client_id does not match any allowed client")

            # Parse and return claims
            return self._parse_claims(payload)

        except PyJWKClientError as e:
            logger.error(f"JWKS client error: {e}")
            raise jwt.InvalidTokenError(f"Failed to fetch signing key: {e}")
        except jwt.ExpiredSignatureError:
            logger.warning("Token has expired")
            raise
        except jwt.InvalidTokenError as e:
            logger.warning(f"Token validation failed: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during token validation: {e}")
            raise jwt.InvalidTokenError(f"Token validation failed: {e}")

    def _parse_claims(self, payload: dict[str, Any]) -> CognitoTokenClaims:
        """Parse JWT payload into CognitoTokenClaims.

        Issue #119: Updated to support custom claims injected by Pre Token Generation Lambda.
        """
        return CognitoTokenClaims(
            sub=payload["sub"],
            iss=payload["iss"],
            client_id=payload.get("client_id", ""),
            token_use=payload["token_use"],
            scope=payload.get("scope"),
            auth_time=payload.get("auth_time", 0),
            exp=payload["exp"],
            iat=payload.get("iat", 0),
            jti=payload.get("jti", ""),
            username=payload.get("username", payload["sub"]),
            cognito_groups=payload.get("cognito:groups", []),
            # Custom attributes - injected by Pre Token Generation Lambda (Issue #119)
            email=payload.get("email"),
            name=payload.get("name"),
            org_id=payload.get("custom:org_id"),
            department_id=payload.get("custom:department_id"),
            team_id=payload.get("custom:team_id"),
            role=payload.get("custom:role"),
            account_type=payload.get("custom:account_type"),
            agent_name=payload.get("custom:agent_name"),
        )

    def decode_without_verification(self, token: str) -> dict[str, Any] | None:
        """
        Decode token without verification (for debugging/logging only).

        Args:
            token: JWT token string

        Returns:
            Token payload or None if decoding fails
        """
        try:
            return jwt.decode(token, options={"verify_signature": False})
        except Exception:
            return None


# Singleton instance
_validator: CognitoJWTValidator | None = None


def get_cognito_validator() -> CognitoJWTValidator:
    """Get or create the singleton Cognito JWT validator."""
    global _validator
    if _validator is None:
        _validator = CognitoJWTValidator()
    return _validator


def validate_cognito_token(token: str) -> CognitoTokenClaims:
    """
    Validate a Cognito JWT token using the singleton validator.

    Args:
        token: JWT token string

    Returns:
        CognitoTokenClaims: Validated token claims

    Raises:
        jwt.InvalidTokenError: If validation fails
    """
    return get_cognito_validator().validate_token(token)

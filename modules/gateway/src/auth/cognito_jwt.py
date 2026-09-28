"""
Cognito JWT Token Validator

This module provides JWT validation for AWS Cognito tokens using JWKS.
It validates the token signature, issuer, audience, and expiration.
"""

import logging
from typing import Any

import boto3
import jwt
from botocore.exceptions import ClientError
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
    - Accepts tokens only from configured App Clients
    """

    def __init__(
        self,
        user_pool_id: str | None = None,
        client_id: str | None = None,
        allowed_client_ids: list[str] | None = None,
        region: str | None = None,
        agent_clients_table: str | None = None,
        dynamodb_resource: Any | None = None,
    ):
        """
        Initialize the JWT validator.

        Args:
            user_pool_id: Cognito User Pool ID (defaults to config)
            client_id: Primary Cognito Client ID (defaults to config)
            allowed_client_ids: Additional allowed client IDs
            region: AWS region (defaults to config)
            agent_clients_table: Dynamic machine-client registry table (defaults to config)
            dynamodb_resource: Optional DynamoDB resource for dependency injection
        """
        settings = get_settings()

        self.user_pool_id = user_pool_id or settings.cognito_user_pool_id
        self.client_id = client_id or settings.cognito_client_id
        self.region = region or settings.aws_region

        if not self.user_pool_id:
            raise ValueError("Cognito User Pool ID must be configured")

        configured_client_ids = [
            self.client_id,
            getattr(settings, "cognito_cli_client_id", ""),
            getattr(settings, "cognito_agent_client_id", ""),
            getattr(settings, "cognito_gitlab_client_id", ""),
            getattr(settings, "cognito_pentest_client_id", ""),
            *(allowed_client_ids or []),
        ]
        self.allowed_client_ids = {configured.strip() for configured in configured_client_ids if isinstance(configured, str) and configured.strip()}
        if not self.allowed_client_ids:
            raise ValueError("At least one Cognito App Client ID must be configured")

        configured_agent_clients_table = agent_clients_table if agent_clients_table is not None else getattr(settings, "agent_clients_table", "")
        self.agent_clients_table = configured_agent_clients_table.strip() if isinstance(configured_agent_clients_table, str) else ""
        self._dynamodb_resource = dynamodb_resource

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

            # Validate token_use claim (must be 'access' or 'id' for API auth)
            token_use = payload.get("token_use")
            if token_use not in ["access", "id"]:
                raise jwt.InvalidTokenError(f"Invalid token_use: {token_use}")

            token_client_id = self._client_from_claims(payload, token_use)
            if not self._client_is_allowed(payload, token_use, token_client_id):
                claim_name = "client_id" if token_use == "access" else "aud"
                logger.warning("Token rejected: %s claim is not an allowed app client (token_use=%s)", claim_name, token_use)
                raise jwt.InvalidTokenError(f"Token {claim_name} does not match any allowed client")

            return self._parse_claims(payload, token_client_id)

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

    def _client_is_allowed(self, payload: dict[str, Any], token_use: str, token_client_id: str) -> bool:
        if token_client_id in self.allowed_client_ids:
            return True

        if token_use != "access" or not self.agent_clients_table:
            return False

        agent = self._get_registered_agent(token_client_id)
        if not agent or agent.get("client_id") != token_client_id or agent.get("status") != "active":
            return False

        if not isinstance(agent.get("org_id"), str) or not agent["org_id"]:
            return False

        if payload.get("custom:account_type") != "service" or payload.get("custom:client_id") != token_client_id:
            return False

        tenant_claims = {
            "custom:org_id": "org_id",
            "custom:team_id": "team_id",
            "custom:department_id": "department_id",
        }
        return all((payload.get(claim_name) or "") == (agent.get(field_name) or "") for claim_name, field_name in tenant_claims.items())

    def _get_registered_agent(self, client_id: str) -> dict[str, Any] | None:
        try:
            if self._dynamodb_resource is None:
                self._dynamodb_resource = boto3.resource("dynamodb", region_name=self.region)
            response = self._dynamodb_resource.Table(self.agent_clients_table).get_item(
                Key={"client_id": client_id},
                ConsistentRead=True,
            )
            item = response.get("Item")
            return item if isinstance(item, dict) else None
        except ClientError as error:
            logger.error("DynamoDB error looking up Cognito machine client: %s", error.response.get("Error", {}).get("Code", "unknown"))
            return None
        except Exception:
            logger.exception("Unexpected error looking up Cognito machine client")
            return None

    @staticmethod
    def _client_from_claims(payload: dict[str, Any], token_use: str) -> str:
        """Return the app client this token was issued for.

        Issue #5653 (A01). Cognito puts it in a different claim per token kind:
        access tokens carry ``client_id``; id tokens carry it as the audience
        (``aud``). Normalising here means one allowlist check covers both instead of
        the access-token-only check that left id tokens unbound to any client.

        ``aud`` is permitted by JWT to be a list. Cognito issues a single-valued
        ``aud`` on id tokens, so that is the only list shape accepted here: a
        one-element list is unwrapped, and any multi-valued ``aud`` is rejected by
        returning a sentinel that cannot appear in an allowlist. Rejecting rather
        than searching the list is deliberate — a token naming several clients is
        not a token for any one of them, and "any entry is allowed" would let a
        disallowed client ride along beside an allowed one.

        """
        if token_use == "access":
            return payload.get("client_id", "")

        aud = payload.get("aud", "")
        if isinstance(aud, str):
            return aud
        if isinstance(aud, list):
            # Single sensible case first; otherwise return a value guaranteed not to
            # be in the allowlist so a mixed audience fails closed.
            if len(aud) == 1 and isinstance(aud[0], str):
                return aud[0]
            return "\x00multi-valued-aud"
        return ""

    def _parse_claims(self, payload: dict[str, Any], validated_client_id: str | None = None) -> CognitoTokenClaims:
        """Parse JWT payload into CognitoTokenClaims.

        Issue #119: Updated to support custom claims injected by Pre Token Generation Lambda.
        """
        return CognitoTokenClaims(
            sub=payload["sub"],
            iss=payload["iss"],
            client_id=validated_client_id or payload.get("client_id", ""),
            token_use=payload["token_use"],
            scope=payload.get("scope"),
            auth_time=payload.get("auth_time", 0),
            exp=payload["exp"],
            iat=payload.get("iat", 0),
            jti=payload.get("jti", ""),
            # Cognito client-credentials access tokens do not carry a username.
            # Preserve that absence so downstream auth can distinguish the M2M
            # token shape instead of fabricating a human-style username from sub.
            username=payload.get("username", ""),
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

        WARNING: Do NOT use the output for authorization decisions.
        The verified auth path is validate_token() which uses JWKS signature verification.

        Args:
            token: JWT token string

        Returns:
            Token payload or None if decoding fails
        """
        try:
            # nosemgrep: unverified-jwt-decode — debug/logging helper only;
            # never used for authz decisions. Verified decode happens in
            # validate_token() (line 145) via PyJWKClient JWKS signature check.
            return jwt.decode(token, options={"verify_signature": False})  # nosemgrep: unverified-jwt-decode
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

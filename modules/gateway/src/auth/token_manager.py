"""
Token Manager for secure JWT token generation, validation, and storage.

This module handles all token-related operations including:
- JWT token generation and validation
- Token hashing for secure storage
- Database token storage and retrieval
- Token expiration and revocation
"""

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import jwt
from jwt import PyJWTError
from passlib.context import CryptContext
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.models.token import Token
from src.shared.schemas.auth import TokenContext

from .exceptions import TokenGenerationError, TokenStorageError, TokenValidationError
from .schemas import TenantInfo, TokenClaims

logger = logging.getLogger(__name__)


class TokenManager:
    """
    Manages JWT token lifecycle including generation, validation, and storage.

    Features:
    - Secure JWT token generation with configurable expiration
    - Token hashing for secure database storage
    - Token validation with comprehensive error handling
    - Token revocation support
    - Automatic token cleanup (expired tokens)
    """

    def __init__(self, secret_key: str):
        """
        Initialize the token manager.

        Args:
            secret_key: Secret key for JWT signing. Must be provided via BG_TOKEN_SECRET_KEY environment variable.

        Raises:
            ValueError: If secret_key is not provided or is empty.
        """
        if not secret_key:
            raise ValueError("Secret key must be provided via BG_TOKEN_SECRET_KEY environment variable")

        self.settings = get_settings()
        self.secret_key = secret_key
        self.algorithm = "HS256"

        # Initialize password hashing context for token hashing
        self.pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def generate_token(self, tenant_info: TenantInfo, duration_hours: int | None = None) -> tuple[str, datetime]:
        """
        Generate a new JWT token for the given tenant information.

        Args:
            tenant_info: Tenant information including org, team, department, and entity details
            duration_hours: Token duration in hours (defaults to config setting)

        Returns:
            tuple[str, datetime]: Generated token and expiration datetime

        Raises:
            TokenGenerationError: If token generation fails
        """
        try:
            # Calculate expiration time
            duration = duration_hours or self.settings.api_key_duration_hours
            expires_at = datetime.now(UTC) + timedelta(hours=duration)

            # Create JWT claims
            now = datetime.now(UTC)
            token_claims = TokenClaims(
                sub=tenant_info.entity_id,
                org_id=tenant_info.org_id,
                team_id=tenant_info.team_id,
                department_id=tenant_info.department_id,
                account_type=tenant_info.account_type,
                is_admin=tenant_info.is_admin,
                exp=int(expires_at.timestamp()),
                iat=int(now.timestamp()),
                jti=self._generate_token_id(),  # Unique token ID for tracking
            )

            # Generate JWT token
            token = jwt.encode(token_claims.model_dump(), self.secret_key, algorithm=self.algorithm)

            logger.debug(f"Generated token for entity {tenant_info.entity_id} (type: {tenant_info.account_type})")
            return token, expires_at

        except Exception as e:
            logger.error(f"Failed to generate token: {e}")
            raise TokenGenerationError(f"Token generation failed: {str(e)}")

    async def store_token(self, token: str, tenant_info: TenantInfo, expires_at: datetime, db: AsyncSession) -> str:
        """
        Store token hash in the database for tracking and revocation.

        Args:
            token: Generated JWT token
            tenant_info: Tenant information
            expires_at: Token expiration datetime
            db: Database session

        Returns:
            str: Token ID for reference

        Raises:
            TokenStorageError: If token storage fails
        """
        try:
            # Hash the token for secure storage
            token_hash = self.hash_token(token)

            # Create token record
            db_token = Token(
                token_hash=token_hash,
                entity_type=tenant_info.account_type,
                entity_id=tenant_info.entity_id,
                org_id=tenant_info.org_id,
                team_id=tenant_info.team_id,
                department_id=tenant_info.department_id,
                is_admin=tenant_info.is_admin,
                expires_at=expires_at,
            )

            db.add(db_token)
            await db.commit()
            await db.refresh(db_token)

            logger.debug(f"Stored token {db_token.id} for entity {tenant_info.entity_id}")
            return db_token.id

        except Exception as e:
            logger.error(f"Failed to store token: {e}")
            await db.rollback()
            raise TokenStorageError(f"Token storage failed: {str(e)}")

    async def validate_token(self, token: str, db: AsyncSession) -> TokenContext:
        """
        Validate JWT token and return context information.

        Args:
            token: JWT token to validate
            db: Database session

        Returns:
            TokenContext: Validated token context

        Raises:
            TokenValidationError: If token validation fails
        """
        try:
            # Decode and validate JWT
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])

            # Parse token claims
            token_claims = TokenClaims(**payload)

            # Check if token exists in database and is not revoked
            token_hash = self.hash_token(token)
            result = await db.execute(
                select(Token).where(Token.token_hash == token_hash, Token.revoked_at.is_(None), Token.expires_at > datetime.now(UTC))
            )
            db_token = result.scalar_one_or_none()

            if not db_token:
                logger.warning("Token not found in database or is revoked/expired")
                raise TokenValidationError("Token is invalid, revoked, or expired")

            # Create and return token context
            return TokenContext(
                user_id=token_claims.sub,
                org_id=token_claims.org_id,
                team_id=token_claims.team_id,
                department_id=token_claims.department_id,
                account_type=token_claims.account_type,
                is_admin=token_claims.is_admin,
                expires_at=datetime.fromtimestamp(token_claims.exp, UTC),
            )

        except jwt.ExpiredSignatureError:
            logger.warning("Token has expired")
            raise TokenValidationError("Token has expired")

        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise TokenValidationError(f"Invalid token: {str(e)}")

        except PyJWTError as e:
            logger.warning(f"JWT validation error: {e}")
            raise TokenValidationError(f"Token validation failed: {str(e)}")

        except Exception as e:
            logger.error(f"Unexpected error during token validation: {e}")
            raise TokenValidationError(f"Token validation failed: {str(e)}")

    async def revoke_token(self, token: str, db: AsyncSession) -> bool:
        """
        Revoke a token by marking it as revoked in the database.

        Args:
            token: JWT token to revoke
            db: Database session

        Returns:
            bool: True if token was successfully revoked

        Raises:
            TokenStorageError: If token revocation fails
        """
        try:
            token_hash = self.hash_token(token)
            now = datetime.now(UTC)

            result = await db.execute(update(Token).where(Token.token_hash == token_hash, Token.revoked_at.is_(None)).values(revoked_at=now))

            await db.commit()

            if result.rowcount > 0:
                logger.debug("Token revoked successfully")
                return True
            else:
                logger.warning("Token not found or already revoked")
                return False

        except Exception as e:
            logger.error(f"Failed to revoke token: {e}")
            await db.rollback()
            raise TokenStorageError(f"Token revocation failed: {str(e)}")

    async def revoke_all_user_tokens(self, entity_id: str, org_id: str, db: AsyncSession) -> int:
        """
        Revoke all tokens for a specific user/service account.

        Args:
            entity_id: User or service account ID
            org_id: Organization ID
            db: Database session

        Returns:
            int: Number of tokens revoked

        Raises:
            TokenStorageError: If batch revocation fails
        """
        try:
            now = datetime.now(UTC)

            result = await db.execute(
                update(Token).where(Token.entity_id == entity_id, Token.org_id == org_id, Token.revoked_at.is_(None)).values(revoked_at=now)
            )

            await db.commit()

            logger.debug(f"Revoked {result.rowcount} tokens for entity {entity_id}")
            return result.rowcount

        except Exception as e:
            logger.error(f"Failed to revoke user tokens: {e}")
            await db.rollback()
            raise TokenStorageError(f"Batch token revocation failed: {str(e)}")

    async def cleanup_expired_tokens(self, db: AsyncSession) -> int:
        """
        Clean up expired tokens from the database.

        Args:
            db: Database session

        Returns:
            int: Number of tokens cleaned up
        """
        try:
            # Delete tokens that expired more than 24 hours ago
            cutoff_time = datetime.now(UTC) - timedelta(hours=24)

            result = await db.execute(select(Token).where(Token.expires_at < cutoff_time))
            expired_tokens = result.scalars().all()

            if expired_tokens:
                for token in expired_tokens:
                    await db.delete(token)

                await db.commit()
                logger.info(f"Cleaned up {len(expired_tokens)} expired tokens")
                return len(expired_tokens)

            return 0

        except Exception as e:
            logger.error(f"Failed to cleanup expired tokens: {e}")
            await db.rollback()
            raise TokenStorageError(f"Token cleanup failed: {str(e)}")

    def hash_token(self, token: str) -> str:
        """
        Generate a secure hash of the token for database storage.

        Args:
            token: JWT token to hash

        Returns:
            str: SHA-256 hash of the token
        """
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _generate_token_id(self) -> str:
        """
        Generate a unique token ID for JWT claims.

        Returns:
            str: Unique token ID
        """
        import uuid

        return str(uuid.uuid4())

    def extract_claims_without_verification(self, token: str) -> dict | None:
        """
        Extract claims from JWT token without signature verification.
        Useful for debugging and logging purposes only.

        WARNING: Do NOT use the output for authorization decisions.
        The verified auth path is validate_token() which checks HMAC signature.

        Args:
            token: JWT token

        Returns:
            Optional[dict]: Token claims or None if parsing fails
        """
        try:
            # nosemgrep: unverified-jwt-decode — debug/logging helper only;
            # never used for authz decisions. Verified decode happens in
            # validate_token() (line 166) with self.secret_key signature check.
            return jwt.decode(token, options={"verify_signature": False})
        except Exception:
            return None

"""
Unit tests for the Token Manager module.

These tests cover JWT token generation, validation, storage, and revocation
with comprehensive error handling and edge cases.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest

from src.auth.exceptions import TokenGenerationError, TokenStorageError, TokenValidationError
from src.auth.schemas import TenantInfo, TokenClaims
from src.auth.token_manager import TokenManager
from src.shared.schemas.auth import TokenContext

from .conftest import create_sample_token


@pytest.mark.unit
class TestTokenManager:
    """Test suite for TokenManager."""

    def test_init(self):
        """Test TokenManager initialization."""
        token_manager = TokenManager("test-secret")

        assert token_manager.secret_key == "test-secret"
        assert token_manager.algorithm == "HS256"
        assert token_manager.pwd_context is not None

    def test_generate_token(self, sample_tenant_info: TenantInfo):
        """Test successful token generation."""
        token_manager = TokenManager("test-secret")

        token, expires_at = token_manager.generate_token(sample_tenant_info)

        assert isinstance(token, str)
        assert isinstance(expires_at, datetime)
        assert expires_at > datetime.now(UTC)

        # Verify token can be decoded
        payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
        assert payload["sub"] == sample_tenant_info.entity_id
        assert payload["org_id"] == sample_tenant_info.org_id

    def test_generate_token_with_custom_duration(self, sample_tenant_info: TenantInfo):
        """Test token generation with custom duration."""
        token_manager = TokenManager("test-secret")

        token, expires_at = token_manager.generate_token(sample_tenant_info, duration_hours=2)

        # Check that the expiration is approximately 2 hours from now
        expected_expiry = datetime.now(UTC) + timedelta(hours=2)
        assert abs((expires_at - expected_expiry).total_seconds()) < 60  # Within 1 minute

    def test_generate_token_failure(self, sample_tenant_info: TenantInfo):
        """Test token generation failure."""
        # Create a token manager with invalid settings that would cause JWT encoding to fail
        with patch("jwt.encode", side_effect=Exception("JWT encoding failed")):
            token_manager = TokenManager("test-secret")

            with pytest.raises(TokenGenerationError) as exc_info:
                token_manager.generate_token(sample_tenant_info)

            assert "Token generation failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_store_token_success(self, db_session, sample_tenant_info: TenantInfo):
        """Test successful token storage."""
        token_manager = TokenManager("test-secret")
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        token_id = await token_manager.store_token("test-token", sample_tenant_info, expires_at, db_session)

        assert isinstance(token_id, str)
        assert len(token_id) > 0

    @pytest.mark.asyncio
    async def test_store_token_failure(self, db_session, sample_tenant_info: TenantInfo):
        """Test token storage failure."""
        token_manager = TokenManager("test-secret")
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        # Simulate database error
        with patch.object(db_session, "commit", side_effect=Exception("DB error")):
            with pytest.raises(TokenStorageError) as exc_info:
                await token_manager.store_token("test-token", sample_tenant_info, expires_at, db_session)

            assert "Token storage failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_token_success(self, db_session, sample_tenant_info: TenantInfo):
        """Test successful token validation."""
        token_manager = TokenManager("test-secret")

        # Generate a token
        token, expires_at = token_manager.generate_token(sample_tenant_info)

        # Store the token in database
        await create_sample_token(db_session, sample_tenant_info, token_hash=token_manager.hash_token(token), expires_at=expires_at)

        # Validate the token
        context = await token_manager.validate_token(token, db_session)

        assert isinstance(context, TokenContext)
        assert context.user_id == sample_tenant_info.entity_id
        assert context.org_id == sample_tenant_info.org_id
        assert context.account_type == sample_tenant_info.account_type

    @pytest.mark.asyncio
    async def test_validate_token_expired_jwt(self, db_session, sample_tenant_info: TenantInfo):
        """Test validation of expired JWT token."""
        token_manager = TokenManager("test-secret")

        # Create an expired token
        past_time = datetime.now(UTC) - timedelta(hours=1)
        token_claims = TokenClaims(
            sub=sample_tenant_info.entity_id,
            org_id=sample_tenant_info.org_id,
            team_id=sample_tenant_info.team_id,
            department_id=sample_tenant_info.department_id,
            account_type=sample_tenant_info.account_type,
            is_admin=sample_tenant_info.is_admin,
            exp=int(past_time.timestamp()),
            iat=int(datetime.now(UTC).timestamp()),
            jti="expired-token",
        )

        expired_token = jwt.encode(token_claims.model_dump(), "test-secret", algorithm="HS256")

        with pytest.raises(TokenValidationError) as exc_info:
            await token_manager.validate_token(expired_token, db_session)

        assert "Token has expired" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_token_invalid_signature(self, db_session):
        """Test validation of token with invalid signature."""
        token_manager = TokenManager("test-secret")

        # Create token with different secret
        future_time = datetime.now(UTC) + timedelta(hours=1)
        claims = {"sub": "user-123", "exp": int(future_time.timestamp()), "iat": int(datetime.now(UTC).timestamp())}

        invalid_token = jwt.encode(claims, "wrong-secret", algorithm="HS256")

        with pytest.raises(TokenValidationError) as exc_info:
            await token_manager.validate_token(invalid_token, db_session)

        assert "Invalid token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_token_not_in_database(self, db_session, sample_tenant_info: TenantInfo):
        """Test validation of token not stored in database."""
        token_manager = TokenManager("test-secret")

        # Generate a valid JWT but don't store it in database
        token, _ = token_manager.generate_token(sample_tenant_info)

        with pytest.raises(TokenValidationError) as exc_info:
            await token_manager.validate_token(token, db_session)

        assert "Token is invalid, revoked, or expired" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_revoked_token(self, db_session, sample_tenant_info: TenantInfo):
        """Test validation of revoked token."""
        token_manager = TokenManager("test-secret")

        # Generate and store a token
        token, expires_at = token_manager.generate_token(sample_tenant_info)

        revoked_time = datetime.now(UTC)
        await create_sample_token(
            db_session, sample_tenant_info, token_hash=token_manager.hash_token(token), expires_at=expires_at, revoked_at=revoked_time
        )

        with pytest.raises(TokenValidationError) as exc_info:
            await token_manager.validate_token(token, db_session)

        assert "Token is invalid, revoked, or expired" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_revoke_token_success(self, db_session, sample_tenant_info: TenantInfo):
        """Test successful token revocation."""
        token_manager = TokenManager("test-secret")

        # Generate and store a token
        token, expires_at = token_manager.generate_token(sample_tenant_info)
        await create_sample_token(db_session, sample_tenant_info, token_hash=token_manager.hash_token(token), expires_at=expires_at)

        # Revoke the token
        result = await token_manager.revoke_token(token, db_session)

        assert result is True

    @pytest.mark.asyncio
    async def test_revoke_token_not_found(self, db_session):
        """Test revoking a token that doesn't exist."""
        token_manager = TokenManager("test-secret")

        result = await token_manager.revoke_token("non-existent-token", db_session)

        assert result is False

    @pytest.mark.asyncio
    async def test_revoke_token_failure(self, db_session, sample_tenant_info: TenantInfo):
        """Test token revocation failure due to database error."""
        token_manager = TokenManager("test-secret")

        with patch.object(db_session, "commit", side_effect=Exception("DB error")):
            with pytest.raises(TokenStorageError) as exc_info:
                await token_manager.revoke_token("test-token", db_session)

            assert "Token revocation failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_revoke_all_user_tokens(self, db_session, sample_tenant_info: TenantInfo):
        """Test revoking all tokens for a user."""
        token_manager = TokenManager("test-secret")

        # Create multiple tokens for the same user
        for i in range(3):
            token, expires_at = token_manager.generate_token(sample_tenant_info)
            await create_sample_token(db_session, sample_tenant_info, token_hash=f"token-hash-{i}", expires_at=expires_at)

        # Revoke all tokens for the user
        count = await token_manager.revoke_all_user_tokens(sample_tenant_info.entity_id, sample_tenant_info.org_id, db_session)

        assert count == 3

    @pytest.mark.asyncio
    async def test_cleanup_expired_tokens(self, db_session, sample_tenant_info: TenantInfo):
        """Test cleaning up expired tokens."""
        token_manager = TokenManager("test-secret")

        # Create an old expired token (more than 24 hours ago)
        old_expiry = datetime.now(UTC) - timedelta(hours=25)
        await create_sample_token(db_session, sample_tenant_info, token_hash="old-expired-token", expires_at=old_expiry)

        # Create a recently expired token (less than 24 hours ago)
        recent_expiry = datetime.now(UTC) - timedelta(hours=1)
        await create_sample_token(db_session, sample_tenant_info, token_hash="recent-expired-token", expires_at=recent_expiry)

        # Clean up expired tokens
        count = await token_manager.cleanup_expired_tokens(db_session)

        assert count == 1  # Only the old expired token should be cleaned up

    def test_hash_token(self):
        """Test token hashing."""
        token_manager = TokenManager("test-secret")

        token = "test-token"
        hash1 = token_manager.hash_token(token)
        hash2 = token_manager.hash_token(token)

        assert hash1 == hash2  # Same input should produce same hash
        assert len(hash1) == 64  # SHA-256 produces 64-character hex string
        assert hash1 != token  # Hash should be different from original token

    def test_generate_token_id(self):
        """Test token ID generation."""
        token_manager = TokenManager("test-secret")

        token_id1 = token_manager._generate_token_id()
        token_id2 = token_manager._generate_token_id()

        assert token_id1 != token_id2  # Should be unique
        assert len(token_id1) == 36  # UUID4 string length

    def test_extract_claims_without_verification(self):
        """Test extracting claims without verification."""
        token_manager = TokenManager("test-secret")

        # Create a valid token
        claims = {"sub": "user-123", "org_id": "org-456", "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())}
        token = jwt.encode(claims, "test-secret", algorithm="HS256")

        extracted = token_manager.extract_claims_without_verification(token)

        assert extracted is not None
        assert extracted["sub"] == "user-123"
        assert extracted["org_id"] == "org-456"

    def test_extract_claims_without_verification_invalid_token(self):
        """Test extracting claims from invalid token."""
        token_manager = TokenManager("test-secret")

        result = token_manager.extract_claims_without_verification("invalid-token")

        assert result is None


# =============================================================================
# Issue #1147: Regression test — tampered signature rejected
# =============================================================================


@pytest.mark.unit
class TestTamperedSignatureRejection:
    """Regression tests ensuring tampered-signature JWTs are rejected.

    Issue #1147: Confirms that the verified decode path (validate_token)
    rejects tokens with invalid signatures, proving that the unverified
    decode helper (extract_claims_without_verification) is not on the auth path.
    """

    @pytest.mark.asyncio
    async def test_tampered_signature_rejected_by_validate_token(self, db_session):
        """Test that validate_token rejects a JWT signed with the wrong key.

        This is the critical security assertion: a forged token with a valid
        structure but wrong HMAC signature must be rejected before any claims
        are trusted for authorization decisions.
        """
        token_manager = TokenManager("correct-secret")

        # Create a token signed with a different secret (simulates attacker forgery)
        forged_claims = {
            "sub": "attacker-controlled-sub",
            "org_id": "attacker-org",
            "team_id": "attacker-team",
            "department_id": "attacker-dept",
            "account_type": "human",
            "is_admin": True,
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "iat": int(datetime.now(UTC).timestamp()),
            "jti": "forged-token-id",
        }
        forged_token = jwt.encode(forged_claims, "wrong-secret", algorithm="HS256")

        # validate_token must reject this — signature verification will fail
        with pytest.raises(TokenValidationError) as exc_info:
            await token_manager.validate_token(forged_token, db_session)

        assert "Invalid token" in str(exc_info.value)

    def test_unverified_extract_parses_forged_token(self):
        """Verify that extract_claims_without_verification DOES parse forged tokens.

        This proves the unverified helper is NOT a security gate — it will
        happily return claims from any well-formed JWT regardless of signature.
        The security boundary is validate_token(), not this helper.
        """
        token_manager = TokenManager("correct-secret")

        forged_claims = {
            "sub": "attacker-controlled-sub",
            "org_id": "attacker-org",
            "is_admin": True,
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        }
        forged_token = jwt.encode(forged_claims, "wrong-secret", algorithm="HS256")

        # The unverified helper returns claims (expected and safe because
        # its output is never used for authorization)
        result = token_manager.extract_claims_without_verification(forged_token)
        assert result is not None
        assert result["sub"] == "attacker-controlled-sub"
        assert result["is_admin"] is True

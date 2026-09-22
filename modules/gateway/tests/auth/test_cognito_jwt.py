"""
Tests for Cognito JWT validation.

Tests the CognitoJWTValidator class that validates JWT tokens issued by AWS Cognito.
"""

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from botocore.exceptions import ClientError

from src.admin.agent_schemas import AgentCreateRequest
from src.admin.agent_service import AgentService
from src.auth.cognito_jwt import (
    CognitoJWTValidator,
    CognitoTokenClaims,
    get_cognito_validator,
    validate_cognito_token,
)


@pytest.fixture
def mock_settings():
    """Mock settings with Cognito configuration."""
    with patch("src.auth.cognito_jwt.get_settings") as mock:
        settings = MagicMock()
        settings.cognito_user_pool_id = "us-east-1_testpool"
        settings.cognito_client_id = "test-client-id"
        settings.cognito_cli_client_id = ""
        settings.cognito_agent_client_id = ""
        settings.cognito_gitlab_client_id = ""
        settings.cognito_pentest_client_id = ""
        settings.agent_clients_table = ""
        settings.aws_region = "us-east-1"
        mock.return_value = settings
        yield settings


@pytest.fixture
def validator(mock_settings):
    """Create a CognitoJWTValidator instance."""
    return CognitoJWTValidator(
        user_pool_id="us-east-1_testpool",
        client_id="test-client-id",
        region="us-east-1",
    )


class TestCognitoJWTValidator:
    """Tests for CognitoJWTValidator."""

    def test_init_builds_correct_urls(self, validator):
        """Test that initialization builds correct Cognito URLs."""
        assert validator.issuer == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool"
        assert validator.jwks_url == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool/.well-known/jwks.json"

    def test_init_raises_without_user_pool_id(self, mock_settings):
        """Test that initialization fails without user pool ID."""
        mock_settings.cognito_user_pool_id = ""
        with pytest.raises(ValueError, match="User Pool ID must be configured"):
            CognitoJWTValidator(user_pool_id="", client_id="test", region="us-east-1")

    def test_init_rejects_an_empty_runtime_client_policy(self, mock_settings):
        mock_settings.cognito_client_id = ""
        with pytest.raises(ValueError, match="App Client ID"):
            CognitoJWTValidator(user_pool_id="us-east-1_test", client_id="", region="us-east-1")


class TestCognitoTokenClaims:
    """Tests for CognitoTokenClaims model."""

    def test_creates_claims_from_access_token_payload(self):
        """Test parsing access token claims."""
        payload = {
            "sub": "user-123",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            "client_id": "test-client-id",
            "token_use": "access",
            "scope": "openid email profile",
            "auth_time": 1234567890,
            "exp": 1234571490,
            "iat": 1234567890,
            "jti": "token-id-123",
            "username": "testuser",
            "cognito:groups": ["admins", "users"],
        }

        claims = CognitoTokenClaims(**payload, cognito_groups=payload.get("cognito:groups", []))

        assert claims.sub == "user-123"
        assert claims.token_use == "access"
        assert claims.username == "testuser"
        assert claims.cognito_groups == ["admins", "users"]

    def test_creates_claims_with_custom_attributes(self):
        """Test parsing ID token claims with custom attributes."""
        payload = {
            "sub": "user-123",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            "client_id": "test-client-id",
            "token_use": "id",
            "auth_time": 1234567890,
            "exp": 1234571490,
            "iat": 1234567890,
            "jti": "token-id-123",
            "username": "testuser",
            "email": "test@example.com",
            "name": "Test User",
            "custom:org_id": "org-456",
            "custom:department_id": "dept-789",
            "custom:team_id": "team-001",
            "custom:role": "platform_admin",
        }

        claims = CognitoTokenClaims(
            sub=payload["sub"],
            iss=payload["iss"],
            client_id=payload["client_id"],
            token_use=payload["token_use"],
            auth_time=payload["auth_time"],
            exp=payload["exp"],
            iat=payload["iat"],
            jti=payload["jti"],
            username=payload["username"],
            email=payload.get("email"),
            name=payload.get("name"),
            org_id=payload.get("custom:org_id"),
            department_id=payload.get("custom:department_id"),
            team_id=payload.get("custom:team_id"),
            role=payload.get("custom:role"),
        )

        assert claims.email == "test@example.com"
        assert claims.name == "Test User"
        assert claims.org_id == "org-456"
        assert claims.department_id == "dept-789"
        assert claims.team_id == "team-001"
        assert claims.role == "platform_admin"


class TestDecodeWithoutVerification:
    """Tests for decode_without_verification method."""

    def test_decodes_valid_token(self, validator):
        """Test decoding a token without verification."""
        payload = {
            "sub": "user-123",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, "secret", algorithm="HS256")

        result = validator.decode_without_verification(token)

        assert result is not None
        assert result["sub"] == "user-123"

    def test_returns_none_for_invalid_token(self, validator):
        """Test that invalid token returns None."""
        result = validator.decode_without_verification("invalid-token")
        assert result is None


class TestGetCognitoValidator:
    """Tests for singleton validator getter."""

    def test_returns_singleton_instance(self, mock_settings):
        """Test that get_cognito_validator returns singleton."""
        # Reset the singleton
        import src.auth.cognito_jwt as cognito_module

        cognito_module._validator = None

        # Mock the validator creation to avoid JWKS client issues
        with patch.object(CognitoJWTValidator, "__init__", return_value=None):
            validator1 = get_cognito_validator()
            validator2 = get_cognito_validator()

            assert validator1 is validator2

        # Reset for other tests
        cognito_module._validator = None


class TestValidateCognitoToken:
    """Tests for the convenience validate_cognito_token function."""

    def test_validates_using_singleton(self, mock_settings):
        """Test that validate_cognito_token uses singleton validator."""
        import src.auth.cognito_jwt as cognito_module

        cognito_module._validator = None

        # This will fail because we can't actually validate tokens without JWKS
        # But we're testing that the function uses the singleton
        with patch.object(CognitoJWTValidator, "validate_token") as mock_validate:
            mock_validate.return_value = CognitoTokenClaims(
                sub="user-123",
                iss="https://test",
                client_id="test",
                token_use="access",
                auth_time=0,
                exp=0,
                iat=0,
                jti="test",
                username="test",
            )

            with patch.object(CognitoJWTValidator, "__init__", return_value=None):
                result = validate_cognito_token("test-token")

            assert result.sub == "user-123"

        # Reset for other tests
        cognito_module._validator = None


# =============================================================================
# Issue #119: Additional tests for unified Cognito JWT auth
# =============================================================================


class TestCognitoJWTValidatorMultipleClients:
    """Tests for supporting multiple client IDs (Issue #119)."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings with Cognito configuration."""
        with patch("src.auth.cognito_jwt.get_settings") as mock:
            settings = MagicMock()
            settings.cognito_user_pool_id = "us-east-1_testpool"
            settings.cognito_client_id = "main-client-id"
            settings.cognito_cli_client_id = ""
            settings.cognito_agent_client_id = ""
            settings.cognito_gitlab_client_id = ""
            settings.cognito_pentest_client_id = ""
            settings.aws_region = "us-east-1"
            mock.return_value = settings
            yield settings

    def test_init_with_allowed_client_ids(self, mock_settings):
        """Test initialization with primary and additional client IDs."""
        validator = CognitoJWTValidator(
            user_pool_id="us-east-1_testpool",
            client_id="main-client-id",
            allowed_client_ids=["agent-client-1", "agent-client-2"],
            region="us-east-1",
        )

        assert "main-client-id" in validator.allowed_client_ids
        assert "agent-client-1" in validator.allowed_client_ids
        assert "agent-client-2" in validator.allowed_client_ids
        assert len(validator.allowed_client_ids) == 3

    def test_init_without_primary_client_id(self, mock_settings):
        """Test initialization without any client policy fails closed."""
        mock_settings.cognito_client_id = ""

        with pytest.raises(ValueError, match="App Client ID"):
            CognitoJWTValidator(user_pool_id="us-east-1_testpool", client_id="", region="us-east-1")

    def test_default_enforces_configured_clients(self, mock_settings):
        mock_settings.cognito_client_id = "web-client-id"
        mock_settings.cognito_cli_client_id = "cli-client-id"
        mock_settings.cognito_agent_client_id = "agent-client-id"
        mock_settings.cognito_gitlab_client_id = "gitlab-client-id"
        mock_settings.cognito_pentest_client_id = "dev-pentest-client-id"

        validator = CognitoJWTValidator(
            user_pool_id="us-east-1_testpool",
            region="us-east-1",
        )

        assert validator.allowed_client_ids == {
            "web-client-id",
            "cli-client-id",
            "agent-client-id",
            "gitlab-client-id",
            "dev-pentest-client-id",
        }


class TestCognitoTokenClaimsServiceAccount:
    """Tests for service account (client_credentials) claims (Issue #119)."""

    def test_service_account_claims(self):
        """Test parsing claims from client_credentials token."""
        claims = CognitoTokenClaims(
            sub="agent-client-123",  # For client_credentials, sub is client_id
            iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            client_id="agent-client-123",
            token_use="access",
            scope="bedrockgw/invoke",
            exp=1234571490,
            iat=1234567890,
            # username is empty for client_credentials
            username="",
            # Custom claims injected by Pre Token Generation Lambda
            org_id="org-456",
            team_id="team-001",
            account_type="service",
            agent_name="my-data-pipeline",
        )

        assert claims.sub == "agent-client-123"
        assert claims.account_type == "service"
        assert claims.agent_name == "my-data-pipeline"
        assert claims.scope == "bedrockgw/invoke"
        assert claims.username == ""  # Empty for client_credentials

    def test_human_user_claims_with_account_type(self):
        """Test parsing claims for human user with account_type."""
        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool",
            client_id="web-client-id",
            token_use="access",
            auth_time=1234567890,
            exp=1234571490,
            iat=1234567890,
            jti="token-id-123",
            username="john@example.com",
            email="john@example.com",
            org_id="org-456",
            team_id="team-001",
            account_type="human",
            role="admin",
        )

        assert claims.account_type == "human"
        assert claims.username == "john@example.com"
        assert claims.role == "admin"


class TestMiddlewareHelperFunctions:
    """Tests for middleware helper functions (Issue #119)."""

    def test_cognito_claims_to_context_human(self):
        """Test converting Cognito claims to TokenContext for human user."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="john@example.com",
            org_id="org-456",
            team_id="team-001",
            department_id="dept-789",
            account_type="human",
            role="admin",
            cognito_groups=["users"],
        )

        context = _cognito_claims_to_context(claims)

        assert context.user_id == "user-123"
        assert context.org_id == "org-456"
        assert context.team_id == "team-001"
        assert context.department_id == "dept-789"
        assert context.account_type == "human"
        assert context.is_admin is True  # role == "admin"

    def test_cognito_claims_to_context_service(self):
        """Test converting Cognito claims to TokenContext for service account."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="agent-client-123",
            iss="https://test",
            client_id="agent-client-123",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="",  # Empty for client_credentials
            org_id="org-456",
            team_id="team-001",
            account_type="service",
        )

        context = _cognito_claims_to_context(claims)

        # For service accounts without username, user_id should be client_id
        assert context.user_id == "agent-client-123"
        assert context.account_type == "service"
        assert context.is_admin is False

    def test_cognito_claims_to_context_platform_admin(self):
        """Test that platform_admin role grants admin privileges."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="admin@example.com",
            org_id="platform",
            role="platform_admin",
            cognito_groups=[],
        )

        context = _cognito_claims_to_context(claims)

        assert context.is_admin is True

    def test_cognito_claims_to_context_admins_group(self):
        """Test that admins group grants admin privileges."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="user@example.com",
            org_id="org-456",
            cognito_groups=["users", "admins"],
        )

        context = _cognito_claims_to_context(claims)

        assert context.is_admin is True

    def test_cognito_claims_to_context_platform_admins_group(self):
        """platform-admins group grants admin — unified across all three copies.

        The is_admin predicate must be identical in auth/dependencies.py,
        auth/middleware.py and auth/auth_service.py: platform_admin/admin role
        or the "admins"/"platform-admins" group, and NEVER the org-scoped
        org_admin role.
        """
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="user@example.com",
            org_id="org-456",
            cognito_groups=["users", "platform-admins"],
        )

        assert _cognito_claims_to_context(claims).is_admin is True

    def test_cognito_claims_to_context_org_admin_is_not_admin(self):
        """org_admin is org-scoped and must NOT be treated as platform admin."""
        from src.auth.middleware import _cognito_claims_to_context

        claims = CognitoTokenClaims(
            sub="user-123",
            iss="https://test",
            client_id="web-client",
            token_use="access",
            exp=1234571490,
            iat=1234567890,
            username="user@example.com",
            org_id="org-456",
            role="org_admin",
            cognito_groups=["users"],
        )

        assert _cognito_claims_to_context(claims).is_admin is False


# =============================================================================
# Issue #1147: Regression test — tampered signature rejected
# =============================================================================


class TestTamperedSignatureRejection:
    """Regression tests ensuring tampered-signature JWTs are rejected.

    Issue #1147: Confirms that the verified decode path (validate_token)
    rejects tokens with invalid signatures, proving that the unverified
    decode helper (decode_without_verification) is not on the auth path.
    """

    def test_tampered_signature_rejected_by_validate_token(self, validator):
        """Test that validate_token rejects a JWT with a tampered signature.

        This is the critical security assertion: even if decode_without_verification
        would happily parse the token, validate_token must reject it because the
        signature doesn't match the JWKS key.
        """
        # Create a token signed with an arbitrary key (simulates attacker forgery)
        forged_payload = {
            "sub": "attacker-controlled-sub",
            "iss": validator.issuer,
            "client_id": "forged-client",
            "token_use": "access",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
        forged_token = jwt.encode(forged_payload, "attacker-secret", algorithm="HS256")

        # validate_token must reject this — the JWKS lookup will fail to find
        # a matching key, or signature verification will fail
        with pytest.raises(jwt.InvalidTokenError):
            validator.validate_token(forged_token)

    def test_unverified_decode_parses_forged_token(self, validator):
        """Verify that decode_without_verification DOES parse forged tokens.

        This proves the unverified helper is NOT a security gate — it will
        happily return claims from any well-formed JWT regardless of signature.
        The security boundary is validate_token(), not this helper.
        """
        forged_payload = {
            "sub": "attacker-controlled-sub",
            "iss": validator.issuer,
            "exp": int(time.time()) + 3600,
        }
        forged_token = jwt.encode(forged_payload, "attacker-secret", algorithm="HS256")

        # The unverified helper returns claims (this is expected and safe
        # because its output is never used for authorization)
        result = validator.decode_without_verification(forged_token)
        assert result is not None
        assert result["sub"] == "attacker-controlled-sub"


class TestIdTokenAudienceBinding:
    """An id token must be bound to an allowed app client — #5653 (A01).

    The two Cognito token kinds carry the issuing app client in different claims:

        access token -> ``client_id``
        id token     -> ``aud``

    ``validate_token`` sets ``verify_aud: False`` because Cognito access tokens carry
    no ``aud`` at all, so PyJWT's built-in audience check cannot be used for them.
    But that option is global, so it also switched off the audience check for ID
    tokens — and nothing replaced it. Access tokens were bound to an allowed client;
    id tokens were bound to nothing but the user pool.

    The practical consequence on a deployment that configures an allowlist: an id
    token minted by the SAME user pool for a DIFFERENT application was accepted,
    while the equivalent access token from that same application was correctly
    rejected. A user pool is frequently shared across applications with different
    privilege levels, so "any app in the pool" is not the same trust statement as
    "this app".

    These tests sign locally and inject the key, so they exercise the real claim
    logic rather than a mocked decode.
    """

    ALLOWED = "spa-client-id"
    CLI_CLIENT = "cli-client-id"
    MACHINE_CLIENT = "machine-client-id"
    GITLAB_CLIENT = "gitlab-client-id"
    PENTEST_CLIENT = "dev-pentest-client-id"
    OTHER_APP = "some-other-app-in-the-same-pool"

    @pytest.fixture(scope="class")
    def rsa_keypair(self):
        """A throwaway RS256 keypair generated per test class.

        Real RSA rather than a shared HMAC string because ``validate_token`` pins
        ``algorithms=["RS256"]``. Signing with HS256 makes every token fail on the
        algorithm before any claim is examined, which would make these tests pass or
        fail for reasons unrelated to the audience logic they exist to check.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return private_pem, key.public_key()

    @pytest.fixture
    def restricted(self, mock_settings):
        """A validator that restricts which app clients are acceptable."""
        return CognitoJWTValidator(
            user_pool_id="us-east-1_testpool",
            client_id=self.ALLOWED,
            allowed_client_ids=[self.CLI_CLIENT, self.MACHINE_CLIENT, self.GITLAB_CLIENT],
            region="us-east-1",
        )

    def _token(self, validator, rsa_keypair, **claims) -> str:
        private_pem, _ = rsa_keypair
        payload = {
            "sub": "user-123",
            "iss": validator.issuer,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            **claims,
        }
        return jwt.encode(payload, private_pem, algorithm="RS256")

    def _validate(self, validator, rsa_keypair, token):
        """Validate against the locally generated public key.

        Only the JWKS fetch is stubbed — signature, issuer, expiry and the claim
        checks under test all run for real.
        """
        _, public_key = rsa_keypair
        signing_key = MagicMock()
        signing_key.key = public_key
        with patch.object(type(validator), "jwk_client", new_callable=MagicMock) as jwk:
            jwk.get_signing_key_from_jwt.return_value = signing_key
            return validator.validate_token(token)

    @staticmethod
    def _dynamodb_with_items(items):
        table = MagicMock()

        def put_item(**kwargs):
            item = kwargs["Item"]
            items[item["client_id"]] = dict(item)

        def get_item(**kwargs):
            client_id = kwargs["Key"]["client_id"]
            return {"Item": items[client_id]} if client_id in items else {}

        table.put_item.side_effect = put_item
        table.get_item.side_effect = get_item
        dynamodb = MagicMock()
        dynamodb.Table.return_value = table
        return dynamodb, table

    def _dynamic_token(self, validator, rsa_keypair, client_id, **overrides):
        claims = {
            "token_use": "access",
            "client_id": client_id,
            "custom:client_id": client_id,
            "custom:account_type": "service",
            "custom:org_id": "tenant-a",
            "custom:team_id": "team-a",
            "custom:department_id": "department-a",
        }
        claims.update(overrides)
        return self._token(validator, rsa_keypair, **claims)

    def test_id_token_for_another_app_is_rejected(self, restricted, rsa_keypair):
        """THE FIX: same user pool, different application, must not be accepted."""
        token = self._token(restricted, rsa_keypair, token_use="id", aud=self.OTHER_APP)

        with pytest.raises(jwt.InvalidTokenError, match="aud"):
            self._validate(restricted, rsa_keypair, token)

    def test_id_token_for_the_allowed_app_is_accepted(self, restricted, rsa_keypair):
        """Regression guard: the legitimate SPA id token still works.

        Without this, "reject all id tokens" would satisfy the test above while
        breaking every browser login.
        """
        claims = self._validate(restricted, rsa_keypair, self._token(restricted, rsa_keypair, token_use="id", aud=self.ALLOWED))
        assert claims.token_use == "id"
        assert claims.client_id == self.ALLOWED

    def test_access_token_binding_is_unchanged(self, restricted, rsa_keypair):
        """The access-token path keeps using client_id, accepted and rejected."""
        ok = self._validate(restricted, rsa_keypair, self._token(restricted, rsa_keypair, token_use="access", client_id=self.ALLOWED))
        assert ok.client_id == self.ALLOWED

        with pytest.raises(jwt.InvalidTokenError, match="client_id"):
            self._validate(restricted, rsa_keypair, self._token(restricted, rsa_keypair, token_use="access", client_id=self.OTHER_APP))

    def test_id_token_with_no_audience_is_rejected(self, restricted, rsa_keypair):
        """A token carrying no client binding at all cannot satisfy an allowlist."""
        with pytest.raises(jwt.InvalidTokenError, match="aud"):
            self._validate(restricted, rsa_keypair, self._token(restricted, rsa_keypair, token_use="id"))

    def test_access_token_with_no_client_id_is_rejected(self, restricted, rsa_keypair):
        with pytest.raises(jwt.InvalidTokenError, match="client_id"):
            self._validate(restricted, rsa_keypair, self._token(restricted, rsa_keypair, token_use="access"))

    def test_mixed_audience_fails_closed(self, restricted, rsa_keypair):
        """Naming an allowed AND a disallowed client is not a token for the allowed one."""
        token = self._token(restricted, rsa_keypair, token_use="id", aud=[self.ALLOWED, self.OTHER_APP])

        with pytest.raises(jwt.InvalidTokenError, match="aud"):
            self._validate(restricted, rsa_keypair, token)

    def test_single_element_audience_list_is_honoured(self, restricted, rsa_keypair):
        """JWT permits a list; a single allowed entry is still that client."""
        claims = self._validate(restricted, rsa_keypair, self._token(restricted, rsa_keypair, token_use="id", aud=[self.ALLOWED]))
        assert claims.token_use == "id"

    @pytest.mark.parametrize(
        "kind,claims",
        [
            ("spa-id-token", {"token_use": "id", "aud": "spa-client-id"}),
            ("cli-access-token", {"token_use": "access", "client_id": "cli-client-id"}),
            ("machine-access-token", {"token_use": "access", "client_id": "machine-client-id"}),
            ("gitlab-id-token", {"token_use": "id", "aud": "gitlab-client-id"}),
        ],
    )
    def test_every_legitimate_client_type_still_authenticates(self, restricted, rsa_keypair, kind, claims):
        """No legitimate client type regresses; each is bound via its own claim."""
        assert self._validate(restricted, rsa_keypair, self._token(restricted, rsa_keypair, **claims)) is not None

    def test_configured_pentest_access_token_is_accepted(self, mock_settings, rsa_keypair):
        mock_settings.cognito_pentest_client_id = self.PENTEST_CLIENT
        validator = CognitoJWTValidator(user_pool_id="us-east-1_testpool", region="us-east-1")

        claims = self._validate(
            validator,
            rsa_keypair,
            self._token(validator, rsa_keypair, token_use="access", client_id=self.PENTEST_CLIENT),
        )

        assert claims.client_id == self.PENTEST_CLIENT

    @pytest.mark.parametrize("token_use,claim", [("id", {"aud": OTHER_APP}), ("access", {"client_id": OTHER_APP})])
    def test_runtime_default_rejects_unconfigured_same_pool_clients(self, validator, rsa_keypair, token_use, claim):
        with pytest.raises(jwt.InvalidTokenError):
            self._validate(validator, rsa_keypair, self._token(validator, rsa_keypair, token_use=token_use, **claim))

    @pytest.mark.asyncio
    async def test_runtime_default_accepts_agent_service_provisioned_client(self, mock_settings, rsa_keypair):
        client_id = "tenant-a-provisioned-client"
        mock_settings.agent_clients_table = "test-agent-clients"
        mock_settings.cognito_domain = "test-domain"
        items = {}
        dynamodb, table = self._dynamodb_with_items(items)
        cognito = MagicMock()
        cognito.create_user_pool_client.return_value = {"UserPoolClient": {"ClientId": client_id}}

        with patch("src.admin.agent_service.get_settings", return_value=mock_settings):
            service = AgentService(cognito_client=cognito, dynamodb_resource=dynamodb)
            await service.create_agent(
                AgentCreateRequest(
                    name="worker",
                    org_id="tenant-a",
                    team_id="team-a",
                    department_id="department-a",
                )
            )

        validator = CognitoJWTValidator(dynamodb_resource=dynamodb)
        claims = self._validate(validator, rsa_keypair, self._dynamic_token(validator, rsa_keypair, client_id))

        assert claims.client_id == client_id
        assert claims.org_id == "tenant-a"
        dynamodb.Table.assert_called_with("test-agent-clients")
        table.get_item.assert_called_once_with(Key={"client_id": client_id}, ConsistentRead=True)

    @pytest.mark.parametrize(
        "record,claim_overrides",
        [
            (None, {}),
            ({"status": "disabled"}, {}),
            ({"org_id": ""}, {"custom:org_id": ""}),
            ({}, {"custom:org_id": "tenant-b"}),
            ({}, {"custom:team_id": "team-b"}),
            ({}, {"custom:account_type": "human"}),
            ({}, {"custom:client_id": "different-client"}),
        ],
        ids=[
            "unregistered",
            "inactive",
            "missing-tenant",
            "wrong-tenant",
            "wrong-team",
            "not-service",
            "wrong-custom-client",
        ],
    )
    def test_runtime_default_rejects_invalid_dynamic_clients(self, mock_settings, rsa_keypair, record, claim_overrides):
        client_id = "tenant-a-dynamic-client"
        mock_settings.agent_clients_table = "test-agent-clients"
        items = {}
        if record is not None:
            items[client_id] = {
                "client_id": client_id,
                "status": "active",
                "org_id": "tenant-a",
                "team_id": "team-a",
                "department_id": "department-a",
                **record,
            }
        dynamodb, _ = self._dynamodb_with_items(items)
        validator = CognitoJWTValidator(dynamodb_resource=dynamodb)
        token = self._dynamic_token(validator, rsa_keypair, client_id, **claim_overrides)

        with pytest.raises(jwt.InvalidTokenError, match="client_id"):
            self._validate(validator, rsa_keypair, token)

    def test_dynamic_registry_never_authorizes_id_tokens(self, mock_settings, rsa_keypair):
        client_id = "tenant-a-dynamic-client"
        mock_settings.agent_clients_table = "test-agent-clients"
        items = {
            client_id: {
                "client_id": client_id,
                "status": "active",
                "org_id": "tenant-a",
                "team_id": "",
                "department_id": "",
            }
        }
        dynamodb, table = self._dynamodb_with_items(items)
        validator = CognitoJWTValidator(dynamodb_resource=dynamodb)
        token = self._token(validator, rsa_keypair, token_use="id", aud=client_id)

        with pytest.raises(jwt.InvalidTokenError, match="aud"):
            self._validate(validator, rsa_keypair, token)
        table.get_item.assert_not_called()

    def test_static_client_does_not_depend_on_dynamic_registry(self, mock_settings, rsa_keypair):
        mock_settings.agent_clients_table = "test-agent-clients"
        dynamodb, table = self._dynamodb_with_items({})
        table.get_item.side_effect = RuntimeError("registry unavailable")
        validator = CognitoJWTValidator(dynamodb_resource=dynamodb)
        token = self._token(validator, rsa_keypair, token_use="access", client_id="test-client-id")

        claims = self._validate(validator, rsa_keypair, token)

        assert claims.client_id == "test-client-id"
        table.get_item.assert_not_called()

    def test_dynamic_registry_error_fails_closed(self, mock_settings, rsa_keypair):
        client_id = "tenant-a-dynamic-client"
        mock_settings.agent_clients_table = "test-agent-clients"
        dynamodb, table = self._dynamodb_with_items({})
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetItem",
        )
        validator = CognitoJWTValidator(dynamodb_resource=dynamodb)
        token = self._dynamic_token(validator, rsa_keypair, client_id)

        with pytest.raises(jwt.InvalidTokenError, match="client_id"):
            self._validate(validator, rsa_keypair, token)

    def test_client_registered_to_another_tenant_is_rejected(self, restricted, rsa_keypair):
        token = self._token(
            restricted,
            rsa_keypair,
            token_use="access",
            client_id=self.OTHER_APP,
            **{"custom:org_id": "other-tenant"},
        )

        with pytest.raises(jwt.InvalidTokenError, match="client_id"):
            self._validate(restricted, rsa_keypair, token)

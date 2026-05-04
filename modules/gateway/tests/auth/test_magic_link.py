"""Tests for magic-link token library and identity-linking endpoints.

Issue #446: Vault Phase 2b — Magic-link identity linking flow

Coverage:
  - Token issue → consume → identity linked successfully
  - Replay: second consume of same nonce is rejected
  - Expired token → rejected with 400
  - Cross-user token consume → 403
  - channel_context mismatch → rejected
  - GET /auth/link/magic validates token without consuming nonce
  - POST /auth/link/magic confirms link, writes user_identities row
  - POST /auth/identities/{provider}/link issues a user-initiated token
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.magic_link import (
    ChannelContextMismatchError,
    NonceAlreadyConsumedError,
    TargetUserMismatchError,
    TokenExpiredError,
    TokenInvalidError,
    consume_nonce,
    issue_token,
    store_nonce,
    verify_token,
)
from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SECRET = "test-magic-link-secret-key-32chars!!"


# ---------------------------------------------------------------------------
# In-memory DB helpers
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


def _make_engine():
    return create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


# ---------------------------------------------------------------------------
# Token context helpers
# ---------------------------------------------------------------------------


def _ctx(
    user_id: str = "user-alice",
    org_id: str = "org-acme",
    team_id: str = "team-eng",
    is_admin: bool = False,
) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id=team_id,
        department_id="dept-eng",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


ALICE = _ctx(user_id="user-alice")
BOB = _ctx(user_id="user-bob")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = _make_engine()
    async with eng.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        org = Organization(
            id="org-acme",
            name="Acme Corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng", org_id="org-acme", name="Engineering")
        team = Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Eng")
        alice = User(id="user-alice", org_id="org-acme", team_id="team-eng", email="alice@test.com")
        bob = User(id="user-bob", org_id="org-acme", team_id="team-eng", email="bob@test.com")
        session.add_all([org, dept, team, alice, bob])
        await session.commit()
        yield session


def _make_app(caller: TokenContext, db_session: AsyncSession) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    async def _get_caller():
        return caller

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_context] = _get_caller
    # Inject a no-op SM mock so credential endpoints don't fail
    mock_sm = MagicMock()
    app.dependency_overrides[get_secrets_manager] = lambda: mock_sm

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Unit tests: magic_link library
# ---------------------------------------------------------------------------


class TestIssueToken:
    def test_token_has_expected_claims(self):
        result = issue_token(
            provider="slack",
            provider_user_id="U123",
            channel_context="T01/C02",
            target_user_id="user-alice",
            secret_key=_SECRET,
        )
        assert "token" in result
        assert "jti" in result
        assert result["expires_at"] > datetime.now(UTC)

    def test_verify_token_succeeds(self):
        result = issue_token(
            provider="slack",
            provider_user_id="U123",
            channel_context="T01/C02",
            target_user_id="user-alice",
            secret_key=_SECRET,
        )
        payload = verify_token(result["token"], _SECRET)
        assert payload["provider"] == "slack"
        assert payload["provider_user_id"] == "U123"
        assert payload["channel_context"] == "T01/C02"
        assert payload["target_user_id"] == "user-alice"
        assert payload["jti"] == result["jti"]

    def test_verify_token_wrong_secret_raises(self):
        result = issue_token(
            provider="slack",
            provider_user_id="U123",
            channel_context=None,
            target_user_id=None,
            secret_key=_SECRET,
        )
        with pytest.raises(TokenInvalidError):
            verify_token(result["token"], "wrong-secret")

    def test_verify_token_expired_raises(self):
        """Forge an already-expired token."""
        import jwt

        now = datetime.now(UTC)
        payload = {
            "iss": "adp-gateway",
            "jti": "test-jti",
            "provider": "slack",
            "provider_user_id": "U123",
            "channel_context": None,
            "target_user_id": None,
            "iat": int((now - timedelta(minutes=20)).timestamp()),
            "exp": int((now - timedelta(minutes=5)).timestamp()),  # already expired
        }
        token = jwt.encode(payload, _SECRET, algorithm="HS256")
        with pytest.raises(TokenExpiredError):
            verify_token(token, _SECRET)

    def test_null_target_user_id_and_channel_context(self):
        result = issue_token(
            provider="github",
            provider_user_id="12345",
            channel_context=None,
            target_user_id=None,
            secret_key=_SECRET,
        )
        payload = verify_token(result["token"], _SECRET)
        assert payload["target_user_id"] is None
        assert payload["channel_context"] is None


class TestConsumeNonce:
    @pytest.mark.asyncio
    async def test_consume_succeeds(self, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-consume-ok",
            channel_context="T01/C02",
            target_user_id="user-alice",
            secret_key=_SECRET,
        )
        await store_nonce(
            jti=result["jti"],
            provider="slack",
            provider_user_id="U-consume-ok",
            channel_context="T01/C02",
            target_user_id="user-alice",
            expires_at=result["expires_at"],
            db=db,
        )
        nonce = await consume_nonce(
            jti=result["jti"],
            channel_context="T01/C02",
            consuming_user_id="user-alice",
            db=db,
        )
        assert nonce.consumed_at is not None

    @pytest.mark.asyncio
    async def test_replay_rejected(self, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-replay",
            channel_context="T01/C02",
            target_user_id=None,
            secret_key=_SECRET,
        )
        await store_nonce(
            jti=result["jti"],
            provider="slack",
            provider_user_id="U-replay",
            channel_context="T01/C02",
            target_user_id=None,
            expires_at=result["expires_at"],
            db=db,
        )
        # First consume succeeds
        await consume_nonce(
            jti=result["jti"],
            channel_context="T01/C02",
            consuming_user_id="user-alice",
            db=db,
        )
        # Second consume is rejected
        with pytest.raises(NonceAlreadyConsumedError):
            await consume_nonce(
                jti=result["jti"],
                channel_context="T01/C02",
                consuming_user_id="user-alice",
                db=db,
            )

    @pytest.mark.asyncio
    async def test_channel_context_mismatch_rejected(self, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-ctx-mismatch",
            channel_context="T01/C02",
            target_user_id=None,
            secret_key=_SECRET,
        )
        await store_nonce(
            jti=result["jti"],
            provider="slack",
            provider_user_id="U-ctx-mismatch",
            channel_context="T01/C02",
            target_user_id=None,
            expires_at=result["expires_at"],
            db=db,
        )
        with pytest.raises(ChannelContextMismatchError):
            await consume_nonce(
                jti=result["jti"],
                channel_context="DIFFERENT/C99",  # wrong channel
                consuming_user_id="user-alice",
                db=db,
            )

    @pytest.mark.asyncio
    async def test_target_user_mismatch_rejected(self, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-user-mismatch",
            channel_context=None,
            target_user_id="user-alice",  # token issued for alice
            secret_key=_SECRET,
        )
        await store_nonce(
            jti=result["jti"],
            provider="slack",
            provider_user_id="U-user-mismatch",
            channel_context=None,
            target_user_id="user-alice",
            expires_at=result["expires_at"],
            db=db,
        )
        # Bob tries to consume a token meant for alice → TargetUserMismatchError
        with pytest.raises(TargetUserMismatchError):
            await consume_nonce(
                jti=result["jti"],
                channel_context=None,
                consuming_user_id="user-bob",
                db=db,
            )


# ---------------------------------------------------------------------------
# Endpoint tests: POST /auth/identities/{provider}/link
# ---------------------------------------------------------------------------


class TestIssueIdentityMagicLink:
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    @patch("src.auth.vault_routes._build_magic_link_url", side_effect=lambda t: f"https://gw.example.com/auth/link/magic?token={t}")
    def test_returns_magic_link_url(self, _mock_url, _mock_secret, db: AsyncSession):
        client = _make_app(ALICE, db)
        resp = client.post(
            "/auth/identities/slack/link",
            json={"provider_user_id": "U999", "channel_context": "T01/C01"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "magic_link_url" in body
        assert "token=" in body["magic_link_url"]

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value="")
    def test_returns_503_when_secret_not_configured(self, _mock_secret, db: AsyncSession):
        client = _make_app(ALICE, db)
        resp = client.post(
            "/auth/identities/slack/link",
            json={"provider_user_id": "U000"},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Endpoint tests: GET /auth/link/magic
# ---------------------------------------------------------------------------


class TestMagicLinkLandingGet:
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_valid_token_returns_confirmation_details(self, _mock_secret, db: AsyncSession):
        # Issue a token for alice
        result = issue_token(
            provider="slack",
            provider_user_id="U-landing-get",
            channel_context="T01/C02",
            target_user_id="user-alice",
            secret_key=_SECRET,
        )
        # Store nonce synchronously (use asyncio.run in a sync test via pytest-asyncio)
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            store_nonce(
                jti=result["jti"],
                provider="slack",
                provider_user_id="U-landing-get",
                channel_context="T01/C02",
                target_user_id="user-alice",
                expires_at=result["expires_at"],
                db=db,
            )
        )

        client = _make_app(ALICE, db)
        resp = client.get(f"/auth/link/magic?token={result['token']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending_confirmation"
        assert body["provider"] == "slack"
        assert body["provider_user_id"] == "U-landing-get"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_wrong_user_gets_403(self, _mock_secret, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-wrong-user",
            channel_context=None,
            target_user_id="user-alice",  # for alice
            secret_key=_SECRET,
        )
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            store_nonce(
                jti=result["jti"],
                provider="slack",
                provider_user_id="U-wrong-user",
                channel_context=None,
                target_user_id="user-alice",
                expires_at=result["expires_at"],
                db=db,
            )
        )

        # Bob tries to use alice's token
        client = _make_app(BOB, db)
        resp = client.get(f"/auth/link/magic?token={result['token']}")
        assert resp.status_code == 403

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_expired_token_returns_400(self, _mock_secret, db: AsyncSession):
        import jwt

        now = datetime.now(UTC)
        payload = {
            "iss": "adp-gateway",
            "jti": "expired-jti-get",
            "provider": "slack",
            "provider_user_id": "U-exp",
            "channel_context": None,
            "target_user_id": None,
            "iat": int((now - timedelta(minutes=20)).timestamp()),
            "exp": int((now - timedelta(minutes=5)).timestamp()),
        }
        token = jwt.encode(payload, _SECRET, algorithm="HS256")
        client = _make_app(ALICE, db)
        resp = client.get(f"/auth/link/magic?token={token}")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "token_expired"


# ---------------------------------------------------------------------------
# Endpoint tests: POST /auth/link/magic  (confirm)
# ---------------------------------------------------------------------------


class TestMagicLinkLandingPost:
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_full_flow_link_succeeds(self, _mock_secret, db: AsyncSession):
        """Issue token → POST confirm → user_identities row created."""
        result = issue_token(
            provider="slack",
            provider_user_id="U-full-flow",
            channel_context="T01/C02",
            target_user_id=None,  # internal-issued (any user may claim)
            secret_key=_SECRET,
        )
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            store_nonce(
                jti=result["jti"],
                provider="slack",
                provider_user_id="U-full-flow",
                channel_context="T01/C02",
                target_user_id=None,
                expires_at=result["expires_at"],
                db=db,
            )
        )

        client = _make_app(ALICE, db)
        resp = client.post(f"/auth/link/magic?token={result['token']}")
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "linked"
        assert body["provider"] == "slack"
        assert body["provider_user_id"] == "U-full-flow"
        assert body["verification_method"] == "magic_link"
        assert body["verified_at"] is not None

        # Verify the DB row was written
        async def _check():
            stmt = select(UserIdentity).where(
                UserIdentity.provider == "slack",
                UserIdentity.provider_user_id == "U-full-flow",
            )
            r = await db.execute(stmt)
            return r.scalar_one_or_none()

        identity = asyncio.get_event_loop().run_until_complete(_check())
        assert identity is not None
        assert identity.verification_method == "magic_link"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_replay_returns_400(self, _mock_secret, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-replay-ep",
            channel_context=None,
            target_user_id=None,
            secret_key=_SECRET,
        )
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            store_nonce(
                jti=result["jti"],
                provider="slack",
                provider_user_id="U-replay-ep",
                channel_context=None,
                target_user_id=None,
                expires_at=result["expires_at"],
                db=db,
            )
        )

        client = _make_app(ALICE, db)
        # First POST succeeds
        resp1 = client.post(f"/auth/link/magic?token={result['token']}")
        # Second POST is a replay → 400
        resp2 = client.post(f"/auth/link/magic?token={result['token']}")
        assert resp1.status_code == 201
        assert resp2.status_code == 400
        assert resp2.json()["detail"]["error"] == "token_already_used"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_cross_user_consume_returns_403(self, _mock_secret, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-cross-user",
            channel_context=None,
            target_user_id="user-alice",  # issued for alice
            secret_key=_SECRET,
        )
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            store_nonce(
                jti=result["jti"],
                provider="slack",
                provider_user_id="U-cross-user",
                channel_context=None,
                target_user_id="user-alice",
                expires_at=result["expires_at"],
                db=db,
            )
        )

        # Bob tries to consume → 403
        client = _make_app(BOB, db)
        resp = client.post(f"/auth/link/magic?token={result['token']}")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "user_mismatch"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_expired_token_returns_400(self, _mock_secret, db: AsyncSession):
        import jwt

        now = datetime.now(UTC)
        payload = {
            "iss": "adp-gateway",
            "jti": "expired-jti-post",
            "provider": "slack",
            "provider_user_id": "U-exp-post",
            "channel_context": None,
            "target_user_id": None,
            "iat": int((now - timedelta(minutes=20)).timestamp()),
            "exp": int((now - timedelta(minutes=5)).timestamp()),
        }
        token = jwt.encode(payload, _SECRET, algorithm="HS256")
        client = _make_app(ALICE, db)
        resp = client.post(f"/auth/link/magic?token={token}")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "token_expired"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_magic_link_url_shape(self, _mock_secret, db: AsyncSession):
        """URL must contain the gateway origin and the signed token param."""
        with patch("src.auth.vault_routes._build_magic_link_url") as mock_url:
            mock_url.side_effect = lambda t: f"https://gw.example.com/auth/link/magic?token={t}"
            client = _make_app(ALICE, db)
            resp = client.post(
                "/auth/identities/github/link",
                json={"provider_user_id": "12345"},
            )
        assert resp.status_code == 201
        url = resp.json()["magic_link_url"]
        assert url.startswith("https://gw.example.com/auth/link/magic?token=")
        # The token param should be a non-trivial JWT (3 dot-separated parts)
        token_param = url.split("?token=")[-1]
        assert token_param.count(".") == 2, "JWT must have 3 parts"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    def test_audit_log_written_on_success(self, _mock_secret, db: AsyncSession):
        result = issue_token(
            provider="slack",
            provider_user_id="U-audit-ok",
            channel_context=None,
            target_user_id=None,
            secret_key=_SECRET,
        )
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            store_nonce(
                jti=result["jti"],
                provider="slack",
                provider_user_id="U-audit-ok",
                channel_context=None,
                target_user_id=None,
                expires_at=result["expires_at"],
                db=db,
            )
        )

        client = _make_app(ALICE, db)
        resp = client.post(f"/auth/link/magic?token={result['token']}")
        assert resp.status_code == 201

        async def _count_audit():
            stmt = select(AuditLog).where(AuditLog.event_type == "magic_link_consumed")
            r = await db.execute(stmt)
            return r.scalars().all()

        logs = asyncio.get_event_loop().run_until_complete(_count_audit())
        assert any(log.actor_id == "user-alice" for log in logs)

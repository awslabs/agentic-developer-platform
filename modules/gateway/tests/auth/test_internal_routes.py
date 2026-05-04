"""Tests for /internal/v1/* endpoints.

Issue #446: Vault Phase 2b — Magic-link identity linking flow

Coverage:
  - POST /internal/v1/issue-magic-link → returns URL (happy path)
  - POST /internal/v1/issue-magic-link → 403 for missing/wrong API key
  - POST /internal/v1/issue-magic-link → 503 when magic_link_secret not set
  - POST /internal/v1/resolve-user → known identity → 200 with user context
  - POST /internal/v1/resolve-user → channel_tenant_map hit → 201 shadow user
  - POST /internal/v1/resolve-user → no map row → 404 + magic_link_url
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.routes import router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import ChannelTenantMap, UserIdentity

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key"
_SECRET = "test-magic-link-secret-key-32chars!!"


def _make_engine():
    return create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


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
        session.add_all([org, dept, team, alice])
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /internal/v1/issue-magic-link
# ---------------------------------------------------------------------------


class TestIssueMagicLink:
    @patch("src.internal.routes._get_magic_link_secret", return_value=_SECRET)
    @patch("src.internal.routes._build_magic_link_url", side_effect=lambda t: f"https://gw.example.com/auth/link/magic?token={t}")
    @patch("src.internal.routes.get_settings")
    def test_happy_path_returns_url(self, mock_settings, _mock_url, _mock_secret, db: AsyncSession):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        settings.magic_link_secret = _SECRET
        settings.gateway_base_url = "https://gw.example.com"
        mock_settings.return_value = settings

        client = _make_app(db)
        resp = client.post(
            "/internal/v1/issue-magic-link",
            json={"provider": "slack", "provider_user_id": "U123", "channel_context": "T01/C01"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "magic_link_url" in body
        assert "token=" in body["magic_link_url"]

    @patch("src.internal.routes.get_settings")
    def test_missing_api_key_returns_403(self, mock_settings, db: AsyncSession):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        mock_settings.return_value = settings

        client = _make_app(db)
        resp = client.post(
            "/internal/v1/issue-magic-link",
            json={"provider": "slack", "provider_user_id": "U123"},
        )
        assert resp.status_code == 403

    @patch("src.internal.routes.get_settings")
    def test_wrong_api_key_returns_403(self, mock_settings, db: AsyncSession):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        mock_settings.return_value = settings

        client = _make_app(db)
        resp = client.post(
            "/internal/v1/issue-magic-link",
            json={"provider": "slack", "provider_user_id": "U123"},
            headers={"X-Internal-Api-Key": "wrong-key"},
        )
        assert resp.status_code == 403

    @patch("src.internal.routes.get_settings")
    def test_secret_not_configured_returns_503(self, mock_settings, db: AsyncSession):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        settings.magic_link_secret = ""
        settings.token_secret_key = ""
        mock_settings.return_value = settings

        client = _make_app(db)
        resp = client.post(
            "/internal/v1/issue-magic-link",
            json={"provider": "slack", "provider_user_id": "U123"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /internal/v1/resolve-user
# ---------------------------------------------------------------------------


class TestResolveUser:
    @patch("src.internal.routes.get_settings")
    def test_known_identity_returns_200(self, mock_settings, db: AsyncSession):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        settings.magic_link_secret = _SECRET
        settings.gateway_base_url = "https://gw.example.com"
        mock_settings.return_value = settings

        # Seed a linked identity for alice
        async def _seed():
            identity = UserIdentity(
                org_id="org-acme",
                user_id="user-alice",
                team_id="team-eng",
                provider="slack",
                provider_user_id="U-known",
                verification_method="magic_link",
            )
            db.add(identity)
            await db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        client = _make_app(db)
        resp = client.post(
            "/internal/v1/resolve-user",
            json={"provider": "slack", "provider_user_id": "U-known"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "user-alice"
        assert body["org_id"] == "org-acme"
        assert body["is_shadow"] is False

    @patch("src.internal.routes._get_magic_link_secret", return_value=_SECRET)
    @patch("src.internal.routes._build_magic_link_url", side_effect=lambda t: f"https://gw.example.com/auth/link/magic?token={t}")
    @patch("src.internal.routes.get_settings")
    def test_channel_tenant_map_hit_creates_shadow_user(self, mock_settings, _mock_url, _mock_secret, db: AsyncSession):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        settings.magic_link_secret = _SECRET
        settings.gateway_base_url = "https://gw.example.com"
        mock_settings.return_value = settings

        # Seed a channel_tenant_map row
        async def _seed():
            mapping = ChannelTenantMap(
                provider="slack",
                provider_scope_id="T-workspace-01",
                org_id="org-acme",
            )
            db.add(mapping)
            await db.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        client = _make_app(db)
        resp = client.post(
            "/internal/v1/resolve-user",
            json={
                "provider": "slack",
                "provider_user_id": "U-shadow",
                "channel_context": "T-workspace-01",
            },
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["is_shadow"] is True
        assert body["org_id"] == "org-acme"

    @patch("src.internal.routes._get_magic_link_secret", return_value=_SECRET)
    @patch("src.internal.routes._build_magic_link_url", side_effect=lambda t: f"https://gw.example.com/auth/link/magic?token={t}")
    @patch("src.internal.routes.get_settings")
    def test_no_map_row_returns_404_with_magic_link(self, mock_settings, _mock_url, _mock_secret, db: AsyncSession):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        settings.magic_link_secret = _SECRET
        settings.gateway_base_url = "https://gw.example.com"
        mock_settings.return_value = settings

        client = _make_app(db)
        resp = client.post(
            "/internal/v1/resolve-user",
            json={"provider": "slack", "provider_user_id": "U-unknown", "channel_context": "T-no-map"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error"] == "user_not_found"
        assert "magic_link_url" in detail
        # URL must have the correct shape
        assert "token=" in detail["magic_link_url"]

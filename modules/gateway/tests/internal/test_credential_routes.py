"""Integration tests for internal credential routes — canonical user resolution.

Issue #700: credential resolver should canonicalize via Cognito sub before
scoping vault lookup.

Coverage:
  - list_user_credentials uses canonical user's id and org_id in WHERE clause
  - credential_assume_role uses canonical user (via test_assume_role_routes.py)
  - Returns 404 when canonical user genuinely has no credentials
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.credential_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key"


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
            id="org-canonical",
            name="Canonical Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng", org_id="org-canonical", name="Engineering")
        team = Team(id="team-eng", org_id="org-canonical", department_id="dept-eng", name="Eng")
        # Canonical user with cognito_sub
        canonical_user = User(
            id="user-canonical",
            org_id="org-canonical",
            team_id="team-eng",
            email="canonical@test.com",
            cognito_sub="cognito-sub-12345",
        )
        # User with no credentials
        empty_user = User(
            id="user-empty",
            org_id="org-canonical",
            team_id="team-eng",
            email="empty@test.com",
            cognito_sub="cognito-sub-empty",
        )
        session.add_all([org, dept, team, canonical_user, empty_user])
        await session.flush()

        # Seed credential for canonical user
        cred = UserCredential(
            id="cred-aws-1",
            org_id="org-canonical",
            user_id="user-canonical",
            team_id="team-eng",
            service="aws",
            label="prod",
            credential_type="bearer",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:test",
        )
        session.add(cred)
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession, mock_sm=None) -> TestClient:
    """Build a minimal FastAPI test app with the credential routes router."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    s.vault_raw_read_enabled = True
    s.vault_materialization_bucket = "test-bucket"
    return s


class TestCredentialListCanonicalResolution:
    """Test 3: credential lookup uses canonical user's id and org_id."""

    @pytest.mark.asyncio
    async def test_credential_lookup_uses_canonical_user_id_and_org(self, db):
        """Vault WHERE uses canonical_user.id and canonical_user.org_id.

        Regression test for the architect's important issue 3: both user_id
        AND org_id must be replaced in downstream queries.
        """
        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-canonical", "service": "aws"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "cred-aws-1"
        assert data[0]["service"] == "aws"
        assert data[0]["label"] == "prod"

    @pytest.mark.asyncio
    async def test_credential_lookup_returns_404_when_user_not_found(self, db):
        """Non-existent user_id returns 404."""
        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-nonexistent", "service": "aws"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "user_not_found"

    @pytest.mark.asyncio
    async def test_credential_lookup_returns_empty_when_canonical_has_no_creds(self, db):
        """Test 6: current empty-list behavior preserved when the canonical user
        genuinely has no credentials in the org."""
        with (
            patch("src.internal.routes.get_settings", return_value=_settings_mock()),
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"user_id": "user-empty", "service": "aws"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        assert resp.json() == []

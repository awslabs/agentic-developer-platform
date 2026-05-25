"""Tests for POST /internal/v1/provenance.

Issue #785: Phase 2-b — action provenance write endpoint.

Coverage:
  - Valid request -> 201 + row inserted
  - Missing actor_user_id FK -> 400
  - Missing triggered_by FK -> 400
  - Missing root_human_id FK -> 400
  - Missing required fields -> 422
  - Missing/invalid auth -> 403
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.provenance_routes import router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.provenance import ActionProvenance

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

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
        import src.shared.models.provenance  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        org = Organization(
            id="org-test",
            name="Test Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng", org_id="org-test", name="Engineering")
        team = Team(id="team-eng", org_id="org-test", department_id="dept-eng", name="Eng")
        alice = User(
            id="user-alice",
            org_id="org-test",
            team_id="team-eng",
            email="alice@test.com",
        )
        bot = User(
            id="user-bot",
            org_id="org-test",
            team_id="team-eng",
            email="bot@test.com",
        )
        session.add_all([org, dept, team, alice, bot])
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession) -> TestClient:
    """Build a minimal FastAPI test app with the provenance router."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    return s


def _valid_body() -> dict:
    return {
        "actor_user_id": "user-bot",
        "triggered_by": "user-alice",
        "root_human_id": "user-alice",
        "is_human_rooted": True,
        "action_kind": "issue_comment",
        "source_event": {"issue": 783, "comment_id": 12345},
        "correlation_id": "corr-abc123",
        "org_id": "org-test",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateProvenance:
    @pytest.mark.asyncio
    async def test_valid_request_returns_201(self, db):
        """Happy path: valid body -> 201 with id and created_at."""
        client = _make_app(db)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=_valid_body(),
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "created_at" in data

        # Verify row exists in DB
        result = await db.execute(select(ActionProvenance).where(ActionProvenance.id == data["id"]))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.actor_user_id == "user-bot"
        assert row.correlation_id == "corr-abc123"
        assert row.is_human_rooted is True

    @pytest.mark.asyncio
    async def test_invalid_actor_returns_400(self, db):
        """actor_user_id not found -> 400."""
        client = _make_app(db)
        body = _valid_body()
        body["actor_user_id"] = "nonexistent-user"
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 400
        assert "invalid_actor" in resp.json()["detail"]["error"]

    @pytest.mark.asyncio
    async def test_invalid_triggered_by_returns_400(self, db):
        """triggered_by references nonexistent user -> 400."""
        client = _make_app(db)
        body = _valid_body()
        body["triggered_by"] = "ghost-user"
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 400
        assert "invalid_triggered_by" in resp.json()["detail"]["error"]

    @pytest.mark.asyncio
    async def test_invalid_root_human_returns_400(self, db):
        """root_human_id not found -> 400."""
        client = _make_app(db)
        body = _valid_body()
        body["root_human_id"] = "no-such-user"
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 400
        assert "invalid_root_human" in resp.json()["detail"]["error"]

    @pytest.mark.asyncio
    async def test_null_triggered_by_is_valid(self, db):
        """triggered_by=null is accepted (initial human action)."""
        client = _make_app(db)
        body = _valid_body()
        body["triggered_by"] = None
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_missing_fields_returns_422(self, db):
        """Missing required fields -> 422 validation error."""
        client = _make_app(db)
        body = {"actor_user_id": "user-alice"}  # missing most fields
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_auth_returns_403(self, db):
        """No auth header -> 403."""
        client = _make_app(db)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=_valid_body(),
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_auth_returns_403(self, db):
        """Wrong API key -> 403."""
        client = _make_app(db)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=_valid_body(),
                headers={"X-Internal-Api-Key": "wrong-key"},
            )
        assert resp.status_code == 403

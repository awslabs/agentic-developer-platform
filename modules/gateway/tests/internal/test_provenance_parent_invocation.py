"""Tests for parent_invocation_id in POST /internal/v1/provenance.

Issue #1460: Parent edge for agent-to-agent lineage.

Coverage:
  - parent_invocation_id=null is accepted (chain root / human-triggered)
  - parent_invocation_id with valid value is stored correctly
  - parent_invocation_id == row id (self-reference) is rejected with 400
  - Migration adds nullable column; existing rows stay null
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


def _valid_body(**overrides) -> dict:
    base = {
        "actor_user_id": "user-bot",
        "triggered_by": "user-alice",
        "root_human_id": "user-alice",
        "is_human_rooted": True,
        "action_kind": "webhook_trigger",
        "source_event": {"event_type": "issue_comment", "repo": "org/repo"},
        "correlation_id": "corr-abc123",
        "org_id": "org-test",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestParentInvocationId:
    @pytest.mark.asyncio
    async def test_null_parent_invocation_id_accepted(self, db):
        """Human-rooted chain root: parent_invocation_id=null is valid."""
        client = _make_app(db)
        body = _valid_body(parent_invocation_id=None)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201
        data = resp.json()

        # Verify row has null parent_invocation_id
        result = await db.execute(select(ActionProvenance).where(ActionProvenance.id == data["id"]))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.parent_invocation_id is None

    @pytest.mark.asyncio
    async def test_omitted_parent_invocation_id_defaults_to_null(self, db):
        """Field not provided at all defaults to null."""
        client = _make_app(db)
        body = _valid_body()
        # Ensure parent_invocation_id is not in the body
        body.pop("parent_invocation_id", None)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201
        data = resp.json()

        result = await db.execute(select(ActionProvenance).where(ActionProvenance.id == data["id"]))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.parent_invocation_id is None

    @pytest.mark.asyncio
    async def test_valid_parent_invocation_id_stored(self, db):
        """Bot event with parent_invocation_id from upstream run is stored."""
        client = _make_app(db)
        parent_id = "upstream-run-msg-id-abc123"
        body = _valid_body(parent_invocation_id=parent_id)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201
        data = resp.json()

        result = await db.execute(select(ActionProvenance).where(ActionProvenance.id == data["id"]))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.parent_invocation_id == parent_id

    @pytest.mark.asyncio
    async def test_self_referential_parent_rejected(self, db):
        """parent_invocation_id == row's own id is rejected (cycle guard)."""
        client = _make_app(db)
        # We can't easily predict the UUID that will be generated,
        # but we can patch new_uuid to return a known value
        known_id = "known-row-id-12345"
        body = _valid_body(parent_invocation_id=known_id)
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.provenance_routes.new_uuid", return_value=known_id),
        ):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "self_referential_parent"

    @pytest.mark.asyncio
    async def test_parent_invocation_id_field_on_model(self, db):
        """ActionProvenance model has parent_invocation_id column."""
        assert hasattr(ActionProvenance, "parent_invocation_id")

"""Tests for GitHub PAT registration via the vault credential endpoint.

Issue #3389: Register GitHub PAT vault flow.

Coverage:
  - PAT registration with fixed conventions (service, label, type, strict)
  - PAT value never returned in responses (redaction)
  - Duplicate registration returns 409 (IntegrityError handling)
  - Delete + re-register cycle
  - List shows PAT metadata without value
  - Strict mode enforced
  - caplog redaction: PAT value never appears in log output (F5)
  - Error response shape matches {detail: {error, message}}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Test database
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def make_engine():
    return create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_context(
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


ALICE = _make_context(user_id="user-alice")


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = make_engine()
    async with eng.begin() as conn:
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
            aws_accounts=["123456789012"],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng", org_id="org-acme", name="Engineering")
        team = Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Eng Team")
        alice = User(id="user-alice", org_id="org-acme", team_id="team-eng", email="alice@acme.com")
        session.add_all([org, dept, team, alice])
        await session.commit()
        yield session


@pytest.fixture
def sm() -> MagicMock:
    """Mock SecretsManagerHelper."""
    mock = MagicMock()
    mock.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-pat"
    mock.delete_secret.return_value = None
    return mock


def _make_client(caller: TokenContext, db_session: AsyncSession, sm_mock: Any) -> TestClient:
    """Build a TestClient with dependency overrides."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    async def _get_caller():
        return caller

    def _get_sm():
        return sm_mock

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_context] = _get_caller
    app.dependency_overrides[get_secrets_manager] = _get_sm

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# PAT registration payload
# ---------------------------------------------------------------------------

PAT_VALUE = "github_pat_SENSITIVE_VALUE_12345_ABCDEFG"

PAT_PAYLOAD = {
    "service": "github",
    "credential_type": "bearer",
    "label": "github-pat",
    "value": PAT_VALUE,
    "strict": True,
    "scope_hint": "user",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPatRegistrationConventions:
    """Verify fixed conventions are stored correctly."""

    def test_register_pat_returns_201_with_metadata(self, db, sm):
        client = _make_client(ALICE, db, sm)
        resp = client.post("/auth/credentials", json=PAT_PAYLOAD)

        assert resp.status_code == 201
        body = resp.json()
        assert body["service"] == "github"
        assert body["label"] == "github-pat"
        assert body["credential_type"] == "bearer"
        assert body["strict"] is True
        assert body["scope"] == "user"

    def test_register_pat_calls_sm_with_correct_args(self, db, sm):
        client = _make_client(ALICE, db, sm)
        client.post("/auth/credentials", json=PAT_PAYLOAD)

        sm.create_secret.assert_called_once_with(
            "github",
            "github-pat",
            PAT_VALUE,
            user_sub="user-alice",
        )

    def test_register_pat_with_expiry(self, db, sm):
        payload = {**PAT_PAYLOAD, "expires_at": "2026-12-31T00:00:00Z"}
        client = _make_client(ALICE, db, sm)
        resp = client.post("/auth/credentials", json=payload)

        assert resp.status_code == 201
        assert resp.json()["expires_at"] is not None


class TestPatRedaction:
    """PAT value must never appear in API responses (F5: or in logs)."""

    def test_pat_value_never_in_response(self, db, sm):
        client = _make_client(ALICE, db, sm)
        resp = client.post("/auth/credentials", json=PAT_PAYLOAD)

        assert resp.status_code == 201
        body = resp.json()
        # value and secret_arn must never be exposed
        assert "value" not in body
        assert "secret_arn" not in body
        # The PAT string itself must not appear anywhere in the response
        assert PAT_VALUE not in resp.text

    def test_pat_not_in_list_response(self, db, sm):
        client = _make_client(ALICE, db, sm)
        client.post("/auth/credentials", json=PAT_PAYLOAD)

        resp = client.get("/auth/credentials")
        assert resp.status_code == 200
        assert PAT_VALUE not in resp.text
        for item in resp.json():
            assert "value" not in item
            assert "secret_arn" not in item

    def test_pat_value_not_in_logs(self, db, sm, caplog):
        """F5: PAT value must never appear in log output during registration."""
        client = _make_client(ALICE, db, sm)
        with caplog.at_level(logging.DEBUG, logger="src.auth"):
            client.post("/auth/credentials", json=PAT_PAYLOAD)

        # Ensure the PAT value does not leak into any log record
        full_log_text = " ".join(record.getMessage() for record in caplog.records)
        assert PAT_VALUE not in full_log_text


class TestPatDuplicateHandling:
    """Duplicate registration returns 409 with correct error shape (F2)."""

    def test_duplicate_returns_409(self, db, sm):
        client = _make_client(ALICE, db, sm)
        # First registration succeeds
        resp1 = client.post("/auth/credentials", json=PAT_PAYLOAD)
        assert resp1.status_code == 201

        # Second registration with same label returns 409
        resp2 = client.post("/auth/credentials", json=PAT_PAYLOAD)
        assert resp2.status_code == 409
        body = resp2.json()
        assert body["detail"]["error"] == "duplicate_credential"
        assert "already exists" in body["detail"]["message"]

    def test_duplicate_cleans_up_sm_secret(self, db, sm):
        """F2: On duplicate registration, the orphaned SM secret is deleted."""
        client = _make_client(ALICE, db, sm)
        # First registration succeeds — creates SM secret
        resp1 = client.post("/auth/credentials", json=PAT_PAYLOAD)
        assert resp1.status_code == 201

        # Second registration triggers IntegrityError → service cleans up orphan
        sm.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123456789012:secret:orphan-secret"
        resp2 = client.post("/auth/credentials", json=PAT_PAYLOAD)
        assert resp2.status_code == 409

        # The orphaned SM secret (from the second create_secret call) must be deleted
        sm.delete_secret.assert_called_with("arn:aws:secretsmanager:us-east-1:123456789012:secret:orphan-secret")

    def test_delete_and_reregister(self, db, sm):
        client = _make_client(ALICE, db, sm)
        # Register
        resp1 = client.post("/auth/credentials", json=PAT_PAYLOAD)
        assert resp1.status_code == 201
        cred_id = resp1.json()["id"]

        # Delete
        resp_del = client.delete(f"/auth/credentials/{cred_id}")
        assert resp_del.status_code == 204

        # Re-register succeeds
        resp2 = client.post("/auth/credentials", json=PAT_PAYLOAD)
        assert resp2.status_code == 201
        assert resp2.json()["id"] != cred_id

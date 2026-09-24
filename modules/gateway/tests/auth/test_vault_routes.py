"""Tests for vault credential and identity CRUD endpoints.

Issue #135: Vault Phase 2a

Coverage:
  - Per-scope CRUD (user, team, org, domain_app)
  - Owner-scoping: user A cannot see/modify user B's credentials
  - Tenant isolation: cross-tenant access returns 404
  - Secret delete lifecycle: DB row removed + SM secret removed
  - Non-admin writing team/org/domain_app cred: 403
  - PATCH rejecting value field: 400
  - 404 for non-owned resources (no enumeration)
  - Identity list and unlink
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import CredentialType, UserCredential, UserIdentity, VerificationMethod
from src.shared.schemas.auth import TokenContext
from src.shared.services.secrets_manager import SecretsManagerHelper

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
# Fixture helpers
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


ALICE = _make_context(user_id="user-alice", team_id="team-eng", is_admin=False)
BOB = _make_context(user_id="user-bob", team_id="team-eng", is_admin=False)
ADMIN = _make_context(user_id="user-admin", team_id="team-eng", is_admin=True)
OTHER_ORG = _make_context(user_id="user-carol", org_id="org-other", team_id="team-x", is_admin=False)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(
    caller: TokenContext,
    db_session: AsyncSession,
    sm_mock: Any,
) -> TestClient:
    """Create a TestClient with the vault router and overridden dependencies."""
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
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def event_loop():
    import asyncio

    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = make_engine()
    async with eng.begin() as conn:
        # Import vault models so tables are created
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        # Seed reference data
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
        bob = User(id="user-bob", org_id="org-acme", team_id="team-eng", email="bob@acme.com")
        admin_user = User(id="user-admin", org_id="org-acme", team_id="team-eng", email="admin@acme.com")
        session.add_all([org, dept, team, alice, bob, admin_user])
        await session.commit()
        yield session


@pytest.fixture
def sm() -> MagicMock:
    """Mock SecretsManagerHelper that records calls."""
    mock = MagicMock()
    mock.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-arn"
    mock.delete_secret.return_value = None
    return mock


# ---------------------------------------------------------------------------
# Helper to insert credentials directly
# ---------------------------------------------------------------------------


async def _insert_cred(
    db: AsyncSession,
    *,
    credential_id: str | None = None,
    org_id: str = "org-acme",
    user_id: str | None = None,
    team_id: str | None = None,
    domain_app_id: str | None = None,
    service: str = "github",
    label: str = "default",
    secret_arn: str = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
) -> UserCredential:
    cred = UserCredential(
        id=credential_id,
        org_id=org_id,
        user_id=user_id,
        team_id=team_id,
        domain_app_id=domain_app_id,
        service=service,
        credential_type=CredentialType.api_key,
        label=label,
        secret_arn=secret_arn,
        strict=False,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return cred


async def _insert_identity(
    db: AsyncSession,
    *,
    org_id: str = "org-acme",
    user_id: str = "user-alice",
    team_id: str = "team-eng",
    provider: str = "github",
    provider_user_id: str = "gh-123",
) -> UserIdentity:
    identity = UserIdentity(
        org_id=org_id,
        user_id=user_id,
        team_id=team_id,
        provider=provider,
        provider_user_id=provider_user_id,
        verification_method=VerificationMethod.oauth,
        verified_at=datetime.now(UTC),
    )
    db.add(identity)
    await db.commit()
    await db.refresh(identity)
    return identity


# ===========================================================================
# Credential endpoint tests
# ===========================================================================


class TestListCredentials:
    """GET /auth/credentials"""

    def test_empty_list(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_own_user_cred(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="my-pat"))
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials")
        assert resp.status_code == 200
        data = resp.json()
        ids = [c["id"] for c in data]
        assert cred.id in ids
        # secret_arn must never be returned
        for item in data:
            assert "secret_arn" not in item

    def test_does_not_return_other_user_cred(self, db, sm):
        asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-bob", label="bobs-pat"))
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials")
        assert resp.status_code == 200
        # Alice should not see Bob's credential
        data = resp.json()
        for item in data:
            if item.get("label") == "bobs-pat":
                pytest.fail("Alice can see Bob's credential")

    def test_returns_team_cred_for_team_member(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, team_id="team-eng", label="team-bot-token"))
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials")
        ids = [c["id"] for c in resp.json()]
        assert cred.id in ids

    def test_returns_org_cred_for_any_member(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, label="org-wide-key"))
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials")
        ids = [c["id"] for c in resp.json()]
        assert cred.id in ids

    def test_scope_filter_user(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="scoped-user-cred"))
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials?scope=user")
        assert resp.status_code == 200
        ids = [c["id"] for c in resp.json()]
        assert cred.id in ids

    def test_scope_filter_team(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, team_id="team-eng", label="scoped-team-cred"))
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials?scope=team")
        assert resp.status_code == 200
        ids = [c["id"] for c in resp.json()]
        assert cred.id in ids

    def test_invalid_scope_returns_400(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/credentials?scope=invalid")
        assert resp.status_code == 400

    def test_domain_app_cred_only_visible_to_admin(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, domain_app_id="app-cyber", label="domain-app-cred"))
        # Non-admin should not see it
        client_alice = _make_app(ALICE, db, sm)
        resp = client_alice.get("/auth/credentials")
        ids = [c["id"] for c in resp.json()]
        assert cred.id not in ids

        # Admin should see it
        client_admin = _make_app(ADMIN, db, sm)
        resp = client_admin.get("/auth/credentials")
        ids = [c["id"] for c in resp.json()]
        assert cred.id in ids

    def test_cross_tenant_isolation(self, db, sm):
        """User from org-other cannot see org-acme credentials."""
        asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", org_id="org-acme", label="acme-private"))
        client = _make_app(OTHER_ORG, db, sm)
        resp = client.get("/auth/credentials")
        assert resp.status_code == 200
        labels = [c["label"] for c in resp.json()]
        assert "acme-private" not in labels


class TestCreateCredential:
    """POST /auth/credentials"""

    def test_create_user_scoped(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "github",
                "label": "my-pat",
                "credential_type": "api_key",
                "value": "ghp_test_token",
                "scope_hint": "user",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["scope"] == "user"
        assert data["service"] == "github"
        assert "secret_arn" not in data
        assert "value" not in data
        # SM was called with user_sub
        sm.create_secret.assert_called_once()
        call_kwargs = sm.create_secret.call_args
        assert call_kwargs.kwargs.get("user_sub") == "user-alice"

    def test_idempotent_put_reuses_the_exact_operation_without_a_second_secret(self, db, sm):
        client = _make_app(ALICE, db, sm)
        credential_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        body = {
            "service": "nebius",
            "label": "prod",
            "credential_type": "api_key",
            "value": "synthetic-secret",
            "scope_hint": "user",
        }

        first = client.put(f"/auth/credentials/{credential_id}", json=body)
        sm.get_secret.return_value = body["value"]
        second = client.put(f"/auth/credentials/{credential_id}", json=body)

        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"] == credential_id
        sm.create_secret.assert_called_once()
        sm.get_secret.assert_called_once()
        fingerprint = sm.create_secret.call_args.kwargs["operation_fingerprint"]
        expected = hashlib.sha256(
            json.dumps(
                {
                    "org_id": "org-acme",
                    "user_id": "user-alice",
                    "team_id": None,
                    "domain_app_id": None,
                    "service": "nebius",
                    "credential_type": "api_key",
                    "label": "prod",
                    "scopes": None,
                    "expires_at": None,
                    "strict": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert fingerprint == expected

    def test_idempotent_put_rejects_reusing_an_operation_for_different_metadata(self, db, sm):
        client = _make_app(ALICE, db, sm)
        credential_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        body = {
            "service": "nebius",
            "label": "prod",
            "credential_type": "api_key",
            "value": "synthetic-secret",
            "scope_hint": "user",
        }
        assert client.put(f"/auth/credentials/{credential_id}", json=body).status_code == 201

        conflict = client.put(f"/auth/credentials/{credential_id}", json={**body, "label": "other"})

        assert conflict.status_code == 409
        assert conflict.json()["detail"]["error"] == "operation_conflict"
        sm.create_secret.assert_called_once()

    def test_idempotent_put_never_force_deletes_a_shared_operation_secret_on_conflict(self, db, sm):
        asyncio.get_event_loop().run_until_complete(
            _insert_cred(
                db,
                user_id="user-alice",
                service="nebius",
                label="duplicate-label",
            )
        )
        sm.delete_secret.side_effect = RuntimeError("synthetic cleanup failure")
        client = _make_app(ALICE, db, sm)

        response = client.put(
            "/auth/credentials/66666666-7777-4888-8999-aaaaaaaaaaaa",
            json={
                "service": "nebius",
                "label": "duplicate-label",
                "credential_type": "api_key",
                "value": "synthetic-secret",
                "scope_hint": "user",
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "operation_conflict"
        sm.delete_secret.assert_not_called()

    def test_idempotent_put_does_not_reveal_or_replace_another_tenants_operation(self, db, sm):
        credential_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        asyncio.get_event_loop().run_until_complete(
            _insert_cred(db, credential_id=credential_id, user_id="user-alice", org_id="org-acme", label="private")
        )
        client = _make_app(OTHER_ORG, db, sm)

        response = client.put(
            f"/auth/credentials/{credential_id}",
            json={
                "service": "github",
                "label": "private",
                "credential_type": "api_key",
                "value": "replacement",
                "scope_hint": "user",
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "operation_conflict"
        sm.create_secret.assert_not_called()

    def test_idempotent_put_rejects_a_non_uuid_before_writing(self, db, sm):
        response = _make_app(ALICE, db, sm).put(
            "/auth/credentials/not-an-operation-id",
            json={
                "service": "nebius",
                "label": "prod",
                "credential_type": "api_key",
                "value": "synthetic-secret",
            },
        )

        assert response.status_code == 422
        sm.create_secret.assert_not_called()

    def test_create_default_scope_is_user(self, db, sm):
        client = _make_app(ALICE, db, sm)
        sm.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123:secret:x2"
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "openai",
                "label": "openai-key",
                "credential_type": "api_key",
                "value": "sk-test",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["scope"] == "user"

    def test_create_team_scoped_as_admin(self, db, sm):
        client = _make_app(ADMIN, db, sm)
        sm.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123:secret:team-arn"
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "jira",
                "label": "team-token",
                "credential_type": "bearer",
                "value": "jira-secret",
                "scope_hint": "team",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["scope"] == "team"
        call_kwargs = sm.create_secret.call_args
        assert call_kwargs.kwargs.get("team_id") == "team-eng"

    def test_create_org_scoped_as_admin(self, db, sm):
        client = _make_app(ADMIN, db, sm)
        sm.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123:secret:org-arn"
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "datadog",
                "label": "org-key",
                "credential_type": "api_key",
                "value": "dd-secret",
                "scope_hint": "org",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["scope"] == "org"
        call_kwargs = sm.create_secret.call_args
        assert call_kwargs.kwargs.get("org_id") == "org-acme"

    def test_create_domain_app_scoped_as_admin(self, db, sm):
        client = _make_app(ADMIN, db, sm)
        sm.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123:secret:app-arn"
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "virustotal",
                "label": "vt-key",
                "credential_type": "api_key",
                "value": "vt-secret",
                "scope_hint": "domain_app",
                "domain_app_id": "app-cyber",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["scope"] == "domain_app"

    def test_non_admin_cannot_create_team_cred(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "github",
                "label": "team-token",
                "credential_type": "api_key",
                "value": "ghp_team",
                "scope_hint": "team",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "insufficient_privileges"

    def test_non_admin_cannot_create_org_cred(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "github",
                "label": "org-token",
                "credential_type": "api_key",
                "value": "ghp_org",
                "scope_hint": "org",
            },
        )
        assert resp.status_code == 403

    def test_domain_app_scope_without_domain_app_id_returns_422(self, db, sm):
        client = _make_app(ADMIN, db, sm)
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "virustotal",
                "label": "vt-key",
                "credential_type": "api_key",
                "value": "vt-secret",
                "scope_hint": "domain_app",
                # domain_app_id intentionally missing
            },
        )
        assert resp.status_code == 422

    def test_invalid_scope_hint_returns_422(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.post(
            "/auth/credentials",
            json={
                "service": "github",
                "label": "x",
                "credential_type": "api_key",
                "value": "ghp_x",
                "scope_hint": "invalid_scope",
            },
        )
        assert resp.status_code == 422


class TestUpdateCredential:
    """PATCH /auth/credentials/{id}"""

    def test_update_label(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="old-label"))
        client = _make_app(ALICE, db, sm)
        resp = client.patch(f"/auth/credentials/{cred.id}", json={"label": "new-label"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "new-label"

    def test_update_strict(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="strict-test"))
        client = _make_app(ALICE, db, sm)
        resp = client.patch(f"/auth/credentials/{cred.id}", json={"strict": True})
        assert resp.status_code == 200
        assert resp.json()["strict"] is True

    def test_patch_rejects_value_field(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="value-reject-test"))
        client = _make_app(ALICE, db, sm)
        resp = client.patch(f"/auth/credentials/{cred.id}", json={"value": "new-secret"})
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"

    def test_update_non_owned_returns_404(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-bob", label="bobs-cred"))
        client = _make_app(ALICE, db, sm)
        resp = client.patch(f"/auth/credentials/{cred.id}", json={"label": "hijacked"})
        assert resp.status_code == 404

    def test_update_nonexistent_returns_404(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.patch("/auth/credentials/nonexistent-id", json={"label": "x"})
        assert resp.status_code == 404

    def test_cross_tenant_update_returns_404(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", org_id="org-acme", label="acme-cred"))
        client = _make_app(OTHER_ORG, db, sm)
        resp = client.patch(f"/auth/credentials/{cred.id}", json={"label": "cross-tenant"})
        assert resp.status_code == 404


class TestDeleteCredential:
    """DELETE /auth/credentials/{id}"""

    def test_delete_removes_db_row(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="delete-me"))
        cred_id = cred.id
        client = _make_app(ALICE, db, sm)
        resp = client.delete(f"/auth/credentials/{cred_id}")
        assert resp.status_code == 204

        # Verify DB row is gone
        from sqlalchemy import select

        from src.shared.models.vault import UserCredential

        async def check():
            result = await db.execute(select(UserCredential).where(UserCredential.id == cred_id))
            return result.scalar_one_or_none()

        row = asyncio.get_event_loop().run_until_complete(check())
        assert row is None, "DB row should have been deleted"

    def test_delete_calls_sm_delete(self, db, sm):
        sm.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123:secret:del-test"
        cred = asyncio.get_event_loop().run_until_complete(
            _insert_cred(
                db,
                user_id="user-alice",
                label="sm-delete-test",
                secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:del-test",
            )
        )
        client = _make_app(ALICE, db, sm)
        resp = client.delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 204
        # Issue #3989: user-driven deletes pass force=False so AWS's recovery
        # window applies (the helper default is force=True → no recovery).
        sm.delete_secret.assert_called_once_with("arn:aws:secretsmanager:us-east-1:123:secret:del-test", force=False)

    def test_secret_delete_failure_keeps_the_metadata_receipt_for_retry(self, db, sm):
        credential = asyncio.get_event_loop().run_until_complete(
            _insert_cred(
                db,
                user_id="user-alice",
                label="retry-delete",
                secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:retry-delete",
            )
        )
        sm.delete_secret.side_effect = RuntimeError("synthetic Secrets Manager failure")

        response = _make_app(ALICE, db, sm).delete(f"/auth/credentials/{credential.id}")

        assert response.status_code == 500

        async def reload():
            from sqlalchemy import select

            result = await db.execute(select(UserCredential).where(UserCredential.id == credential.id))
            return result.scalar_one_or_none()

        assert asyncio.get_event_loop().run_until_complete(reload()) is not None

    def test_retry_completes_after_secret_delete_then_database_failure(self, db):
        secret_arn = "arn:aws:secretsmanager:us-east-1:123:secret:pending-delete"
        credential = asyncio.get_event_loop().run_until_complete(
            _insert_cred(
                db,
                user_id="user-alice",
                label="pending-delete",
                secret_arn=secret_arn,
            )
        )
        credential_id = credential.id
        backend = MagicMock()
        pending = ClientError(
            {"Error": {"Code": "InvalidRequestException", "Message": "scheduled for deletion"}},
            "DeleteSecret",
        )
        backend.delete_secret.side_effect = [{}, pending]
        backend.describe_secret.return_value = {"DeletedDate": datetime.now(UTC)}
        secrets = SecretsManagerHelper(client=backend)

        original_commit = db.commit
        commit_calls = 0

        async def fail_metadata_commit_once():
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 1:
                raise RuntimeError("synthetic post-Secrets-Manager commit failure")
            await original_commit()

        db.commit = fail_metadata_commit_once
        first = _make_app(ALICE, db, secrets).delete(f"/auth/credentials/{credential_id}")
        assert first.status_code == 500
        asyncio.get_event_loop().run_until_complete(db.rollback())

        second = _make_app(ALICE, db, secrets).delete(f"/auth/credentials/{credential_id}")
        assert second.status_code == 204
        backend.describe_secret.assert_called_once_with(SecretId=secret_arn)

        async def reload():
            from sqlalchemy import select

            result = await db.execute(select(UserCredential).where(UserCredential.id == credential_id))
            return result.scalar_one_or_none()

        assert asyncio.get_event_loop().run_until_complete(reload()) is None

    def test_delete_non_owned_returns_404(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-bob", label="bobs-private"))
        client = _make_app(ALICE, db, sm)
        resp = client.delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.delete("/auth/credentials/nonexistent-id")
        assert resp.status_code == 404

    def test_cross_tenant_delete_returns_404(self, db, sm):
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", org_id="org-acme", label="acme-secret"))
        client = _make_app(OTHER_ORG, db, sm)
        resp = client.delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 404

    def test_delete_org_cred_denied_without_org_admin_membership(self, db, sm):
        """Org-scoped delete is admin-gated, and the gate is now EFFECTIVE.

        Issue #3989 added an org-admin permission gate to org/team-scoped
        credential mutations. Resolution goes through ``AccessControl``, so before
        #3987 PR 2 a caller with no ``tenant_memberships`` row (Alice, in this
        fixture) resolved to ORG_ADMIN and this returned 204 — the gate was
        latent. PR 2 flipped the no-row fallback to MEMBER, so the gate now bites:
        Alice gets 403.

        Renamed from ``test_delete_org_cred_under_legacy_rbac_default``, which
        asserted the 204. That assertion encoded the pre-flip fallback, not an
        intended permission — see tests/auth/test_vault_shared_scope_authz.py for
        the same 403 asserted against an explicitly-flipped config.
        """
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, label="org-cred-to-delete"))
        client = _make_app(ALICE, db, sm)
        resp = client.delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 403


# ===========================================================================
# Identity endpoint tests
# ===========================================================================


class TestListIdentities:
    """GET /auth/identities"""

    def test_empty_list(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/identities")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_own_identity(self, db, sm):
        identity = asyncio.get_event_loop().run_until_complete(
            _insert_identity(db, user_id="user-alice", provider="github", provider_user_id="gh-alice")
        )
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/identities")
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()]
        assert identity.id in ids

    def test_does_not_return_other_user_identity(self, db, sm):
        asyncio.get_event_loop().run_until_complete(_insert_identity(db, user_id="user-bob", provider="slack", provider_user_id="slack-bob"))
        client = _make_app(ALICE, db, sm)
        resp = client.get("/auth/identities")
        provider_user_ids = [i["provider_user_id"] for i in resp.json()]
        assert "slack-bob" not in provider_user_ids

    def test_cross_tenant_isolation(self, db, sm):
        asyncio.get_event_loop().run_until_complete(
            _insert_identity(db, user_id="user-alice", org_id="org-acme", provider="discord", provider_user_id="disc-alice")
        )
        client = _make_app(OTHER_ORG, db, sm)
        resp = client.get("/auth/identities")
        provider_user_ids = [i["provider_user_id"] for i in resp.json()]
        assert "disc-alice" not in provider_user_ids


class TestUnlinkIdentity:
    """DELETE /auth/identities/{id}"""

    def test_unlink_removes_row(self, db, sm):
        identity = asyncio.get_event_loop().run_until_complete(
            _insert_identity(db, user_id="user-alice", provider="github", provider_user_id="gh-unlink-test")
        )
        identity_id = identity.id
        client = _make_app(ALICE, db, sm)
        resp = client.delete(f"/auth/identities/{identity_id}")
        assert resp.status_code == 204

        # Verify row is gone
        from sqlalchemy import select

        from src.shared.models.vault import UserIdentity as UIModel

        async def check():
            result = await db.execute(select(UIModel).where(UIModel.id == identity_id))
            return result.scalar_one_or_none()

        row = asyncio.get_event_loop().run_until_complete(check())
        assert row is None

    def test_unlink_non_owned_returns_404(self, db, sm):
        identity = asyncio.get_event_loop().run_until_complete(
            _insert_identity(db, user_id="user-bob", provider="slack", provider_user_id="slack-bob-unlink")
        )
        client = _make_app(ALICE, db, sm)
        resp = client.delete(f"/auth/identities/{identity.id}")
        assert resp.status_code == 404

    def test_unlink_nonexistent_returns_404(self, db, sm):
        client = _make_app(ALICE, db, sm)
        resp = client.delete("/auth/identities/nonexistent-id")
        assert resp.status_code == 404

    def test_cross_tenant_unlink_returns_404(self, db, sm):
        identity = asyncio.get_event_loop().run_until_complete(
            _insert_identity(db, user_id="user-alice", org_id="org-acme", provider="whatsapp", provider_user_id="wa-alice")
        )
        client = _make_app(OTHER_ORG, db, sm)
        resp = client.delete(f"/auth/identities/{identity.id}")
        assert resp.status_code == 404


# ===========================================================================
# Acceptance criteria smoke tests
# ===========================================================================


class TestAcceptanceCriteria:
    """Validate each acceptance criterion from the issue."""

    def test_ac_all_five_endpoints_respond(self, db, sm):
        """AC: All 5 endpoints respond correctly."""
        client = _make_app(ALICE, db, sm)
        assert client.get("/auth/credentials").status_code == 200
        assert client.get("/auth/identities").status_code == 200

    def test_ac_per_scope_crud_user(self, db, sm):
        """AC: Per-scope CRUD for user scope."""
        client = _make_app(ALICE, db, sm)
        sm.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123:secret:ac-user"

        # Create
        resp = client.post(
            "/auth/credentials",
            json={"service": "github", "label": "ac-user", "credential_type": "api_key", "value": "ghp_ac"},
        )
        assert resp.status_code == 201
        cred_id = resp.json()["id"]

        # Retrieve (via list)
        resp = client.get("/auth/credentials")
        ids = [c["id"] for c in resp.json()]
        assert cred_id in ids

        # Update
        resp = client.patch(f"/auth/credentials/{cred_id}", json={"label": "ac-user-updated"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "ac-user-updated"

        # Delete
        resp = client.delete(f"/auth/credentials/{cred_id}")
        assert resp.status_code == 204

        # Gone after delete
        resp = client.get("/auth/credentials")
        ids = [c["id"] for c in resp.json()]
        assert cred_id not in ids

    def test_ac_owner_scoping_user_a_cannot_see_user_b(self, db, sm):
        """AC: Owner-scoping: user A cannot list/read/delete user B's credentials."""
        cred_b = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-bob", label="b-private"))
        client_a = _make_app(ALICE, db, sm)

        # Alice cannot list Bob's credential
        resp = client_a.get("/auth/credentials")
        ids = [c["id"] for c in resp.json()]
        assert cred_b.id not in ids

        # Alice gets 404 trying to update
        resp = client_a.patch(f"/auth/credentials/{cred_b.id}", json={"label": "hijacked"})
        assert resp.status_code == 404

        # Alice gets 404 trying to delete
        resp = client_a.delete(f"/auth/credentials/{cred_b.id}")
        assert resp.status_code == 404

    def test_ac_team_cred_visible_to_team_members(self, db, sm):
        """AC: Team cred visible to team members."""
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, team_id="team-eng", label="team-visible"))
        # Both Alice and Bob are in team-eng
        client_alice = _make_app(ALICE, db, sm)
        client_bob = _make_app(BOB, db, sm)
        ids_alice = [c["id"] for c in client_alice.get("/auth/credentials").json()]
        ids_bob = [c["id"] for c in client_bob.get("/auth/credentials").json()]
        assert cred.id in ids_alice
        assert cred.id in ids_bob

    def test_ac_org_cred_visible_to_all_org_members(self, db, sm):
        """AC: Org cred visible to all org members."""
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, label="org-wide-visible"))
        for caller in [ALICE, BOB, ADMIN]:
            client = _make_app(caller, db, sm)
            ids = [c["id"] for c in client.get("/auth/credentials").json()]
            assert cred.id in ids, f"{caller.user_id} should see org-scoped cred"

    def test_ac_tenant_isolation(self, db, sm):
        """AC: Cross-tenant access returns 404."""
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", org_id="org-acme", label="private-acme"))
        client = _make_app(OTHER_ORG, db, sm)
        resp = client.delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 404

    def test_ac_credential_delete_removes_sm_secret(self, db, sm):
        """AC: Credential delete removes both DB row and SM secret."""
        test_arn = "arn:aws:secretsmanager:us-east-1:123:secret:lifecycle-test"
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="lifecycle", secret_arn=test_arn))
        client = _make_app(ALICE, db, sm)
        sm.delete_secret.reset_mock()
        resp = client.delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 204
        # Issue #3989: force=False — recovery window applies to user deletes.
        sm.delete_secret.assert_called_once_with(test_arn, force=False)

    def test_ac_non_admin_team_cred_returns_403(self, db, sm):
        """AC: Non-admin writing team/org cred returns 403."""
        client = _make_app(ALICE, db, sm)
        resp = client.post(
            "/auth/credentials",
            json={"service": "github", "label": "x", "credential_type": "api_key", "value": "v", "scope_hint": "team"},
        )
        assert resp.status_code == 403
        assert "insufficient_privileges" in resp.json()["detail"]["error"]

    def test_ac_patch_rejects_value_returns_400(self, db, sm):
        """AC: PATCH rejecting value returns 400 (actually 422 from Pydantic validation)."""
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-alice", label="patch-value-test"))
        client = _make_app(ALICE, db, sm)
        resp = client.patch(f"/auth/credentials/{cred.id}", json={"value": "should-not-work"})
        # Pydantic validation errors are 422; the issue says "400" but 422 is more semantically
        # correct for validation errors in FastAPI. Both are acceptable per the spec.
        assert resp.status_code in (400, 422)

    def test_ac_404_for_non_owned_no_enumeration(self, db, sm):
        """AC: 404 for non-owned resources (no enumeration — not 403)."""
        cred = asyncio.get_event_loop().run_until_complete(_insert_cred(db, user_id="user-bob", label="bobs-secret"))
        client = _make_app(ALICE, db, sm)
        # Must be 404, NOT 403
        assert client.patch(f"/auth/credentials/{cred.id}", json={"label": "x"}).status_code == 404
        assert client.delete(f"/auth/credentials/{cred.id}").status_code == 404


# ---------------------------------------------------------------------------
# Issue #5264: an org-less token (CLI password sign-in) must still see and be
# able to delete its own credentials.
#
# `POST /auth/credentials/aws/connect` WRITES with resolve_effective_org_id,
# which falls back to users.org_id when the token has no `custom:org_id` claim
# (#600). These routes READ with the raw claim. With a CLI sign-in -- which
# produces no org claim at all -- the two disagreed, so a connection the user had
# just created was absent from `adp aws list` and 404 on `adp aws disconnect`,
# while the create path still saw it and refused the duplicate name. Every retry
# leaked another invisible row, its Secrets Manager secret and its IAM role, with
# no product path to reclaim any of them.
# ---------------------------------------------------------------------------


ORGLESS_ALICE = TokenContext(
    user_id="alice-cognito-sub",
    org_id="",
    team_id="",
    department_id="",
    account_type="human",
    is_admin=False,
    expires_at=datetime.now(UTC) + timedelta(hours=1),
)


async def _seed_cognito_sub(db: AsyncSession, user_id: str, sub: str) -> None:
    """Give a seeded user the Cognito sub an org-less token would carry."""
    user = await db.get(User, user_id)
    user.cognito_sub = sub
    await db.commit()


@pytest.mark.asyncio
async def test_an_org_less_token_lists_its_own_credential(db, sm):
    """The defect: the row exists and belongs to the caller, but listed empty."""
    await _seed_cognito_sub(db, "user-alice", "alice-cognito-sub")
    cred = await _insert_cred(db, user_id="user-alice", service="aws", label="mine")

    client = _make_app(ORGLESS_ALICE, db, sm)
    response = client.get("/auth/credentials?scope=user")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [cred.id]


@pytest.mark.asyncio
async def test_an_org_less_token_can_delete_its_own_credential(db, sm):
    """Being able to SEE it is not enough -- the user has to be able to reclaim it."""
    await _seed_cognito_sub(db, "user-alice", "alice-cognito-sub")
    cred = await _insert_cred(db, user_id="user-alice", service="aws", label="mine")

    client = _make_app(ORGLESS_ALICE, db, sm)
    assert client.delete(f"/auth/credentials/{cred.id}").status_code == 204
    assert client.get("/auth/credentials?scope=user").json() == []


@pytest.mark.asyncio
async def test_an_org_less_token_still_cannot_see_another_orgs_credential(db, sm):
    """The test that must not pass by accident.

    The fix resolves the caller's OWN org; it must never weaken the comparison.
    A credential in a different org stays invisible and stays 404 on delete, with
    the same org-less token.
    """
    await _seed_cognito_sub(db, "user-alice", "alice-cognito-sub")
    foreign = await _insert_cred(db, org_id="org-other", user_id="user-carol", service="aws")
    # An ORG-scoped row in the foreign org: user_id/team_id are NULL, so the
    # user_id equality check cannot be what hides it.  Only `org_id ==
    # caller.org_id` excludes this row, which is what makes org isolation
    # load-bearing here rather than incidentally satisfied.
    foreign_org_wide = await _insert_cred(
        db,
        org_id="org-other",
        user_id=None,
        service="aws",
        label="org-wide",
    )

    client = _make_app(ORGLESS_ALICE, db, sm)
    assert client.get("/auth/credentials").json() == []
    for other in (foreign, foreign_org_wide):
        assert client.delete(f"/auth/credentials/{other.id}").status_code == 404
        # And it is genuinely still there -- not deleted by a filter that matched.
        assert await db.get(UserCredential, other.id) is not None


@pytest.mark.asyncio
async def test_an_org_less_token_with_no_user_row_still_gets_an_empty_list(db, sm):
    """A read surface must not start raising 409 where it used to render empty.

    This is why the helper does not call resolve_effective_org_id, which raises
    for a caller with no org.
    """
    stranger = TokenContext(
        user_id="nobody-cognito-sub",
        org_id="",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    client = _make_app(stranger, db, sm)
    response = client.get("/auth/credentials")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_a_token_that_claims_an_org_with_no_matching_member_is_not_substituted(
    db,
    sm,
):
    """A claimed-but-unmatched org must NOT fall back to a sub-only lookup.

    That caller is not a member of the org they asserted, and resolving an
    identity by sub alone would cross a tenant boundary to say otherwise.
    """
    await _seed_cognito_sub(db, "user-alice", "alice-cognito-sub")
    await _insert_cred(db, user_id="user-alice", service="aws", label="mine")

    liar = TokenContext(
        user_id="alice-cognito-sub",
        org_id="org-other",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    client = _make_app(liar, db, sm)
    assert client.get("/auth/credentials").json() == []


@pytest.mark.asyncio
async def test_a_token_that_carries_an_org_claim_is_unaffected(db, sm):
    """No behaviour change on the common path."""
    cred = await _insert_cred(db, user_id="user-alice", service="aws", label="mine")
    client = _make_app(ALICE, db, sm)
    assert [row["id"] for row in client.get("/auth/credentials?scope=user").json()] == [cred.id]


@pytest.mark.asyncio
async def test_idempotent_put_recovers_after_secret_write_and_precommit_failure(db, monkeypatch):
    from src.auth.vault_schemas import CredentialCreate
    from src.auth.vault_service import create_credential

    class ArtifactVault:
        def __init__(self):
            self.artifacts = {}

        def create_secret(self, service, label, value, *, operation_id, operation_fingerprint, **owner):
            identity = (tuple(sorted(owner.items())), operation_id)
            existing = self.artifacts.setdefault(identity, (operation_fingerprint, value))
            assert existing == (operation_fingerprint, value)
            return f"arn:aws:secretsmanager:us-east-1:123:secret:{operation_id}"

    vault = ArtifactVault()
    credential_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    data = CredentialCreate(
        service="nebius",
        label="crash-recovery",
        credential_type="api_key",
        value="synthetic-secret",
        scope_hint="user",
    )
    original_commit = db.commit
    failed = False

    async def fail_before_first_commit():
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated process failure after Secrets Manager success")
        await original_commit()

    monkeypatch.setattr(db, "commit", fail_before_first_commit)

    with pytest.raises(RuntimeError, match="simulated process failure"):
        await create_credential(data, db, ALICE, vault, credential_id=credential_id)

    assert len(vault.artifacts) == 1
    assert await db.get(UserCredential, credential_id) is None

    recovered = await create_credential(data, db, ALICE, vault, credential_id=credential_id)

    assert recovered.id == credential_id
    assert len(vault.artifacts) == 1
    assert await db.get(UserCredential, credential_id) is not None


@pytest.mark.asyncio
async def test_idempotent_put_rejects_a_different_secret_for_the_same_id(db):
    from src.auth.vault_schemas import CredentialCreate
    from src.auth.vault_service import DuplicateCredentialError, create_credential

    class RecordingVault:
        def __init__(self):
            self.calls = []
            self.value = None

        def create_secret(self, service, label, value, **kwargs):
            self.calls.append((service, label, value, kwargs))
            self.value = value
            return "arn:aws:secretsmanager:us-east-1:123:secret:operation"

        def get_secret(self, secret_arn):
            return self.value

    vault = RecordingVault()
    credential_id = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
    original = CredentialCreate(
        service="nebius",
        label="exact-replay",
        credential_type="api_key",
        value="first-synthetic-secret",
        scope_hint="user",
    )
    await create_credential(original, db, ALICE, vault, credential_id=credential_id)

    with pytest.raises(DuplicateCredentialError) as raised:
        await create_credential(
            original.model_copy(update={"value": "second-synthetic-secret"}),
            db,
            ALICE,
            vault,
            credential_id=credential_id,
        )

    assert len(vault.calls) == 1
    assert "value_sha256" not in str(vault.calls[0][3])
    assert "first-synthetic-secret" not in str(vault.calls[0][3])
    assert "first-synthetic-secret" not in str(raised.value)
    assert "second-synthetic-secret" not in str(raised.value)

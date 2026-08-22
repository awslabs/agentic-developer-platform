"""Shared-scope credential mutation authorization (Issue #3989, f-7800b0cc).

Child E of sub-EPIC #3984. ``vault_service._get_owned_credential`` returned any
credential the caller could *see*, and both mutation call sites (PATCH, DELETE)
went through it — so every org member could rewrite the label of, or
irrecoverably destroy, an org- or team-scoped (shared) credential that only an
admin was allowed to create in the first place. Visibility is not authority to
mutate.

These tests run the REAL ``AccessControl`` against a real session with
``rbac_least_privilege_default=True`` (#3987 PR 2's default), because the gate is
LATENT under the current shipped default: a caller with no
``tenant_memberships`` row still resolves to ORG_ADMIN. Without flipping the
config here the 403 assertions would silently pass for the wrong reason.

Also pinned here:
  * an org ADMIN must still be able to manage their own org's shared credentials
    (the gate is a permission check, not ``caller.is_admin`` — that claim means
    PLATFORM admin and would lock every tenant admin out of their own secrets)
  * ``domain_app`` scope keeps 404, not 403 — its visibility is admin-only, so a
    403 would newly confirm the row exists
  * user-scoped CRUD by the owner is untouched
  * ``delete_secret`` is called with ``force=False`` on the user-driven delete
    path, and keeps ``force=True`` on the create-orphan cleanup path
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.config import AdminConfig, set_admin_config
from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import CredentialType, UserCredential
from src.shared.schemas.auth import TokenContext

ORG = "org-acme"
TEAM = "team-eng"

# Cognito subs: TokenContext.user_id carries the JWT `sub`, and
# vault_routes._resolve_user_id_in_context rewrites it to users.id before the
# service runs. Both identifiers must resolve to the same membership row.
MEMBER_SUB = "sub-member"
ORG_ADMIN_SUB = "sub-orgadmin"
PLATFORM_SUB = "sub-platform"


def _context(sub: str, *, is_admin: bool = False) -> TokenContext:
    return TokenContext(
        user_id=sub,
        org_id=ORG,
        team_id=TEAM,
        department_id="dept-eng",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def least_privilege_config():
    """Run every test in this module with #3987 PR 2's least-privilege default.

    The shared-scope gate resolves the caller's role through AccessControl, so
    under the shipped default (False) a membership-less caller is still
    ORG_ADMIN and every 403 below would be unreachable.
    """
    set_admin_config(AdminConfig(rbac_least_privilege_default=True, rbac_role_cache_ttl_seconds=30.0))
    yield
    set_admin_config(AdminConfig())


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    """Seed one org with a plain member, an org admin, and a platform admin."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Organization(
                    id=ORG,
                    name="Acme Corp",
                    aws_accounts=["123456789012"],
                    role_mappings={},
                    settings={},
                    github_installation_ids=[],
                    cognito_client_ids=[],
                ),
                Department(id="dept-eng", org_id=ORG, name="Engineering"),
                Team(id=TEAM, org_id=ORG, department_id="dept-eng", name="Eng Team"),
                User(id="pg-member", org_id=ORG, team_id=TEAM, email="member@acme.com", cognito_sub=MEMBER_SUB),
                User(id="pg-orgadmin", org_id=ORG, team_id=TEAM, email="orgadmin@acme.com", cognito_sub=ORG_ADMIN_SUB),
                User(id="pg-platform", org_id=ORG, team_id=TEAM, email="platform@acme.com", cognito_sub=PLATFORM_SUB),
            ]
        )
        await session.flush()
        session.add_all(
            [
                TenantMembership(user_id="pg-member", tenant_id=ORG, role="member", is_active=True, joined_via="org_membership"),
                TenantMembership(user_id="pg-orgadmin", tenant_id=ORG, role="org_admin", is_active=True, joined_via="org_membership"),
            ]
        )
        await session.commit()
        yield session


@pytest.fixture
def sm() -> MagicMock:
    mock = MagicMock()
    mock.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:123456789012:secret:new-arn"
    mock.delete_secret.return_value = None
    return mock


def _client(caller: TokenContext, db_session: AsyncSession, sm_mock: Any) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    async def _get_caller():
        return caller

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_context] = _get_caller
    app.dependency_overrides[get_secrets_manager] = lambda: sm_mock

    return TestClient(app, raise_server_exceptions=False)


async def _insert_cred(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    team_id: str | None = None,
    domain_app_id: str | None = None,
    label: str = "shared",
    secret_arn: str = "arn:aws:secretsmanager:us-east-1:123456789012:secret:seeded",
) -> UserCredential:
    cred = UserCredential(
        org_id=ORG,
        user_id=user_id,
        team_id=team_id,
        domain_app_id=domain_app_id,
        service="github",
        credential_type=CredentialType.api_key,
        label=label,
        secret_arn=secret_arn,
        strict=False,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return cred


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestMemberCannotMutateSharedCredentials:
    """The finding: a plain member could PATCH/DELETE org- and team-scoped creds."""

    def test_member_patch_org_cred_returns_403(self, db, sm):
        cred = _run(_insert_cred(db, label="org-wide-key"))
        resp = _client(_context(MEMBER_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "hijacked"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "insufficient_privileges"

    def test_member_delete_org_cred_returns_403(self, db, sm):
        cred = _run(_insert_cred(db, label="org-wide-key"))
        resp = _client(_context(MEMBER_SUB), db, sm).delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 403
        # The destructive half of the finding: the SM secret must not be touched.
        sm.delete_secret.assert_not_called()

    def test_member_patch_team_cred_returns_403(self, db, sm):
        cred = _run(_insert_cred(db, team_id=TEAM, label="team-bot-token"))
        resp = _client(_context(MEMBER_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "hijacked"})
        assert resp.status_code == 403

    def test_member_delete_team_cred_returns_403(self, db, sm):
        cred = _run(_insert_cred(db, team_id=TEAM, label="team-bot-token"))
        resp = _client(_context(MEMBER_SUB), db, sm).delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 403
        sm.delete_secret.assert_not_called()

    def test_denied_delete_leaves_db_row_intact(self, db, sm):
        """403 must be a real refusal, not a 403 after the row was already gone."""
        cred = _run(_insert_cred(db, label="org-survivor"))
        cred_id = cred.id
        assert _client(_context(MEMBER_SUB), db, sm).delete(f"/auth/credentials/{cred_id}").status_code == 403

        from sqlalchemy import select

        row = _run(db.execute(select(UserCredential).where(UserCredential.id == cred_id))).scalar_one_or_none()
        assert row is not None


class TestOrgAdminRetainsAccess:
    """Regression guard for the gate itself: it must not lock out tenant admins.

    ``TokenContext.is_admin`` is PLATFORM-only, so gating on it would mean only a
    platform operator could rotate a tenant's shared secret. These tests fail if
    the gate is ever narrowed back to that claim.
    """

    def test_org_admin_can_patch_org_cred(self, db, sm):
        cred = _run(_insert_cred(db, label="org-wide-key"))
        resp = _client(_context(ORG_ADMIN_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "rotated"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["label"] == "rotated"

    def test_org_admin_can_delete_org_cred(self, db, sm):
        cred = _run(_insert_cred(db, label="org-wide-key", secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:org-key"))
        resp = _client(_context(ORG_ADMIN_SUB), db, sm).delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 204, resp.text

    def test_org_admin_can_patch_team_cred(self, db, sm):
        cred = _run(_insert_cred(db, team_id=TEAM, label="team-bot-token"))
        resp = _client(_context(ORG_ADMIN_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "rotated"})
        assert resp.status_code == 200, resp.text

    def test_platform_admin_can_delete_org_cred(self, db, sm):
        """A platform admin has no membership row; the token claim still suffices."""
        cred = _run(_insert_cred(db, label="org-wide-key"))
        resp = _client(_context(PLATFORM_SUB, is_admin=True), db, sm).delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 204, resp.text


class TestDomainAppScopeKeeps404:
    """Item 7 of the approved design: domain_app must NOT switch to 403."""

    def test_member_patch_domain_app_cred_returns_404(self, db, sm):
        cred = _run(_insert_cred(db, domain_app_id="app-cyber", label="misp-key"))
        resp = _client(_context(MEMBER_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "x"})
        assert resp.status_code == 404

    def test_org_admin_patch_domain_app_cred_returns_404(self, db, sm):
        """domain_app visibility is platform-admin-only, so even an org admin gets 404."""
        cred = _run(_insert_cred(db, domain_app_id="app-cyber", label="misp-key"))
        resp = _client(_context(ORG_ADMIN_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "x"})
        assert resp.status_code == 404

    def test_platform_admin_can_patch_domain_app_cred(self, db, sm):
        cred = _run(_insert_cred(db, domain_app_id="app-cyber", label="misp-key"))
        resp = _client(_context(PLATFORM_SUB, is_admin=True), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "rotated"})
        assert resp.status_code == 200, resp.text


class TestUserScopeUnaffected:
    """The gate must only bite on shared scopes."""

    def test_member_can_patch_own_user_cred(self, db, sm):
        cred = _run(_insert_cred(db, user_id="pg-member", label="my-pat"))
        resp = _client(_context(MEMBER_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "my-pat-2"})
        assert resp.status_code == 200, resp.text

    def test_member_can_delete_own_user_cred(self, db, sm):
        cred = _run(_insert_cred(db, user_id="pg-member", label="my-pat"))
        resp = _client(_context(MEMBER_SUB), db, sm).delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 204, resp.text

    def test_member_still_gets_404_for_another_users_cred(self, db, sm):
        """Enumeration protection on the user scope is unchanged (404, not 403)."""
        cred = _run(_insert_cred(db, user_id="pg-orgadmin", label="not-mine"))
        assert _client(_context(MEMBER_SUB), db, sm).patch(f"/auth/credentials/{cred.id}", json={"label": "x"}).status_code == 404
        assert _client(_context(MEMBER_SUB), db, sm).delete(f"/auth/credentials/{cred.id}").status_code == 404


class TestDeleteRecoveryWindow:
    """Item 3: force=False on the user-driven delete, force=True on orphan cleanup."""

    def test_user_delete_preserves_recovery_window(self, db, sm):
        arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:recoverable"
        cred = _run(_insert_cred(db, user_id="pg-member", label="recoverable", secret_arn=arn))
        resp = _client(_context(MEMBER_SUB), db, sm).delete(f"/auth/credentials/{cred.id}")
        assert resp.status_code == 204
        # force=False → AWS's 7-30 day recovery window applies. The helper default
        # is force=True (ForceDeleteWithoutRecovery), which made a mis-click or a
        # compromised session unrecoverable.
        sm.delete_secret.assert_called_once_with(arn, force=False)

    def test_orphan_cleanup_still_force_deletes(self, db, sm):
        """The duplicate-create rollback path must stay force=True.

        The credential was never created, so the value the user just submitted
        must be destroyed immediately rather than parked in a recovery window.
        """
        _run(_insert_cred(db, user_id="pg-member", label="dup", secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:existing"))
        orphan_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:orphan"
        sm.create_secret.return_value = orphan_arn

        resp = _client(_context(MEMBER_SUB), db, sm).post(
            "/auth/credentials",
            json={"service": "github", "label": "dup", "credential_type": "api_key", "value": "v", "scope_hint": "user"},
        )

        assert resp.status_code == 409
        sm.delete_secret.assert_called_once_with(orphan_arn)

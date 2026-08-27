"""Tests for POST /internal/v1/github-installation-token — the GitHub-token gatekeeper.

Issue #4272 (·A-3 Phase 1). The platform GitHub App private key used to be
exported into every agent subprocess so the worker could re-mint before the
1-hour expiry. This route replaces that: the key stays in the gateway, and a run
gets a short-lived token scoped to its OWN installation and the one repo it was
assigned.

Coverage, in order of what actually protects the tenant boundary:
  - 403 when the run's registry row binds a DIFFERENT installation (confused
    deputy — the caller's installation_id is attacker-influenceable)
  - 403 when the row carries NO installation_id (fail-closed on absence; the
    attribute is written conditionally at ingress, so this state is reachable)
  - 403 when the row is missing entirely, and when no invocation_id is sent
  - The binding does NOT consult ENFORCE_CREDENTIAL_BINDING (false on at least
    one live environment — a control gated on it silently shadows)
  - 409 when the installation resolves cleanly to another tenant
  - 200 returns expires_at from GITHUB's response, not a local now+1h guess
  - The mint request is least-privilege: repositories == [this repo] and a
    permissions map no broader than AGENT_RUN_PERMISSIONS
  - Audit row written on mint AND on denial
  - 403 unauthenticated
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.routes import router
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key"

_OWNER_TENANT = "org-acme"
_OTHER_TENANT = "org-globex"
_BOUND_INSTALLATION = 555001
_FOREIGN_INSTALLATION = 555002

_INVOCATION_ID = "evt-abc-123"

_GITHUB_EXPIRES_AT = "2026-08-27T16:45:00Z"


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


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
        owner = Organization(
            id=_OWNER_TENANT,
            name="Acme",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[str(_BOUND_INSTALLATION)],
            cognito_client_ids=[],
            github_org_id="acme",
        )
        other = Organization(
            id=_OTHER_TENANT,
            name="Globex",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[str(_FOREIGN_INSTALLATION)],
            cognito_client_ids=[],
            github_org_id="globex",
        )
        session.add_all([owner, other])
        # channel_tenant_map is the CORROBORATED record of ownership — written
        # server-side from what GitHub supplied at install. github_installation_ids
        # alone is client-writable, so the resolver treats it as a self-assertion
        # and returns UNATTESTABLE on the unattested path. Both records must exist
        # for a real installation, so both are seeded here.
        session.add_all(
            [
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id="acme-account",
                    org_id=_OWNER_TENANT,
                    installation_id=str(_BOUND_INSTALLATION),
                ),
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id="globex-account",
                    org_id=_OTHER_TENANT,
                    installation_id=str(_FOREIGN_INSTALLATION),
                ),
            ]
        )
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock(*, enforce_credential_binding: bool = False) -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    s.webhook_events_table = "adp-test-webhook-events"
    # The installation binding must be independent of this flag. Tests set it
    # False (its real value on embark1) precisely so that a guard accidentally
    # gated on it would fail these tests instead of shadowing in production.
    s.enforce_credential_binding = enforce_credential_binding
    return s


def _ddb_table_mock(item: dict | None) -> MagicMock:
    """Mock the webhook-events DDB Table. ``None`` -> row absent."""
    table = MagicMock()
    table.query.return_value = {"Items": [item] if item is not None else []}
    return table


def _bound_row(
    *,
    installation_id: int | None = _BOUND_INSTALLATION,
    tenant_id: str = _OWNER_TENANT,
) -> dict:
    row: dict = {"tenant_id": tenant_id, "arrived_at": "2026-08-27T15:00:00Z"}
    if installation_id is not None:
        row["installation_id"] = installation_id
    return row


def _body(installation_id: int = _BOUND_INSTALLATION, **over) -> dict:
    payload = {
        "installation_id": installation_id,
        "repo_owner": "acme",
        "repo_name": "widgets",
        "invocation_id": _INVOCATION_ID,
    }
    payload.update(over)
    return payload


def _post(
    db_session: AsyncSession,
    *,
    row: dict | None = _bound_row(),
    body: dict | None = None,
    mint: AsyncMock | None = None,
    settings: MagicMock | None = None,
    headers: dict | None = None,
):
    """POST the route with DDB + mint + credential resolution stubbed out."""
    settings = settings or _settings_mock()
    mint = mint or AsyncMock(return_value=("ghs_brokered_token", _GITHUB_EXPIRES_AT))
    client = _make_app(db_session)

    with (
        patch("src.internal.routes.get_settings", return_value=settings),
        patch("src.internal.auth_deps.get_settings", return_value=settings),
        patch(
            "src.internal.credential_binding._get_dynamodb_table",
            return_value=_ddb_table_mock(row),
        ),
        patch(
            "src.internal.routes.resolve_tenant_app_credentials",
            new=AsyncMock(return_value=("99001", "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----")),
        ),
        patch("src.internal.routes.mint_installation_token_with_expiry", new=mint),
    ):
        resp = client.post(
            "/internal/v1/github-installation-token",
            json=body if body is not None else _body(),
            headers=headers if headers is not None else {"X-Internal-Api-Key": _VALID_KEY},
        )
    return resp, mint


async def _audit_rows(db: AsyncSession, event_type: str) -> list[AuditLog]:
    result = await db.execute(select(AuditLog).where(AuditLog.event_type == event_type))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Confused-deputy protection — the reason this route exists
# ---------------------------------------------------------------------------


class TestInstallationBindingEnforced:
    @pytest.mark.asyncio
    async def test_mismatched_installation_is_rejected(self, db):
        """A run bound to installation A cannot mint for installation B.

        The worker takes installation_id verbatim from the SQS envelope, so it is
        attacker-influenceable. Without this check the gatekeeper would be a
        cleaner privilege-escalation primitive than the bug it replaces.
        """
        resp, mint = _post(db, body=_body(installation_id=_FOREIGN_INSTALLATION))

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "installation_binding_mismatch"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_row_without_installation_id_is_rejected(self, db):
        """Fail-closed on absence, not fail-open.

        installation_id is written conditionally into the webhook-events row, so
        a row legitimately may not carry one. That must be a 403, not a 200.
        """
        resp, mint = _post(db, row=_bound_row(installation_id=None))

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "installation_binding_failed"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_registry_row_is_rejected(self, db):
        resp, mint = _post(db, row=None)

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_invocation_id_is_rejected(self, db):
        body = _body()
        body.pop("invocation_id")
        resp, mint = _post(db, body=body)

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_binding_enforces_even_when_enforce_flag_is_false(self, db):
        """The guard must not be gated on ENFORCE_CREDENTIAL_BINDING.

        That flag is false on at least one live environment. A control behind it
        shadows instead of enforcing — which would leave this route mintable for
        an arbitrary installation while every unit test still passed.
        """
        resp, mint = _post(
            db,
            body=_body(installation_id=_FOREIGN_INSTALLATION),
            settings=_settings_mock(enforce_credential_binding=False),
        )

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_denial_is_audited(self, db):
        """An ownership denial leaves a trail — org_id is claimed by nobody here."""
        # Row binds an installation that no organization row claims -> NOT_FOUND.
        resp, mint = _post(
            db,
            row=_bound_row(installation_id=999999),
            body=_body(installation_id=999999),
        )

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()
        rows = await _audit_rows(db, "github_installation_token_denied")
        assert len(rows) == 1
        assert rows[0].details["reason"] == "ownership_check_failed"


class TestOwnershipCheck:
    @pytest.mark.asyncio
    async def test_installation_owned_by_another_tenant_is_conflict(self, db):
        """Row claims tenant T for an installation that provably belongs to U.

        Distinct from a binding mismatch: the request agrees with the row, but the
        row itself is wrong. Fails closed rather than trusting the row.
        """
        resp, mint = _post(
            db,
            row=_bound_row(installation_id=_FOREIGN_INSTALLATION, tenant_id=_OWNER_TENANT),
            body=_body(installation_id=_FOREIGN_INSTALLATION),
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "installation_not_owned"
        mint.assert_not_awaited()


# ---------------------------------------------------------------------------
# Happy path — expiry passthrough and least privilege
# ---------------------------------------------------------------------------


class TestMintHappyPath:
    @pytest.mark.asyncio
    async def test_returns_token_and_githubs_expires_at(self, db):
        """expires_at must be GitHub's value, not a locally-computed now+1h.

        The TS TokenManager schedules its refresh off this. A guess drifts, and
        the run dies mid-flight at the real expiry.
        """
        resp, mint = _post(db)

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["token"] == "ghs_brokered_token"
        assert payload["expires_at"] == _GITHUB_EXPIRES_AT
        # The App ID is public (only the private key is secret) and the worker still
        # needs it: it sets GH_APP_ID, which the TS canInitTokenManager() requires in
        # both modes, and it forms the bot commit identity. In broker mode the pod
        # does no vault read, so this response is the only place it can come from —
        # omitting it recreates the same silent 1-hour token-refresh death.
        assert payload["app_id"] == "99001"
        mint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mint_is_scoped_to_the_single_repo_and_minimal_permissions(self, db):
        """Least privilege: this repo only, and only the verbs an agent run needs."""
        from src.knowledge.github_app_service import AGENT_RUN_PERMISSIONS

        resp, mint = _post(db)

        assert resp.status_code == 200, resp.text
        kwargs = mint.await_args.kwargs
        assert kwargs["repositories"] == ["widgets"], "token must be scoped to the run's one repo, not all installation repos"

        permissions = kwargs["permissions"]
        assert permissions, "a mint with no permissions map inherits the App's full grant"
        assert set(permissions) <= set(AGENT_RUN_PERMISSIONS)
        # Nothing that would let a hijacked run change the org or its automation.
        for forbidden in ("administration", "members", "secrets", "workflows", "actions"):
            assert forbidden not in permissions

    @pytest.mark.asyncio
    async def test_mint_uses_the_bound_installation_not_the_request(self, db):
        """The minted installation comes from the registry row, positionally."""
        resp, mint = _post(db)

        assert resp.status_code == 200, resp.text
        assert mint.await_args.args[2] == _BOUND_INSTALLATION

    @pytest.mark.asyncio
    async def test_mint_is_audited(self, db):
        resp, _ = _post(db)

        assert resp.status_code == 200, resp.text
        rows = await _audit_rows(db, "github_installation_token_minted")
        assert len(rows) == 1
        assert rows[0].org_id == _OWNER_TENANT
        assert rows[0].details["installation_id"] == _BOUND_INSTALLATION
        assert rows[0].details["repositories"] == ["widgets"]
        assert rows[0].details["expires_at"] == _GITHUB_EXPIRES_AT
        # The token itself must never be audited.
        assert "token" not in rows[0].details


class TestMintFailure:
    @pytest.mark.asyncio
    async def test_github_rejection_is_502_and_audited(self, db):
        resp, _ = _post(db, mint=AsyncMock(side_effect=RuntimeError("GitHub said no")))

        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]["error"] == "mint_failed"
        rows = await _audit_rows(db, "github_installation_token_denied")
        assert len(rows) == 1
        assert rows[0].details["reason"] == "mint_failed"

    @pytest.mark.asyncio
    async def test_no_token_in_error_response(self, db):
        """A mint failure must not echo GitHub's body (it can carry credentials)."""
        resp, _ = _post(db, mint=AsyncMock(side_effect=RuntimeError("ghs_leaked_secret")))

        assert resp.status_code == 502
        assert "ghs_leaked_secret" not in resp.text


class TestAuthn:
    @pytest.mark.asyncio
    async def test_unauthenticated_is_rejected(self, db):
        resp, mint = _post(db, headers={})

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_internal_key_is_rejected(self, db):
        resp, mint = _post(db, headers={"X-Internal-Api-Key": "nope"})

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

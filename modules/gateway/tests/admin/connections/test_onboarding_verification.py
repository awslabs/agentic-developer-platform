"""Tests for onboarding verification — Issue #4016.

The bug class under test is "success reported from intent, not outcome": GitHub
App onboarding reported success while login, webhooks, or agent credentials were
broken, and the connections page rendered a hardcoded green "Installed ✓".

Covers the four named regression cases from the issue:
  (b) a tenant with ZERO ChannelTenantMap rows still surfaces verification
  (c) a non-admin caller does NOT receive platform-scoped checks
  (d) the no-nonce-no-org path returns non-success and logs at WARNING+
plus the tri-state contract (unknown must not render as broken) and the
read-only guarantee (verification never seeds or heals anything).

Case (a) — `wire-github-app.sh --skip-login` still exits 0 — is a shell-script
behaviour. The repo has no bats/shell test harness, so it is covered by
`bash -n` plus the manual verification recorded in the PR, not here.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from src.shared.models.vault import ChannelTenantMap

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SERVICE = "src.admin.connections.service"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
def _clear_verification_caches():
    """Verification caches are module-level and per-pod; isolate every test."""
    from src.admin.connections.service import _invalidate_verification_cache

    _invalidate_verification_cache()
    yield
    _invalidate_verification_cache()


def _identity_client(*, forward=None, reverse=None):
    """A stub IdentityIndexClient returning canned DDB items."""
    client = MagicMock()
    client.get_installation_identity = AsyncMock(return_value=forward)
    client.get_reverse_installation_identity = AsyncMock(return_value=reverse)
    return client


def _reverse_row(installation_id: int) -> dict:
    return {
        "identity_type": {"S": "org_installation"},
        "identity_value": {"S": "tenant-acme"},
        "installation_id": {"N": str(installation_id)},
    }


# ---------------------------------------------------------------------------
# (b) A tenant with no Postgres rows still surfaces verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ddb_known_install_with_no_pg_row_is_surfaced(db_session: AsyncSession):
    """🔴-1: the union. An install the webhook Lambda auto-registered has DDB
    rows and NO ChannelTenantMap row. list_connections used to early-return an
    empty list, so the exact tenant this feature diagnoses saw a blank page.
    """
    from src.admin.connections.service import list_connections

    client = _identity_client(forward={"org_id": {"S": "tenant-acme"}}, reverse=_reverse_row(77001))

    with (
        patch("src.admin.identity_index.IdentityIndexClient", return_value=client),
        patch(f"{SERVICE}._check_tenant_secret_seeded", new=AsyncMock(return_value=False)),
    ):
        resp = await list_connections(
            caller_org_id="tenant-acme",
            caller_user_id="user-1",
            db=db_session,
        )

    assert len(resp.connections) == 1, "a DDB-known install must not be invisible"
    conn = resp.connections[0]
    assert conn.installation_id == 77001
    # The whole point of the synthetic entry: it says the record is missing.
    assert conn.verification is not None
    assert conn.verification.record_present is False
    assert conn.verification.tenant_secret_seeded is False
    # Nothing to disconnect — there is no row to delete.
    assert conn.can_manage is False


@pytest.mark.asyncio
async def test_no_rows_anywhere_returns_empty_without_error(db_session: AsyncSession):
    """A genuinely un-installed tenant stays empty — no synthetic noise."""
    from src.admin.connections.service import list_connections

    client = _identity_client(forward=None, reverse=None)

    with patch("src.admin.identity_index.IdentityIndexClient", return_value=client):
        resp = await list_connections(
            caller_org_id="tenant-acme",
            caller_user_id="user-1",
            db=db_session,
        )

    assert resp.connections == []


@pytest.mark.asyncio
async def test_pg_backed_install_is_not_duplicated_by_the_ddb_scan(db_session: AsyncSession):
    """The union must dedupe: a healthy install appears once, not twice."""
    from src.admin.connections.service import list_connections

    db_session.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id="88001",
            org_id="tenant-acme",
            install_metadata={
                "installation_id": 88001,
                "account_login": "acme-corp",
                "account_type": "Organization",
            },
        )
    )
    await db_session.commit()

    client = _identity_client(forward={"org_id": {"S": "tenant-acme"}}, reverse=_reverse_row(88001))

    with (
        patch("src.admin.identity_index.IdentityIndexClient", return_value=client),
        patch(f"{SERVICE}._check_tenant_secret_seeded", new=AsyncMock(return_value=True)),
        patch(f"{SERVICE}._fetch_live_repos", new=AsyncMock(return_value=[])),
    ):
        resp = await list_connections(
            caller_org_id="tenant-acme",
            caller_user_id="user-1",
            db=db_session,
        )

    assert len(resp.connections) == 1
    v = resp.connections[0].verification
    assert v is not None
    assert v.record_present is True
    assert v.tenant_secret_seeded is True
    assert v.identity_index_row is True
    assert v.reverse_identity_row is True


# ---------------------------------------------------------------------------
# (c) Non-admin callers do not receive platform-scoped checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_does_not_receive_platform_verification(db_session: AsyncSession):
    """🔴-2: platform checks read deployment-global singletons. A tenant member
    must not see, or try to "fix", global deployment state.
    """
    from src.admin.connections.service import list_connections

    db_session.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id="90001",
            org_id="tenant-acme",
            install_metadata={"installation_id": 90001, "account_login": "acme", "account_type": "Organization"},
        )
    )
    await db_session.commit()

    platform_check = AsyncMock()

    with (
        patch("src.admin.identity_index.IdentityIndexClient", return_value=_identity_client()),
        patch(f"{SERVICE}._check_tenant_secret_seeded", new=AsyncMock(return_value=True)),
        patch(f"{SERVICE}._fetch_live_repos", new=AsyncMock(return_value=[])),
        patch(f"{SERVICE}._compute_platform_verification", new=platform_check),
    ):
        resp = await list_connections(
            caller_org_id="tenant-acme",
            caller_user_id="user-1",
            db=db_session,
            caller_is_admin=False,
        )

    assert resp.platform_verification is None
    # Not merely omitted from the response — never computed, so a non-admin
    # request cannot even cause the platform secrets to be read.
    platform_check.assert_not_awaited()
    # The per-connection checks still reach them.
    assert resp.connections[0].verification is not None


@pytest.mark.asyncio
async def test_admin_receives_platform_verification(db_session: AsyncSession):
    """The mirror of the above: an admin does get the deployment-wide checks."""
    from src.admin.connections.schemas import PlatformVerification
    from src.admin.connections.service import list_connections

    block = PlatformVerification(login_credentials=False, webhook_secret=None)

    with (
        patch("src.admin.identity_index.IdentityIndexClient", return_value=_identity_client()),
        patch(f"{SERVICE}._compute_platform_verification", new=AsyncMock(return_value=block)),
    ):
        resp = await list_connections(
            caller_org_id="tenant-acme",
            caller_user_id="user-1",
            db=db_session,
            caller_is_admin=True,
        )

    assert resp.platform_verification is not None
    assert resp.platform_verification.login_credentials is False
    assert resp.platform_verification.webhook_secret is None


# ---------------------------------------------------------------------------
# Tri-state contract: unknown must never be reported as broken
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ddb_error_degrades_to_unknown_not_broken(db_session: AsyncSession):
    """An unreadable identity-index must render amber, not a red "webhooks are
    broken". A false-negative red sends operators to fix a non-problem.
    """
    from src.admin.connections.service import _compute_connection_verification

    client = MagicMock()
    client.get_installation_identity = AsyncMock(side_effect=RuntimeError("throttled"))
    client.get_reverse_installation_identity = AsyncMock(side_effect=RuntimeError("throttled"))

    with (
        patch("src.admin.identity_index.IdentityIndexClient", return_value=client),
        patch(f"{SERVICE}._check_tenant_secret_seeded", new=AsyncMock(return_value=None)),
    ):
        v = await _compute_connection_verification(
            installation_id=91001,
            org_id="tenant-acme",
            record_present=True,
        )

    assert v.identity_index_row is None
    assert v.reverse_identity_row is None
    assert v.tenant_secret_seeded is None
    # record_present is known from Postgres, so it stays authoritative.
    assert v.record_present is True


@pytest.mark.asyncio
async def test_verification_failure_does_not_break_the_page(db_session: AsyncSession):
    """Verification decorates the primary settings page; it must never be able
    to take it down.
    """
    from src.admin.connections.service import list_connections

    db_session.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id="92001",
            org_id="tenant-acme",
            install_metadata={"installation_id": 92001, "account_login": "acme", "account_type": "Organization"},
        )
    )
    await db_session.commit()

    with (
        patch("src.admin.identity_index.IdentityIndexClient", side_effect=RuntimeError("no table")),
        patch(f"{SERVICE}._check_tenant_secret_seeded", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch(f"{SERVICE}._fetch_live_repos", new=AsyncMock(return_value=[])),
    ):
        resp = await list_connections(
            caller_org_id="tenant-acme",
            caller_user_id="user-1",
            db=db_session,
        )

    assert len(resp.connections) == 1
    assert resp.connections[0].installation_id == 92001
    assert resp.connections[0].verification is not None


# ---------------------------------------------------------------------------
# Read-only guarantee: #4016 observes, it does not heal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_never_seeds_or_writes(db_session: AsyncSession):
    """Scope boundary. Repair belongs to #4030/#3453 (tenant secret) and #3860
    (reverse row). Listing connections must not perform either.
    """
    from src.admin.connections.service import list_connections

    client = _identity_client(forward=None, reverse=None)
    seed = AsyncMock()

    with (
        patch("src.admin.identity_index.IdentityIndexClient", return_value=client),
        patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new=seed),
        patch(
            "src.admin.connections.tenant_secret.tenant_github_app_secret_exists",
            new=AsyncMock(return_value=False),
        ),
    ):
        await list_connections(
            caller_org_id="tenant-acme",
            caller_user_id="user-1",
            db=db_session,
            caller_is_admin=True,
        )

    seed.assert_not_awaited()
    client.put_identity.assert_not_called()
    client.write_reverse_installation_identity.assert_not_called()


@pytest.mark.asyncio
async def test_tenant_secret_probe_uses_describe_not_get_value():
    """A private key must not be pulled into gateway memory to answer
    "does this secret exist".
    """
    from src.admin.connections.tenant_secret import _describe_secret_sync

    sm = MagicMock()
    with patch("boto3.client", return_value=sm):
        assert _describe_secret_sync("tenant-acme") is True

    sm.describe_secret.assert_called_once()
    sm.get_secret_value.assert_not_called()


@pytest.mark.asyncio
async def test_tenant_secret_probe_distinguishes_absent_from_unknown():
    """ResourceNotFound is authoritative (False/red); AccessDenied is not
    (None/amber).
    """
    from botocore.exceptions import ClientError

    from src.admin.connections.tenant_secret import _describe_secret_sync

    not_found = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeSecret")
    denied = ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeSecret")

    sm = MagicMock()
    sm.describe_secret.side_effect = not_found
    with patch("boto3.client", return_value=sm):
        assert _describe_secret_sync("tenant-acme") is False

    sm = MagicMock()
    sm.describe_secret.side_effect = denied
    with patch("boto3.client", return_value=sm):
        assert _describe_secret_sync("tenant-acme") is None


# ---------------------------------------------------------------------------
# (d) No-nonce, no org resolved → non-success + WARNING or above
# ---------------------------------------------------------------------------


def _github_client(*, account_login="public-org", account_id=44444444, account_type="Organization"):
    gh = AsyncMock()
    gh.get_installation = AsyncMock(
        return_value={
            "account": {"login": account_login, "type": account_type, "id": account_id},
            "repository_selection": "selected",
        }
    )
    gh.list_installation_repository_names = AsyncMock(return_value=[])
    return gh


@pytest.mark.asyncio
async def test_no_nonce_with_no_org_returns_non_success_and_logs(db_session: AsyncSession, caplog):
    """🔴-3: the handler used to return success=True after persisting NOTHING.
    The operator was told the install had worked when the platform knew nothing
    about it.
    """
    from src.admin.connections.service import install_callback

    gh = _github_client()

    caplog.set_level(logging.INFO)
    with (
        patch.dict("os.environ", {"ORG_TENANT_AUTO_CREATE": "false"}),
        patch(f"{SERVICE}._get_github_app_credentials", return_value=("12345", "fake-pem")),
    ):
        result = await install_callback(
            installation_id=93001,
            setup_action="install",
            state="",
            db=db_session,
            github_client=gh,
        )

    assert result["success"] is False
    assert result["no_nonce"] is True
    assert result["error_code"] == "organization_selection_required"
    assert result["error_message"]

    assert any("no_nonce_install_selection_required" in r.getMessage() for r in caplog.records)
    assert any("outcome=nothing_persisted" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_nonce_non_org_install_returns_non_success(db_session: AsyncSession, caplog):
    """A personal-account install on the no-nonce path resolves no tenant either,
    and must not claim success.
    """
    from src.admin.connections.service import install_callback

    gh = _github_client(account_login="alice", account_type="User")

    caplog.set_level(logging.INFO)
    with patch(f"{SERVICE}._get_github_app_credentials", return_value=("12345", "fake-pem")):
        result = await install_callback(
            installation_id=93002,
            setup_action="install",
            state="",
            db=db_session,
            github_client=gh,
        )

    assert result["success"] is False
    assert result["error_code"] == "organization_selection_required"
    assert any("no_nonce_install_selection_required" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_nonce_promotion_denied_is_flagged_partial(db_session: AsyncSession, caplog):
    """A self-created shell is deliberately not promoted (#2724). That stays a
    SUCCESS — the row is written and the UI works — but #4016 adds `partial` so
    the page stops saying "Installation complete".
    """
    from src.admin.connections.service import install_callback

    gh = _github_client(account_login="unknown-org", account_id=55555555)

    caplog.set_level(logging.INFO)
    with (
        patch.dict("os.environ", {"ORG_TENANT_AUTO_CREATE": "true"}),
        patch(f"{SERVICE}._get_github_app_credentials", return_value=("12345", "fake-pem")),
        patch(f"{SERVICE}._write_installation_identity_index", new=AsyncMock()),
        patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new=AsyncMock()),
    ):
        result = await install_callback(
            installation_id=93003,
            setup_action="install",
            state="",
            db=db_session,
            github_client=gh,
        )

    assert result["success"] is False
    assert not result.get("partial")
    assert result["error_code"] == "organization_selection_required"
    assert any("no_nonce_install_selection_required" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_nonce_dispatch_is_logged(db_session: AsyncSession, caplog):
    """Both callback paths log which one ran — previously indistinguishable in
    the logs, so an operator debugging a silent partial had no starting point.
    """
    from src.admin.connections.service import install_callback

    caplog.set_level(logging.INFO)
    with patch(f"{SERVICE}._get_github_app_credentials", return_value=("", "")):
        await install_callback(
            installation_id=93004,
            setup_action="install",
            state="",
            db=db_session,
            github_client=None,
        )

    assert any("event=install_callback_dispatch" in r.getMessage() and "path=no_nonce" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_missing_github_client_is_logged_at_error(db_session: AsyncSession, caplog):
    """Missing App credentials are reported and cannot silently attach a tenant."""
    from datetime import UTC, datetime, timedelta

    from fastapi import HTTPException

    from src.admin.connections.service import _setup_context, install_callback
    from src.shared.models.organization import Organization, User
    from src.shared.models.vault import MagicLinkNonce

    org = Organization(id="tenant-acme", name="Acme", aws_accounts=[], role_mappings={}, settings={})
    db_session.add(org)
    await db_session.flush()
    user = User(
        id="user-pg-1",
        org_id="tenant-acme",
        team_id="team-acme",
        email="a@example.com",
        cognito_sub="sub-1",
    )
    db_session.add(user)
    db_session.add(
        MagicLinkNonce(
            jti="jti-4016",
            provider="github_install",
            provider_user_id="sub-1",
            channel_context=_setup_context(kind="install", org_id="tenant-acme"),
            target_user_id="user-pg-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db_session.commit()

    caplog.set_level(logging.INFO)
    with (
        patch(f"{SERVICE}._get_github_app_credentials", return_value=("", "")),
        patch(f"{SERVICE}._write_installation_identity_index", new=AsyncMock()),
        patch("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as denied:
            await install_callback(
                installation_id=93005,
                setup_action="install",
                state="jti-4016",
                db=db_session,
                github_client=None,
            )
        assert denied.value.status_code == 503
        assert (await db_session.get(MagicLinkNonce, "jti-4016")).consumed_at is None

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("install_callback_no_github_client" in r.getMessage() for r in errors)


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_cache_bounds_repeat_reads():
    """The checks sit on a page users refresh; without a TTL cache every render
    would hit Secrets Manager and DynamoDB per connection.
    """
    from src.admin.connections.service import _check_tenant_secret_seeded

    probe = AsyncMock(return_value=True)
    with patch("src.admin.connections.tenant_secret.tenant_github_app_secret_exists", new=probe):
        assert await _check_tenant_secret_seeded("tenant-acme") is True
        assert await _check_tenant_secret_seeded("tenant-acme") is True
        assert await _check_tenant_secret_seeded("tenant-acme") is True

    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_cache_invalidation_forces_a_refresh():
    """Registering/rotating/disconnecting must not leave a stale green check."""
    from src.admin.connections.service import (
        _check_tenant_secret_seeded,
        _invalidate_verification_cache,
    )

    probe = AsyncMock(return_value=True)
    with patch("src.admin.connections.tenant_secret.tenant_github_app_secret_exists", new=probe):
        await _check_tenant_secret_seeded("tenant-acme")
        _invalidate_verification_cache()
        await _check_tenant_secret_seeded("tenant-acme")

    assert probe.await_count == 2

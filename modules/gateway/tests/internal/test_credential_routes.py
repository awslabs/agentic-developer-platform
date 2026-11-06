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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_binding import GatewayCredentialAuthority
from src.internal.credential_injector import sanitize_proxy_response
from src.internal.credential_routes import _identity_access_token, get_secrets_manager, router
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key"


@pytest.mark.asyncio
async def test_identity_token_exchange_requires_current_owned_consent(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from src.internal import credential_routes

    monkeypatch.setattr(credential_routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    owner = SimpleNamespace(id="user-alice", org_id="org-acme")
    pointer = SimpleNamespace(
        user_id=owner.id,
        org_id=owner.org_id,
        service="github",
        credential_type="oauth_token",
        agent_consent=True,
        expires_at=None,
        scopes={"oauth_scopes": ["read:user"]},
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
    )
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload-token"}
    identity.get_resource_oauth2_token.return_value = {"accessToken": "private-owner-token"}
    assert await _identity_access_token(pointer, owner, identity) == "private-owner-token"
    identity.get_workload_access_token_for_user_id.assert_called_once_with(workloadName="adp-tools-dev-oauth-user", userId=owner.id)
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["read:user"]
    for change in (
        {"user_id": "user-bob"},
        {"org_id": "org-other"},
        {"agent_consent": False},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"scopes": {"oauth_scopes": ["repo"]}},
    ):
        rejected = SimpleNamespace(**(pointer.__dict__ | change))
        identity.reset_mock()
        with pytest.raises(HTTPException) as exc:
            await _identity_access_token(rejected, owner, identity)
        assert exc.value.status_code == 403
        assert exc.value.detail == {"error": "not_connected"}
        identity.get_workload_access_token_for_user_id.assert_not_called()
        identity.get_resource_oauth2_token.assert_not_called()


@pytest.mark.parametrize(
    "credential_type,secret,injected",
    [
        ("bearer", "synthetic-token", "Bearer synthetic-token"),
        ("api_key", "synthetic-token", "ApiKey synthetic-token"),
        ("basic_auth", "user:synthetic-token", "Basic dXNlcjpzeW50aGV0aWMtdG9rZW4="),
    ],
)
def test_proxy_echo_sanitizer_keeps_only_safe_fields(credential_type, secret, injected):
    from urllib.parse import quote

    headers, body = sanitize_proxy_response(
        credential_type,
        secret,
        {"Content-Type": "text/plain", "X-Secret": secret, "ETag": quote(secret, safe=""), "Content-Length": "123"},
        f"echo={secret}; authorization={injected}; encoded={quote(secret, safe='')}",
    )
    assert set(headers) == {"Content-Type", "ETag"}
    assert secret not in str((headers, body))
    assert injected not in str((headers, body))
    assert quote(secret, safe="") not in str((headers, body))
    assert "[redacted]" in body


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
            agent_consent=True,
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


def _make_app(db_session: AsyncSession, mock_sm=None, *, user="user-canonical") -> TestClient:
    """Build a minimal FastAPI test app with the credential routes router."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[get_secrets_manager] = lambda: mock_sm
    from tests.internal.broker_fixture import install_broker_fixture

    install_broker_fixture(app, user=user, run="canonical-run", tenant="org-canonical")
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    s.vault_raw_read_enabled = True
    s.vault_materialization_bucket = "test-bucket"
    # Issue #3175: credential binding defaults to shadow mode (off).
    s.enforce_credential_binding = False
    s.webhook_events_table = "adp-test-webhook-events"
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["proxy-request", "credential-raw-read"])
@pytest.mark.parametrize("consent_state", ["granted", "missing", "revoked", "deleted", "replaced"])
async def test_worker_delivery_requires_current_personal_consent(db, endpoint, consent_state):
    from tests.auth.proxy_stream import stream_from_request

    credential = await db.get(UserCredential, "cred-aws-1")
    if consent_state == "missing":
        credential.agent_consent = False
        await db.commit()

    async def fetch_secret(*_):
        if consent_state in {"revoked", "deleted", "replaced"}:
            if consent_state == "deleted":
                statement = delete(UserCredential).where(UserCredential.id == credential.id)
            else:
                values = {"secret_arn": "replacement-secret"} if consent_state == "replaced" else {"agent_consent": False}
                statement = update(UserCredential).where(UserCredential.id == credential.id).values(**values)
            await db.execute(statement.execution_options(synchronize_session=False))
            await db.commit()
            # Keep the selected snapshot consented to detect reliance on stale ORM state.
            assert credential.agent_consent is True
        return "private-provider-token"

    upstream = AsyncMock()
    upstream.__aenter__.return_value = upstream
    upstream.request.return_value = SimpleNamespace(status_code=200, headers={}, text="provider response")
    upstream.stream = lambda **kwargs: stream_from_request(upstream, **kwargs)
    with (
        patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        patch("src.internal.credential_routes._validate_proxy_url", return_value="https://api.example.test"),
        patch("src.internal.credential_routes._validate_credential_host_binding"),
        patch("src.internal.credential_routes._fetch_secret", side_effect=fetch_secret) as secret_fetch,
        patch("httpx.AsyncClient", return_value=upstream),
    ):
        body = {
            "user_id": "user-canonical",
            "invocation_id": "canonical-run",
            "agent_id": "developer",
            "task_id": "consent-test",
            "service": "aws",
            "label": "prod",
        }
        if endpoint == "proxy-request":
            body.update(method="GET", url="https://api.example.test")
        response = _make_app(db, MagicMock()).post(f"/internal/v1/{endpoint}", json=body)

    if consent_state == "granted":
        assert response.status_code == 200, response.text
        if endpoint == "proxy-request":
            upstream.request.assert_awaited_once()
            assert upstream.request.call_args.kwargs["headers"]["Authorization"] == "Bearer private-provider-token"
        else:
            assert response.json()["value"] == "private-provider-token"
    else:
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "not_consented"
        assert "private-provider-token" not in response.text
        upstream.request.assert_not_awaited()
        if consent_state == "missing":
            secret_fetch.assert_not_awaited()
        else:
            secret_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_proxy_denial_audits_without_reading_secret(db):
    mock_sm = MagicMock()
    client = _make_app(db, mock_sm)

    async def verified_gateway(request: Request):
        request.state.agent_tools_credential_authority = GatewayCredentialAuthority(
            user_id="user-canonical",
            tenant_id="org-canonical",
            agent_id="tools-facade",
            credential_ids=frozenset(),
            assertion_expires_at=9999999999,
        )

    client.app.dependency_overrides[verify_internal_or_irsa] = verified_gateway
    with (
        patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        patch("src.internal.credential_routes._validate_proxy_url", return_value="https://api.example.test"),
    ):
        response = client.post(
            "/internal/v1/proxy-request",
            json={
                "service": "aws",
                "label": "prod",
                "method": "GET",
                "url": "https://api.example.test",
            },
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "not_consented"
    mock_sm.get_secret.assert_not_called()
    audits = (await db.execute(select(AuditLog).where(AuditLog.event_type == "vault_proxy_request_denied"))).scalars().all()
    assert len(audits) == 1
    assert audits[0].org_id == "org-canonical"
    assert audits[0].details["reason"] == "not_consented"
    assert audits[0].details["authorized_user_id"] == "user-canonical"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial,url,error,message",
    [
        (
            "global",
            "https://blocked.example.test/status",
            "proxy_host_denied",
            "Host 'blocked.example.test' is not in the proxy allowlist",
        ),
        (
            "credential",
            "https://api.stripe.com/status",
            "credential_host_binding_denied",
            "A 'github' credential may not be sent to host 'api.stripe.com'",
        ),
    ],
)
async def test_gateway_proxy_denies_forbidden_destination_without_provider(db, denial, url, error, message):
    credential = await db.get(UserCredential, "cred-aws-1")
    credential.service = "github"
    await db.commit()
    mock_sm = MagicMock()
    client = _make_app(db, mock_sm)

    async def verified_gateway(request: Request):
        request.state.agent_tools_credential_authority = GatewayCredentialAuthority(
            user_id="user-canonical",
            tenant_id="org-canonical",
            agent_id="tools-facade",
            credential_ids=frozenset({"cred-aws-1"}),
            assertion_expires_at=9999999999,
        )

    client.app.dependency_overrides[verify_internal_or_irsa] = verified_gateway
    settings = _settings_mock()
    settings.vault_proxy_require_https = True
    settings.vault_proxy_host_allowlist = "api.github.com,api.stripe.com"
    settings.vault_enforce_credential_host_binding = True
    with (
        patch("src.internal.credential_routes.get_settings", return_value=settings),
        patch("src.internal.credential_routes.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 0))]) as dns,
        patch("src.internal.credential_routes._fetch_secret", new_callable=AsyncMock) as secret_fetch,
        patch("httpx.AsyncClient") as upstream,
    ):
        response = client.post(
            "/internal/v1/proxy-request",
            json={"service": "github", "label": "prod", "method": "GET", "url": url},
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {"error": error, "message": message}
    assert dns.call_count == (0 if denial == "global" else 1)
    secret_fetch.assert_not_awaited()
    mock_sm.get_secret.assert_not_called()
    upstream.assert_not_called()
    audits = (await db.execute(select(AuditLog).where(AuditLog.event_type == "vault_proxy_request_denied"))).scalars().all()
    assert len(audits) == 1
    assert audits[0].details["reason"] == error
    assert audits[0].details["authorized_user_id"] == "user-canonical"


@pytest.mark.asyncio
async def test_gateway_proxy_denies_foreign_selection_before_provider(db):
    client = _make_app(db, MagicMock())

    async def verified_gateway(request: Request):
        request.state.agent_tools_credential_authority = GatewayCredentialAuthority(
            user_id="user-canonical",
            tenant_id="org-canonical",
            agent_id="tools-facade",
            credential_ids=frozenset({"cred-aws-1"}),
            assertion_expires_at=9999999999,
        )

    client.app.dependency_overrides[verify_internal_or_irsa] = verified_gateway
    foreign = SimpleNamespace(
        id="cred-aws-1",
        agent_consent=True,
        org_id="org-canonical",
        user_id="user-empty",
        service="aws",
        label="prod",
        provider="vault",
        secret_arn="synthetic-secret-ref",
    )
    with (
        patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        patch("src.internal.credential_routes._validate_proxy_url", return_value="https://api.example.test"),
        patch("src.internal.credential_routes._resolve_credential", new_callable=AsyncMock, return_value=foreign),
        patch("src.internal.credential_routes._fetch_secret", new_callable=AsyncMock) as secret_fetch,
    ):
        response = client.post(
            "/internal/v1/proxy-request",
            json={"service": "aws", "label": "prod", "method": "GET", "url": "https://api.example.test"},
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "not_consented"
    secret_fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revalidation", ["denied", "missing_authority"])
async def test_gateway_proxy_rechecks_revocation_before_provider(db, revalidation):
    client = _make_app(db, MagicMock())

    async def clear_authority(request):
        request.state.agent_tools_credential_authority = None

    async def verified_gateway(request: Request):
        request.state.agent_tools_credential_authority = GatewayCredentialAuthority(
            user_id="user-canonical",
            tenant_id="org-canonical",
            agent_id="tools-facade",
            credential_ids=frozenset({"cred-aws-1"}),
            assertion_expires_at=9999999999,
        )

    client.app.dependency_overrides[verify_internal_or_irsa] = verified_gateway
    with (
        patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        patch("src.internal.credential_routes._validate_proxy_url", return_value="https://api.example.test"),
        patch("src.internal.credential_routes._validate_credential_host_binding"),
        patch("src.internal.credential_routes._fetch_secret", new_callable=AsyncMock) as secret_fetch,
        patch(
            "src.internal.tools_credential_auth.verify_tools_credential_request",
            new_callable=AsyncMock,
            side_effect=HTTPException(403, {"error": "not_consented"}) if revalidation == "denied" else clear_authority,
        ) as recheck,
    ):
        response = client.post(
            "/internal/v1/proxy-request",
            json={"service": "aws", "label": "prod", "method": "GET", "url": "https://api.example.test"},
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "not_consented"
    recheck.assert_awaited_once()
    secret_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_list_uses_verified_owner_without_public_identity_and_returns_metadata_only(db):
    client = _make_app(db)

    async def verified_gateway(request: Request):
        request.state.agent_tools_credential_authority = GatewayCredentialAuthority(
            user_id="user-canonical",
            tenant_id="org-canonical",
            agent_id="tools-facade",
            credential_ids=frozenset({"cred-aws-1"}),
            assertion_expires_at=9999999999,
        )

    client.app.dependency_overrides[verify_internal_or_irsa] = verified_gateway
    with patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()):
        response = client.get("/internal/v1/user-credentials?service=aws")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    assert set(response.json()[0]) == {"id", "service", "label", "credential_type", "expires_at", "last_used_at"}
    assert response.json()[0]["id"] == "cred-aws-1"
    assert "secret_arn" not in response.text


class TestCredentialListCanonicalResolution:
    """Test 3: credential lookup uses canonical user's id and org_id."""

    @pytest.mark.asyncio
    async def test_credential_lookup_uses_canonical_user_id_and_org(self, db):
        """Vault WHERE uses canonical_user.id and canonical_user.org_id.

        Regression test for the architect's important issue 3: both user_id
        AND org_id must be replaced in downstream queries.
        """
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db)
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"invocation_id": "canonical-run", "user_id": "user-canonical", "service": "aws"},
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
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, user="user-nonexistent")
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"invocation_id": "canonical-run", "user_id": "user-nonexistent", "service": "aws"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "user_not_found"

    @pytest.mark.asyncio
    async def test_credential_lookup_returns_empty_when_canonical_has_no_creds(self, db):
        """Test 6: current empty-list behavior preserved when the canonical user
        genuinely has no credentials in the org."""
        with (
            patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
            patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        ):
            client = _make_app(db, user="user-empty")
            resp = client.get(
                "/internal/v1/user-credentials",
                params={"invocation_id": "canonical-run", "user_id": "user-empty", "service": "aws"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.asyncio
async def test_github_pat_grace_persists_and_denial_is_audited(db):
    from datetime import UTC, datetime, timedelta

    from src.shared.services.github_pat_migration import github_pat_retired

    credential = UserCredential(
        org_id="org-canonical",
        user_id="user-canonical",
        service="github",
        label="github-pat",
        credential_type="bearer",
        secret_arn="test-pat-secret",
    )
    db.add(credential)
    await db.commit()
    start = datetime(2026, 10, 14, tzinfo=UTC)
    assert not await github_pat_retired(db, credential, grace_days=30, now=start)
    assert not await github_pat_retired(db, credential, grace_days=30, now=start + timedelta(days=29))
    assert not await github_pat_retired(db, credential, grace_days=30, now=start + timedelta(days=30, seconds=-1))
    assert await github_pat_retired(db, credential, grace_days=30, now=start + timedelta(days=30))
    from src.shared.models.organization import Organization

    org = await db.get(Organization, "org-canonical")
    assert org.settings["github_pat_grace_started_at"] == start.isoformat()
    events = (await db.execute(select(AuditLog).where(AuditLog.event_type == "github_pat_delivery_denied"))).scalars().all()
    assert len(events) == 1
    assert events[0].details["credential_id"] == credential.id
    assert not await github_pat_retired(
        db,
        UserCredential(
            org_id="org-canonical",
            service="slack",
            label="github-pat",
            credential_type="bearer",
        ),
        grace_days=0,
        now=start + timedelta(days=31),
    )
    other_org = Organization(
        id="org-other",
        name="Other Org",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db.add(other_org)
    await db.commit()
    assert not await github_pat_retired(
        db,
        UserCredential(
            org_id="org-other",
            service="github",
            label="github-pat",
            credential_type="bearer",
        ),
        grace_days=30,
        now=start + timedelta(days=31),
    )


@pytest.mark.asyncio
async def test_retired_github_pat_proxy_refuses_before_secret_fetch(db):
    from unittest.mock import AsyncMock

    credential = UserCredential(
        org_id="org-canonical",
        user_id="user-canonical",
        service="github",
        label="github-pat",
        credential_type="bearer",
        secret_arn="test-pat-secret",
    )
    db.add(credential)
    await db.commit()
    mock_sm = MagicMock()
    client = _make_app(db, mock_sm)
    settings = _settings_mock()
    settings.github_pat_grace_days = 0
    with (
        patch("src.internal.credential_routes.get_settings", return_value=settings),
        patch("src.internal.credential_routes._validate_proxy_url", return_value="https://api.github.com"),
        patch("src.internal.credential_routes.verify_selected_user_credential", new_callable=AsyncMock),
        patch("src.internal.credential_routes.require_credential_capability"),
        patch("src.internal.credential_routes.resolve_broker_binding", return_value=MagicMock(resolved_user_id="user-canonical")),
    ):
        response = client.post(
            "/internal/v1/proxy-request",
            json={
                "user_id": "user-canonical",
                "agent_id": "tools-facade",
                "task_id": "tool-call",
                "service": "github",
                "label": "github-pat",
                "method": "GET",
                "url": "https://api.github.com",
            },
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "github_pat_retired"
    assert "Settings > Credentials" in response.json()["detail"]["message"]
    mock_sm.get_secret.assert_not_called()


@pytest.mark.asyncio
async def test_disconnected_github_connection_cannot_mint_new_token(db):
    mock_identity = MagicMock()
    client = _make_app(db, user="user-empty")
    with (
        patch("src.internal.credential_routes.get_settings", return_value=_settings_mock()),
        patch("src.internal.credential_routes._validate_proxy_url", return_value="https://api.github.com"),
        patch("src.internal.credential_routes.require_credential_capability"),
        patch("src.internal.credential_routes.resolve_broker_binding", return_value=MagicMock(resolved_user_id="user-empty")),
        patch("src.internal.credential_routes.get_identity_client", return_value=mock_identity),
    ):
        response = client.post(
            "/internal/v1/proxy-request",
            json={
                "user_id": "user-empty",
                "agent_id": "tools-facade",
                "task_id": "tool-call",
                "service": "github",
                "method": "GET",
                "url": "https://api.github.com",
            },
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "not_connected"
    mock_identity.get_resource_oauth2_token.assert_not_called()


@pytest.mark.asyncio
async def test_identity_refresh_rejects_incomplete_or_failed_exchange_without_token(monkeypatch):
    from src.internal import credential_routes

    monkeypatch.setattr(credential_routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    owner = SimpleNamespace(id="user-alice", org_id="org-acme")
    pointer = SimpleNamespace(
        user_id=owner.id,
        org_id=owner.org_id,
        service="github",
        credential_type="oauth_token",
        agent_consent=True,
        expires_at=None,
        scopes={"oauth_scopes": ["read:user"]},
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
    )
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload-token"}
    for response, status, error in (
        ({"sessionStatus": "IN_PROGRESS", "accessToken": "do-not-use"}, 403, "not_connected"),
        ({"sessionStatus": "FAILED", "accessToken": "do-not-use"}, 502, "identity_unavailable"),
        ({"accessToken": "private token"}, 403, "not_connected"),
    ):
        identity.get_resource_oauth2_token.return_value = response
        with pytest.raises(HTTPException) as refused:
            await _identity_access_token(pointer, owner, identity)
        assert refused.value.status_code == status and refused.value.detail == {"error": error}
        assert "do-not-use" not in str(refused.value)

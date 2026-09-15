"""Real vault endpoints retain configured delivery behind protected worker auth."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.internal.credential_routes import get_secrets_manager, router
from src.orchestration.models import OrchestrationAcceptedPlan  # noqa: F401 — register policy tables before fixture creation
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential


@pytest.fixture
async def vault(db_session, monkeypatch):
    db_session.add(Organization(id="vault-org", name="Vault org"))
    await db_session.flush()
    db_session.add(User(id="vault-user", org_id="vault-org", team_id="", email="vault@example.test"))
    await db_session.flush()
    cred = UserCredential(
        id="vault-key",
        org_id="vault-org",
        user_id="vault-user",
        service="github",
        label="deployment",
        credential_type="api_key",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture-key",
    )
    db_session.add(cred)
    await db_session.commit()
    grant = DelegatedGrant(
        grant_id="grant:vault",
        tenant_id="vault-org",
        principal="vault-run#1",
        authority=AuthorityReference("human_event", "approval", "vault-user", "vault-org"),
        allowed_actions=frozenset(),
        flow_id="vault-flow",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    events = MagicMock()
    events.get_item.return_value = {"Item": {"authorized_user_id": {"S": "vault-user"}}}
    runtime = SimpleNamespace(
        authenticate=lambda *_: (None, SimpleNamespace(tenant_id="vault-org", invocation_id="vault-run", principal="vault-run#1"), None, grant),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=lambda *_: {"arrived_at": {"S": "2026-09-15T00:00:00Z"}}, client=events),
    )

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_):
            pass

    settings = SimpleNamespace(
        enforce_credential_binding=False,
        webhook_events_table="events",
        vault_raw_read_enabled=True,
        vault_materialization_bucket="vault-fixture",
        aws_region="us-east-1",
        vault_proxy_require_https=True,
        vault_proxy_host_allowlist="api.github.com",
        vault_enforce_credential_host_binding=True,
    )
    identity = SimpleNamespace(scope="internal", user_id="authority-worker", credential_scopes=["credential:raw-read", "credential:materialize"])
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: SessionContext)
    monkeypatch.setattr("src.shared.config.get_settings", lambda: settings)
    monkeypatch.setattr("src.internal.credential_routes.get_settings", lambda: settings)
    monkeypatch.setattr(
        "src.internal.credential_routes.resolve_credential_binding",
        lambda **_: SimpleNamespace(resolved_user_id="vault-user", from_registry=True, drift_detected=False),
    )
    sm = MagicMock()
    sm.get_secret.return_value = "fixture-api-key"
    app = FastAPI()
    app.include_router(router)
    from src.internal.task_credentials import router as task_router

    app.include_router(task_router)
    # Some integration fixtures reload auth modules. Patch the external IAM lookup
    # used by each registered dependency, keeping real broker verification intact.
    for route in app.routes:
        for dependency in getattr(getattr(route, "dependant", None), "dependencies", []):
            if dependency.call.__name__ == "verify_internal_or_irsa":
                monkeypatch.setitem(dependency.call.__globals__, "extract_iam_identity_from_headers", lambda _: identity)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secrets_manager] = lambda: sm
    return SimpleNamespace(
        client=TestClient(app),
        db=db_session,
        cred=cred,
        settings=settings,
        identity=identity,
        runtime=runtime,
        sm=sm,
        headers={"X-Caller-Identity": "worker", "X-Adp-Run-Credential": "proof", "X-Adp-Workload-Token": "pod"},
        body={
            "user_id": "vault-user",
            "invocation_id": "vault-run",
            "agent_id": "operations",
            "task_id": "deploy",
            "service": "github",
            "label": "deployment",
        },
    )


@pytest.mark.asyncio
async def test_raw_api_key_delivery_and_rotation_preserve_audit(vault):
    headers = {**vault.headers, "X-Agent-Scopes": "credential:raw-read"}
    vault.sm.get_secret.side_effect = ["fixture-key-one", "fixture-key-two"]
    for key in ("fixture-key-one", "fixture-key-two"):
        response = vault.client.post("/internal/v1/credential-raw-read", json=vault.body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["value"] == key
        assert response.json()["credential_type"] == "api_key"
    logs = (await vault.db.scalars(select(AuditLog))).all()
    assert len(logs) == 2
    assert all(row.details["credential_id"] == "vault-key" for row in logs)
    assert all("fixture-key" not in str(row.details) for row in logs)
    assert vault.runtime.validate_flow.await_count == 2


@pytest.mark.asyncio
async def test_proxy_injects_selected_key_and_preserves_host_restriction(vault):
    context = MagicMock()
    upstream = context.__aenter__.return_value
    upstream.request = AsyncMock(return_value=httpx.Response(200, text="provider-ok"))
    with (
        patch("src.internal.credential_routes.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
        patch("src.internal.credential_routes.httpx.AsyncClient", return_value=context),
    ):
        response = vault.client.post(
            "/internal/v1/proxy-request", json={**vault.body, "method": "GET", "url": "https://api.github.com/user"}, headers=vault.headers
        )
        assert response.status_code == 200, response.text
        assert upstream.request.await_args.kwargs["headers"]["Authorization"] == "ApiKey fixture-api-key"
        assert response.json()["body"] == "provider-ok"
        refused = vault.client.post(
            "/internal/v1/proxy-request", json={**vault.body, "method": "GET", "url": "https://outside.example.test/user"}, headers=vault.headers
        )
        assert refused.status_code == 403
    vault.sm.get_secret.assert_called_once()
    upstream.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_materialization_and_metadata_remain_available(vault):
    vault.cred.credential_type = "config_file"
    await vault.db.commit()
    with patch("boto3.client") as aws:
        aws.return_value.generate_presigned_url.return_value = "https://fixture.s3.amazonaws.com/file"
        response = vault.client.post(
            "/internal/v1/credential-materialize", json=vault.body, headers={**vault.headers, "X-Agent-Scopes": "credential:materialize"}
        )
        assert response.status_code == 201, response.text
        assert aws.return_value.put_object.call_args.kwargs["Body"] == b"fixture-api-key"
        assert aws.return_value.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 300
    response = vault.client.get(
        "/internal/v1/user-credentials", params={"user_id": "vault-user", "invocation_id": "vault-run"}, headers=vault.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == "vault-key"
    assert "secret_arn" not in response.text and "fixture-api-key" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "missing-scope", "wrong-user", "wrong-run", "wrong-label", "registry-scope"])
async def test_raw_key_existing_and_protected_refusals_precede_secret_read(vault, reason):
    body = dict(vault.body)
    headers = {**vault.headers, "X-Agent-Scopes": "credential:raw-read"}
    if reason == "disabled":
        vault.settings.vault_raw_read_enabled = False
    elif reason == "missing-scope":
        headers.pop("X-Agent-Scopes")
    elif reason == "registry-scope":
        vault.identity.credential_scopes = []
    else:
        body[{"wrong-user": "user_id", "wrong-run": "invocation_id", "wrong-label": "label"}[reason]] = "other"
    response = vault.client.post("/internal/v1/credential-raw-read", json=body, headers=headers)
    assert response.status_code in (403, 404), response.text
    vault.sm.get_secret.assert_not_called()


@pytest.mark.asyncio
async def test_task_session_delivery_revalidates_authority_and_audits_without_secrets(vault):
    value = {
        "Version": 1,
        "AccessKeyId": "source-key",
        "SecretAccessKey": "source-secret",
        "SessionToken": "source-token",
        "Expiration": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    with patch("src.internal.task_credentials.issue_task_session", return_value=value):
        response = vault.client.post(
            "/internal/v1/worker-task-credentials", json={"user_id": "vault-user", "invocation_id": "vault-run"}, headers=vault.headers
        )
    assert response.status_code == 200, response.text
    assert response.json() == value
    assert response.headers["cache-control"] == "no-store"
    assert vault.runtime.validate_flow.await_count == 3
    logs = (await vault.db.scalars(select(AuditLog))).all()
    assert len(logs) == 1 and logs[0].event_type == "worker_task_session_issued"
    assert logs[0].details["invocation_id"] == "vault-run"
    assert "source-key" not in str(logs[0].details) and "source-secret" not in str(logs[0].details)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revoked", "shortened", "unprotected", "wrong-user", "wrong-run", "caller-policy"])
async def test_task_session_is_never_delivered_without_current_authority(vault, monkeypatch, reason):
    from dataclasses import replace

    from src.agentauth.bootstrap import BootstrapRefusedError

    value = {
        "Version": 1,
        "AccessKeyId": "source-key",
        "SecretAccessKey": "source-secret",
        "SessionToken": "source-token",
        "Expiration": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    body = {"user_id": "vault-user", "invocation_id": "vault-run"}
    headers = dict(vault.headers)
    if reason == "revoked":
        vault.runtime.validate_flow.side_effect = [None, BootstrapRefusedError("cancelled")]
    elif reason == "shortened":
        context = vault.runtime.authenticate()
        vault.runtime.authenticate = MagicMock(
            side_effect=[context, (*context[:3], replace(context[3], expires_at=datetime.now(UTC) + timedelta(minutes=10)))]
        )
    elif reason == "unprotected":
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
        headers = {key: value for key, value in headers.items() if key.lower() not in {"x-adp-run-credential", "x-adp-workload-token"}}
    elif reason == "caller-policy":
        body["targets"] = ["arn:aws:iam::222222222222:role/customer"]
    else:
        body["user_id" if reason == "wrong-user" else "invocation_id"] = "other"
    with patch("src.internal.task_credentials.issue_task_session", return_value=value) as issue:
        response = vault.client.post("/internal/v1/worker-task-credentials", json=body, headers=headers)
    assert response.status_code in (404, 422), response.text
    assert "source-key" not in response.text and "source-secret" not in response.text
    if reason not in {"revoked", "shortened"}:
        issue.assert_not_called()

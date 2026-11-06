"""Offline issuer-to-recipient contract without a PostgreSQL test service."""

import asyncio
import json
import runpy
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.auth import native_credential_authority as authority
from src.auth import native_credential_service as state
from src.auth import native_credentials as routes
from src.auth.cognito_jwt import CognitoJWTValidator
from src.shared.models.base import Base
from src.shared.models.native_credentials import NativeCredentialRequest as Record
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.models.vault import UserCredential, UserIdentity
from tests.auth.test_native_credentials_postgres import CONFIG, SECRET, Providers, approved, aws, create_body, decrypt

BASE = "/auth/native-credentials"


@pytest.fixture
async def native(monkeypatch):
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "234567890123")
    monkeypatch.delenv("ADP_GATEWAY_ROLE_ARN", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    migration = runpy.run_path(str(Path(__file__).resolve().parents[2] / "alembic/versions/108_native_credentials.py"))

    def schema(connection):
        Base.metadata.create_all(
            connection,
            tables=[m.__table__ for m in (Organization, Team, User, TeamMembership, TenantMembership, UserIdentity, UserCredential, ToolEntitlement)],
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    async with engine.begin() as connection:
        await connection.run_sync(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        db.add(Organization(id="tenant", name="Fictional native issuer tests"))
        db.add(Team(id="team", org_id="tenant", department_id="department", name="Test team"))
        for user in ("owner", "other"):
            db.add(
                User(
                    id=user,
                    cognito_sub="subject-" + user,
                    org_id="tenant",
                    team_id="team",
                    email=user + "@example.test",
                    user_kind="human",
                    is_shadow=False,
                )
            )
        await db.flush()
        for user in ("owner", "other"):
            db.add(TenantMembership(user_id=user, tenant_id="tenant", is_active=True))
            db.add(TeamMembership(user_id=user, org_id="tenant", team_id="team"))
        db.add(
            UserCredential(
                id="credential",
                org_id="tenant",
                user_id="owner",
                service="ssh",
                label="test",
                credential_type="ssh_key",
                secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:offline",
                agent_consent=True,
            )
        )
        for operation in ("assume_role", "materialize"):
            db.add(ToolEntitlement(org_id="tenant", team_id="", tool_name="credentials." + operation, enabled=True))
    providers = Providers(asyncio.get_running_loop())
    service = state.NativeCredentialService(factory, CONFIG, providers)
    monkeypatch.setattr(routes, "service", lambda: service)
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    validator = CognitoJWTValidator(
        user_pool_id="us-east-1_offline", client_id="browser-client", allowed_client_ids=["native-client"], region="us-east-1", agent_clients_table=""
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key()))
    jwk["kid"] = "offline"
    validator.jwk_client.fetch_data = lambda: {"keys": [jwk]}
    monkeypatch.setattr(authority, "get_cognito_validator", lambda: validator)

    def token(client="native-client", **claims):
        now = int(time.time())
        payload = {
            "sub": "subject-owner",
            "iss": validator.issuer,
            "client_id": client,
            "token_use": "access",
            "auth_time": now,
            "exp": now + 3600,
            "iat": now,
            "jti": client + "-session",
            "username": "owner@example.test",
            "custom:org_id": "tenant",
            "custom:account_type": "human",
            **claims,
        }
        return jwt.encode(payload, signing, algorithm="RS256", headers={"kid": "offline"})

    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CONFIG.issuer) as client:

        async def call(method, path, value=None, *, browser=False, bearer=None, origin=True, **kwargs):
            headers = {"Authorization": "Bearer " + (bearer or token("browser-client" if browser else "native-client"))}
            if browser and origin:
                headers["Origin"] = CONFIG.approval_origin
            return await client.request(method, BASE + path, headers=headers, json=value, **kwargs)

        yield SimpleNamespace(
            engine=engine, factory=factory, providers=providers, service=service, token=token, call=call, client=client, migration=migration
        )
    await engine.dispose()


@pytest.mark.parametrize("operation", ["materialize", "assume_role"])
async def test_private_delivery_envelope_is_nonrecoverable_metadata(native, operation):
    value = await aws(native) if operation == "assume_role" else create_body()
    result, recipient = await approved(native, value)
    receipt = result["receipt_id"]
    response = await native.call("POST", f"/requests/{receipt}/issue", recipient.proof)
    assert response.status_code == 200, response.text
    envelope = response.json()
    payload = decrypt(native, recipient, envelope)
    if operation == "materialize":
        from src.auth.native_credential_wire import decode

        assert decode(payload["file_base64"]) == SECRET
        assert "sts" not in native.providers.effects
    else:
        assert payload == dict(access_key_id="offline-access-key", secret_access_key=SECRET.decode(), session_token="offline-session")
        assert envelope["body"]["credential_expires_at"] - envelope["body"]["issued_at"] == 900
        assert native.providers.effects.count("sts") == 1
    assert SECRET not in response.content
    assert response.headers["cache-control"] == "no-store"
    async with native.factory() as db:
        row = await db.scalar(select(Record).where(Record.id == receipt))
        stored = json.dumps({field.name: getattr(row, field.name) for field in row.__table__.columns}, default=str)
    for forbidden in (SECRET.decode(), envelope["signature"], envelope["body"]["ciphertext"]):
        assert forbidden not in stored
    before = list(native.providers.effects)
    retry = await native.call("POST", f"/requests/{receipt}/issue", recipient.proof)
    assert retry.status_code == 409
    assert native.providers.effects == before


async def test_expired_private_request_reconciles_ids_without_provider_effect(native, monkeypatch):
    result, recipient = await approved(native)
    receipt = result["receipt_id"]
    async with native.factory() as db:
        record = await db.get(Record, receipt)
        deadline = record.expires_at
    before = list(native.providers.effects)
    with monkeypatch.context() as clock:
        clock.setattr(state, "time", SimpleNamespace(time=lambda: deadline + 1))
        for path in (f"/requests/{receipt}", f"/requests/by-request/{result['request_id']}"):
            response = await native.call("GET", path)
            assert response.status_code == 200
            assert response.json()["status"] == "expired"
            assert {"receipt_id", "request_id", "execution_id"} <= response.json().keys()
            assert SECRET not in response.content
        issue = await native.call("POST", f"/requests/{receipt}/issue", recipient.proof)
    assert issue.status_code == 409
    assert native.providers.effects == before
    assert SECRET not in issue.content


@pytest.mark.parametrize("operation", ["materialize", "assume_role"])
async def test_expired_issued_capability_has_only_metadata_on_original_ids(native, monkeypatch, operation):
    value = await aws(native) if operation == "assume_role" else create_body()
    result, recipient = await approved(native, value)
    receipt = result["receipt_id"]
    response = await native.call("POST", f"/requests/{receipt}/issue", recipient.proof)
    assert response.status_code == 200
    async with native.factory() as db:
        record = await db.get(Record, receipt)
        issued_at, credential_expires_at = record.issued_at, record.credential_expires_at
    maximum = 900 if operation == "assume_role" else value["use_seconds"]
    assert type(issued_at) is int and type(credential_expires_at) is int
    assert 0 < credential_expires_at - issued_at <= maximum
    if operation == "assume_role":
        assert credential_expires_at - issued_at == 900
    before = list(native.providers.effects)
    with monkeypatch.context() as clock:
        clock.setattr(state, "time", SimpleNamespace(time=lambda: credential_expires_at + 1))
        for path in (f"/requests/{receipt}", f"/requests/by-request/{value['request_id']}"):
            status = await native.call("GET", path)
            assert status.status_code == 200
            assert status.json()["receipt_id"] == receipt
            assert SECRET not in status.content
            assert response.json()["body"]["ciphertext"] not in status.text
            assert response.json()["signature"] not in status.text
        retry = await native.call("POST", f"/requests/{receipt}/issue", recipient.proof)
    assert retry.status_code == 409
    assert native.providers.effects == before
    assert SECRET not in retry.content


@pytest.mark.parametrize("operation", ["materialize", "assume_role"])
@pytest.mark.parametrize("consent", ["tool", "credential"])
@pytest.mark.parametrize("stage", ["missing", "revoked"])
async def test_native_missing_or_revoked_consent_denies_before_provider(native, operation, consent, stage):
    value = await aws(native) if operation == "assume_role" else create_body()
    foreign_token = native.token(sub="subject-other", username="other@example.test", jti="other-session")
    before = list(native.providers.effects)
    foreign = await native.call("POST", "/requests", value, bearer=foreign_token)
    assert foreign.status_code == 403
    assert foreign.json() == {"detail": {"error": "native_selected_credential_not_consented"}}
    assert native.providers.effects == before

    if stage == "revoked":
        result, recipient = await approved(native, value)
        before = list(native.providers.effects)
    if consent == "tool":
        native.providers.policy_value["consented_tools"].remove("credentials." + operation)
        expected_error = "native_tool_not_consented"
    else:
        async with native.factory() as db, db.begin():
            await db.execute(update(UserCredential).where(UserCredential.id == "credential").values(agent_consent=False))
        expected_error = "native_selected_credential_not_consented"

    if stage == "missing":
        denied = await native.call("POST", "/requests", value)
    else:
        denied = await native.call("POST", f"/requests/{result['receipt_id']}/issue", recipient.proof)
    assert denied.status_code == 403
    assert denied.json() == {"detail": {"error": expected_error}}
    assert native.providers.effects == before
    assert SECRET not in denied.content


@pytest.mark.parametrize("missing", ["allowed_tools", "consented_tools", "entitlement", "credential_consent"])
async def test_catalogue_registration_never_grants_native_authority(native, monkeypatch, missing):
    from src.internal.tools_catalogue import tool_catalogue

    monkeypatch.setenv("ADP_NATIVE_CREDENTIALS_ENABLED", "true")
    monkeypatch.delenv("ADP_TOOLS_GATEWAY_ROLE_ARN", raising=False)
    monkeypatch.setattr(authority.NativeConfig, "configured", lambda: CONFIG)
    assert "credentials.assume_role" in {tool["name"] for tool in tool_catalogue()["tools"]}
    value = await aws(native)
    expected = "native_tool_not_consented"
    if missing in {"allowed_tools", "consented_tools"}:
        native.providers.policy_value[missing].remove("credentials.assume_role")
    else:
        async with native.factory() as db, db.begin():
            if missing == "entitlement":
                await db.execute(update(ToolEntitlement).where(ToolEntitlement.tool_name == "credentials.assume_role").values(enabled=False))
                expected = "native_tool_not_entitled"
            else:
                await db.execute(update(UserCredential).where(UserCredential.id == "credential").values(agent_consent=False))
                expected = "native_selected_credential_not_consented"
    before = list(native.providers.effects)
    denied = await native.call("POST", "/requests", value)
    assert denied.status_code == 403 and denied.json() == {"detail": {"error": expected}}
    assert native.providers.effects == before
    recovered = await native.call("GET", f"/requests/by-request/{value['request_id']}")
    assert recovered.status_code == 404

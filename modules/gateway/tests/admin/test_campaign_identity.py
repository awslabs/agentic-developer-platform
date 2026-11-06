"""Campaign recovery must fence membership edits and reject surviving JWTs."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request

from src.admin import campaign_identity as routes
from src.admin import membership_revocation
from src.auth import dependencies
from src.auth.cognito_jwt import CognitoJWTValidator
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def fixture_identity(db_session):
    db_session.add(Organization(id="campaign-tenant", name="Campaign tenant"))
    await db_session.flush()
    user = User(id="campaign-user", cognito_sub="campaign-sub", org_id="campaign-tenant", team_id="", email="fixture@example.invalid")
    db_session.add(user)
    await db_session.flush()
    member = TenantMembership(user_id=user.id, tenant_id=user.org_id, role="member", is_active=False, revoked_at=datetime.now(UTC))
    db_session.add(member)
    await db_session.commit()
    actor = TokenContext(
        user_id="operator",
        org_id=user.org_id,
        team_id="",
        department_id="",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return user, member, actor


async def activate(db, actor):
    _, before = await routes.snapshot(db, actor.org_id, "campaign-sub", actor)
    operation_id = uuid4()
    receipt = await routes.edit_identity(
        actor.org_id, "campaign-sub", operation_id, routes.IdentityEdit(expected_version=before["version"], enabled=True), db, actor
    )
    return operation_id, receipt


async def test_receipts_restore_only_the_original_revocation(db_session, fixture_identity):
    _, member, actor = fixture_identity
    original = member.revoked_at
    operation_id, receipt = await activate(db_session, actor)
    assert member.revoked_at is None
    assert await routes.read_operation(actor.org_id, "campaign-sub", operation_id, db_session, actor) == receipt
    restore_id = uuid4()
    body = routes.IdentityEdit(expected_version=receipt["after"]["version"], enabled=False, restore_operation_id=operation_id)
    restored = await routes.edit_identity(actor.org_id, "campaign-sub", restore_id, body, db_session, actor)
    assert member.revoked_at.replace(tzinfo=UTC) == original.replace(tzinfo=UTC)
    assert await routes.edit_identity(actor.org_id, "campaign-sub", restore_id, body, db_session, actor) == restored
    assert restored["after"]["value"] == {"enabled": False}


@pytest.mark.parametrize("change", ["user", "member"])
async def test_operator_change_after_client_read_is_not_overwritten(db_session, fixture_identity, change):
    user, member, actor = fixture_identity
    operation_id, receipt = await activate(db_session, actor)
    if change == "user":
        user.email = "new-owner@example.invalid"
    else:
        member.joined_via = "operator-reassigned"
    await db_session.commit()
    with pytest.raises(HTTPException) as refused:
        await routes.edit_identity(
            actor.org_id,
            "campaign-sub",
            uuid4(),
            routes.IdentityEdit(expected_version=receipt["after"]["version"], enabled=False, restore_operation_id=operation_id),
            db_session,
            actor,
        )
    assert refused.value.status_code == 409
    assert member.revoked_at is None
    assert user.email == "new-owner@example.invalid" if change == "user" else member.joined_via == "operator-reassigned"


async def test_active_or_privileged_membership_cannot_be_claimed(db_session, fixture_identity):
    _, member, actor = fixture_identity
    await activate(db_session, actor)
    with pytest.raises(HTTPException) as refused:
        await activate(db_session, actor)
    assert refused.value.status_code == 409
    member.role = "org_admin"
    await db_session.commit()
    with pytest.raises(HTTPException) as refused:
        await routes.snapshot(db_session, actor.org_id, "campaign-sub", actor)
    assert refused.value.status_code == 409


async def test_restore_requires_the_exact_activation_receipt(db_session, fixture_identity):
    _, member, actor = fixture_identity
    _, receipt = await activate(db_session, actor)
    with pytest.raises(HTTPException) as refused:
        await routes.edit_identity(
            actor.org_id,
            "campaign-sub",
            uuid4(),
            routes.IdentityEdit(expected_version=receipt["after"]["version"], enabled=False, restore_operation_id=uuid4()),
            db_session,
            actor,
        )
    assert refused.value.status_code == 409
    assert member.revoked_at is None


async def test_unprivileged_caller_cannot_inspect_or_activate_a_fixture(db_session, fixture_identity):
    from src.admin.exceptions import AccessDeniedError

    _, member, actor = fixture_identity
    original = member.revoked_at
    caller = actor.model_copy(update={"user_id": "campaign-sub", "is_admin": False})
    with pytest.raises(AccessDeniedError):
        await routes.snapshot(db_session, actor.org_id, "campaign-sub", caller)
    assert member.revoked_at == original


async def test_journal_cleanup_rejects_the_same_signed_token_at_real_authentication(db_session, fixture_identity, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "tests/performance/gateway"))
    from campaign_adp import ADPFixtureBackend
    from campaign_recovery import RecoveryJournal

    user, member, actor = fixture_identity
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    validator = CognitoJWTValidator(user_pool_id="us-east-1_fixture", client_id="fixture-client", region="us-east-1")
    jwks = MagicMock()
    jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())
    monkeypatch.setattr(CognitoJWTValidator, "jwk_client", jwks)
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: validator)
    original_check = membership_revocation.require_not_revoked_context
    monkeypatch.setattr(membership_revocation, "require_not_revoked_context", lambda context: original_check(context, db_session))
    token = jwt.encode(
        {
            "sub": user.cognito_sub,
            "username": "fixture",
            "iss": validator.issuer,
            "exp": int(time.time()) + 3600,
            "token_use": "access",
            "client_id": "fixture-client",
            "custom:org_id": actor.org_id,
            "custom:account_type": "human",
        },
        key,
        algorithm="RS256",
    )
    app = FastAPI()
    app.include_router(routes.router, prefix="/admin")

    async def auth(request: Request):
        authorization = request.headers.get("authorization", "")
        return actor if authorization == "Bearer test-operator" else await dependencies.get_current_user(request, authorization)

    app.dependency_overrides[dependencies.get_current_user] = auth
    app.dependency_overrides[get_db] = lambda: db_session
    tmp_path.chmod(0o700)
    for name, content in (("admin", "test-operator"), ("health", "{}")):
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o600)
    token_path = tmp_path / "issued-token"
    fixtures = [
        {
            "kind": "identity",
            "binding": user.cognito_sub,
            "tenant": actor.org_id,
            "root_human": user.cognito_sub,
            "username": "fixture",
            "ownership_receipt": "owned",
            "desired": {"enabled": True},
        },
        {
            "kind": "secret",
            "binding": str(token_path),
            "identity": user.cognito_sub,
            "password_secret_id": "fixture-password",
            "client_id": "fixture-client",
            "ownership_receipt": "owned",
            "desired": {"reference": str(token_path)},
        },
    ]
    config = {
        "target": "https://gateway.example.invalid",
        "campaign": "auth-regression",
        "owner": "operator",
        "journal_dir": str(tmp_path),
        "gateway_admin_token_file": str(tmp_path / "admin"),
        "health_file": str(tmp_path / "health"),
        "health_owner": "operator",
        "aws_connection": "fixture",
        "aws_region": "us-east-1",
        "user_pool_id": "fixture",
        "fixtures": fixtures,
    }
    backend = ADPFixtureBackend(config)
    monkeypatch.setattr(backend, "verify_baseline", lambda: True)
    monkeypatch.setattr(backend, "_verify_aws", lambda: None)
    monkeypatch.setattr(backend, "_user", lambda binding: {"Enabled": True})

    def aws(service, operation, payload):
        assert operation in {"get-secret-value", "admin-initiate-auth"}
        return (
            {"SecretString": json.dumps({"username": "fixture", "password": "fixture-password"})}
            if operation == "get-secret-value"
            else {"AuthenticationResult": {"AccessToken": token}}
        )

    monkeypatch.setattr(backend, "_aws", aws)
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config["target"]) as client:

        async def send(method, route, payload, credential):
            response = await client.request(method, route, json=payload, headers={"Authorization": "Bearer " + credential})
            result = response.json()
            if response.status_code == 403 and result.get("detail", {}).get("error") == "tenant_membership_revoked":
                return {"unusable": True}
            response.raise_for_status()
            return result

        def transport(method, route, payload=None, *, token=None):
            return asyncio.run_coroutine_threadsafe(send(method, route, payload, token or "test-operator"), loop).result(timeout=10)

        monkeypatch.setattr(backend, "_http", transport)
        journal = RecoveryJournal(tmp_path, config["campaign"], config["owner"])
        for fixture in fixtures:
            await asyncio.to_thread(journal.mutate, backend, fixture["kind"], fixture["binding"], fixture["desired"], "owned")
        headers = {"Authorization": "Bearer " + token}
        before = await client.get("/admin/campaign-identity", headers=headers)
        assert before.status_code == 200 and before.json()["subject"] == user.cognito_sub
        receipt = await asyncio.to_thread(journal.recover, backend, 20, True)
        after = await client.get("/admin/campaign-identity", headers=headers)
        assert after.status_code == 403 and after.json()["detail"]["error"] == "tenant_membership_revoked"
        assert receipt["status"] == "clean" and member.revoked_at is not None
        assert not token_path.exists()

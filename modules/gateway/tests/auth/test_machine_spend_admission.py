"""Compose verified Cognito claims, proxy authentication and machine approval.

The IdP validator is the only authentication double. Alias/principal resolution
uses a real database; the downstream app records whether paid work was admitted.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth import middleware
from src.auth.approval_middleware import ApprovalEnforcementMiddleware
from src.auth.cognito_jwt import CognitoTokenClaims
from src.shared.models.base import Base
from src.shared.models.persona_models import ServicePrincipal, ServicePrincipalAlias


def claims(**overrides):
    values = dict(
        sub="cognito-sub-distinct-from-client",
        username="cognito-sub-distinct-from-client",
        client_id="registered-client",
        token_use="access",
        iss="https://test.invalid/pool",
        exp=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        iat=int(datetime.now(UTC).timestamp()),
        account_type="service",
        org_id="tenant-a",
        scope="bedrockgw/invoke",
    )
    return CognitoTokenClaims(**{**values, **overrides})


@pytest.fixture
async def machine_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(
                conn,
                tables=[ServicePrincipal.__table__, ServicePrincipalAlias.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="principal-a", org_id="tenant-a", display_name="Machine", status="active", approved_by="admin"
            )
        )
        session.add(
            ServicePrincipalAlias(
                id="alias-a",
                canonical_service_principal_id="principal-a",
                org_id="tenant-a",
                alias_source="cognito_m2m",
                alias_id="registered-client",
                is_active=True,
                registered_by="admin",
            )
        )
        await session.commit()
    monkeypatch.setenv("BG_ENFORCE_ORG_ASSIGNMENT", "true")
    monkeypatch.setenv("BG_APPROVAL_FAIL_OPEN", "false")
    monkeypatch.setattr(ApprovalEnforcementMiddleware, "_get_session", staticmethod(factory))
    monkeypatch.setattr(middleware, "get_cognito_validator", lambda: SimpleNamespace(validate_token=lambda _: claims()))
    yield factory
    await engine.dispose()


async def request():
    admitted, messages = [], []

    async def paid_work(scope, receive, send):
        admitted.append(scope["state"]["token_context"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        messages.append(message)

    app = middleware.TokenContextMiddleware(ApprovalEnforcementMiddleware(paid_work))
    await app(
        {"type": "http", "method": "POST", "path": "/v1/messages", "headers": [(b"authorization", b"Bearer validated-by-idp-double")], "state": {}},
        receive,
        send,
    )
    return next(item["status"] for item in messages if item["type"] == "http.response.start"), admitted


async def test_registered_machine_reaches_paid_work_without_human_membership(machine_db):
    status, admitted = await request()
    assert status == 200
    (context,) = admitted
    assert context.user_id == "registered-client"
    assert context.canonical_service_principal_id == "principal-a"
    assert context.canonical_alias_source == "cognito_m2m"
    assert context.org_id == context.attributed_org_id == "tenant-a"
    assert context._authenticated_subject_id == "cognito-sub-distinct-from-client"
    assert context.token_scopes == "bedrockgw/invoke"
    assert context.is_admin is False


@pytest.mark.parametrize("case", ["other-tenant", "other-source", "other-client", "revoked", "suspended", "missing"])
async def test_unregistered_or_inactive_machine_never_reaches_paid_work(machine_db, case):
    async with machine_db() as session:
        alias = await session.get(ServicePrincipalAlias, "alias-a")
        if case == "other-tenant":
            alias.org_id = "tenant-b"
        elif case == "other-source":
            alias.alias_source = "sa_registration"
        elif case == "other-client":
            alias.alias_id = "cognito-sub-distinct-from-client"
        elif case == "revoked":
            alias.is_active = False
            alias.revoked_at = datetime.now(UTC)
        elif case == "suspended":
            (await session.get(ServicePrincipal, "principal-a")).status = "suspended"
        else:
            await session.delete(alias)
        await session.commit()
    status, admitted = await request()
    assert status == 409
    assert admitted == []


async def test_revocation_is_observed_on_next_request_with_same_token(machine_db):
    assert (await request())[0] == 200
    async with machine_db() as session:
        alias = await session.get(ServicePrincipalAlias, "alias-a")
        alias.is_active = False
        alias.revoked_at = datetime.now(UTC)
        await session.commit()
    status, admitted = await request()
    assert status == 409
    assert admitted == []


async def test_unavailable_machine_registry_is_retryable_and_never_admits(machine_db, monkeypatch):
    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ApprovalEnforcementMiddleware, "_get_session", staticmethod(unavailable))
    status, admitted = await request()
    assert status == 503
    assert admitted == []


def test_service_claims_without_client_id_are_not_replaced_by_subject():
    with pytest.raises(HTTPException) as refusal:
        middleware._cognito_claims_to_context(claims(client_id=""))
    assert refusal.value.status_code == 401

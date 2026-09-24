from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from src.auth.aws_connection_authority import connection_binding
from src.auth.vault_schemas import CredentialCreate
from src.domain_proxy import superplane as proxy

ACCOUNT_ID = "123456789012"
CONNECTION_ID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/ADP-Agent-Role"
EXTERNAL_ID = "synthetic-external-id"


def request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/superplane/v1/accounts",
            "raw_path": b"/superplane/v1/accounts",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer synthetic")],
        }
    )


def token(**overrides):
    values = {
        "account_type": "human",
        "user_id": "cognito-sub",
        "org_id": "org-1",
        "cognito_username": "operator",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def credential(**overrides):
    values = {
        "id": CONNECTION_ID,
        "org_id": "org-1",
        "user_id": "user-1",
        "team_id": None,
        "domain_app_id": None,
        "service": "aws",
        "credential_type": "aws_role",
        "label": "prod",
        "strict": False,
        "expires_at": None,
        "aws_verification_attempt": "verification-attempt",
        "aws_verified_version_id": "version-1",
        "secret_arn": "opaque-secret-address",
        "aws_verified_at": datetime(2026, 9, 22, tzinfo=UTC),
        "scopes": {
            "status": "verified",
            "account_id": ACCOUNT_ID,
            "role_arn": ROLE_ARN,
        },
    }
    values.update(overrides)
    result = SimpleNamespace(**values)
    result.aws_verified_binding = connection_binding(result)
    return result


def secret(**overrides):
    values = {
        "account_id": ACCOUNT_ID,
        "role_arn": ROLE_ARN,
        "external_id": EXTERNAL_ID,
    }
    values.update(overrides)
    return json.dumps(values)


def body(**overrides):
    values = {
        "name": "prod",
        "provider": "aws",
        "account_id": ACCOUNT_ID,
        "adp_credential_id": CONNECTION_ID,
    }
    values.update(overrides)
    return proxy.AccountRegistrationRequest.model_validate(values)


async def configure(monkeypatch, *, stored=None, upstream=None):
    monkeypatch.setattr(proxy, "_resolve_user_id", AsyncMock(return_value="user-1"))
    monkeypatch.setattr(proxy, "resolve_effective_org_id", AsyncMock(return_value="org-1"))
    proxy_call = AsyncMock(
        return_value=upstream
        or Response(
            json.dumps(
                {
                    "id": "11111111-2222-3333-4444-555555555555",
                    "account_id": ACCOUNT_ID,
                    "role_arn": ROLE_ARN,
                    "external_id": EXTERNAL_ID,
                    "adp_credential_ids": [CONNECTION_ID],
                }
            ),
            status_code=201,
            media_type="application/json",
        )
    )

    async def send(*args, **kwargs):
        await kwargs["before_send"]()
        return proxy_call.return_value

    proxy_call.side_effect = send
    monkeypatch.setattr(proxy, "_proxy_to_domain", proxy_call)
    db = SimpleNamespace(scalar=AsyncMock(return_value=stored if stored is not None else credential()), rollback=AsyncMock())
    secrets = SimpleNamespace(get_secret=lambda _arn: secret(), current_version_id=lambda _arn: "version-1")
    secrets.get_secret_at_version = lambda arn, version: (secrets.get_secret(arn), version)
    return db, secrets, proxy_call


@pytest.mark.asyncio
async def test_adapter_resolves_owned_verified_connection_and_sends_domain_schema(
    monkeypatch,
):
    db, secrets, proxy_call = await configure(monkeypatch)

    response = await proxy.register_account(body(), request(), token(), db, secrets)

    forwarded = json.loads(proxy_call.await_args.kwargs["content"])
    assert forwarded == {
        "name": "prod",
        "provider": "aws",
        "account_id": ACCOUNT_ID,
        "role_arn": ROLE_ARN,
        "external_id": EXTERNAL_ID,
        "adp_credential_ids": [CONNECTION_ID],
    }
    public = json.loads(bytes(response.body))
    assert response.status_code == 201
    assert "role_arn" not in public
    assert "external_id" not in public
    assert public["adp_credential_ids"] == [CONNECTION_ID]


@pytest.mark.asyncio
async def test_foreign_or_unowned_connection_is_not_resolved(monkeypatch):
    db, secrets, proxy_call = await configure(monkeypatch, stored=None)
    db.scalar.return_value = None
    secrets.get_secret = lambda _arn: pytest.fail("foreign secret was read")

    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)

    assert raised.value.status_code == 404
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_connection_fails_before_secret_read(monkeypatch):
    pending = credential(scopes={"status": "pending", "account_id": ACCOUNT_ID, "role_arn": ROLE_ARN})
    db, secrets, proxy_call = await configure(monkeypatch, stored=pending)
    secrets.get_secret = lambda _arn: pytest.fail("pending secret was read")

    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)

    assert raised.value.status_code == 409
    assert raised.value.detail["error"] == "connection_not_verified"
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_writable_verified_scope_is_not_verification_provenance(
    monkeypatch,
):
    caller_request = CredentialCreate.model_validate(
        {
            "service": "aws",
            "label": "forged",
            "credential_type": "aws_role",
            "value": secret(),
            "scopes": {
                "status": "verified",
                "account_id": ACCOUNT_ID,
                "role_arn": ROLE_ARN,
            },
        }
    )
    forged = credential(
        aws_verified_at=None,
        scopes=caller_request.scopes,
    )
    db, secrets, proxy_call = await configure(monkeypatch, stored=forged)
    secrets.get_secret = lambda _arn: pytest.fail("unproven secret was read")

    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)

    assert raised.value.status_code == 409
    assert raised.value.detail["error"] == "connection_not_verified"
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_mismatch_fails_without_domain_mutation(monkeypatch):
    db, secrets, proxy_call = await configure(monkeypatch)
    secrets.get_secret = lambda _arn: secret(account_id="999999999999")

    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)

    assert raised.value.status_code == 409
    assert raised.value.detail["error"] == "connection_metadata_mismatch"
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_caller_metadata_cannot_disguise_another_roles_account(monkeypatch):
    other_role = "arn:aws:iam::999999999999:role/ADP-Agent-Role"
    stored = credential(scopes={"status": "verified", "account_id": ACCOUNT_ID, "role_arn": other_role})
    db, secrets, proxy_call = await configure(monkeypatch, stored=stored)
    secrets.get_secret = lambda _arn: secret(role_arn=other_role)
    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)
    assert raised.value.status_code == 409
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "after"])
async def test_expired_connection_is_refused_before_admission(monkeypatch, when):
    expired = credential(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    db, secrets, proxy_call = await configure(monkeypatch, stored=expired if when == "before" else credential())
    if when == "before":
        secrets.get_secret = lambda _arn: pytest.fail("expired connection material was read")
    else:

        def expire_during_read(_arn):
            db.scalar.return_value = expired
            return secret()

        secrets.get_secret = expire_during_read
    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)
    assert raised.value.status_code == 409
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["verification_failed", "new_attempt", "owner", "deleted", "version"])
async def test_concurrent_connection_change_prevents_domain_admission(monkeypatch, change):
    db, secrets, proxy_call = await configure(monkeypatch)

    def change_during_read(_arn):
        if change == "verification_failed":
            db.scalar.return_value = credential(aws_verified_at=None)
        elif change == "new_attempt":
            db.scalar.return_value = credential(aws_verification_attempt="new-attempt")
        elif change in {"owner", "deleted"}:
            db.scalar.return_value = None
        else:
            secrets.current_version_id = lambda _arn: "version-2"
        return secret()

    secrets.get_secret = change_during_read
    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)
    assert raised.value.status_code in {404, 409}
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotated_version_cannot_reuse_old_verification(monkeypatch):
    db, secrets, proxy_call = await configure(monkeypatch)
    secrets.current_version_id = lambda _arn: "version-2"
    secrets.get_secret = lambda _arn: pytest.fail("unverified version was read")
    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)
    assert raised.value.status_code == 409
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "version"])
async def test_route_discovery_cannot_bypass_final_expiry_or_version_check(monkeypatch, change):
    db, secrets, proxy_call = await configure(monkeypatch)

    async def discover_then_send(*args, **kwargs):
        if change == "expiry":
            db.scalar.return_value.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            secrets.current_version_id = lambda _arn: "version-2"
        await kwargs["before_send"]()
        pytest.fail("changed authority was forwarded after discovery")

    proxy_call.side_effect = discover_then_send
    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)
    assert raised.value.status_code == 409
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_account_cannot_use_the_human_adapter(monkeypatch):
    db, secrets, proxy_call = await configure(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(account_type="service"), db, secrets)

    assert raised.value.status_code == 403
    db.scalar.assert_not_awaited()
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_domain_permission_denial_is_preserved(monkeypatch):
    denial = Response(
        '{"detail":"provision permission required"}',
        status_code=403,
        media_type="application/json",
    )
    db, secrets, proxy_call = await configure(monkeypatch, upstream=denial)

    response = await proxy.register_account(body(), request(), token(), db, secrets)

    assert response.status_code == 403
    assert json.loads(bytes(response.body))["detail"] == "provision permission required"
    proxy_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_overlength_stored_external_id_is_rejected_without_echo_or_forward(monkeypatch, caplog):
    overlength_external_id = "trust-value-" + "x" * 260
    db, secrets, proxy_call = await configure(monkeypatch)
    secrets.get_secret = lambda _arn: secret(external_id=overlength_external_id)

    with pytest.raises(HTTPException) as raised:
        await proxy.register_account(body(), request(), token(), db, secrets)

    assert raised.value.status_code == 409
    assert overlength_external_id not in str(raised.value.detail)
    assert overlength_external_id not in caplog.text
    proxy_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_domain_error_body_cannot_echo_resolved_trust_metadata(monkeypatch, caplog):
    rejected = Response(
        json.dumps(
            {
                "detail": [
                    {"loc": ["body", "external_id"], "input": EXTERNAL_ID},
                    {"loc": ["body", "role_arn"], "input": ROLE_ARN},
                ],
                "external_id": EXTERNAL_ID,
                "role_arn": ROLE_ARN,
            }
        ),
        status_code=422,
        media_type="application/json",
    )
    db, secrets, proxy_call = await configure(monkeypatch, upstream=rejected)

    response = await proxy.register_account(body(), request(), token(), db, secrets)

    rendered = bytes(response.body).decode()
    assert response.status_code == 422
    assert EXTERNAL_ID not in rendered
    assert ROLE_ARN not in rendered
    assert EXTERNAL_ID not in caplog.text
    assert ROLE_ARN not in caplog.text
    proxy_call.assert_awaited_once()


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "gcp"},
        {"account_id": "123"},
        {"adp_credential_id": "arn:aws:secretsmanager:region:account:secret:value"},
    ],
)
def test_adapter_request_rejects_unsupported_or_secret_shaped_inputs(payload):
    with pytest.raises(ValueError):
        body(**payload)


def test_static_account_adapter_precedes_the_generic_forwarder():
    post_paths = [route.path for route in proxy.router.routes if "POST" in getattr(route, "methods", set())]
    assert post_paths.index("/superplane/v1/accounts") < post_paths.index("/superplane/v1/{path:path}")

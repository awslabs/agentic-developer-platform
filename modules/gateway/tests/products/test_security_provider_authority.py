"""Provider identity uses only current scoped credentials and rechecks afterward."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.auth import native_launch_revocation
from src.products import security_provider_authority as provider


@pytest.fixture
def selected(monkeypatch):
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    session_check = AsyncMock()
    monkeypatch.setattr(native_launch_revocation, "require_native_launch_session", session_check)
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected", "region": "us-east-1"}
    connection = {"record_id": "credential"}
    credential = SimpleNamespace(
        aws_verified_version_id="version",
        secret_arn="private-secret-reference",
        scopes=identity,
        aws_external_id="private-external-reference",
        label="Selected",
    )
    binding = SimpleNamespace(
        state="ready", configuration_id="configuration", connection_ref=connection, provider_identity=identity, generation=1, revision=1
    )
    authority = {
        **identity,
        "configuration_id": "configuration",
        "configuration_revision": 1,
        "scope": {"tenant_id": "tenant"},
        "connection_ref": connection,
        "authorization": {"delegation_id": "grant", "delegation_revision": 1},
    }
    actor = SimpleNamespace(user_id="owner", expires_at=datetime.now(UTC) + timedelta(hours=1))
    backend = SimpleNamespace(
        actor=actor,
        db=SimpleNamespace(expire_all=Mock(), get=AsyncMock(return_value=credential)),
        binding_authority=AsyncMock(return_value=(SimpleNamespace(binding=AsyncMock(return_value=binding)), authority)),
    )
    helper = SimpleNamespace(
        current_version_id=Mock(return_value="version"),
        get_secret_at_version=Mock(return_value=(json.dumps({**identity, "external_id": credential.aws_external_id}), "version")),
    )
    monkeypatch.setattr(provider, "SecretsManagerHelper", lambda: helper)
    assumed = SimpleNamespace(
        access_key_id="fictional-key",
        secret_access_key="fictional-secret",
        session_token="fictional-session",
        assumed_role_arn="arn:aws:sts::123456789012:assumed-role/Selected/read",
    )
    assume = Mock(return_value=assumed)
    monkeypatch.setattr(provider, "assume_role", assume)
    sts = SimpleNamespace(get_caller_identity=Mock(return_value={"Account": "123456789012", "Arn": assumed.assumed_role_arn}))
    session = Mock(return_value=SimpleNamespace(client=Mock(return_value=sts)))
    monkeypatch.setattr(provider.boto3, "Session", session)
    return SimpleNamespace(
        backend=backend,
        binding=binding,
        authority=authority,
        helper=helper,
        assume=assume,
        session=session,
        sts=sts,
        session_check=session_check,
        http=SimpleNamespace(headers={"Authorization": "Bearer fictional"}),
    )


async def test_provider_read_scopes_sts_and_never_uses_ambient_credentials(selected):
    c = selected
    result = await provider.read(c.backend, "product", "project", "binding", c.http)
    assert result["provider_identity"]["account_id"] == "123456789012"
    assert c.backend.binding_authority.await_count == c.session_check.await_count == 2
    arguments = c.assume.call_args.kwargs
    policy = json.loads(arguments["session_policy"])
    assert policy["Statement"][0]["Action"] == ["sts:GetCallerIdentity"]
    assert c.session.call_args.kwargs["aws_access_key_id"] == "fictional-key"
    assert "fictional" not in json.dumps(result)


@pytest.mark.parametrize("change", ["binding", "expiry", "logout", "provider"])
async def test_current_scope_and_session_must_survive_provider_read(selected, change):
    c = selected
    original = c.sts.get_caller_identity.return_value

    def read():
        if change == "binding":
            c.binding.revision += 1
        elif change == "expiry":
            c.backend.actor.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "logout":
            c.session_check.side_effect = HTTPException(401, "revoked")
        elif change == "provider":
            return {**original, "Account": "000000000000"}
        return original

    c.sts.get_caller_identity.side_effect = read
    with pytest.raises(HTTPException):
        await provider.read(c.backend, "product", "project", "binding", c.http)
    assert c.assume.call_count == 1


async def test_expired_session_never_reaches_connection_or_provider(selected):
    c = selected
    c.backend.actor.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as denied:
        await provider.read(c.backend, "product", "project", "binding", c.http)
    assert denied.value.status_code == 401
    c.assume.assert_not_called()
    c.backend.binding_authority.assert_not_awaited()

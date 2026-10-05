"""A registrant must control a role's trust, not merely know its ARN."""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.auth.aws_connection_authority import verified_connection_evidence
from src.internal import sts_assume_service as sts
from src.shared.aws_role_trust import validate_customer_role
from tests.auth import test_aws_connection_authority as fixtures

connection, db, engine, verify = fixtures.connection, fixtures.db, fixtures.engine, fixtures.verify

ROLE = "arn:aws:iam::123456789012:role/customer/Deploy"
KWARGS = dict(
    role_arn=ROLE,
    external_id="server-issued",
    session_duration_seconds=900,
    default_region="us-east-1",
    user_id="alice",
    agent_id="connect-verify",
    task_id="verify",
    label="test",
)


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    monkeypatch.setenv("ADP_GATEWAY_ROLE_ARN", "arn:aws:iam::999999999999:role/adp-gateway")


def denied(code="AccessDenied"):
    return ClientError({"Error": {"Code": code, "Message": "private provider details"}}, "AssumeRole")


@pytest.mark.parametrize(
    "behavior,accepted",
    [
        ([denied(), denied()], True),
        ([denied("Throttling")], False),
        ([denied(), denied("ValidationError")], False),
        ([{"Credentials": {"AccessKeyId": "x", "SecretAccessKey": "x", "SessionToken": "x", "Expiration": "later"}}], False),
        ([denied(), {"Credentials": {"AccessKeyId": "x", "SecretAccessKey": "x", "SessionToken": "x", "Expiration": "later"}}], False),
    ],
)
def test_only_explicit_denials_of_wrong_and_absent_ids_prove_trust(monkeypatch, behavior, accepted):
    client = MagicMock()
    client.assume_role.side_effect = behavior
    monkeypatch.setattr(sts.boto3, "client", lambda *a, **kw: client)
    if accepted:
        assert sts.require_external_id_enforcement(**KWARGS) is None
    else:
        with pytest.raises(sts.STSAssumeError):
            sts.require_external_id_enforcement(**KWARGS)
    calls = client.assume_role.call_args_list
    assert calls[0].kwargs["ExternalId"] != KWARGS["external_id"]
    if len(calls) == 2:
        assert "ExternalId" not in calls[1].kwargs
        assert {k: v for k, v in calls[0].kwargs.items() if k != "ExternalId"} == calls[1].kwargs


@pytest.mark.parametrize("external_id", [None, "", " "])
def test_delivery_rejects_missing_trust_before_aws(monkeypatch, external_id):
    client = MagicMock()
    monkeypatch.setattr(sts.boto3, "client", client)
    with pytest.raises(sts.STSAssumeError):
        sts.assume_role(**{**KWARGS, "external_id": external_id})
    client.assert_not_called()


@pytest.mark.parametrize(
    "role",
    [
        "arn:aws:iam::999999999999:role/path/ADP-Agent-customer",
        "arn:aws:iam::999999999999:role/ordinary",
        "arn:aws:iam::123456789012:role/path/adp-gateway",
        "arn:aws:iam::123456789012:role/path/bedrockgw-runner",
    ],
)
def test_platform_and_reserved_targets_are_refused(role):
    with pytest.raises(ValueError):
        validate_customer_role(role)


def test_customer_quick_create_namespace_is_allowed():
    validate_customer_role("arn:aws:iam::123456789012:role/ADP-Agent-customer")


def test_unknown_platform_identity_fails_closed(monkeypatch):
    monkeypatch.delenv("ADP_GATEWAY_ACCOUNT_ID")
    monkeypatch.delenv("ADP_GATEWAY_ROLE_ARN")
    with pytest.raises(ValueError):
        validate_customer_role(ROLE)


@pytest.mark.asyncio
async def test_legacy_or_generic_record_cannot_verify(db, connection):
    credential, helper = connection
    credential.aws_external_id = None
    await db.commit()
    with pytest.raises(HTTPException):
        await verify(db, connection)
    assert credential.aws_verified_at is None


@pytest.mark.asyncio
async def test_caller_secret_cannot_replace_server_trust_id(db, connection):
    credential, helper = connection
    raw, version = helper.get_secret_at_version.return_value
    secret = json.loads(raw)
    secret["external_id"] = "attacker-chosen"
    helper.get_secret_at_version.return_value = json.dumps(secret), version
    with pytest.raises(HTTPException):
        await verify(db, connection)


@pytest.mark.asyncio
async def test_failed_negative_probe_never_publishes_evidence(db, connection, monkeypatch):
    from src.auth import aws_connect_routes as routes

    monkeypatch.setattr(routes, "require_external_id_enforcement", MagicMock(side_effect=sts.STSAssumeError("provider details")))
    response = await verify(db, connection)
    assert response.status == "failed"
    assert response.reason == "trust_verification_failed"
    with pytest.raises(HTTPException):
        verified_connection_evidence(connection[0])


@pytest.mark.asyncio
async def test_workspace_validator_cannot_assume_unproven_generic_role(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.auth import vault_authority

    credential = SimpleNamespace(
        credential_type="aws_role",
        aws_external_id=None,
        expires_at=None,
        secret_arn="synthetic-secret",
        user_id="alice",
        org_id="org",
        label="generic",
        service="aws",
        scopes={"status": "verified"},
    )
    monkeypatch.setattr(vault_authority, "owned_current", AsyncMock(return_value=credential))
    monkeypatch.setattr(vault_authority, "_delegated", AsyncMock())
    monkeypatch.setattr(vault_authority, "_snapshot", lambda _: ("secret", "aws", "generic", "aws_role"))
    provider = MagicMock()
    monkeypatch.setattr(vault_authority, "configured_validator", provider)
    with pytest.raises(HTTPException):
        await vault_authority.validate_workspace_credential(
            AsyncMock(),
            MagicMock(),
            MagicMock(),
            SimpleNamespace(user_id="alice", org_id="org"),
            credential_id="generic",
            workspace_id="workspace",
        )
    provider.assert_not_called()


def test_separate_platform_bedrock_account_is_refused(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("src.shared.config.get_settings", lambda: SimpleNamespace(platform_bedrock_account_id="111111111111"))
    with pytest.raises(ValueError):
        validate_customer_role("arn:aws:iam::111111111111:role/ADP-Agent-probe")

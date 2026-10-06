"""Paid session authority refuses target drift and revocation across STS I/O."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.auth.vault_delivery import DeliveryRefusedError
from src.internal import domain_provider_session as broker
from src.internal.sts_assume_service import AssumeRoleResult

ROLE = "arn:aws:iam::123456789012:role/provider"
ARN = "arn:aws:sts::123456789012:assumed-role/provider/session"
ROLE_ID = "AROA" + "A" * 17 + ":session"


def result(**changes):
    fields = dict(
        access_key_id="key",
        secret_access_key="secret",
        session_token="token",
        expiration=(datetime.now(UTC) + timedelta(seconds=899)).isoformat(),
        region="us-east-1",
        profile_name="ignored",
        assumed_role_arn=ARN,
        assumed_role_id=ROLE_ID,
    )
    fields.update(changes)
    return AssumeRoleResult(**fields)


@pytest.mark.parametrize(
    "change",
    [
        {"assumed_role_arn": "arn:aws:sts::000000000000:assumed-role/provider/session"},
        {"assumed_role_id": "AROA" + "A" * 17 + ":another"},
        {"assumed_role_id": "AIDA" + "A" * 17 + ":session"},
        {"expiration": (datetime.now(UTC) + timedelta(seconds=1000)).isoformat()},
        {"expiration": datetime.now(UTC).replace(tzinfo=None).isoformat()},
        {"session_token": ""},
    ],
)
def test_sdk_response_identity_and_expiry_refuse(change):
    with pytest.raises(HTTPException):
        broker.verify_session(result(**change), ROLE, datetime.now(UTC) + timedelta(hours=1), issued_at=datetime.now(UTC))


def test_sdk_response_identity_and_expiry_valid():
    issued = datetime.now(UTC)
    assert broker.verify_session(result(), ROLE, issued + timedelta(hours=1), issued_at=issued) > issued


@pytest.fixture
def context(monkeypatch):
    future = datetime.now(UTC) + timedelta(hours=1)
    operation = dict(
        requester="human",
        plan_digest="digest",
        approval_id="approval",
        approval_expires_at=future,
        job_id="job",
        workspace_id="workspace",
        request_payload="sealed",
    )
    lease = dict(
        operation_id="operation",
        org_id="domain",
        workspace_id="workspace",
        holder="run#1",
        attempt_id="run#1",
        fence_token=1,
        runtime_deadline=future,
    )
    binding = SimpleNamespace(org_id="domain", adp_org_id="tenant")
    state = (binding, "run#1", "record", SimpleNamespace(expires_at=future), operation, lease)
    credential = SimpleNamespace(
        id="credential", user_id="canonical-human", org_id="tenant", credential_type="aws_role", secret_arn="secret-arn", scopes={}
    )
    user = SimpleNamespace(id="canonical-human", org_id="tenant", user_kind="human", is_shadow=False)
    changed = SimpleNamespace(after=None, calls=0, sts=0)

    async def current(request, operation_id):
        assert operation_id == "operation"
        return state

    async def deliver(db, sm, **kw):
        assert kw["credential_id"] == "credential" and kw["recipient"] == "run#1"
        await kw["refresh_executor"]()
        if changed.sts and changed.after == "delegation":
            raise DeliveryRefusedError("revoked")
        return credential, SimpleNamespace(reveal=lambda: "{}")

    @asynccontextmanager
    async def operation_session(selected):
        assert selected is binding
        yield "operation-db"

    async def resolve(db, *, subject, adp_org_id):
        assert subject == "human" and adp_org_id == "tenant"
        return {
            "subject": subject,
            "adp_org_id": adp_org_id,
            "principal_type": "human",
            "active": True,
            "enabled": True,
            "membership_id": "membership",
        }

    async def noop(*args, **kwargs):
        pass

    async def membership(*args):
        return None if changed.sts and changed.after == "membership" else user

    def sts(**kwargs):
        assert kwargs["user_id"] == "canonical-human" and kwargs["role_arn"] == ROLE
        assert kwargs["external_id"] == "server-owned" and kwargs["session_duration_seconds"] == 900
        changed.sts += 1
        if changed.after == "requester":
            operation["requester"] = "other"
        elif changed.after == "lease":
            lease["fence_token"] += 1
        elif changed.after == "owner":
            user.id = "other"
        return result()

    monkeypatch.setattr(broker, "current", current)
    monkeypatch.setattr(broker, "sealed_target", lambda operation, region: (("credential", "aws", "label"), "123456789012"))
    monkeypatch.setattr(broker, "deliver_credential", deliver)
    monkeypatch.setattr(broker, "operation_session", operation_session)
    monkeypatch.setattr(broker, "current_human_identity", resolve)
    monkeypatch.setattr(broker, "verified_connection_evidence", lambda credential: ("attempt", "version", "binding", future))
    monkeypatch.setattr(broker, "connection_material", lambda *args: (ROLE, "server-owned", "123456789012"))
    monkeypatch.setattr(broker, "assume_role", sts)
    monkeypatch.setattr("src.internal.credential_routes._write_audit", noop)
    return SimpleNamespace(
        state=state,
        changed=changed,
        db=SimpleNamespace(refresh=noop, commit=noop, scalar=membership),
        sm=SimpleNamespace(current_version_id=lambda arn: "version"),
        body=SimpleNamespace(operation_id="operation", region="us-east-1"),
    )


async def test_current_paid_session_is_short_lived_and_excludes_raw_connection(context):
    value = await broker.provider_session(None, context.body, context.db, context.sm)
    assert value["role_arn"] == ROLE and value["assumed_role_id"] == ROLE_ID
    assert "external_id" not in value and "user_id" not in value


@pytest.mark.parametrize("mutation", ["requester", "lease", "owner", "delegation", "membership"])
async def test_revocation_during_sts_does_not_release_session(context, mutation):
    context.changed.after = mutation
    with pytest.raises((HTTPException, DeliveryRefusedError)):
        await broker.provider_session(None, context.body, context.db, context.sm)
    assert context.changed.sts == 1


async def test_sts_minimum_never_extends_authority(context):
    context.state[4]["approval_expires_at"] = datetime.now(UTC) + timedelta(seconds=899)
    with pytest.raises(HTTPException):
        await broker.provider_session(None, context.body, context.db, context.sm)
    assert context.changed.sts == 0


def test_target_comes_from_sealed_request(monkeypatch):
    lifecycle = dict(mode="existing-account-managed", target_account_id="123456789012", region="us-east-1")
    parameters = {"lifecycle_request": json.dumps(lifecycle)}
    identity = SimpleNamespace(
        admitted_credential_reference=lambda *args: ("credential", "aws", "label"),
        admitted_credential_target=lambda *args: ("aws", "123456789012"),
        decode_payload=lambda *args: SimpleNamespace(parameters=parameters),
    )
    monkeypatch.setattr(broker, "harness", lambda name: identity)
    operation = {"request_payload": "sealed", "plan_digest": "digest"}
    assert broker.sealed_target(operation, "us-east-1")[1] == "123456789012"
    with pytest.raises(HTTPException):
        broker.sealed_target(operation, "eu-west-1")
    for mutation in ("new-account-managed", "shared"):
        if mutation == "shared":
            parameters["shared_membership"] = "membership"
        else:
            lifecycle["mode"] = mutation
            parameters["lifecycle_request"] = json.dumps(lifecycle)
        with pytest.raises(HTTPException):
            broker.sealed_target(operation, "us-east-1")


async def test_preflight_checks_current_connection_without_sts(context):
    answer = await broker.provider_session(None, context.body, context.db, context.sm, preflight_only=True)
    assert answer["admits_work"] is True and answer["operation_id"] == "operation" and context.changed.sts == 0


def test_maintained_sts_preserves_sdk_identity_and_applies_narrow_policy(monkeypatch):
    import boto3
    from botocore.stub import Stubber

    from src.internal import sts_assume_service

    client = boto3.client("sts", region_name="us-east-1", aws_access_key_id="key", aws_secret_access_key="secret")
    policy = '{"Version":"2012-10-17","Statement":[]}'
    params = dict(
        RoleArn=ROLE,
        RoleSessionName="adp-superplane-operation-operatio",
        DurationSeconds=900,
        ExternalId="external",
        Policy=policy,
        Tags=[
            {"Key": "adp:user_id", "Value": "human"},
            {"Key": "adp:agent_id", "Value": "superplane-operation"},
            {"Key": "adp:task_id", "Value": "operation"},
            {"Key": "adp:persona", "Value": "superplane-operation"},
        ],
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "assume_role",
            {
                "Credentials": {
                    "AccessKeyId": "ASIA" + "A" * 16,
                    "SecretAccessKey": "s" * 40,
                    "SessionToken": "token",
                    "Expiration": datetime.now(UTC) + timedelta(seconds=900),
                },
                "AssumedRoleUser": {"Arn": ARN, "AssumedRoleId": ROLE_ID},
            },
            params,
        )
        monkeypatch.setattr(sts_assume_service.boto3, "client", lambda *args, **kw: client)
        answer = sts_assume_service.assume_role(
            role_arn=ROLE,
            external_id="external",
            session_duration_seconds=900,
            default_region="us-east-1",
            user_id="human",
            agent_id="superplane-operation",
            task_id="operation",
            label="label",
            session_policy=policy,
        )
    assert answer.assumed_role_arn == ARN and answer.assumed_role_id == ROLE_ID


async def test_preflight_does_not_mint_or_require_full_sts_minimum(context):
    context.state[4]["approval_expires_at"] = datetime.now(UTC) + timedelta(seconds=600)
    answer = await broker.provider_session(None, context.body, context.db, context.sm, preflight_only=True)
    assert answer["admits_work"] is True
    assert datetime.fromisoformat(answer["authority_expires_at"]) == context.state[4]["approval_expires_at"]
    assert context.changed.sts == 0
    with pytest.raises(HTTPException):
        await broker.provider_session(None, context.body, context.db, context.sm)
    assert context.changed.sts == 0

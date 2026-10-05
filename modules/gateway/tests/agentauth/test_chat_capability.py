"""Capability foundation against protected DynamoDB emulation.

Live-root and directory callbacks are controlled fixtures, not live IAM evidence.
"""

import base64
import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.chat_capability import (
    VERSION,
    ChatAuthorizationRefusedError,
    ChatAuthorizationUnavailableError,
    ChatCapabilityService,
    ChatLaunch,
    ChatLaunchStore,
)
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, CredentialError, verify_credential
from src.agentauth.workload import VerifiedPod

NOW = 1_800_000_000
POD = VerifiedPod("pod-a", "sandbox-a", "sandboxes", "sandbox", "127.0.0.1")


@pytest.fixture
def setup():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="chat-authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        launches = ChatLaunchStore(BootstrapStore(table_name="chat-authority", dynamodb_client=client))
        launch = ChatLaunch(
            run_id="run-a",
            tenant_id="tenant-a",
            user_id="alice",
            team_id="team-a",
            session_id="session-a",
            sandbox_uid="pod-a",
            image_digest="sha256:" + "a" * 64,
            attempt=1,
            credential_epoch=1,
            lease_generation=1,
            grant_id="grant-a",
            grant_epoch=1,
            operations=frozenset({"history.read", "artifact.read"}),
            expires_at=NOW + 1000,
        )
        launches.register(launch)
        state = {
            "root_active": True,
            "session_active": True,
            "lease_generation": 1,
            "credential_epoch": 1,
            "grant_epoch": 1,
            "members": {("tenant-a", "alice", "team-a")},
        }

        def current(saved, now):
            return (
                state["root_active"]
                and state["session_active"]
                and all(getattr(saved, field) == state[field] for field in ("lease_generation", "credential_epoch", "grant_epoch"))
            )

        service = ChatCapabilityService(
            launches,
            current=current,
            member=lambda tenant, user, team: (tenant, user, team) in state["members"],
            env={CREDENTIAL_KEY_ENV: "synthetic-test-signing-key"},
        )
        yield service, launches, launch, state


def verify(service, token, **changes):
    return service.verify(token, **{"run_id": "run-a", "session_id": "session-a", "operation": "history.read", "now": NOW, **changes})


def test_immutable_registration_and_scope_round_trip(setup):
    service, launches, launch, _ = setup
    launches.register(launch)
    with pytest.raises(ChatAuthorizationRefusedError):
        launches.register(launch.model_copy(update={"tenant_id": "tenant-b", "user_id": "bob"}))
    token = service.issue("run-a", POD, now=NOW)
    assert verify(service, token) == launch
    assert verify(service, token, now=NOW + 299) == launch
    with pytest.raises(CredentialError):
        verify_credential(token, env=service.env)


@pytest.mark.parametrize(
    "changes", [{"run_id": "run-b"}, {"session_id": "session-b"}, {"operation": "history.append"}, {"now": NOW + 300}, {"now": NOW - 1}]
)
def test_capability_is_bound_to_run_session_operation_and_time(setup, changes):
    service, _, _, _ = setup
    with pytest.raises(ChatAuthorizationRefusedError):
        verify(service, service.issue("run-a", POD, now=NOW), **changes)


@pytest.mark.parametrize(
    "field,value",
    [("root_active", False), ("session_active", False), ("lease_generation", 2), ("credential_epoch", 2), ("grant_epoch", 2), ("members", set())],
)
def test_current_authority_and_membership_are_rechecked_before_expiry(setup, field, value):
    service, _, _, state = setup
    token = service.issue("run-a", POD, now=NOW)
    state[field] = value
    with pytest.raises(ChatAuthorizationRefusedError):
        verify(service, token, now=NOW + 1)
    with pytest.raises(ChatAuthorizationRefusedError):
        service.issue("run-a", POD, now=NOW + 1)


def test_other_workload_cannot_bootstrap_a_guessed_run(setup):
    service, _, _, _ = setup
    with pytest.raises(ChatAuthorizationRefusedError):
        service.issue("run-a", VerifiedPod("pod-b", "sandbox-b", "sandboxes", "sandbox", "127.0.0.2"), now=NOW)


@pytest.mark.parametrize("changes", [{"audience": "other-service"}, {"expires_at": NOW + 301}, {"issued_at": True}, {"user_id": "bob"}])
def test_even_signed_invalid_claims_are_rejected(setup, changes):
    service, _, _, _ = setup
    token = service.issue("run-a", POD, now=NOW)
    body = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    body = base64.urlsafe_b64encode(json.dumps({**claims, **changes}).encode()).rstrip(b"=").decode()
    with pytest.raises(ChatAuthorizationRefusedError):
        verify(service, f"{VERSION}.{body}.{service._sign(body)}")


@pytest.mark.parametrize("token", ["", "a" * 4097, "run-v1.body.signature", "chat-data-v1.%%%%.invalid", "chat-data-v1.☃.invalid"])
def test_malformed_capability_is_denied(setup, token):
    with pytest.raises(ChatAuthorizationRefusedError):
        verify(setup[0], token)


def test_changed_protected_launch_invalidates_existing_capability(setup):
    service, launches, launch, _ = setup
    token = service.issue("run-a", POD, now=NOW)
    replacement = launch.model_copy(update={"lease_generation": 2})
    launches.store.client.update_item(
        TableName=launches.store.table,
        Key={"pk": {"S": "CHAT-LAUNCH#run-a"}, "sk": {"S": "LAUNCH"}},
        UpdateExpression="SET document = :document",
        ExpressionAttributeValues={":document": {"S": replacement.model_dump_json()}},
    )
    with pytest.raises(ChatAuthorizationRefusedError):
        verify(service, token)


def test_ttl_is_capped_by_the_launch_deadline(setup):
    service, _, launch, _ = setup
    token = service.issue("run-a", POD, now=launch.expires_at - 2)
    assert verify(service, token, now=launch.expires_at - 1) == launch
    with pytest.raises(ChatAuthorizationRefusedError):
        verify(service, token, now=launch.expires_at)


def test_private_and_shared_resource_access_requires_current_membership(setup):
    service, _, launch, state = setup
    resource = {
        "tenant_id": "tenant-a",
        "owner_user_id": "alice",
        "team_id": "team-a",
        "acl_user_ids": frozenset(),
        "session_id": "session-a",
        "now": NOW,
    }
    service.authorize_resource(launch, **resource)
    for change in ({"tenant_id": "tenant-b"}, {"owner_user_id": "bob"}, {"owner_user_id": ""}, {"session_id": "guessed"}):
        with pytest.raises(ChatAuthorizationRefusedError):
            service.authorize_resource(launch, **{**resource, **change})
    shared = {**resource, "owner_user_id": "bob", "acl_user_ids": frozenset({"alice"}), "team_id": "shared-team"}
    with pytest.raises(ChatAuthorizationRefusedError):
        service.authorize_resource(launch, **shared)
    state["members"].add(("tenant-a", "alice", "shared-team"))
    service.authorize_resource(launch, **shared)
    state["members"].remove(("tenant-a", "alice", "shared-team"))
    with pytest.raises(ChatAuthorizationRefusedError):
        service.authorize_resource(launch, **shared)


def test_teamless_member_can_use_private_chat_without_gaining_team_access(setup):
    service, launches, original, state = setup
    launch = ChatLaunch.model_validate({**original.model_dump(), "run_id": "personal-run", "team_id": ""})
    launches.register(launch)
    state["members"] = {("tenant-a", "alice", "")}
    token = service.issue(launch.run_id, POD, now=NOW)
    assert verify(service, token, run_id=launch.run_id) == launch
    resource = {
        "tenant_id": "tenant-a",
        "owner_user_id": "alice",
        "team_id": "",
        "acl_user_ids": frozenset(),
        "session_id": "session-a",
        "now": NOW,
    }
    service.authorize_resource(launch, **resource)
    with pytest.raises(ChatAuthorizationRefusedError):
        service.authorize_resource(launch, **{**resource, "owner_user_id": "bob"})
    with pytest.raises(ChatAuthorizationRefusedError):
        service.authorize_resource(launch, **{**resource, "team_id": "team-a", "owner_user_id": "bob", "acl_user_ids": frozenset({"alice"})})
    state["members"].clear()
    with pytest.raises(ChatAuthorizationRefusedError):
        verify(service, token, run_id=launch.run_id, now=NOW + 1)
    with pytest.raises(ChatAuthorizationRefusedError):
        service.issue(launch.run_id, POD, now=NOW + 1)


@pytest.mark.parametrize("source", ["current", "member"])
def test_authorization_outage_does_not_use_stale_permission(setup, source):
    service, _, _, _ = setup
    token = service.issue("run-a", POD, now=NOW)

    def unavailable(*args):
        raise RuntimeError("private diagnostic must not escape")

    setattr(service, source, unavailable)
    with pytest.raises(ChatAuthorizationUnavailableError, match="unavailable") as error:
        verify(service, token)
    assert "private diagnostic" not in str(error.value)


def test_storage_outage_is_not_a_missing_launch(setup, monkeypatch):
    service, launches, _, _ = setup
    token = service.issue("run-a", POD, now=NOW)

    def unavailable(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")

    monkeypatch.setattr(launches.store.client, "get_item", unavailable)
    with pytest.raises(ChatAuthorizationUnavailableError):
        verify(service, token)


def test_no_unsigned_or_shared_credential_fallback(setup):
    service, _, _, _ = setup
    service.env = {}
    with pytest.raises(ChatAuthorizationUnavailableError):
        service.issue("run-a", POD, now=NOW)

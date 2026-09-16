"""Real persisted registration retries and liveness races (#5028 AC4)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import BotoCoreError
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore, envelope_digest, issue_bound_credential
from src.agentauth.composition import build_authorization_service
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.agentauth.registration import AgentRegistrationService, RegistrationRefusedError
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import VerifiedPod


@pytest.fixture
def registered_context():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        for name, pk, sk in [("authority", "pk", "sk"), ("events", "event_id", "arrived_at")]:
            ddb.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
        store = BootstrapStore(table_name="authority", dynamodb_client=ddb)
        now = datetime.now(UTC)
        ddb.put_item(
            TableName="authority",
            Item={
                "pk": {"S": "TENANT#tenant"},
                "sk": {"S": "AUTHORITY#approval"},
                "status": {"S": "active"},
                "human_id": {"S": "human"},
                "authority_kind": {"S": "github_event"},
            },
        )
        grant = DelegatedGrant(
            grant_id="grant-a",
            tenant_id="tenant",
            principal="run-a#1",
            authority=AuthorityReference("github_event", "approval", "human", "tenant"),
            allowed_actions=frozenset({AgentAction.MONITOR}),
            target_relationships=frozenset({TargetRelationship.SELF}),
            repo_scope=frozenset({"org/repo"}),
            expires_at=now + timedelta(days=1),
        )
        envelope = {
            "message_id": "run-a",
            "tenant_id": "tenant",
            "persona": "developer",
            "arrived_at": "2026-09-13T09:00:00Z",
            "source_ref": {"repo": "org/repo", "issue": 42},
        }
        store.provision_pending(envelope=envelope, grant=grant, now=now)
        pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
        record = store.bind(invocation_id="run-a", digest=envelope_digest(envelope), pod=pod, now=now)
        key = {"event_id": {"S": "run-a"}, "arrived_at": {"S": envelope["arrived_at"]}}
        ddb.put_item(TableName="events", Item={**key, "tenant_id": {"S": "tenant"}, "status": {"S": "in_progress"}})
        env = {CREDENTIAL_KEY_ENV: "test-only-isolated-gateway-key"}
        token = issue_bound_credential(record, now=now, env=env)["credential"]
        policy = build_authorization_service(
            authority_table="authority",
            events_table="events",
            dynamodb_client=ddb,
            dynamodb_resource=boto3.resource("dynamodb", region_name="us-east-1"),
            env=env,
        )
        service = AgentRegistrationService(policy=policy, authority_table="authority", events_table="events", dynamodb_client=ddb, env=env)
        yield SimpleNamespace(
            ddb=ddb,
            store=store,
            service=service,
            record=record,
            pod=pod,
            key=key,
            credential=token,
            expiry=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            env=env,
            now=now,
            grant=grant,
        )


def register(context, **overrides):
    args = dict(credential_token=context.credential, pod=context.pod, token="a" * 40, token_expires_at=context.expiry)
    return context.service.register_control(**{**args, **overrides})


def event(context):
    return context.ddb.get_item(TableName="events", Key=context.key, ConsistentRead=True)["Item"]


def wrap_first_write(context, monkeypatch, after):
    state = {"fired": False}
    for name in ("update_item", "transact_write_items"):
        original = getattr(context.ddb, name)

        def wrapped(*, _original=original, **kwargs):
            result = _original(**kwargs)
            if not state["fired"]:
                state["fired"] = True
                after()
            return result

        monkeypatch.setattr(context.ddb, name, wrapped)
    return state


def test_lost_write_response_preserves_generation(registered_context, monkeypatch):
    ctx = registered_context

    def lose():
        raise BotoCoreError()

    state = wrap_first_write(ctx, monkeypatch, lose)
    try:
        register(ctx)
    except AuthorityStoreError:
        pass  # The contract permits a retry; it cannot allocate a new generation.
    result = register(ctx)
    assert state["fired"]
    assert result.generation == int(event(ctx)["control_generation"]["N"]) == 1


def test_overlapping_identical_registration_allocates_once(registered_context, monkeypatch):
    ctx = registered_context
    nested = []
    wrap_first_write(ctx, monkeypatch, lambda: nested.append(register(ctx)))
    outer = register(ctx)
    assert outer.generation == nested[0].generation == int(event(ctx)["control_generation"]["N"]) == 1


def test_changed_registration_cannot_replace_winner(registered_context):
    ctx = registered_context
    register(ctx)
    with pytest.raises(RegistrationRefusedError):
        register(ctx, token="b" * 40)
    with pytest.raises(RegistrationRefusedError):
        register(ctx, token_expires_at="2099-01-01T00:00:00Z")
    assert event(ctx)["control_generation"]["N"] == "1"
    assert event(ctx)["control_token"]["S"] == "a" * 40


def test_revoked_grant_refuses_registration_and_status(registered_context):
    ctx = registered_context
    ctx.ddb.update_item(
        TableName="authority",
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1"}},
        UpdateExpression="SET revoked = :v",
        ExpressionAttributeValues={":v": {"BOOL": True}},
    )
    with pytest.raises(RegistrationRefusedError):
        register(ctx)
    with pytest.raises(RegistrationRefusedError):
        ctx.service.record_status(credential_token=ctx.credential, pod=ctx.pod, status="in_progress")
    assert "control_generation" not in event(ctx)


def test_registration_checks_execution_at_commit(registered_context, monkeypatch):
    ctx = registered_context
    actual = ctx.ddb.transact_write_items

    def revoke_before_commit(**kwargs):
        ctx.ddb.update_item(
            TableName="authority",
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
            UpdateExpression="SET #st = :v",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":v": {"S": "cancelled"}},
        )
        return actual(**kwargs)

    monkeypatch.setattr(ctx.ddb, "transact_write_items", revoke_before_commit)
    with pytest.raises(RegistrationRefusedError):
        register(ctx)
    assert "control_generation" not in event(ctx)


def report(context, status="complete", **fields):
    context.service.record_status(credential_token=context.credential, pod=context.pod, status=status, fields=fields)


def test_terminal_report_revokes_execution_and_preserves_generation(registered_context):
    ctx = registered_context
    registration = register(ctx)
    report(ctx, summary="Finished")
    row = event(ctx)
    assert row["status"] == {"S": "complete"}
    assert row["control_generation"] == {"N": str(registration.generation)}
    assert not {"control_token", "control_address", "control_port", "control_token_expires_at"} & row.keys()
    assert ctx.store.authority.load_execution(invocation_id="run-a", tenant_id="tenant").status == "completed"
    with pytest.raises(RegistrationRefusedError):
        register(ctx)
    with pytest.raises(RegistrationRefusedError):
        report(ctx, status="in_progress")
    report(ctx, summary="Finished")
    with pytest.raises(RegistrationRefusedError):
        report(ctx, status="failed", error_message="Different outcome")
    ctx.service.clear_control(credential_token=ctx.credential, pod=ctx.pod, generation=registration.generation)
    assert event(ctx)["control_generation"] == {"N": "1"}


def test_terminal_cleanup_still_works_after_grant_revocation(registered_context):
    ctx = registered_context
    register(ctx)
    ctx.ddb.update_item(
        TableName="authority",
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1"}},
        UpdateExpression="SET revoked = :v",
        ExpressionAttributeValues={":v": {"BOOL": True}},
    )
    report(ctx, status="failed", stop_reason="Authority ended")
    assert "control_token" not in event(ctx)
    assert ctx.store.authority.load_execution(invocation_id="run-a", tenant_id="tenant").status == "completed"


@pytest.mark.parametrize("lost_response", [False, True])
def test_terminal_releases_only_own_reservation_once(registered_context, monkeypatch, lost_response):
    ctx = registered_context
    register(ctx)
    ctx.ddb.update_item(
        TableName="authority",
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
        UpdateExpression="SET parent_grant_id = :grant, dispatch_reservation_id = :reservation",
        ExpressionAttributeValues={":grant": {"S": "parent-grant"}, ":reservation": {"S": "own"}},
    )
    for sk, fields in [
        ("RESV#parent-grant", {"in_flight": {"N": "2"}, "total_dispatched": {"N": "2"}}),
        ("RESV#parent-grant#own", {"state": {"S": "held"}}),
        ("RESV#parent-grant#other", {"state": {"S": "held"}}),
    ]:
        ctx.ddb.put_item(TableName="authority", Item={"pk": {"S": "TENANT#tenant"}, "sk": {"S": sk}, **fields})
    if lost_response:

        def lose():
            raise BotoCoreError()

        wrap_first_write(ctx, monkeypatch, lose)
    report(ctx)
    report(ctx)
    counter = ctx.store._read("TENANT#tenant", "RESV#parent-grant")
    assert counter["in_flight"] == {"N": "1"}
    assert counter["total_dispatched"] == {"N": "2"}
    assert ctx.store._read("TENANT#tenant", "RESV#parent-grant#own")["state"] == {"S": "released"}
    assert ctx.store._read("TENANT#tenant", "RESV#parent-grant#other")["state"] == {"S": "held"}


def test_superseded_attempt_cannot_complete_or_clear(registered_context):
    ctx = registered_context
    register(ctx)
    ctx.ddb.update_item(
        TableName="authority",
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
        UpdateExpression="SET current_attempt = :next",
        ExpressionAttributeValues={":next": {"N": "2"}},
    )
    with pytest.raises(RegistrationRefusedError):
        report(ctx)
    with pytest.raises(RegistrationRefusedError):
        ctx.service.clear_control(credential_token=ctx.credential, pod=ctx.pod, generation=1)
    assert event(ctx)["control_token"] == {"S": "a" * 40}


def renewal(context, **overrides):
    return {
        "credential_token": context.credential,
        "pod": context.pod,
        "generation": 1,
        "expected_epoch": 1,
        "rotation_id": str(uuid4()),
        "token": "r" * 40,
        "token_expires_at": context.expiry,
        **overrides,
    }


@pytest.mark.parametrize("lost_response", [False, True])
def test_rotation_and_retry_keep_generation_and_advance_only_credential_epoch(registered_context, monkeypatch, lost_response):
    ctx = registered_context
    register(ctx)
    request = renewal(ctx)
    if lost_response:

        def lose():
            raise BotoCoreError()

        wrap_first_write(ctx, monkeypatch, lose)
    first = ctx.service.renew_control(**request)
    second = ctx.service.renew_control(**request)
    assert first == second
    assert first["control_credential_epoch"] == 2
    state = ctx.service.control_registration_state(credential_token=ctx.credential, pod=ctx.pod, generation=1)
    assert state == first
    assert "control_token" not in state
    assert event(ctx)["control_generation"] == {"N": "1"}
    assert event(ctx)["control_token"] == {"S": "r" * 40}
    assert event(ctx)["control_credential_epoch"] == {"N": "2"}
    with pytest.raises(RegistrationRefusedError):
        ctx.service.renew_control(**{**request, "token": "s" * 40})
    report(ctx)
    with pytest.raises(RegistrationRefusedError):
        ctx.service.renew_control(**request)
    with pytest.raises(RegistrationRefusedError):
        ctx.service.control_registration_state(credential_token=ctx.credential, pod=ctx.pod, generation=1)


def test_revocation_between_renewal_read_and_commit_preserves_old_token(registered_context, monkeypatch):
    ctx = registered_context
    register(ctx)
    actual = ctx.ddb.transact_write_items

    def revoke(**kwargs):
        ctx.ddb.update_item(
            TableName="authority",
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1"}},
            UpdateExpression="SET revoked = :v",
            ExpressionAttributeValues={":v": {"BOOL": True}},
        )
        return actual(**kwargs)

    monkeypatch.setattr(ctx.ddb, "transact_write_items", revoke)
    with pytest.raises(RegistrationRefusedError):
        ctx.service.renew_control(**renewal(ctx))
    assert event(ctx)["control_token"] == {"S": "a" * 40}

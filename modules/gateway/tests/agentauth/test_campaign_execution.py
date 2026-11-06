import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, envelope_digest, issue_bound_credential
from src.agentauth.campaign_execution import CampaignExecutionConflictError, CampaignExecutionRecovery
from src.agentauth.execution import ExecutionStateError
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.agentauth.routes import AgentRuntime
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import VerifiedPod

ENV = {"AGENT_RUN_CREDENTIAL_KEY": "campaign-isolated-test-signing-key"}


@pytest.fixture
def native():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="campaign-authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = BootstrapStore(table_name="campaign-authority", dynamodb_client=client)
        client.put_item(
            TableName=store.table,
            Item={
                "pk": {"S": "TENANT#tenant"},
                "sk": {"S": "AUTHORITY#approval"},
                "status": {"S": "active"},
                "human_id": {"S": "human"},
                "authority_kind": {"S": "github_event"},
            },
        )
        pods, credentials = {}, {}
        now = datetime.now(UTC)
        for invocation, parent in (("root", None), ("child", "root"), ("unrelated", None)):
            envelope = {
                "message_id": invocation,
                "tenant_id": "tenant",
                "persona": "developer",
                "arrived_at": now.isoformat(),
                "source_ref": {"repo": "example/project", "issue": 1},
            }
            metadata = {}
            if parent:
                envelope["correlation"] = {"parent_principal": f"{parent}#1"}
                metadata = {"parent_grant_id": {"S": f"grant-{parent}"}, "parent_grant_epoch": {"N": "1"}}
            grant = DelegatedGrant(
                grant_id=f"grant-{invocation}",
                tenant_id="tenant",
                principal=f"{invocation}#1",
                authority=AuthorityReference("github_event", "approval", "human", "tenant"),
                allowed_actions=frozenset({AgentAction.MONITOR}),
                delegable_actions=frozenset({AgentAction.MONITOR}),
                target_relationships=frozenset({TargetRelationship.SELF}),
                repo_scope=frozenset({"example/project"}),
                expires_at=now + timedelta(hours=1),
            )
            store.provision_pending(envelope=envelope, grant=grant, now=now, execution_metadata=metadata)
            pod = VerifiedPod(uid=f"pod-{invocation}", name=f"worker-{invocation}", namespace="agents", service_account="worker", ip="127.0.0.1")
            record = store.bind(invocation_id=invocation, digest=envelope_digest(envelope), pod=pod, now=now)
            pods[invocation] = pod
            credentials[invocation] = issue_bound_credential(record, now=now, env=ENV)["credential"]
        runtime = AgentRuntime(store=store, workloads=SimpleNamespace(verify=lambda token: pods[token]), env=ENV)
        yield SimpleNamespace(store=store, client=client, service=CampaignExecutionRecovery(store), runtime=runtime, credentials=credentials)


def retirement(native, invocation="root"):
    captured = native.service.capture(tenant="tenant", invocation=invocation, human="human")
    return {"tenant": "tenant", "invocation": invocation, "human": "human", "expected_version": captured["version"], "operation_id": str(uuid4())}


def test_enrollment_reconciles_a_lost_transaction_reply_without_retiring_authority(native, monkeypatch):
    arguments = retirement(native)
    transact = native.client.transact_write_items

    def lost(**kwargs):
        transact(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    monkeypatch.setattr(native.client, "transact_write_items", lost)
    with pytest.raises(AuthorityStoreError):
        native.service.enroll(**arguments)
    receipt = native.service.enroll(**arguments)
    assert receipt["status"] == "prepared"
    assert receipt["version"] == arguments["expected_version"]
    native.runtime.authenticate(native.credentials["root"], "root")
    native.runtime.authenticate(native.credentials["child"], "child")


def test_enrollment_transaction_refuses_a_binding_changed_after_reauthorization(native):
    arguments = retirement(native)

    def concurrent_change():
        execution = native.store._read("TENANT#tenant", "EXEC#root")
        execution["current_credential_epoch"] = {"N": "2"}
        native.client.put_item(TableName=native.store.table, Item=execution)

    with pytest.raises(AuthorityStoreError):
        native.service.enroll(**arguments, before_commit=concurrent_change)
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_ENROLL#{arguments['operation_id']}") is None


def test_retirement_fences_same_native_credential_and_fanout_without_touching_unrelated(native):
    root = retirement(native)
    child = retirement(native, "child")
    for invocation in ("root", "child", "unrelated"):
        native.runtime.authenticate(native.credentials[invocation], invocation)
    receipt = native.service.retire(**root)
    assert receipt["status"] == "authority_revoked"
    with pytest.raises(ExecutionStateError):
        native.runtime.authenticate(native.credentials["root"], "root")
    with pytest.raises(BootstrapRefusedError, match="parent delegation refused"):
        native.runtime.authenticate(native.credentials["child"], "child")
    native.runtime.authenticate(native.credentials["unrelated"], "unrelated")
    assert native.service.retire(**root) == receipt
    assert native.service.retire(**child)["status"] == "authority_revoked"
    assert native.store._read("TENANT#tenant", "GRANT#root#1")["revoked"] == {"BOOL": False}


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_attempt", {"N": "2"}),
        ("current_credential_epoch", {"N": "2"}),
        ("workload_binding", {"S": "replacement-pod"}),
        ("parent_principal", {"S": "unrelated#1"}),
        ("status", {"S": "completed"}),
    ],
)
def test_changed_execution_is_preserved(native, field, value):
    arguments = retirement(native)
    raw = native.store._read("TENANT#tenant", "EXEC#root")
    raw[field] = value
    native.client.put_item(TableName=native.store.table, Item=raw)
    with pytest.raises(CampaignExecutionConflictError):
        native.service.retire(**arguments)
    assert native.store._read("TENANT#tenant", "EXEC#root") == raw


@pytest.mark.parametrize(
    "row,field,value",
    [
        ("EXEC#root", "current_credential_epoch", {"N": "2"}),
        ("EXEC#root", "parent_principal", {"S": "unrelated#1"}),
        ("GRANT#root#1", "authority_human_id", {"S": "operator-replacement"}),
        ("AUTHORITY#approval", "status", {"S": "revoked"}),
    ],
)
def test_transaction_refuses_changes_after_final_read(native, monkeypatch, row, field, value):
    arguments = retirement(native)
    transact = native.client.transact_write_items

    def race(**kwargs):
        raw = native.store._read("TENANT#tenant", row)
        raw[field] = value
        native.client.put_item(TableName=native.store.table, Item=raw)
        return transact(**kwargs)

    monkeypatch.setattr(native.client, "transact_write_items", race)
    with pytest.raises(CampaignExecutionConflictError, match="raced"):
        native.service.retire(**arguments)
    assert native.store._read("TENANT#tenant", "EXEC#root")["status"] == {"S": "active"}
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_RETIRE#{arguments['operation_id']}") is None


def test_lost_transaction_reply_is_reconciled_and_receipt_cannot_target_another_execution(native, monkeypatch):
    arguments = retirement(native)
    transact = native.client.transact_write_items

    def lost(**kwargs):
        transact(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    monkeypatch.setattr(native.client, "transact_write_items", lost)
    with pytest.raises(AuthorityStoreError):
        native.service.retire(**arguments)
    monkeypatch.setattr(native.client, "transact_write_items", transact)
    receipt = native.service.retire(**arguments)
    assert receipt["status"] == "authority_revoked"
    other = retirement(native, "unrelated")
    other["operation_id"] = arguments["operation_id"]
    with pytest.raises(CampaignExecutionConflictError, match="receipt"):
        native.service.retire(**other)
    native.runtime.authenticate(native.credentials["unrelated"], "unrelated")


def test_receipt_replay_cannot_certify_a_new_epoch(native):
    arguments = retirement(native)
    native.service.retire(**arguments)
    raw = native.store._read("TENANT#tenant", "EXEC#root")
    raw["current_credential_epoch"] = {"N": "2"}
    native.client.put_item(TableName=native.store.table, Item=raw)
    with pytest.raises(CampaignExecutionConflictError, match="receipt"):
        native.service.retire(**arguments)


def test_foreign_owner_and_tenant_cannot_capture_or_retire(native):
    arguments = retirement(native)
    before = copy.deepcopy(native.store._read("TENANT#tenant", "EXEC#root"))
    for changes in ({"human": "another-human"}, {"tenant": "another-tenant"}):
        with pytest.raises(CampaignExecutionConflictError):
            native.service.retire(**{**arguments, **changes})
        with pytest.raises(CampaignExecutionConflictError):
            native.service.capture(**{**{key: arguments[key] for key in ("tenant", "invocation", "human")}, **changes})
    assert native.store._read("TENANT#tenant", "EXEC#root") == before

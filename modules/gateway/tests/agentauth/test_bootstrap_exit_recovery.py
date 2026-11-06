"""Provider evidence and Moto DynamoDB conditional transactions for historical bootstrap recovery."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.archived_worker_exit import ArchivedWorkerExitReader, ExitRecoveryRefusedError, ExitRecoveryUnavailableError
from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.bootstrap_exit_recovery import BootstrapExitRecovery, ExitRecoveryRequest
from tests.agentauth.test_abort_reconciliation import AUTHORITY, EVENTS, INVOCATION, TENANT, execution, put_event, read_event
from tests.agentauth.test_abort_reconciliation import client as client_fixture

client = client_fixture
UID = "11111111-1111-4111-8111-111111111111"
AUDIT = "22222222-2222-4222-8222-222222222222"
OPERATION = "33333333-3333-4333-8333-333333333333"
IMAGE = "sha256:" + "a" * 64
AUDIT_AT = "2026-09-24T12:01:00Z"


def audit_document():
    return {
        "kind": "Event",
        "apiVersion": "audit.k8s.io/v1",
        "level": "RequestResponse",
        "auditID": AUDIT,
        "stage": "ResponseComplete",
        "verb": "delete",
        "responseStatus": {"code": 200},
        "stageTimestamp": AUDIT_AT,
        "requestURI": "/api/v1/namespaces/adp-agents/pods/worker-1",
        "objectRef": {"resource": "pods", "namespace": "adp-agents", "name": "worker-1"},
        "responseObject": {
            "kind": "Pod",
            "apiVersion": "v1",
            "metadata": {"name": "worker-1", "uid": UID, "namespace": "adp-agents"},
            "spec": {
                "serviceAccountName": "agent-authority-worker-sa",
                "restartPolicy": "Never",
                "containers": [{"name": "agent-worker", "image": "registry/runtime@" + IMAGE}],
            },
            "status": {
                "phase": "Failed",
                "containerStatuses": [
                    {
                        "name": "agent-worker",
                        "imageID": "registry/runtime@" + IMAGE,
                        "state": {"terminated": {"exitCode": 1, "startedAt": "2026-09-24T12:00:00Z", "finishedAt": "2026-09-24T12:00:03Z"}},
                    }
                ],
            },
        },
    }


def reader(document=None):
    logs = Mock()
    logs.filter_log_events.return_value = {"events": [{"eventId": "provider-event", "message": json.dumps(document or audit_document())}]}
    service = ArchivedWorkerExitReader(
        client=logs,
        log_group="/aws/eks/adp-dev-eks-cluster/cluster",
        namespace="adp-agents",
        service_account="agent-authority-worker-sa",
        image_digests=frozenset({IMAGE}),
    )
    return service, logs


def observe(service):
    return service.observe(audit_id=AUDIT, audit_at=AUDIT_AT, name="worker-1", uid=UID, arrived_at="2026-09-24T11:59:00Z")


def test_provider_terminal_object_supplies_bounded_independent_evidence():
    service, logs = reader()
    result = observe(service)
    assert result.audit_id == AUDIT and result.image_digest == IMAGE and result.exit_code == 1
    assert len(result.digest) == 64
    args = logs.filter_log_events.call_args.kwargs
    assert args["endTime"] - args["startTime"] == 120001
    assert args["logGroupName"] == "/aws/eks/adp-dev-eks-cluster/cluster"
    assert args["filterPattern"] == '{ $.auditID = "' + AUDIT + '" }'


def test_optional_request_received_audit_stage_is_not_a_second_exit():
    service, logs = reader()
    received = {**audit_document(), "stage": "RequestReceived"}
    logs.filter_log_events.return_value["events"].append({"eventId": "request-stage", "message": json.dumps(received)})
    assert observe(service).exit_code == 1


@pytest.mark.parametrize(
    "path,value",
    [
        (("stage",), "RequestReceived"),
        (("verb",), "get"),
        (("responseStatus", "code"), 404),
        (("responseObject", "metadata", "uid"), "replacement"),
        (("responseObject", "metadata", "name"), "another"),
        (("responseObject", "metadata", "namespace"), "other"),
        (("responseObject", "spec", "serviceAccountName"), "shared"),
        (("responseObject", "spec", "restartPolicy"), "Always"),
        (("responseObject", "status", "phase"), "Running"),
        (("responseObject", "status", "containerStatuses", 0, "state"), {"running": {}}),
        (("responseObject", "status", "containerStatuses", 0, "state", "terminated", "exitCode"), True),
        (("responseObject", "status", "containerStatuses", 0, "imageID"), "unknown@sha256:" + "b" * 64),
        (("responseObject", "status", "containerStatuses", 0, "state", "terminated", "finishedAt"), "2026-09-24T12:02:00Z"),
        (("requestURI",), "/api/v1/namespaces/other/pods/worker-1"),
        (("responseObject",), {"status": {"phase": "Failed"}}),
    ],
)
def test_incomplete_or_foreign_audit_evidence_is_refused(path, value):
    doc = audit_document()
    current = doc
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value
    service, _ = reader(doc)
    with pytest.raises(ExitRecoveryRefusedError):
        observe(service)


@pytest.mark.parametrize("mode", ["missing", "duplicate", "pagination", "outage"])
def test_unknown_exit_never_becomes_a_verdict(mode):
    service, logs = reader()
    if mode == "missing":
        logs.filter_log_events.return_value = {"events": []}
    elif mode == "duplicate":
        logs.filter_log_events.return_value["events"].append({"eventId": "different-event", "message": json.dumps(audit_document())})
    elif mode == "pagination":
        logs.filter_log_events.return_value["nextToken"] = "same-token"
    else:
        logs.filter_log_events.side_effect = EndpointConnectionError(endpoint_url="https://audit.invalid")
    with pytest.raises((ExitRecoveryRefusedError, ExitRecoveryUnavailableError)):
        observe(service)


@pytest.fixture
def recovery(client):
    store = BootstrapStore(table_name=AUTHORITY, dynamodb_client=client)
    raw = execution(aborted=False)
    raw.update(current_credential_epoch={"N": "1"}, workload_binding={"S": UID}, spent_usd={"N": "2.50"}, budget_usd={"N": "5"})
    client.put_item(TableName=AUTHORITY, Item=raw)
    put_event(client, status="webhook_received", total_cost_usd={"N": "2.50"})
    pk = {"S": f"TENANT#{TENANT}"}
    records = [
        {
            "pk": pk,
            "sk": {"S": f"GRANT#{INVOCATION}#1"},
            "principal": {"S": f"{INVOCATION}#1"},
            "tenant_id": {"S": TENANT},
            "authority_human_id": {"S": "human"},
            "authority_org_id": {"S": TENANT},
            "authority_kind": {"S": "github_event"},
            "authority_reference_id": {"S": "root-event"},
            "revoked": {"BOOL": False},
            "revocation_epoch": {"N": "1"},
            "expires_at": {"S": "2026-09-24T12:01:10Z"},
        },
        {
            "pk": pk,
            "sk": {"S": "AUTHORITY#root-event"},
            "status": {"S": "active"},
            "human_id": {"S": "human"},
            "authority_kind": {"S": "github_event"},
        },
        {"pk": {"S": f"POD#{UID}"}, "sk": {"S": "BINDING"}, "invocation_id": {"S": INVOCATION}, "tenant_id": {"S": TENANT}, "attempt": {"N": "1"}},
    ]
    for item in records:
        client.put_item(TableName=AUTHORITY, Item=item)
    service, logs = reader()
    request = ExitRecoveryRequest(
        operation_id=OPERATION, expected_attempt=1, expected_credential_epoch=1, expected_workload_uid=UID, audit_id=AUDIT, audit_at=AUDIT_AT
    )
    return SimpleNamespace(
        store=store,
        raw=raw,
        records=records,
        service=BootstrapExitRecovery(store=store, events_table=EVENTS, reader=service),
        request=request,
        logs=logs,
    )


def recover(ctx, *, context=None, **kwargs):
    context = context or ctx.service.context(tenant=TENANT, invocation=INVOCATION)
    return ctx.service.recover(tenant=TENANT, invocation=INVOCATION, actor="human", context=context, request=ctx.request, **kwargs)


def test_atomic_failure_keeps_original_attempt_binding_cost_and_grants(recovery):
    ctx = recovery
    assert recover(ctx, preview=True)["status"] == "verified"
    assert read_event(ctx.store.client)["status"] == {"S": "webhook_received"}
    result = recover(ctx)
    assert result["status"] == "recovered" and result["outcome"] == "failed"
    current = ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}")
    assert current == {
        **ctx.raw,
        "status": {"S": "completed"},
        "terminal_outcome": {"S": "failed"},
        "terminal_reconciled_by": {"S": f"kubernetes-audit:{AUDIT}"},
    }
    event = read_event(ctx.store.client)
    assert event["status"] == {"S": "failed"} and event["total_cost_usd"] == {"N": "2.50"}
    for item in ctx.records:
        assert ctx.store._read(item["pk"]["S"], item["sk"]["S"]) == item
    # Repeating one operation reads its original receipt, without a new observation.
    ctx.logs.filter_log_events.reset_mock()
    assert recover(ctx) == result
    ctx.logs.filter_log_events.assert_not_called()


@pytest.mark.parametrize(
    "mutation", ["attempt", "epoch", "uid", "registration", "parent", "revocation", "binding", "owner", "terminal", "chain", "launch"]
)
def test_transaction_refuses_identity_or_completion_race(recovery, mutation):
    ctx = recovery
    context = ctx.service.context(tenant=TENANT, invocation=INVOCATION)
    if mutation in {"attempt", "epoch", "uid", "parent", "chain"}:
        key, value = {
            "attempt": ("current_attempt", {"N": "2"}),
            "epoch": ("current_credential_epoch", {"N": "2"}),
            "uid": ("workload_binding", {"S": "changed"}),
            "parent": ("parent_grant_id", {"S": "new-parent"}),
            "chain": ("chain_depth", {"N": "1"}),
        }[mutation]
        changed = {**ctx.raw, key: value}
        ctx.store.client.put_item(TableName=AUTHORITY, Item=changed)
    elif mutation == "registration":
        ctx.store.client.put_item(
            TableName=AUTHORITY, Item={"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": f"REG#{INVOCATION}#1"}, "generation": {"N": "1"}}
        )
    elif mutation == "terminal":
        put_event(ctx.store.client, status="complete")
    elif mutation == "launch":
        ctx.store.client.put_item(TableName=AUTHORITY, Item={**ctx.records[0], "launch_authority_human_id": {"S": "human"}})
    else:
        item = copy.deepcopy(ctx.records[2 if mutation == "binding" else 0])
        item[{"binding": "attempt", "owner": "authority_human_id", "revocation": "revoked"}[mutation]] = (
            {"N": "2"} if mutation == "binding" else {"S": "other"} if mutation == "owner" else {"BOOL": True}
        )
        ctx.store.client.put_item(TableName=AUTHORITY, Item=item)
    with pytest.raises(ExitRecoveryRefusedError):
        recover(ctx, context=context)
    assert read_event(ctx.store.client)["status"] == {"S": "complete" if mutation == "terminal" else "webhook_received"}
    assert ctx.service.receipt(tenant=TENANT, invocation=INVOCATION, attempt=1) is None


@pytest.mark.parametrize("field,value", [("expected_attempt", 2), ("expected_credential_epoch", 2), ("expected_workload_uid", OPERATION)])
def test_request_preconditions_refuse_before_provider_read(recovery, field, value):
    ctx = recovery
    ctx.request = ctx.request.model_copy(update={field: value})
    with pytest.raises(ExitRecoveryRefusedError):
        recover(ctx)
    ctx.logs.filter_log_events.assert_not_called()


def test_lost_transaction_response_reconciles_receipt_without_reset(recovery, monkeypatch):
    ctx = recovery
    original = ctx.store.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

    monkeypatch.setattr(ctx.store.client, "transact_write_items", lost)
    assert recover(ctx)["status"] == "recovered"
    ctx.request = ctx.request.model_copy(update={"operation_id": AUDIT})
    with pytest.raises(ExitRecoveryRefusedError):
        recover(ctx)


def test_failed_transaction_leaves_every_record_unchanged(recovery, monkeypatch):
    ctx = recovery
    monkeypatch.setattr(ctx.store.client, "transact_write_items", Mock(side_effect=EndpointConnectionError(endpoint_url="https://dynamodb.invalid")))
    with pytest.raises(ExitRecoveryUnavailableError):
        recover(ctx)
    assert read_event(ctx.store.client)["status"] == {"S": "webhook_received"}
    assert ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}") == ctx.raw


@pytest.mark.parametrize(
    "field",
    [
        "parent_grant_epoch",
        "dispatch_reservation_id",
        "orchestration_continuation_action",
        "orchestration_review_repairs",
        "orchestration_correction_receipt",
        "orchestration_node_attempt",
    ],
)
def test_partial_foreign_lineage_is_not_standalone_recovery(recovery, field):
    ctx = recovery
    ctx.store.client.put_item(TableName=AUTHORITY, Item={**ctx.raw, field: {"S": "partial"}})
    with pytest.raises(ExitRecoveryRefusedError):
        recover(ctx)
    ctx.logs.filter_log_events.assert_not_called()
    assert read_event(ctx.store.client)["status"] == {"S": "webhook_received"}

"""Recovery across outages and restarts, using real DynamoDB transactions in moto."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.retained_abort_recovery import recover_retained_abort_pods
from tests.agentauth.test_abort_reconciliation import (
    AUTHORITY,
    EVENTS,
    INVOCATION,
    TENANT,
    execution,
    put_event,
    read_event,
)
from tests.agentauth.test_abort_reconciliation import (
    client as client_fixture,
)

client = client_fixture


@pytest.fixture
def recovery(client):
    store = BootstrapStore(table_name=AUTHORITY, dynamodb_client=client)
    raw = execution()
    client.put_item(TableName=AUTHORITY, Item=raw)
    put_event(client)
    hint = {"name": "worker-1", "uid": "pod-uid-1", "invocation_id": INVOCATION, "tenant_id": TENANT}
    retention = SimpleNamespace(discover=Mock(return_value=([hint], "next-page")), release=Mock())
    workloads = SimpleNamespace(exit_retention=retention, has_exited=Mock(return_value=True))
    return SimpleNamespace(store=store, workloads=workloads, retention=retention, raw=raw, hint=hint)


def recover(ctx, **kwargs):
    return recover_retained_abort_pods(store=ctx.store, workloads=ctx.workloads, events_table=kwargs.pop("events_table", EVENTS), **kwargs)


def test_no_sql_claim_needed_and_release_follows_durable_retirement(recovery):
    ctx = recovery

    def release(**hint):
        assert hint == ctx.hint
        assert read_event(ctx.store.client)["status"] == {"S": "aborted"}
        raw = ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}")
        assert raw["status"] == {"S": "completed"}
        assert raw["terminal_outcome"] == {"S": "aborted"}

    ctx.retention.release.side_effect = release
    assert recover(ctx, cursor="previous-page") == (1, "next-page")
    ctx.retention.discover.assert_called_once_with(cursor="previous-page")
    ctx.retention.release.assert_called_once_with(**ctx.hint)


def test_table_outage_then_new_recovery_pass_keeps_evidence_until_success(recovery):
    ctx = recovery
    assert recover(ctx, events_table="unavailable-events")[0] == 0
    ctx.retention.release.assert_not_called()
    assert ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}")["status"] == {"S": "active"}
    # No in-memory retry state and no SQL session is carried between passes.
    ctx.store = BootstrapStore(table_name=AUTHORITY, dynamodb_client=ctx.store.client)
    assert recover(ctx)[0] == 1
    assert read_event(ctx.store.client)["status"] == {"S": "aborted"}


@pytest.mark.parametrize("field", ["tenant_id", "invocation_id", "pod_name", "workload_binding"])
def test_forged_discovery_hint_cannot_mutate_a_different_execution(recovery, field):
    ctx = recovery
    ctx.raw[field] = {"S": "different"}
    ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
    assert recover(ctx)[0] == 0
    ctx.workloads.has_exited.assert_not_called()
    ctx.retention.release.assert_not_called()
    assert read_event(ctx.store.client)["status"] == {"S": "in_progress"}


def test_live_or_missing_pod_does_not_become_terminal(recovery):
    ctx = recovery
    ctx.workloads.has_exited.return_value = False
    assert recover(ctx)[0] == 0
    ctx.retention.release.assert_not_called()
    assert read_event(ctx.store.client)["status"] == {"S": "in_progress"}


@pytest.mark.parametrize("status", ["active", "completed"])
def test_interrupted_acceptance_recovers_without_inventing_abort(recovery, status):
    ctx = recovery
    del ctx.raw["abort_command_id"]
    ctx.raw["status"] = {"S": status}
    ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
    if status == "completed":
        put_event(ctx.store.client, status="complete")
    assert recover(ctx)[0] == 1
    expected = "failed" if status == "active" else "complete"
    assert read_event(ctx.store.client)["status"] == {"S": expected}
    assert "abort_command_id" not in ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}")


@pytest.mark.parametrize("outcome", ["complete", "failed"])
def test_existing_terminal_report_is_preserved(recovery, outcome):
    ctx = recovery
    put_event(ctx.store.client, status=outcome)
    assert recover(ctx)[0] == 1
    assert read_event(ctx.store.client)["status"] == {"S": outcome}
    assert ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}")["terminal_outcome"] == {"S": outcome}


def test_authority_write_failure_retries_after_event_repair(recovery, monkeypatch):
    ctx = recovery
    original = ctx.store.client.transact_write_items

    def unavailable(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://authority.test")

    monkeypatch.setattr(ctx.store.client, "transact_write_items", unavailable)
    assert recover(ctx)[0] == 0
    assert read_event(ctx.store.client)["status"] == {"S": "aborted"}
    ctx.retention.release.assert_not_called()
    monkeypatch.setattr(ctx.store.client, "transact_write_items", original)
    assert recover(ctx)[0] == 1


def test_normal_terminal_report_wins_retirement_race(recovery, monkeypatch):
    ctx = recovery
    original = ctx.store.client.transact_write_items

    def concurrent_report(**kwargs):
        put_event(ctx.store.client, status="complete")
        ctx.raw.update(status={"S": "completed"}, terminal_outcome={"S": "complete"})
        ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
        return original(**kwargs)

    monkeypatch.setattr(ctx.store.client, "transact_write_items", concurrent_report)
    assert recover(ctx)[0] == 0
    ctx.retention.release.assert_not_called()
    monkeypatch.setattr(ctx.store.client, "transact_write_items", original)
    assert recover(ctx)[0] == 1
    assert read_event(ctx.store.client)["status"] == {"S": "complete"}
    assert ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}")["terminal_outcome"] == {"S": "complete"}


def test_finalizer_failure_retries_without_double_releasing_reservation(recovery):
    from src.agentauth.exit_retention import ExitRetentionError

    ctx = recovery
    ctx.raw.update(parent_grant_id={"S": "grant"}, dispatch_reservation_id={"S": "reservation"})
    ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
    for suffix, attributes in [
        ("RESV#grant", {"in_flight": {"N": "2"}}),
        ("RESV#grant#reservation", {"state": {"S": "held"}, "grant_id": {"S": "grant"}, "reservation_id": {"S": "reservation"}}),
    ]:
        ctx.store.client.put_item(
            TableName=AUTHORITY,
            Item={
                "pk": {"S": f"TENANT#{TENANT}"},
                "sk": {"S": suffix},
                **attributes,
            },
        )
    ctx.retention.release.side_effect = ExitRetentionError("conflict")
    assert recover(ctx)[0] == 0
    assert ctx.store._read(f"TENANT#{TENANT}", "RESV#grant")["in_flight"] == {"N": "1"}
    ctx.retention.release.side_effect = None
    assert recover(ctx)[0] == 1
    assert ctx.store._read(f"TENANT#{TENANT}", "RESV#grant")["in_flight"] == {"N": "1"}


async def test_production_maintenance_recovers_with_work_claims_disabled(recovery, monkeypatch):
    import asyncio

    from src.agentauth import retained_abort_recovery, routes
    from src.orchestration import work_admission

    ctx = recovery
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", EVENTS)
    monkeypatch.setattr(routes, "get_agent_runtime", lambda: ctx)
    original = retained_abort_recovery.recover_retained_abort_pods
    called = Mock(side_effect=original)
    monkeypatch.setattr(retained_abort_recovery, "recover_retained_abort_pods", called)

    async def end_after_one_pass(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(work_admission.asyncio, "sleep", end_after_one_pass)
    with pytest.raises(asyncio.CancelledError):
        await work_admission.maintain_work_claims()
    called.assert_called_once()
    ctx.retention.release.assert_called_once()
    assert read_event(ctx.store.client)["status"] == {"S": "aborted"}


@pytest.mark.parametrize("winner", ["abort", "normal-report"])
def test_interrupted_acceptance_transaction_loses_to_concurrent_winner(recovery, monkeypatch, winner):
    ctx = recovery
    del ctx.raw["abort_command_id"]
    ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
    original = ctx.store.client.transact_write_items

    def concurrent(**kwargs):
        if winner == "abort":
            ctx.store.authority.record_abort_intent(
                invocation_id=INVOCATION,
                tenant_id=TENANT,
                attempt=1,
                command_id="winning-abort",
                body_digest="a" * 64,
            )
        else:
            put_event(ctx.store.client, status="complete")
            ctx.raw.update(status={"S": "completed"}, terminal_outcome={"S": "complete"})
            ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
        return original(**kwargs)

    monkeypatch.setattr(ctx.store.client, "transact_write_items", concurrent)
    assert recover(ctx)[0] == 0
    ctx.retention.release.assert_not_called()
    assert read_event(ctx.store.client)["status"] == {"S": "in_progress" if winner == "abort" else "complete"}
    monkeypatch.setattr(ctx.store.client, "transact_write_items", original)
    assert recover(ctx)[0] == 1
    assert read_event(ctx.store.client)["status"] == {"S": "aborted" if winner == "abort" else "complete"}


def test_interrupted_acceptance_retirement_refuses_a_late_abort_marker(recovery):
    from src.agentauth.store import AbortIntentConflictError

    ctx = recovery
    del ctx.raw["abort_command_id"]
    ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
    assert recover(ctx)[0] == 1
    with pytest.raises(AbortIntentConflictError):
        ctx.store.authority.record_abort_intent(
            invocation_id=INVOCATION,
            tenant_id=TENANT,
            attempt=1,
            command_id="too-late",
            body_digest="a" * 64,
        )
    assert read_event(ctx.store.client)["status"] == {"S": "failed"}


@pytest.mark.parametrize("terminated", [False, True])
def test_composed_kubernetes_retention_survives_deletion_and_reporting_outage(recovery, tmp_path, terminated):
    import copy
    import json

    import httpx

    from src.agentauth.exit_retention import FINALIZER, LABEL
    from src.agentauth.exit_retention import INVOCATION as INVOCATION_ANNOTATION
    from src.agentauth.exit_retention import TENANT as TENANT_ANNOTATION
    from src.agentauth.workload import KubernetesWorkloadVerifier

    ctx = recovery
    token = tmp_path / "gateway-token"
    token.write_text("gateway-token")
    pod = {
        "metadata": {
            "name": "worker-1",
            "uid": "pod-uid-1",
            "resourceVersion": "10",
            "deletionTimestamp": "2026-09-24T10:00:00Z",
            "finalizers": ["other.example/keep", FINALIZER],
            "labels": {LABEL: "true"},
            "annotations": {INVOCATION_ANNOTATION: INVOCATION, TENANT_ANNOTATION: TENANT},
        },
        "spec": {"serviceAccountName": "agent-authority-worker-sa"},
        "status": {
            "phase": "Succeeded" if terminated else "Running",
            "containerStatuses": [
                {"name": "agent-worker", "state": {"terminated": {"exitCode": 0}} if terminated else {"running": {}}},
            ],
        },
    }
    patches = []

    def transport(request):
        assert request.headers["Authorization"] == "Bearer gateway-token"
        if request.url.path.endswith("/pods"):
            assert request.url.params["labelSelector"] == f"{LABEL}=true"
            return httpx.Response(200, json={"items": [copy.deepcopy(pod)], "metadata": {}})
        assert request.url.path == "/api/v1/namespaces/adp-agents/pods/worker-1"
        if request.method == "PATCH":
            operations = json.loads(request.content)
            assert operations[:2] == [
                {"op": "test", "path": "/metadata/uid", "value": "pod-uid-1"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "10"},
            ]
            # This is the actual release HTTP boundary: reporting and authority
            # retirement must already be durable, despite the deletion request.
            assert read_event(ctx.store.client)["status"] == {"S": "aborted"}
            assert ctx.store._read(f"TENANT#{TENANT}", f"EXEC#{INVOCATION}")["status"] == {"S": "completed"}
            patches.append(operations)
            for operation in operations[2:]:
                pod["metadata"][operation["path"].split("/")[-1]] = operation["value"]
        return httpx.Response(200, json=copy.deepcopy(pod))

    with httpx.Client(base_url="https://kubernetes.default.svc", transport=httpx.MockTransport(transport)) as client:

        def fresh_workloads():
            return KubernetesWorkloadVerifier(
                client=client,
                image_digests=frozenset({"sha256:" + "a" * 64}),
                namespace="adp-agents",
                service_account="agent-authority-worker-sa",
                gateway_token_path=token,
            )

        ctx.workloads = fresh_workloads()
        assert recover(ctx, events_table="unavailable-events")[0] == 0
        assert FINALIZER in pod["metadata"]["finalizers"]
        assert not patches
        ctx.workloads = fresh_workloads()
        ctx.store = BootstrapStore(table_name=AUTHORITY, dynamodb_client=ctx.store.client)
        assert recover(ctx)[0] == int(terminated)
    if terminated:
        assert pod["metadata"]["finalizers"] == ["other.example/keep"]
        assert len(patches) == 1
    else:
        assert FINALIZER in pod["metadata"]["finalizers"]
        assert not patches
        assert read_event(ctx.store.client)["status"] == {"S": "in_progress"}


@pytest.mark.parametrize("defect", ["missing-key", "wrong-tenant", "pending-authority", "retired-without-report", "missing-status"])
def test_interrupted_acceptance_keeps_unresolved_evidence(recovery, defect):
    ctx = recovery
    del ctx.raw["abort_command_id"]
    if defect == "missing-key":
        del ctx.raw["arrived_at"]
    elif defect == "pending-authority":
        ctx.raw["status"] = {"S": "pending"}
    elif defect == "retired-without-report":
        ctx.raw["status"] = {"S": "completed"}
    ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
    if defect == "wrong-tenant":
        put_event(ctx.store.client, tenant="other-tenant")
    elif defect == "missing-status":
        row = read_event(ctx.store.client)
        del row["status"]
        ctx.store.client.put_item(TableName=EVENTS, Item=row)
    assert recover(ctx)[0] == int(defect == "missing-status")
    if defect != "missing-status":
        ctx.retention.release.assert_not_called()
    else:
        assert read_event(ctx.store.client)["status"] == {"S": "failed"}


def test_incomplete_reservation_binding_keeps_exit_evidence(recovery):
    ctx = recovery
    ctx.raw["parent_grant_id"] = {"S": "grant"}
    ctx.store.client.put_item(TableName=AUTHORITY, Item=ctx.raw)
    assert recover(ctx)[0] == 0
    ctx.retention.release.assert_not_called()

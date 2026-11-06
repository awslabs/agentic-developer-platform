import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace
from uuid import UUID

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from tools_facade.encoding import canonical
from tools_facade.executions import ADMISSION_KEY, ExecutionStore


def decision(*, owner="person-one", tenant="tenant-one", team="team-one", run=None, pod=None, expiry=None):
    return SimpleNamespace(
        principal=SimpleNamespace(user_id=owner, tenant_id=tenant, team_id=team),
        run_id=run, workload_id=pod, run_expires_at=expiry, receipt_id="first-gateway-receipt",
    )


def test_owner_can_resume_after_restarting_store_and_refreshing_token():
    first = ExecutionStore(clock=lambda: 1000)
    identifier = first.admit(None, "websearch___search", decision())
    assert str(UUID(identifier)) == identifier
    resumed = ExecutionStore(clock=lambda: 1010)
    assert resumed.admit(UUID(identifier), "websearch___search", decision()) == identifier
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": identifier}}, ConsistentRead=True,
    )["Item"]
    assert row["deadline_at"]["N"] == "1900" and row["retention_ttl"]["N"] == str(1900 + 30 * 86400)
    assert row["schema_version"]["S"] == "1.0" and row["generation"]["N"] == "1"
    assert row["frozen_grants"]["S"] == '["websearch___search"]'


@pytest.mark.parametrize("other", [
    decision(owner="other"), decision(tenant="other"), decision(team="other"),
    decision(run="run-one", pod="pod-one", expiry=1700),
])
def test_foreign_ownership_and_binding_fail_without_revealing_record(other):
    store = ExecutionStore(clock=lambda: 1000)
    identifier = UUID(store.admit(None, "websearch___search", decision()))
    with pytest.raises(HTTPException) as failure:
        store.admit(identifier, "websearch___search", other)
    assert failure.value.status_code == 404


def test_workload_run_and_pod_binding_and_frozen_tool_survive_restart():
    origin = decision(run="run-one", pod="pod-one", expiry=1200)
    identifier = UUID(ExecutionStore(clock=lambda: 1000).admit(None, "websearch___search", origin))
    store = ExecutionStore(clock=lambda: 1001)
    assert store.admit(identifier, "websearch___search", origin) == str(identifier)
    for other in (decision(run="run-two", pod="pod-one", expiry=1200),
                  decision(run="run-one", pod="pod-two", expiry=1200)):
        with pytest.raises(HTTPException) as failure:
            store.admit(identifier, "websearch___search", other)
        assert failure.value.status_code == 404
    with pytest.raises(HTTPException) as failure:
        store.admit(identifier, "browser___open", origin)
    assert failure.value.status_code == 404
    with pytest.raises(HTTPException) as failure:
        ExecutionStore(clock=lambda: 1200).admit(identifier, "websearch___search", origin)
    assert failure.value.status_code == 403


def test_unavailable_store_and_expired_run_never_admit(monkeypatch):
    monkeypatch.delenv("ADP_TOOLS_EXECUTION_TABLE")
    with pytest.raises(HTTPException) as missing:
        ExecutionStore(clock=lambda: 1000).admit(None, "websearch___search", decision())
    assert missing.value.status_code == 503
    with pytest.raises(HTTPException) as expired:
        ExecutionStore(table="adp-tools-dev-executions", clock=lambda: 1000).admit(
            None, "websearch___search", decision(run="run-one", pod="pod-one", expiry=1000),
        )
    assert expired.value.status_code == 403


REQUEST = UUID("00000000-0000-4000-8000-000000000099")
TOOL = "websearch___search"
ARGUMENTS = {"query": "site:example.org"}


def test_jira_write_journal_records_original_binding_before_dispatch_and_never_reclaims(monkeypatch):
    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    original = decision()
    original.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    original.connection_id = "connection-one"
    tool = "atlassian___create_jira_issue"
    arguments = {"cloudId": "site-one", "fields": {"summary": "run-owned"}}
    original.atlassian_approval = {
        "tool": tool, "request_id": str(REQUEST),
        "payload_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
        "policy_revision": 1, "consent_source": "explicit",
    }
    store = ExecutionStore(clock=lambda: 1000)
    key, item, created = store.claim(None, REQUEST, tool, arguments, original)
    assert created is True
    assert json.loads(item["atlassian_binding"]["S"]) == {
        "connection": original.atlassian_connection, "release": release, "approval": original.atlassian_approval,
        "readback_locator": None,
    }
    store.dispatched(key)
    restarted = ExecutionStore(clock=lambda: 1001)
    _, prior, created = restarted.claim(None, REQUEST, tool, arguments, original)
    assert not created and prior["status"]["S"] == "unknown"
    with pytest.raises(HTTPException) as conflict:
        restarted.claim(None, REQUEST, tool, {**arguments, "fields": {"summary": "changed"}}, original)
    assert conflict.value.status_code == 409
    confirmed = store.confirmed(key, {"result": {"structuredContent": {"id": "JIRA-42"}}})
    receipt = restarted.receipt(confirmed)
    assert receipt["result"]["result"]["structuredContent"]["id"] == "JIRA-42"
    assert receipt["site_id"] == "site-one" and receipt["backend_generation"] == 1
    assert receipt["operation_id"] == key and receipt["tool"] == tool
    assert receipt["payload_sha256"] == item["request_digest"]["S"]


def test_jira_write_journal_requires_original_site_and_release(monkeypatch):
    original = decision()
    original.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    original.connection_id = "connection-one"
    original.atlassian_approval = {
        "tool": "atlassian___create_jira_issue", "request_id": str(REQUEST),
        "payload_sha256": hashlib.sha256(canonical({"cloudId": "site-other"})).hexdigest(),
        "policy_revision": 1, "consent_source": "explicit",
    }
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", "{}")
    with pytest.raises(HTTPException) as denied:
        ExecutionStore(clock=lambda: 1000).claim(
            None, REQUEST, "atlassian___create_jira_issue", {"cloudId": "site-other"}, original,
        )
    assert denied.value.status_code == 403


@pytest.mark.parametrize("family,arguments,ttl", [(TOOL, ARGUMENTS, None),
    ("code_interpreter___start", {}, 2), ("browser___start", {"url": "https://example.com"}, 2)])
@pytest.mark.parametrize("settled", [False, True])
def test_request_recovery_preserves_original_after_lifecycle_fence(family, arguments, ttl, settled):
    store = ExecutionStore(clock=lambda: 1000)
    key, original, _ = store.claim(None, REQUEST, family, arguments, decision(), ttl_seconds=ttl)
    store.dispatched(key)
    if settled:
        store.confirmed(key, {"result": {"original": "saved"}})
    identifier = UUID(original["operation_execution_id"]["S"])
    expected = store.lookup(identifier, REQUEST, family, decision())
    restarted = ExecutionStore(clock=lambda: 2000)
    restarted.close(identifier, family, decision())
    if settled:
        proof = {"execution_id": str(identifier), "status": "confirmed", "termination_confirmed": True,
                 "provider_status": "TERMINATED"}
        assert restarted.confirm_closed(identifier, family, decision(), proof)["cleanup_status"] == "confirmed"
    pause_admission()
    assert restarted.recover_request(REQUEST, family, arguments, decision(), ttl) == expected
    assert restarted.receipt(expected)["status"] == ("confirmed" if settled else "unknown")
    rows = store._client().scan(TableName=store._table())["Items"]
    assert len(rows) == 3  # Original root, original operation, and admission fence.


@pytest.mark.parametrize("mismatch", ["owner", "tenant", "team", "run", "pod", "tool", "arguments",
                                      "ttl", "execution", "request", "grants", "root_retention", "op_retention"])
def test_request_recovery_mismatches_never_disclose_or_create(mismatch):
    store = ExecutionStore(clock=lambda: 1000)
    origin = decision(run="run-one", pod="pod-one", expiry=3000)
    tool, arguments = "browser___start", {"url": "https://example.com"}
    key, original, _ = store.claim(None, REQUEST, tool, arguments, origin, ttl_seconds=2)
    store.dispatched(key)
    identifier = UUID(original["operation_execution_id"]["S"])
    other, request_id, ttl, execution_id = origin, REQUEST, 2, None
    if mismatch in {"owner", "tenant", "team", "run", "pod"}:
        values = {"run": "run-one", "pod": "pod-one", "expiry": 3000, mismatch: "other"}
        other = decision(**values)
    elif mismatch == "tool":
        tool = "code_interpreter___start"
    elif mismatch == "arguments":
        arguments = {"url": "https://changed.example"}
    elif mismatch == "ttl":
        ttl = None
    elif mismatch == "execution":
        execution_id = REQUEST
    elif mismatch == "request":
        request_id = identifier
    elif mismatch == "grants":
        store._client().update_item(TableName=store._table(), Key={"execution_id": {"S": str(identifier)}},
            UpdateExpression="SET frozen_grants = :none", ExpressionAttributeValues={":none": {"S": "[]"}})
    else:
        row_key = str(identifier) if mismatch == "root_retention" else key
        store._client().update_item(TableName=store._table(), Key={"execution_id": {"S": row_key}},
            UpdateExpression="SET retention_ttl = :expired", ExpressionAttributeValues={":expired": {"N": "1000"}})
    before = store._client().scan(TableName=store._table())["Items"]
    with pytest.raises(HTTPException) as missing:
        store.recover_request(request_id, tool, arguments, other, ttl, execution_id)
    assert missing.value.status_code == 404
    assert store._client().scan(TableName=store._table())["Items"] == before


@pytest.mark.parametrize("unknown", [True, False])
def test_direct_close_refences_legacy_search_and_requires_every_original(unknown):
    store, identifier, _ = legacy_search_with_sibling(unknown=unknown)
    client = store._client()
    key = {"execution_id": {"S": str(identifier)}}
    client.update_item(TableName=store._table(), Key=key,
        UpdateExpression="SET #state = :closed, cleanup_status = :confirmed REMOVE cleanup_partition, next_cleanup_at",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closed": {"S": "closed"}, ":confirmed": {"S": "confirmed"}})
    assert store.close(identifier, TOOL, decision())["cleanup_status"] == "unknown"
    result = store.confirm_closed(identifier, TOOL, decision())
    assert result["cleanup_status"] == ("unknown" if unknown else "confirmed")
    row = client.get_item(TableName=store._table(), Key=key, ConsistentRead=True)["Item"]
    if unknown:
        assert "cleanup_evidence" not in row and row["cleanup_partition"]["S"] == "due"
    else:
        assert row["cleanup_evidence"]["S"] == "historical_operations_settled"
        # Legacy Search proof is modern durable proof too; direct and scheduled
        # cleanup must not refence it or rescan after its evidence was persisted.
        assert store.close(identifier, TOOL, decision())["cleanup_status"] == "confirmed"
        client.update_item(TableName=store._table(), Key=key,
            UpdateExpression="SET cleanup_partition = :due, next_cleanup_at = :now",
            ExpressionAttributeValues={":due": {"S": "due"}, ":now": {"N": "1000"}})
        store.repair_terminal_due(client.get_item(TableName=store._table(), Key=key)["Item"])
        repaired = client.get_item(TableName=store._table(), Key=key)["Item"]
        assert repaired["state"]["S"] == "closed" and "cleanup_partition" not in repaired


def test_stale_scheduler_snapshot_cannot_refence_directly_proved_terminal_row():
    store = ExecutionStore(clock=lambda: 1000)
    identifier = UUID(store.admit(None, "browser___start", decision()))
    key = {"execution_id": {"S": str(identifier)}}
    client = store._client()
    client.update_item(TableName=store._table(), Key=key,
        UpdateExpression=("SET #state = :closed, cleanup_status = :unknown, "
                          "cleanup_evidence = :proof, next_cleanup_at = :now"),
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closed": {"S": "closed"}, ":unknown": {"S": "unknown"},
            ":proof": {"S": "provider_terminated"}, ":now": {"N": "1000"}})
    stale = client.get_item(TableName=store._table(), Key=key)["Item"]
    store.confirm_closed(identifier, "browser___start", decision(), {"execution_id": str(identifier),
        "status": "confirmed", "termination_confirmed": True, "provider_status": "TERMINATED"})
    proved = client.get_item(TableName=store._table(), Key=key)["Item"]
    assert store.cleanup_proved(proved)
    store.repair_terminal_due(stale)
    assert client.get_item(TableName=store._table(), Key=key)["Item"] == proved


def test_operation_evidence_is_atomic_and_never_hides_unknown_work():
    store = ExecutionStore(clock=lambda: 1000)
    first_key, first, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    identifier = UUID(first["operation_execution_id"]["S"])
    second_key, _, _ = store.claim(identifier, UUID("00000000-0000-4000-8000-000000000088"),
                                   TOOL, {"query": "second"}, decision())
    store.dispatched(first_key)
    store.dispatched(second_key)
    store.confirmed(first_key, {"result": {"result": {"status": "confirmed"}}})
    assert not store.stateless_ready(identifier)
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": str(identifier)}},
        ConsistentRead=True)["Item"]
    assert row["accepted_operations"]["N"] == "2"
    assert row["unresolved_operations"]["N"] == "1"
    assert store.claim(identifier, UUID("00000000-0000-4000-8000-000000000088"),
                       TOOL, {"query": "second"}, decision())[2] is False
    assert not store.stateless_ready(identifier)
    store.confirmed(second_key, {"result": {"result": {"status": "confirmed"}}})
    assert store.stateless_ready(identifier)


def test_legacy_execution_cannot_be_proved_complete_from_one_operation():
    store = ExecutionStore(clock=lambda: 1000)
    key, operation, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    identifier = UUID(operation["operation_execution_id"]["S"])
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(TableName="adp-tools-dev-executions", Key={"execution_id": {"S": str(identifier)}},
                       UpdateExpression="REMOVE accepted_operations, unresolved_operations, operation_tracking")
    store.dispatched(key)
    store.confirmed(key, {"result": {"result": {"status": "confirmed"}}})
    assert not store.stateless_ready(identifier)


def legacy_search_with_sibling(*, unknown=False):
    store = ExecutionStore(clock=lambda: 1000)
    first_key, first, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    identifier = UUID(first["operation_execution_id"]["S"])
    sibling_key, _, _ = store.claim(identifier, UUID("00000000-0000-4000-8000-000000000088"),
                                   TOOL, {"query": "second"}, decision())
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(TableName="adp-tools-dev-executions", Key={"execution_id": {"S": str(identifier)}},
                       UpdateExpression="REMOVE accepted_operations, unresolved_operations, operation_tracking")
    store.dispatched(first_key)
    store.dispatched(sibling_key)
    store.confirmed(first_key, {"result": {"result": {"status": "confirmed"}}})
    if not unknown:
        store.confirmed(sibling_key, {"result": {"result": {"status": "confirmed"}}})
    return store, identifier, sibling_key


def test_legacy_scan_requires_fence_and_every_sibling_before_owner_confirmation(monkeypatch):
    store, identifier, sibling_key = legacy_search_with_sibling(unknown=True)
    assert not store.stateless_ready(identifier)
    store.close(identifier, TOOL, decision())
    with pytest.raises(HTTPException) as refused:
        store.claim(identifier, UUID("00000000-0000-4000-8000-000000000077"), TOOL,
                    {"query": "too late"}, decision())
    assert refused.value.status_code == 403
    client = store._client()
    original_scan = client.scan
    pages = []

    def paged_scan(**kwargs):
        assert kwargs["ConsistentRead"] is True
        assert kwargs["FilterExpression"] == "operation_execution_id = :execution"
        kwargs["Limit"] = 1
        page = original_scan(**kwargs)
        pages.append(page)
        return page

    monkeypatch.setattr(client, "scan", paged_scan)
    store.client = client
    assert not store.stateless_ready(identifier)
    assert store.confirm_closed(identifier, TOOL, decision())["state"] == "closing"
    store.confirmed(sibling_key, {"result": {"result": {"status": "confirmed"}}})
    restarted = ExecutionStore(clock=lambda: 1001, client=client)
    assert restarted.stateless_ready(identifier)
    assert len(pages) > 1
    assert restarted.confirm_closed(identifier, TOOL, decision())["cleanup_status"] == "confirmed"
    saved = client.get_item(TableName="adp-tools-dev-executions", Key={"execution_id": {"S": str(identifier)}},
                            ConsistentRead=True)["Item"]
    assert saved["cleanup_evidence"]["S"] == "historical_operations_settled"
    assert "cleanup_partition" not in saved and "operation_tracking" not in saved


def test_legacy_scan_never_treats_truncated_page_as_exhaustive(monkeypatch):
    store, identifier, _ = legacy_search_with_sibling()
    store.close(identifier, TOOL, decision())
    client = store._client()
    original_scan = client.scan
    calls = []

    def incomplete_scan(**kwargs):
        calls.append(kwargs)
        page = original_scan(**kwargs)
        page["LastEvaluatedKey"] = {"execution_id": {"S": f"unseen-{len(calls)}"}}
        return page

    monkeypatch.setattr(client, "scan", incomplete_scan)
    store.client = client
    assert not store.stateless_ready(identifier)
    assert len(calls) == 10
    assert store.confirm_closed(identifier, TOOL, decision())["state"] == "closing"
    saved = client.get_item(TableName="adp-tools-dev-executions",
                            Key={"execution_id": {"S": str(identifier)}}, ConsistentRead=True)["Item"]
    assert saved["state"]["S"] == "closing"


def test_legacy_scan_rejects_expired_or_unreadable_operation_evidence():
    store, identifier, sibling_key = legacy_search_with_sibling()
    store.close(identifier, TOOL, decision())
    client = store._client()
    client.update_item(TableName="adp-tools-dev-executions", Key={"execution_id": {"S": sibling_key}},
                       UpdateExpression="REMOVE result_status")
    assert not store.stateless_ready(identifier)
    assert store.confirm_closed(identifier, TOOL, decision())["state"] == "closing"
    client.update_item(TableName="adp-tools-dev-executions", Key={"execution_id": {"S": sibling_key}},
                       UpdateExpression="SET result_status = :success, retention_ttl = :expired",
                       ExpressionAttributeValues={":success": {"S": "success"}, ":expired": {"N": "1000"}})
    assert not store.stateless_ready(identifier)


def pause_admission():
    boto3.client("dynamodb", region_name="us-east-1").put_item(
        TableName="adp-tools-dev-executions",
        Item={"execution_id": {"S": ADMISSION_KEY}, "schema_version": {"S": "1.0"}},
        ConditionExpression="attribute_not_exists(execution_id)",
    )


def test_admission_off_survives_restart_without_removing_uncertain_reservations():
    store = ExecutionStore(clock=lambda: 1000)
    key, original, created = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert created
    store.dispatched(key)
    receipt = store.lookup(UUID(original["operation_execution_id"]["S"]), REQUEST, TOOL, decision())
    assert receipt["status"]["S"] == "unknown"
    pause_admission()
    restarted = ExecutionStore(clock=lambda: 1001)
    with pytest.raises(HTTPException) as denied:
        restarted.claim(None, UUID("00000000-0000-4000-8000-000000000088"), TOOL, ARGUMENTS, decision())
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        restarted.admit(None, TOOL, decision())
    assert denied.value.status_code == 403
    _, retry, created = restarted.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert not created and retry == receipt
    confirmed = restarted.confirmed(key, {"jsonrpc": "2.0", "result": {"text": "paid"}})
    assert restarted.lookup(UUID(original["operation_execution_id"]["S"]), REQUEST, TOOL, decision()) == confirmed
    assert confirmed["receipt_id"]["S"] == original["receipt_id"]["S"]
    assert confirmed["request_id"]["S"] == original["request_id"]["S"]
    assert confirmed["dispatch_intent"]["S"] == "dispatched"
    assert confirmed["retention_ttl"] == original["retention_ttl"]
    assert restarted.result_chunk(confirmed, 0, 8192)["data_base64"]
    restarted.close(UUID(original["operation_execution_id"]["S"]), TOOL, decision())
    assert restarted.lookup(UUID(original["operation_execution_id"]["S"]), REQUEST, TOOL, decision()) == confirmed
    ExecutionStore(clock=lambda: 1900).fence_due()
    assert restarted.lookup(UUID(original["operation_execution_id"]["S"]), REQUEST, TOOL, decision()) == confirmed


def test_admission_off_fences_preexisting_pending_dispatch_without_erasing_receipt():
    store = ExecutionStore(clock=lambda: 1000)
    key, operation, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    pause_admission()
    with pytest.raises(HTTPException) as denied:
        ExecutionStore(clock=lambda: 1001).dispatched(key)
    assert denied.value.status_code == 403
    identifier = UUID(operation["operation_execution_id"]["S"])
    saved = ExecutionStore(clock=lambda: 1001).lookup(identifier, REQUEST, TOOL, decision())
    assert saved["status"]["S"] == "pending" and saved["dispatch_intent"]["S"] == "claimed"
    _, replay, created = ExecutionStore(clock=lambda: 1001).claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert not created and replay == saved
    with pytest.raises(HTTPException) as foreign:
        store.lookup(identifier, REQUEST, TOOL, decision(owner="another-person"))
    assert foreign.value.status_code == 404
    ExecutionStore(clock=lambda: 1900).fence_due()
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": str(identifier)}}, ConsistentRead=True,
    )["Item"]
    assert row["state"]["S"] == "closing" and row["cleanup_status"]["S"] == "unknown"
    assert ExecutionStore(clock=lambda: 1900).lookup(identifier, REQUEST, TOOL, decision()) == saved


def test_concurrent_same_request_has_one_execution_and_one_durable_operation():
    client = boto3.client("dynamodb", region_name="us-east-1")
    transaction_lock = Lock()

    class AtomicMotoClient:
        def transact_write_items(self, **kwargs):
            with transaction_lock:
                return client.transact_write_items(**kwargs)

        def get_item(self, **kwargs):
            with transaction_lock:
                return client.get_item(**kwargs)

    atomic_client = AtomicMotoClient()

    def claim():
        return ExecutionStore(client=atomic_client, table="adp-tools-dev-executions", clock=lambda: 1000).claim(
            None, REQUEST, TOOL, ARGUMENTS, decision(),
        )

    with ThreadPoolExecutor(max_workers=6) as workers:
        attempts = list(workers.map(lambda _: claim(), range(6)))
    assert sum(created for _, _, created in attempts) == 1
    assert len({operation["operation_execution_id"]["S"] for _, operation, _ in attempts}) == 1
    assert {operation["receipt_id"]["S"] for _, operation, _ in attempts} == {"first-gateway-receipt"}
    rows = boto3.client("dynamodb", region_name="us-east-1").scan(TableName="adp-tools-dev-executions")["Items"]
    assert len(rows) == 2


def test_changed_payload_conflicts_and_foreign_run_cannot_probe_payload():
    store = ExecutionStore(clock=lambda: 1000)
    store.claim(None, REQUEST, TOOL, ARGUMENTS, decision(run="run-one", pod="pod-one", expiry=1200))
    with pytest.raises(HTTPException) as conflict:
        store.claim(None, REQUEST, TOOL, {"query": "different"}, decision(run="run-one", pod="pod-one", expiry=1200))
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as foreign:
        store.claim(None, REQUEST, TOOL, {"query": "different"}, decision(run="run-two", pod="pod-one", expiry=1200))
    assert foreign.value.status_code == 404


def test_restart_before_and_after_dispatch_never_replays_paid_mutation():
    store = ExecutionStore(clock=lambda: 1000)
    key, original, created = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert created and original["status"]["S"] == "pending"
    key_again, pending, created = ExecutionStore(clock=lambda: 1001).claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert key_again == key and not created and pending["status"]["S"] == "pending"
    store.dispatched(key)
    _, unknown, created = ExecutionStore(clock=lambda: 1002).claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert not created and unknown["status"]["S"] == "unknown"
    assert unknown["dispatch_intent"]["S"] == "dispatched"
    confirmed = store.confirmed(key, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "failed"}})
    receipt = store.receipt(confirmed)
    assert receipt["status"] == "confirmed" and receipt["result_status"] == "error"
    _, replay, created = ExecutionStore(clock=lambda: 1003).claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert not created and store.receipt(replay) == receipt


def test_lost_transaction_reply_reads_original_claim_without_resubmitting():
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")

    class LostResponse:
        def transact_write_items(self, **kwargs):
            dynamodb.transact_write_items(**kwargs)
            raise ClientError({"Error": {"Code": "RequestTimeout"}}, "TransactWriteItems")

        def get_item(self, **kwargs):
            return dynamodb.get_item(**kwargs)

    store = ExecutionStore(client=LostResponse(), table="adp-tools-dev-executions", clock=lambda: 1000)
    _, pending, created = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert not created and pending["status"]["S"] == "pending"
    _, recovered, created = ExecutionStore(clock=lambda: 1001).claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert not created and store.receipt(recovered) == store.receipt(pending)


def test_deadline_worker_fences_original_execution_and_preserves_unknown_cleanup():
    store = ExecutionStore(clock=lambda: 1000)
    key, operation, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS,
                                    decision(run="run-one", pod="pod-one", expiry=1200))
    identifier = operation["operation_execution_id"]["S"]
    ExecutionStore(clock=lambda: 1199).fence_due()
    assert store.admit(UUID(identifier), TOOL, decision(run="run-one", pod="pod-one", expiry=1200)) == identifier
    ExecutionStore(clock=lambda: 1200).fence_due()
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": identifier}}, ConsistentRead=True,
    )["Item"]
    assert row["state"]["S"] == "closing" and row["cleanup_status"]["S"] == "unknown"

    assert row["retention_ttl"]["N"] == str(1200 + 30 * 86400)
    ExecutionStore(clock=lambda: 1300).fence_due()
    with pytest.raises(HTTPException) as refused:
        store.dispatched(key)
    assert refused.value.status_code == 403
    assert store.receipt(operation)["status"] == "pending"


def test_lost_dispatch_reply_cannot_be_mistaken_for_expired_run_or_replayed():
    store = ExecutionStore(clock=lambda: 1000)
    key, original, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")

    class LostResponse:
        def get_item(self, **kwargs):
            return dynamodb.get_item(**kwargs)

        def transact_write_items(self, **kwargs):
            dynamodb.transact_write_items(**kwargs)
            raise ClientError({"Error": {"Code": "RequestTimeout"}}, "TransactWriteItems")

    uncertain = ExecutionStore(client=LostResponse(), table="adp-tools-dev-executions", clock=lambda: 1000)
    with pytest.raises(HTTPException) as failure:
        uncertain.dispatched(key)
    assert failure.value.status_code == 503
    _, recovered, created = ExecutionStore(clock=lambda: 1001).claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    assert not created and recovered["status"]["S"] == "unknown"
    assert recovered["operation_execution_id"] == original["operation_execution_id"]


def test_dispatch_rechecks_deadline_even_if_reconciler_has_not_run():
    now = [1000]
    store = ExecutionStore(clock=lambda: now[0])
    key, operation, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    now[0] = 1900
    with pytest.raises(HTTPException) as refused:
        store.dispatched(key)
    assert refused.value.status_code == 403
    assert store.receipt(operation)["status"] == "pending"
    ExecutionStore(clock=lambda: 1900).fence_due()
    identifier = operation["operation_execution_id"]["S"]
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": identifier}}, ConsistentRead=True,
    )["Item"]
    assert row["state"]["S"] == "closing" and row["cleanup_status"]["S"] == "unknown"


def test_concurrent_deadline_workers_do_not_widen_cleanup_or_invent_a_handle():
    store = ExecutionStore(clock=lambda: 1000)
    _, operation, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    client = boto3.client("dynamodb", region_name="us-east-1")
    call_lock = Lock()

    class AtomicMotoClient:
        def query(self, **kwargs):
            with call_lock:
                return client.query(**kwargs)

        def update_item(self, **kwargs):
            with call_lock:
                return client.update_item(**kwargs)

    atomic_client = AtomicMotoClient()
    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(lambda _: ExecutionStore(client=atomic_client, clock=lambda: 1900).fence_due(), range(3)))
    identifier = operation["operation_execution_id"]["S"]
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": identifier}}, ConsistentRead=True,
    )["Item"]
    assert row["state"]["S"] == "closing" and row["cleanup_status"]["S"] == "unknown"
    assert "provider_handle" not in row and "cleanup_evidence" not in row


@pytest.mark.parametrize("dispatch_first", [True, False])
def test_close_and_dispatch_are_ordered_without_claiming_provider_stop(dispatch_first):
    store = ExecutionStore(clock=lambda: 1000)
    key, operation, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    identifier = UUID(operation["operation_execution_id"]["S"])
    if dispatch_first:
        store.dispatched(key)
    first = store.close(identifier, TOOL, decision())
    assert first == {"execution_id": str(identifier), "state": "closing", "cleanup_status": "unknown"}
    assert ExecutionStore(clock=lambda: 1001).close(identifier, TOOL, decision()) == first
    if not dispatch_first:
        with pytest.raises(HTTPException) as failure:
            store.dispatched(key)
        assert failure.value.status_code == 403
    saved = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": key}}, ConsistentRead=True,
    )["Item"]
    assert saved["status"]["S"] == ("unknown" if dispatch_first else "pending")
    with pytest.raises(HTTPException) as refused:
        store.claim(identifier, UUID("00000000-0000-4000-8000-000000000098"), TOOL, ARGUMENTS, decision())
    assert refused.value.status_code == 403


def test_concurrent_dispatch_and_close_have_only_two_safe_outcomes():
    store = ExecutionStore(clock=lambda: 1000)
    key, operation, _ = store.claim(None, REQUEST, TOOL, ARGUMENTS, decision())
    identifier = UUID(operation["operation_execution_id"]["S"])

    def dispatch():
        try:
            store.dispatched(key)
            return "dispatched"
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        dispatched = workers.submit(dispatch)
        closed = workers.submit(store.close, identifier, TOOL, decision())
        assert closed.result()["cleanup_status"] == "unknown"
        result = dispatched.result()
    assert result in ("dispatched", 403)
    saved = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": key}}, ConsistentRead=True,
    )["Item"]
    assert saved["status"]["S"] == ("unknown" if result == "dispatched" else "pending")


@pytest.mark.parametrize("other", [
    decision(owner="other"), decision(tenant="other"), decision(team="other"),
    decision(run="run-one", pod="pod-one", expiry=1700),
])
def test_only_original_owner_and_run_can_close(other):
    store = ExecutionStore(clock=lambda: 1000)
    identifier = UUID(store.admit(None, TOOL, decision()))
    with pytest.raises(HTTPException) as failure:
        store.close(identifier, TOOL, other)
    assert failure.value.status_code == 404
    with pytest.raises(HTTPException) as failure:
        store.close(identifier, "browser___open", decision())
    assert failure.value.status_code == 404
    assert store.admit(identifier, TOOL, decision()) == str(identifier)


def test_owned_browser_step_reference_fences_original_execution():
    store = ExecutionStore(clock=lambda: 1000)
    allowed = ["browser___start", "browser___step", "browser___close"]
    _, start, _ = store.claim(None, REQUEST, "browser___start", {"url": "https://example.org"},
                              decision(), frozen_grants=allowed)
    identifier = UUID(start["operation_execution_id"]["S"])
    assert store.close(identifier, "browser___step", decision()) == {
        "execution_id": str(identifier), "state": "closing", "cleanup_status": "unknown"}
    assert store.confirm_closed(identifier, "browser___step", decision())["state"] == "closing"
    proof = {"execution_id": str(identifier), "status": "confirmed", "termination_confirmed": True,
             "provider_status": "TERMINATED"}
    assert store.confirm_closed(identifier, "browser___step", decision(), proof)["state"] == "closed"
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": str(identifier)}})["Item"]
    assert row["tool"]["S"] == "browser___start"
    assert row["cleanup_evidence"]["S"] == "provider_terminated"
    assert row["cleanup_attempts"]["N"] == "1"
    assert "cleanup_partition" not in row and "next_cleanup_at" not in row


def test_interpreter_session_preserves_frozen_tools_without_cross_family_widening():
    start_tool = "code_interpreter___start"
    execute_tool = "code_interpreter___execute"
    allowed = [start_tool, execute_tool, "code_interpreter___file", "code_interpreter___result"]
    store = ExecutionStore(clock=lambda: 1000)
    start_id = UUID("00000000-0000-4000-8000-000000000021")
    execute_id = UUID("00000000-0000-4000-8000-000000000022")
    _, start, created = store.claim(None, start_id, start_tool, {}, decision(), frozen_grants=allowed)
    assert created
    identifier = UUID(start["operation_execution_id"]["S"])
    restarted = ExecutionStore(clock=lambda: 1001)
    assert restarted.admit(identifier, execute_tool, decision()) == str(identifier)
    key, executed, fresh = restarted.claim(identifier, execute_id, execute_tool, {"code": "print(42)"}, decision())
    assert fresh and executed["operation_execution_id"]["S"] == str(identifier)
    assert restarted.lookup(identifier, execute_id, execute_tool, decision()) == executed
    with pytest.raises(HTTPException) as missing_grant:
        restarted.claim(identifier, UUID("00000000-0000-4000-8000-000000000023"),
                        "code_interpreter___close", {}, decision())
    assert missing_grant.value.status_code == 404
    with pytest.raises(HTTPException) as foreign_family:
        restarted.admit(identifier, "browser___step", decision())
    assert foreign_family.value.status_code == 404
    with pytest.raises(HTTPException) as foreign_owner:
        restarted.admit(identifier, execute_tool, decision(owner="other-person"))
    assert foreign_owner.value.status_code == 404
    original = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": str(identifier)}})["Item"]
    assert json.loads(original["frozen_grants"]["S"]) == allowed
    assert original["tool"]["S"] == start_tool
    assert restarted.close(identifier, execute_tool, decision())["state"] == "closing"
    assert restarted.confirm_closed(identifier, execute_tool, decision())["state"] == "closing"
    proof = {"execution_id": str(identifier), "status": "confirmed", "termination_confirmed": True}
    assert restarted.confirm_closed(identifier, execute_tool, decision(), proof)["state"] == "closed"
    with pytest.raises(HTTPException) as fenced:
        restarted.claim(identifier, UUID("00000000-0000-4000-8000-000000000024"),
                        execute_tool, {}, decision())
    assert fenced.value.status_code == 403
    assert restarted.lookup(identifier, execute_id, execute_tool, decision()) == executed


def test_preexisting_interpreter_start_only_record_does_not_gain_new_operations():
    store = ExecutionStore(clock=lambda: 1000)
    identifier = UUID(store.admit(None, "code_interpreter___start", decision()))
    with pytest.raises(HTTPException) as refused:
        store.admit(identifier, "code_interpreter___execute", decision())
    assert refused.value.status_code == 404
    assert store.admit(identifier, "code_interpreter___start", decision()) == str(identifier)


def test_start_lifetime_is_frozen_at_admission_and_bound_to_request_identity():
    start_tool = "browser___start"
    arguments = {"url": "https://example.org"}
    store = ExecutionStore(clock=lambda: 1000)
    key, operation, created = store.claim(None, REQUEST, start_tool, arguments, decision(), ttl_seconds=300)
    assert created and operation["execution_deadline_at"]["N"] == "1300"
    identifier = UUID(operation["operation_execution_id"]["S"])
    assert store.admit(identifier, start_tool, decision()) == str(identifier)
    retry = ExecutionStore(clock=lambda: 1100)
    assert retry.claim(None, REQUEST, start_tool, arguments, decision(), ttl_seconds=300) == (key, operation, False)
    assert retry.claim(identifier, REQUEST, start_tool, arguments, decision(),
                       ttl_seconds=300) == (key, operation, False)
    for changed in (301, None):
        with pytest.raises(HTTPException) as mismatch:
            retry.claim(None, REQUEST, start_tool, arguments, decision(), ttl_seconds=changed)
        assert mismatch.value.status_code == 409
    with pytest.raises(HTTPException) as mismatch:
        retry.claim(identifier, REQUEST, start_tool, arguments, decision(), ttl_seconds=301)
    assert mismatch.value.status_code == 409
    with pytest.raises(HTTPException) as foreign:
        retry.claim(identifier, REQUEST, start_tool, arguments, decision(team="foreign"), ttl_seconds=301)
    assert foreign.value.status_code == 404
    with pytest.raises(HTTPException) as expired:
        ExecutionStore(clock=lambda: 1300).dispatched(key)
    assert expired.value.status_code == 403


@pytest.mark.parametrize("lifetime", [0, 901, True, 1.5, "300"])
def test_invalid_start_lifetime_never_claims_an_execution(lifetime):
    with pytest.raises(HTTPException) as invalid:
        ExecutionStore(clock=lambda: 1000).claim(None, REQUEST, "browser___start", {"url": "https://example.org"},
                                                  decision(), ttl_seconds=lifetime)
    assert invalid.value.status_code == 422
    with pytest.raises(HTTPException) as unsupported:
        ExecutionStore(clock=lambda: 1000).claim(None, REQUEST, TOOL, ARGUMENTS, decision(), ttl_seconds=300)
    assert unsupported.value.status_code == 422


@pytest.mark.parametrize("invalid", [0, 901, True, "300", 1.5])
def test_facade_rejects_invalid_lifetime_at_request_boundary(invalid):
    from pydantic import ValidationError

    from tools_facade.server import ToolCall

    with pytest.raises(ValidationError):
        ToolCall.model_validate({"tool": "browser___start", "request_id": str(REQUEST),
                                 "arguments": {"url": "https://example.org"}, "ttl_seconds": invalid})


def test_start_on_existing_execution_uses_remaining_root_deadline():
    store = ExecutionStore(clock=lambda: 1000)
    _, original, _ = store.claim(None, REQUEST, "browser___start", {"url": "https://example.org"},
                                 decision(), frozen_grants=["browser___start"])
    identifier = UUID(original["operation_execution_id"]["S"])
    _, child, created = ExecutionStore(clock=lambda: 1100).claim(
        identifier, UUID("00000000-0000-4000-8000-000000000079"), "browser___start",
        {"url": "https://example.net"}, decision())
    assert created and child["execution_deadline_at"] == original["execution_deadline_at"]
    with pytest.raises(HTTPException) as changed:
        ExecutionStore(clock=lambda: 1100).claim(identifier,
            UUID("00000000-0000-4000-8000-000000000080"), "browser___start",
            {"url": "https://example.net"}, decision(), ttl_seconds=300)
    assert changed.value.status_code == 422

@pytest.mark.parametrize("alias", ["create_jira_issue", "update_jira_issue",
                                   "comment_jira_issue", "transition_jira_issue"])
def test_jira_write_report_survives_restart_and_changed_payload_cannot_redispatch(monkeypatch, alias):
    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    owner = decision(run="run-one", pod="pod-one", expiry=3000)
    owner.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    owner.connection_id = "connection-one"
    tool = "atlassian___" + alias
    arguments = {"cloudId": "site-one", "fields": {"summary": "owned"}}
    owner.atlassian_approval = {"tool": tool, "request_id": str(REQUEST),
                                "payload_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
                                "policy_revision": 1, "consent_source": "explicit"}
    store = ExecutionStore(clock=lambda: 1000)
    key, claimed, created = store.claim(None, REQUEST, tool, arguments, owner)
    assert created and claimed["dispatch_intent"]["S"] == "claimed"
    assert store.due_write_reports() == []
    store.dispatched(key)
    restarted = ExecutionStore(clock=lambda: 1001)
    unknown = restarted.due_write_reports()[0]
    assert unknown["execution_id"]["S"] == key
    assert restarted.receipt(unknown)["report_status"] == "pending"
    assert restarted.receipt(unknown)["status"] == "unknown"
    assert restarted.claim(None, REQUEST, tool, arguments, owner)[2] is False
    with pytest.raises(HTTPException) as conflict:
        restarted.claim(None, REQUEST, tool, {**arguments, "fields": {"summary": "changed"}}, owner)
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException):
        restarted.dispatched(key)
    with pytest.raises(HTTPException) as unverified:
        store.confirmed(key, {"error": {"message": "provider unavailable"}})
    assert unverified.value.status_code == 503
    assert store.finish_write_report(unknown, recorded=False)["write_report_status"]["S"] == "pending"
    assert restarted.due_write_reports() == []
    restarted.clock = lambda: 1061
    retry = restarted.due_write_reports()[0]
    assert retry["status"]["S"] == "unknown"
    assert restarted.finish_write_report(retry, recorded=True)["write_report_status"]["S"] == "recorded"
    confirmed = store.confirmed(key, {"result": {"structuredContent": {"id": "JIRA-42"}}})
    assert confirmed["write_report_status"]["S"] == "pending"
    root_key = {"execution_id": {"S": confirmed["operation_execution_id"]["S"]}}
    root = store._client().get_item(TableName=store._table(), Key=root_key)["Item"]
    assert root["unresolved_operations"]["N"] == "1"
    assert store.finish_write_report(retry, recorded=True) is None
    assert restarted.due_write_reports()[0]["status"]["S"] == "confirmed"
    final = restarted.finish_write_report(confirmed, recorded=True)
    assert restarted.receipt(final)["report_status"] == "recorded"
    assert restarted.receipt(final)["backend_generation"] == 1
    root = store._client().get_item(TableName=store._table(), Key=root_key)["Item"]
    assert root["unresolved_operations"]["N"] == "0"
    assert restarted.finish_write_report(confirmed, recorded=True) is None
    assert restarted.due_write_reports() == []

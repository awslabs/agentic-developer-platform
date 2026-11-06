"""Scheduled cleanup resumes durable closing rows without replaying provider work."""

import base64
import hashlib
import io
import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import boto3
import pytest

from tools_facade import reconciler
from tools_facade.executions import ExecutionStore
from tools_facade.reconciler import run_once

TABLE = "adp-tools-dev-executions"
SEARCH = "websearch___search"


def decision(*, expiry=None):
    return SimpleNamespace(principal=SimpleNamespace(tenant_id="tenant-one", owner_id="person-one",
        user_id="person-one", team_id="team-one"), run_id="test-run" if expiry else None,
        workload_id="test-worker" if expiry else None, run_expires_at=expiry, receipt_id="fixture-receipt")


def row(identifier):
    return boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName=TABLE, Key={"execution_id": {"S": str(identifier)}}, ConsistentRead=True)["Item"]


def confirmed_search(store, *, expiry=None):
    authority = decision(expiry=expiry)
    key, operation, _ = store.claim(None, uuid4(), SEARCH, {"query": "fixture"}, authority)
    store.dispatched(key)
    store.confirmed(key, {"jsonrpc": "2.0", "result": {"result": {"status": "confirmed"}}})
    return UUID(operation["operation_execution_id"]["S"]), authority


def test_scheduled_worker_finishes_explicitly_closing_search_after_restart():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id, authority = confirmed_search(store)
    store.close(execution_id, SEARCH, authority)
    assert row(execution_id)["state"]["S"] == "closing"
    run_once(ExecutionStore(clock=lambda: 1001))
    saved = row(execution_id)
    assert saved["state"]["S"] == "closed" and saved["cleanup_status"]["S"] == "confirmed"
    assert "cleanup_partition" not in saved and "next_cleanup_at" not in saved
    assert saved["accepted_operations"]["N"] == "1" and saved["unresolved_operations"]["N"] == "0"
    assert saved["cleanup_attempts"]["N"] == "1"
    assert saved["cleanup_evidence"]["S"] == "all_operations_settled"
    assert saved["cleanup_confirmed_at"]["N"] == "1001"


def test_scheduled_worker_reconciles_fenced_historical_siblings_after_restart():
    store = ExecutionStore(clock=lambda: 1000)
    authority = decision()
    first_key, original, _ = store.claim(None, uuid4(), SEARCH, {"query": "first"}, authority)
    execution_id = UUID(original["operation_execution_id"]["S"])
    second_key, _, _ = store.claim(execution_id, uuid4(), SEARCH, {"query": "second"}, authority)
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(TableName=TABLE, Key={"execution_id": {"S": str(execution_id)}},
                       UpdateExpression="REMOVE accepted_operations, unresolved_operations, operation_tracking")
    store.dispatched(first_key)
    store.dispatched(second_key)
    store.confirmed(first_key, {"result": {"status": "confirmed"}})
    store.close(execution_id, SEARCH, authority)
    run_once(ExecutionStore(clock=lambda: 1001))
    assert row(execution_id)["state"]["S"] == "closing"
    assert row(execution_id)["next_cleanup_at"]["N"] == "1061"
    store.confirmed(second_key, {"result": {"status": "confirmed"}})
    run_once(ExecutionStore(clock=lambda: 1061))
    saved = row(execution_id)
    assert saved["state"]["S"] == "closed"
    assert saved["cleanup_evidence"]["S"] == "historical_operations_settled"
    assert saved["cleanup_attempts"]["N"] == "2"
    assert "cleanup_partition" not in saved


def test_scheduled_worker_resumes_expired_search_on_later_pass():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id, _ = confirmed_search(store, expiry=1200)
    run_once(ExecutionStore(clock=lambda: 1200))
    assert row(execution_id)["state"]["S"] == "closing"
    run_once(ExecutionStore(clock=lambda: 1260))
    assert row(execution_id)["state"]["S"] == "closed"


def native_row(*, deadline=1600, limit=2):
    store = ExecutionStore(clock=lambda: 1000)
    target = UUID(store.admit(None, "browser___start", decision()))
    store.close(target, "browser___start", decision())
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(TableName=TABLE, Key={"execution_id": {"S": str(target)}},
        UpdateExpression="SET native_test_run_id = :run, native_test_reservation_id = :hold, "
                         "native_cleanup_deadline_at = :deadline, native_cleanup_attempts_remaining = :limit",
        ExpressionAttributeValues={":run": {"S": str(uuid4())}, ":hold": {"S": "original-held-reservation"},
                                   ":deadline": {"N": str(deadline)}, ":limit": {"N": str(limit)}})
    return target


def native_handoff(item, *, now=1001):
    from tools_facade.native_admission import NativeCleanupAdmission, NativeCleanupHandoff
    from tools_facade.server import Decision

    operation_id = str(uuid4())
    response = Decision(schema_version="1.0", allowed=True, receipt_id=item["native_test_reservation_id"]["S"],
        principal={"user_id": "person-one", "root_human_id": "person-one",
                   "tenant_id": "tenant-one", "team_id": "team-one"},
        run_id=item["native_test_run_id"]["S"], workload_id="native-test-offline", run_expires_at=1600)
    admission = NativeCleanupAdmission(run_id=response.run_id, operation_id=operation_id,
        original_operation_id=str(uuid4()), execution_id=item["execution_id"]["S"], reservation_id=response.receipt_id,
        attempt=int(item["cleanup_attempts"]["N"]), status="dispatch_started", started_at=float(now),
        dispatch_deadline_at=1510.0, replay=False)
    return NativeCleanupHandoff(admission=admission, decision=response, execution_id=item["execution_id"]["S"],
        tool=item["tool"]["S"], headers={"x-adp-native-test-operation": operation_id}, clock=lambda: now)


def test_native_automatic_cleanup_stops_after_original_finite_attempts_without_forging_completion():
    target, calls = native_row(), []

    def failed(item, *, native_handoff):
        native_handoff.require_fresh()
        calls.append(item["execution_id"]["S"])
        raise TimeoutError("offline unknown provider outcome")

    for now in (1001, 1061, 1181, 1500, 1700):
        run_once(ExecutionStore(clock=lambda: now), stop_provider=failed,
                 native_authorizer=lambda item: native_handoff(item, now=now))
    saved = row(target)
    assert calls == [str(target), str(target)]
    assert saved["cleanup_attempts"]["N"] == "2"
    assert saved["state"]["S"] == "closing" and saved["cleanup_status"]["S"] == "unknown"
    assert "cleanup_partition" not in saved and "cleanup_evidence" not in saved
    assert saved["native_test_reservation_id"]["S"] == "original-held-reservation"


def test_native_automatic_cleanup_requires_gateway_funding_and_obeys_original_horizon():
    target, calls = native_row(deadline=1090), []
    run_once(ExecutionStore(clock=lambda: 1001), stop_provider=calls.append,
             native_authorizer=lambda item: (_ for _ in ()).throw(AssertionError("past-horizon authorization")))
    assert calls == [] and "cleanup_partition" not in row(target)
    target = native_row()
    for now in (1001, 1061, 1181):
        run_once(ExecutionStore(clock=lambda: now), stop_provider=calls.append,
                 native_authorizer=lambda item: None)
    assert calls == [] and row(target)["cleanup_status"]["S"] == "unknown"
    assert "cleanup_partition" not in row(target)


def test_scheduled_worker_does_not_confirm_unknown_search_sibling():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id, authority = confirmed_search(store)
    unknown, _, _ = store.claim(execution_id, uuid4(), SEARCH, {"query": "second"}, authority)
    store.dispatched(unknown)
    store.close(execution_id, SEARCH, authority)
    run_once(ExecutionStore(clock=lambda: 1001))
    saved = row(execution_id)
    assert saved["state"]["S"] == "closing" and saved["cleanup_status"]["S"] == "unknown"
    assert saved["unresolved_operations"]["N"] == "1"
    assert saved["cleanup_partition"]["S"] == "due"
    assert saved["cleanup_last_outcome"]["S"] == "unknown"


def test_scheduled_worker_is_bounded_and_resumes_remaining_records():
    store = ExecutionStore(clock=lambda: 1000)
    identifiers = []
    for _ in range(12):
        execution_id, authority = confirmed_search(store)
        store.close(execution_id, SEARCH, authority)
        identifiers.append(execution_id)
    run_once(ExecutionStore(clock=lambda: 1001), max_records=4)
    assert sum(row(identifier)["state"]["S"] == "closed" for identifier in identifiers) == 4
    for _ in range(2):
        run_once(ExecutionStore(clock=lambda: 1001), max_records=4)
    assert all(row(identifier)["state"]["S"] == "closed" for identifier in identifiers)


def test_scheduled_worker_catches_up_search_backlog_before_new_session_cleanup():
    # Reproduce the observed 156-row backlog without claiming a provider-time
    # guarantee: this proves selection/ledger progress with prompt responses.
    store = ExecutionStore(clock=lambda: 1000)
    for _ in range(156):
        execution_id, authority = confirmed_search(store)
        store.close(execution_id, SEARCH, authority)
    target = UUID(store.admit(None, "browser___start", decision(expiry=1300)))
    observed = []

    def stopped(item):
        observed.append(item["execution_id"]["S"])
        return {"execution_id": str(target), "status": "confirmed",
                "termination_confirmed": True, "provider_status": "TERMINATED"}

    for now in range(1000, 1421, 60):
        run_once(ExecutionStore(clock=lambda: now), stop_provider=stopped)
    saved = row(target)
    assert saved["state"]["S"] == "closed"
    assert int(saved["cleanup_confirmed_at"]["N"]) <= 1420
    assert observed == [str(target)]


def test_scheduled_worker_defers_provider_without_claiming_and_still_finishes_search():
    store = ExecutionStore(clock=lambda: 1000)
    target = UUID(store.admit(None, "browser___start", decision()))
    store.close(target, "browser___start", decision())
    search, authority = confirmed_search(store)
    store.close(search, SEARCH, authority)
    before = row(target)
    elapsed = [0]
    window = reconciler.CleanupWindow(monotonic=lambda: elapsed[0])
    elapsed[0] = 10  # Cheap work fits; signing + provider + durable write do not.
    calls = []
    result = run_once(store, stop_provider=calls.append, window=window)
    assert not calls
    assert row(target) == before
    assert row(search)["state"]["S"] == "closed"
    assert result["deferred"] == 1 and result["deadline_reached"] is True


def test_scheduled_worker_preserves_attempt_after_lost_provider_reply_and_deadline():
    store = ExecutionStore(clock=lambda: 1000)
    target = UUID(store.admit(None, "browser___start", decision()))
    store.close(target, "browser___start", decision())
    elapsed = [0]
    window = reconciler.CleanupWindow(monotonic=lambda: elapsed[0])
    bounded = ExecutionStore(client=window.client("dynamodb"), clock=lambda: 1001)

    def lost_reply(_item):
        elapsed[0] = 44
        raise TimeoutError("provider completion is unknown")

    result = run_once(bounded, stop_provider=lost_reply, window=window)
    saved = row(target)
    assert saved["state"]["S"] == "closing"
    assert saved["cleanup_attempts"]["N"] == "1"
    assert saved["cleanup_last_outcome"]["S"] == "unknown"
    assert saved["next_cleanup_at"]["N"] == "1061"
    assert result["deadline_reached"] is True
    assert "cleanup_evidence" not in saved


def test_scheduled_worker_deadline_interrupts_legacy_scan_without_claiming_settlement(monkeypatch):
    store = ExecutionStore(clock=lambda: 1000)
    target, authority = confirmed_search(store)
    store.close(target, SEARCH, authority)
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(TableName=TABLE, Key={"execution_id": {"S": str(target)}},
                      UpdateExpression="REMOVE operation_tracking, accepted_operations, unresolved_operations")
    elapsed = [0]
    window = reconciler.CleanupWindow(monotonic=lambda: elapsed[0])
    scans = []
    real_scan = client.scan

    def slow_scan(**kwargs):
        scans.append(kwargs)
        result = real_scan(**kwargs)
        elapsed[0] = 44
        return {**result, "LastEvaluatedKey": {"execution_id": {"S": str(target)}}}

    monkeypatch.setattr(client, "scan", slow_scan)
    bounded = ExecutionStore(client=reconciler._WindowClient(window, client, 2), clock=lambda: 1001)
    result = run_once(bounded, window=window)
    assert len(scans) == 1
    assert result["deadline_reached"] is True
    assert row(target)["state"]["S"] == "closing"
    assert "cleanup_evidence" not in row(target)


def test_scheduled_clients_disable_sdk_retries_and_refuse_request_after_window():
    elapsed = [0]
    window = reconciler.CleanupWindow(monotonic=lambda: elapsed[0])
    for service in ("dynamodb", "kms", "lambda"):
        wrapped = window.client(service)
        config = wrapped.client.meta.config
        assert config.retries["total_max_attempts"] == 1
        assert 0 < config.connect_timeout <= 1 and 0 < config.read_timeout <= 30
    elapsed[0] = 45
    with pytest.raises(reconciler.CleanupDeadline):
        window.client("dynamodb").get_item(TableName=TABLE, Key={"execution_id": {"S": "missing"}})


def test_scheduled_worker_reports_startup_deadline_without_query_or_claim():
    elapsed = [0]
    window = reconciler.CleanupWindow(monotonic=lambda: elapsed[0])
    elapsed[0] = 44

    def query(_limit):
        pytest.fail("Expired tick must not read or claim work")

    result = run_once(SimpleNamespace(due_cleanup=query), window=window)
    assert result == {"selected": 0, "processed": 0, "deferred": 0, "deadline_reached": True}


def test_scheduled_worker_reports_initial_query_deadline_but_preserves_storage_failures():
    def deadline(_limit):
        raise reconciler.CleanupDeadline()

    result = run_once(SimpleNamespace(due_cleanup=deadline))
    assert result == {"selected": 0, "processed": 0, "deferred": 0, "deadline_reached": True}

    def unavailable(_limit):
        raise RuntimeError("store unavailable")

    with pytest.raises(RuntimeError, match="store unavailable"):
        run_once(SimpleNamespace(due_cleanup=unavailable))


def test_scheduled_worker_preserves_unknown_stateful_cleanup():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id = UUID(store.admit(None, "browser___start", decision()))
    store.close(execution_id, "browser___start", decision())
    resumed = []
    run_once(ExecutionStore(clock=lambda: 1001), stop_provider=lambda item: resumed.append(item["tool"]["S"]))
    assert resumed == ["browser___start"]
    assert row(execution_id)["state"]["S"] == "closing"
    assert row(execution_id)["cleanup_status"]["S"] == "unknown"


def test_scheduled_worker_retries_failed_provider_cleanup_after_restart():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id = UUID(store.admit(None, "browser___start", decision()))
    store.close(execution_id, "browser___start", decision())

    def unavailable(_item):
        raise RuntimeError("provider unavailable")

    run_once(store, stop_provider=unavailable)
    assert row(execution_id)["next_cleanup_at"]["N"] == "1060"
    assert row(execution_id)["state"]["S"] == "closing"
    assert row(execution_id)["cleanup_attempts"]["N"] == "1"
    assert row(execution_id)["cleanup_last_outcome"]["S"] == "provider_error"
    resumed = []
    run_once(ExecutionStore(clock=lambda: 1059), stop_provider=resumed.append)
    assert resumed == []
    run_once(ExecutionStore(clock=lambda: 1060), stop_provider=resumed.append)
    assert len(resumed) == 1
    assert row(execution_id)["cleanup_status"]["S"] == "unknown"
    assert row(execution_id)["cleanup_attempts"]["N"] == "2"
    assert row(execution_id)["cleanup_last_attempt_at"]["N"] == "1060"
    assert row(execution_id)["cleanup_last_outcome"]["S"] == "provider_unknown"
    assert row(execution_id)["next_cleanup_at"]["N"] == "1180"


def test_scheduled_worker_confirms_only_valid_provider_termination_after_restart():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id = UUID(store.admit(None, "browser___start", decision()))
    store.close(execution_id, "browser___start", decision())
    proof = {"execution_id": str(execution_id), "status": "confirmed",
             "termination_confirmed": True, "provider_status": "TERMINATED"}
    run_once(store, stop_provider=lambda _: {**proof, "provider_status": "RUNNING"})
    assert row(execution_id)["state"]["S"] == "closing"
    assert row(execution_id)["cleanup_last_outcome"]["S"] == "provider_unknown"
    run_once(ExecutionStore(clock=lambda: 1060), stop_provider=lambda _: proof)
    saved = row(execution_id)
    assert saved["state"]["S"] == "closed" and saved["cleanup_status"]["S"] == "confirmed"
    assert saved["cleanup_evidence"]["S"] == "provider_terminated"
    assert saved["cleanup_confirmed_at"]["N"] == "1060"
    assert saved["cleanup_attempts"]["N"] == "2"
    assert "cleanup_partition" not in saved and "next_cleanup_at" not in saved


def test_scheduled_worker_requires_original_undispatched_proof():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id = UUID(store.admit(None, "browser___start", decision()))
    store.close(execution_id, "browser___start", decision())
    proof = {"execution_id": str(execution_id), "status": "confirmed",
             "termination_confirmed": True, "provider_status": "NOT_STARTED"}
    run_once(store, stop_provider=lambda _: proof)
    assert row(execution_id)["state"]["S"] == "closing"
    run_once(ExecutionStore(clock=lambda: 1060), stop_provider=lambda _: {
        **proof, "execution_id": str(uuid4()), "cleanup_proof": "start_not_dispatched"})
    assert row(execution_id)["state"]["S"] == "closing"
    run_once(ExecutionStore(clock=lambda: 1180), stop_provider=lambda _: {
        **proof, "cleanup_proof": "start_not_dispatched"})
    saved = row(execution_id)
    assert saved["state"]["S"] == "closed" and saved["cleanup_status"]["S"] == "confirmed"
    assert saved["cleanup_evidence"]["S"] == "start_not_dispatched"
    assert ExecutionStore.cleanup_proved(saved)
    assert "cleanup_partition" not in saved and "next_cleanup_at" not in saved


def test_scheduled_worker_keeps_uncertain_interpreter_handle_pending():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id = UUID(store.admit(None, "code_interpreter___start", decision()))
    store.close(execution_id, "code_interpreter___start", decision())
    run_once(store, stop_provider=lambda _: {"execution_id": str(execution_id),
              "status": "pending", "termination_confirmed": False})
    assert row(execution_id)["state"]["S"] == "closing"
    assert row(execution_id)["cleanup_last_outcome"]["S"] == "provider_unknown"
    assert row(execution_id)["next_cleanup_at"]["N"] == "1060"


def test_scheduled_worker_refences_legacy_terminal_without_proof():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id, authority = confirmed_search(store)
    store.close(execution_id, SEARCH, authority)
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(TableName=TABLE, Key={"execution_id": {"S": str(execution_id)}},
        UpdateExpression="SET #state = :closed, cleanup_status = :confirmed",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closed": {"S": "closed"}, ":confirmed": {"S": "confirmed"}})
    run_once(ExecutionStore(clock=lambda: 1001))
    saved = row(execution_id)
    assert saved["state"]["S"] == "closing" and saved["cleanup_status"]["S"] == "unknown"
    assert saved["next_cleanup_at"]["N"] == "1061"
    run_once(ExecutionStore(clock=lambda: 1061))
    assert row(execution_id)["cleanup_evidence"]["S"] == "all_operations_settled"
    assert "cleanup_partition" not in row(execution_id)


def test_scheduled_worker_removes_stale_due_index_only_for_proved_terminal():
    store = ExecutionStore(clock=lambda: 1000)
    execution_id, authority = confirmed_search(store)
    store.close(execution_id, SEARCH, authority)
    store.confirm_closed(execution_id, SEARCH, authority)
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(TableName=TABLE, Key={"execution_id": {"S": str(execution_id)}},
        UpdateExpression="SET cleanup_partition = :due, next_cleanup_at = :at",
        ExpressionAttributeValues={":due": {"S": "due"}, ":at": {"N": "1000"}})
    run_once(ExecutionStore(clock=lambda: 1001))
    assert row(execution_id)["state"]["S"] == "closed"
    assert "cleanup_partition" not in row(execution_id)


@pytest.mark.parametrize("root,expiry", [("browser___start", None),
                                          ("code_interpreter___start", 1200)])
def test_scheduled_worker_uses_scoped_signed_cleanup_call_after_owner_revocation(monkeypatch, root, expiry):
    store = ExecutionStore(clock=lambda: 1000)
    execution_id = UUID(store.admit(None, root, decision(expiry=expiry)))
    store.close(execution_id, root, decision(expiry=expiry))
    monkeypatch.setenv("ADP_TOOLS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("ADP_TOOLS_ENVIRONMENT", "dev")
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN",
        "arn:aws:iam::123456789012:role/adp-dev-role-tools-reconciler")
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_KEY_ARN",
        "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000003")
    monkeypatch.setenv("ADP_TOOLS_CLEANUP_LAMBDA_ARN",
        "arn:aws:lambda:us-east-1:123456789012:function:adp-dev-shared-tools-shared")
    original = boto3.client
    calls = []

    class KMS:
        def sign(self, **kwargs):
            assert kwargs["KeyId"].endswith("key/00000000-0000-0000-0000-000000000003")
            return {"Signature": b"cleanup-signature"}

    class Lambda:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            payload = json.loads(kwargs["Payload"])
            envelope = json.loads(base64.urlsafe_b64decode(payload["adp_assertion"] + "=="))
            claims = envelope["claims"]
            expected_tool = ("browser" if root == "browser___start" else "code-interpreter") + "___execution_close"
            assert claims["tool"] == expected_tool
            assert claims["principal"] == {"tenant_id": "tenant-one", "team_id": "team-one",
                                            "user_id": "person-one"}
            assert claims["cleanup_reconcile"] is True and claims["run_expires_at"] is None
            assert claims["run_id"] == ("test-run" if expiry else None)
            assert claims["workload_id"] == ("test-worker" if expiry else None)
            assert json.loads(base64.b64decode(kwargs["ClientContext"]))["custom"] == {
                "bedrockAgentCoreToolName": expected_tool}
            proof = {"execution_id": str(execution_id), "termination_confirmed": True,
                     "status": "confirmed", "provider_status": "TERMINATED"}
            return {"Payload": io.BytesIO(json.dumps({"result": proof}).encode())}

    monkeypatch.setattr(reconciler.boto3, "client", lambda service, **kwargs:
        KMS() if service == "kms" else Lambda() if service == "lambda" else original(service, **kwargs))
    run_once(ExecutionStore(clock=lambda: 1201 if expiry else 1000), stop_provider=reconciler.stop_provider)
    assert len(calls) == 1
    assert row(execution_id)["cleanup_evidence"]["S"] == "provider_terminated"
    assert row(execution_id)["cleanup_attempts"]["N"] == "1"


def test_jira_report_repair_retries_unknown_then_settles_original_generation(monkeypatch):
    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    authority = decision()
    authority.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    authority.connection_id = "connection-one"
    tool = "atlassian___comment_jira_issue"
    request_id = uuid4()
    arguments = {"cloudId": "site-one", "issueId": "ISSUE-42", "comment": "owned"}
    authority.atlassian_approval = {"tool": tool, "request_id": str(request_id),
                                    "payload_sha256": hashlib.sha256(reconciler.canonical(arguments)).hexdigest(),
                                    "policy_revision": 1, "consent_source": "explicit"}
    store = ExecutionStore(clock=lambda: 1000)
    key, original, _ = store.claim(None, request_id, tool, arguments, authority)
    store.dispatched(key)
    reports = []

    def report(item):
        reports.append((item["status"]["S"], json.loads(item["atlassian_binding"]["S"])["release"]))
        if len(reports) == 1:
            raise ConnectionError("acknowledgement lost")
        return True

    run_once(ExecutionStore(clock=lambda: 1001), report_write=report)
    assert row(key)["write_report_status"]["S"] == "pending"
    assert row(key)["next_cleanup_at"]["N"] == "1061"
    root = original["operation_execution_id"]["S"]
    assert row(root)["unresolved_operations"]["N"] == "1"
    run_once(ExecutionStore(clock=lambda: 1061), report_write=report)
    assert row(key)["status"]["S"] == "unknown"
    assert row(key)["write_report_status"]["S"] == "recorded"
    store.confirmed(key, {"result": {"structuredContent": {"id": "COMMENT-42"}}})
    assert row(root)["unresolved_operations"]["N"] == "1"
    run_once(ExecutionStore(clock=lambda: 1062), report_write=report)
    assert row(root)["unresolved_operations"]["N"] == "0"
    assert row(key)["write_report_status"]["S"] == "recorded"
    assert [status for status, _ in reports] == ["unknown", "unknown", "confirmed"]
    assert all(saved_release == release for _, saved_release in reports)
    assert ExecutionStore(clock=lambda: 1063).claim(None, request_id, tool, arguments, authority)[2] is False


def test_jira_report_transport_signs_original_receipt_and_rejects_missing_ack(monkeypatch):
    from unittest.mock import Mock

    from tools_facade import server

    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 3}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    monkeypatch.setenv("ADP_TOOLS_OUTCOME_REPAIR_URL",
                       "https://fixture.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/outcome-repair")
    authority = decision()
    authority.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    authority.connection_id = "connection-one"
    tool = "atlassian___update_jira_issue"
    request_id = uuid4()
    arguments = {"cloudId": "site-one", "issueId": "ISSUE-42"}
    authority.atlassian_approval = {"tool": tool, "request_id": str(request_id),
                                    "payload_sha256": hashlib.sha256(reconciler.canonical(arguments)).hexdigest(),
                                    "policy_revision": 1, "consent_source": "explicit"}
    store = ExecutionStore(clock=lambda: 1000)
    key, _, _ = store.claim(None, request_id, tool, arguments, authority)
    store.dispatched(key)
    operation = row(key)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps({**release, "backend_generation": 4}))
    signed = []
    def sign(url, body, service, region):
        signed.append(json.loads(body))
        return {}

    monkeypatch.setattr(server, "signed_headers", sign)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, content, headers):
            assert url == os.environ["ADP_TOOLS_OUTCOME_REPAIR_URL"]
            return Mock(raise_for_status=lambda: None, json=lambda: {"status": "not-recorded"})

    monkeypatch.setattr(reconciler.httpx, "Client", Client)
    with pytest.raises(ValueError, match="acknowledgement"):
        reconciler.repair_write_report(operation)
    assert signed[0]["backend_generation"] == 3
    assert signed[0]["operation_id"] == key
    assert signed[0]["result_status"] == "unknown"
    assert "atlassian_binding" not in signed[0]
    assert row(key)["write_report_status"]["S"] == "pending"


def test_jira_unknown_readback_survives_restart_route_change_and_failure(monkeypatch):
    release = {"contract_version": "1.0", "source_revision": "a" * 40,
               "image_digest": "sha256:" + "b" * 64, "group": "tool-connectors",
               "group_generation": 1, "backend_generation": 3}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    authority = decision()
    authority.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    authority.connection_id = "connection-one"
    tool = "atlassian___comment_jira_issue"
    request_id = uuid4()
    arguments = {"cloudId": "site-one", "issueId": "ISSUE-42", "comment": "owned"}
    authority.atlassian_approval = {"tool": tool, "request_id": str(request_id),
                                    "payload_sha256": hashlib.sha256(reconciler.canonical(arguments)).hexdigest(),
                                    "policy_revision": 1, "consent_source": "explicit"}
    store = ExecutionStore(clock=lambda: 1000)
    key, original, _ = store.claim(None, request_id, tool, arguments, authority)
    store.dispatched(key)
    root = original["operation_execution_id"]["S"]
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps({**release, "backend_generation": 4}))
    calls = []

    def readback(item):
        calls.append(item)
        assert json.loads(item["atlassian_binding"]["S"])["release"] == release
        raise ConnectionError("original adapter unavailable")

    run_once(ExecutionStore(clock=lambda: 1001), report_write=lambda item: True, readback=readback)
    saved = row(key)
    assert saved["write_report_status"]["S"] == "recorded"
    assert saved["write_readback_status"]["S"] == "pending"
    assert saved["write_readback_evidence"]["S"] == "unavailable"
    assert saved["next_cleanup_at"]["N"] == "1061"
    assert saved["status"]["S"] == "unknown"
    assert row(root)["unresolved_operations"]["N"] == "1"
    assert len(calls) == 1
    run_once(ExecutionStore(clock=lambda: 1061), readback=lambda item: True)
    saved = row(key)
    assert saved["write_readback_evidence"]["S"] == "inconclusive"
    assert saved["next_cleanup_at"]["N"] == "1121"
    assert saved["status"]["S"] == "unknown"
    assert row(root)["unresolved_operations"]["N"] == "1"
    assert ExecutionStore(clock=lambda: 1062).claim(None, request_id, tool, arguments, authority)[2] is False
    assert ExecutionStore(clock=lambda: 1062).due_write_readbacks() == []
    assert len(ExecutionStore(clock=lambda: 1121).due_write_readbacks()) == 1


def test_jira_readback_transport_signs_original_generation_without_write(monkeypatch):
    from unittest.mock import Mock

    from tools_facade import server

    release = {"contract_version": "1.0", "source_revision": "a" * 40,
               "image_digest": "sha256:" + "b" * 64, "group": "tool-connectors",
               "group_generation": 1, "backend_generation": 3}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READBACK_URL",
                       "https://fixture.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/atlassian/reconcile")
    monkeypatch.setenv("ADP_TOOLS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("ADP_TOOLS_ENVIRONMENT", "dev")
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-dev-role-tools-reconciler")
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_KEY_ARN",
                       "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111")
    authority = decision()
    authority.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    authority.connection_id = "connection-one"
    tool = "atlassian___update_jira_issue"
    request_id = uuid4()
    arguments = {"cloudId": "site-one", "issueIdOrKey": "ISSUE-42"}
    authority.atlassian_approval = {"tool": tool, "request_id": str(request_id),
                                    "payload_sha256": hashlib.sha256(reconciler.canonical(arguments)).hexdigest(),
                                    "policy_revision": 1, "consent_source": "explicit"}
    store = ExecutionStore(clock=lambda: 1000)
    key, _, _ = store.claim(None, request_id, tool, arguments, authority)
    store.dispatched(key)
    item = row(key)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps({**release, "backend_generation": 4}))
    signed = []
    monkeypatch.setattr(server, "signed_headers",
                        lambda url, body, service, region: signed.append(json.loads(body)) or {})

    class Window:
        def client(self, service):
            assert service == "kms"
            return Mock(sign=lambda **kwargs: signed.append(json.loads(kwargs["Message"])) or {"Signature": b"fixture"})

        def permits(self, seconds):
            return True

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, content, headers):
            assert url == os.environ["ADP_TOOLS_ATLASSIAN_READBACK_URL"]
            body = json.loads(content)
            assert body["purpose"] == "reconcile" and body["operation"] == "get_jira_issue"
            return Mock(raise_for_status=lambda: None, content=b"{}", json=lambda: {
                "status": "unknown", "operation_id": key, "backend_generation": 3,
                "readback_performed": True})

    monkeypatch.setattr(reconciler.httpx, "Client", Client)
    assert reconciler.readback_write(item, window=Window()) is True
    assert signed[0]["atlassian_operation"]["release"] == release
    assert signed[0]["atlassian_operation"]["operation_id"] == key
    assert signed[0]["read_arguments_sha256"] == hashlib.sha256(reconciler.canonical(arguments)).hexdigest()
    assert signed[1]["binding"]["release"]["backend_generation"] == 3
    assert row(key)["status"]["S"] == "unknown"
    missing = {**item, "atlassian_binding": {"S": json.dumps({**json.loads(item["atlassian_binding"]["S"]),
                                                            "readback_locator": None})}}
    with pytest.raises(ValueError, match="original Jira readback"):
        reconciler.readback_write(missing, window=Window())


def test_uncertain_write_recovery_journal_survives_lost_report_and_backend_handoff(monkeypatch):
    from fastapi import HTTPException

    release = {"contract_version": "1.0", "source_revision": "a" * 40,
               "image_digest": "sha256:" + "b" * 64, "group": "tool-connectors",
               "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    authority = decision()
    authority.atlassian_connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    authority.connection_id = "connection-one"
    tool = "atlassian___update_jira_issue"
    request_id = uuid4()
    arguments = {"cloudId": "site-one", "issueIdOrKey": "FIX-42", "fields": {"summary": "owned-update"}}
    authority.atlassian_approval = {"tool": tool, "request_id": str(request_id),
                                    "payload_sha256": hashlib.sha256(reconciler.canonical(arguments)).hexdigest(),
                                    "policy_revision": 1, "consent_source": "explicit"}
    store = ExecutionStore(clock=lambda: 1000)
    key, operation, created = store.claim(None, request_id, tool, arguments, authority)
    assert created is True
    store.dispatched(key)
    provider = {"effects": 1, "issue": {"id": "FIX-42", "summary": "owned-update"},
                "unrelated": {"id": "FOREIGN-1", "summary": "unchanged"}}
    root = operation["operation_execution_id"]["S"]
    reports = []
    readbacks = []
    current_backend = [2]

    def report(item):
        reports.append(item)
        assert json.loads(item["atlassian_binding"]["S"])["release"] == release
        if len(reports) == 1:
            raise ConnectionError("outcome report reply lost")
        return True

    def readback(item):
        readbacks.append(item)
        original = json.loads(item["atlassian_binding"]["S"])
        assert original["release"] == release
        assert original["readback_locator"] == {"cloudId": "site-one", "issueIdOrKey": "FIX-42"}
        if current_backend[0] != release["backend_generation"]:
            raise ConnectionError("original backend is fenced")
        if len(readbacks) == 2:
            raise ConnectionError("provider readback reply lost")
        assert provider["issue"] == {"id": "FIX-42", "summary": "owned-update"}
        return True

    run_once(ExecutionStore(clock=lambda: 1001), report_write=report, readback=readback)
    assert row(key)["write_report_status"]["S"] == "pending"
    assert row(key)["next_cleanup_at"]["N"] == "1061"
    assert readbacks == [] and provider["effects"] == 1
    run_once(ExecutionStore(clock=lambda: 1061), report_write=report, readback=readback)
    assert row(key)["write_report_status"]["S"] == "recorded"
    assert row(key)["write_readback_evidence"]["S"] == "unavailable"
    assert row(key)["next_cleanup_at"]["N"] == "1121"
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps({**release, "backend_generation": 2}))
    assert ExecutionStore(clock=lambda: 1062).claim(None, request_id, tool, arguments, authority)[2] is False
    with pytest.raises(HTTPException) as changed:
        ExecutionStore(clock=lambda: 1062).claim(None, request_id, tool, {**arguments, "fields": {}}, authority)
    assert changed.value.status_code == 409
    current_backend[0] = 1
    run_once(ExecutionStore(clock=lambda: 1121), readback=readback)
    assert row(key)["write_readback_evidence"]["S"] == "unavailable"
    assert row(key)["next_cleanup_at"]["N"] == "1181"
    run_once(ExecutionStore(clock=lambda: 1181), readback=readback)
    saved = row(key)
    assert saved["write_readback_evidence"]["S"] == "inconclusive"
    assert saved["status"]["S"] == "unknown"
    assert saved["next_cleanup_at"]["N"] == "1241"
    assert row(root)["unresolved_operations"]["N"] == "1"
    assert provider == {"effects": 1, "issue": {"id": "FIX-42", "summary": "owned-update"},
                        "unrelated": {"id": "FOREIGN-1", "summary": "unchanged"}}
    assert len(reports) == 2 and len(readbacks) == 3

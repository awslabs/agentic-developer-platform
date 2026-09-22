"""Durable pending payloads survive outages without authorizing a second run."""

import io
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import run_report
from lib.pr_binding import resume_handoff


@pytest.fixture
def spool(monkeypatch):
    assignment = {
        "credential": "adprpt1." + "x" * 43,
        "ownership_nonce": "a" * 32,
        "run_id": "run-1",
        "attempt": 1,
        "tenant_id": "org",
        "repo": "org/repo",
    }
    monkeypatch.setattr(run_report, "_assignment", assignment)
    monkeypatch.setattr(
        run_report, "request", lambda *args: {"worker_receipt": {"ownership_nonce": "a" * 32}}
    )
    monkeypatch.setenv("AGENT_RUN_LOGS_BUCKET", "logs")
    objects = {}
    client = MagicMock()

    def get(**kwargs):
        if kwargs["Key"] not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(objects[kwargs["Key"]])}

    def put(**kwargs):
        if kwargs.get("IfNoneMatch") and kwargs["Key"] in objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        objects[kwargs["Key"]] = kwargs["Body"]

    client.get_object.side_effect = get
    client.put_object.side_effect = put
    monkeypatch.setattr(run_report, "_spool_client", lambda: client)
    return client, objects


def test_start_is_durable_and_redelivery_never_runs_development_again(spool, monkeypatch):
    run_report.begin_delivery()
    assert run_report.read_spool()["phase"] == "executing"
    monkeypatch.setattr(run_report, "request", lambda *args: {})
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        resume_handoff()
    assert "credential" not in next(iter(spool[1].values())).decode()


@pytest.mark.parametrize("has_spool", [False, True])
def test_explicit_server_retirement_preserves_spool_and_never_reports_success(spool, monkeypatch, has_spool):
    if has_spool:
        run_report.begin_delivery()
        run_report.spool_candidate({"head_sha": "a" * 40})
    before = dict(spool[1])
    monkeypatch.setattr(run_report, "request", MagicMock(return_value={
        "block_code": "execution_assignment_superseded", "retryable": False,
        "candidate_pr": {"head_sha": "a" * 40},
    }))
    terminal = MagicMock()
    monkeypatch.setattr(run_report, "terminal", terminal)
    with pytest.raises(run_report.SupersededDelivery):
        resume_handoff()
    run_report.request.assert_called_once_with()
    terminal.assert_not_called()
    assert spool[1] == before


@pytest.mark.parametrize("snapshot", [
    {"block_code": "execution_assignment_superseded"},
    {"block_code": "execution_assignment_superseded", "retryable": True},
    {"block_code": "execution_assignment_unverifiable", "retryable": False},
    {"block_code": "delivery_recovery_required", "retryable": False},
])
def test_unknown_or_retryable_status_does_not_retire_delivery(spool, monkeypatch, snapshot):
    monkeypatch.setattr(run_report, "request", lambda: snapshot)
    assert resume_handoff() is False


def test_gateway_outage_candidate_replays_from_existing_artifact(spool, monkeypatch):
    candidate = {
        "repo": "org/repo",
        "pr_number": 50,
        "provider_repository_id": 42,
        "provider_pr_node_id": "PR_known",
        "head_sha": "a" * 40,
    }
    run_report.begin_delivery()
    run_report.spool_candidate(candidate)
    snapshot = {"binding_receipt": {"bound": True, **candidate}}
    request = MagicMock(side_effect=[{}, snapshot])
    monkeypatch.setattr(run_report, "request", request)
    terminal = MagicMock()
    monkeypatch.setattr(run_report, "terminal", terminal)
    assert resume_handoff() is True
    request.assert_called_with("/pull-request", candidate)
    terminal.assert_called_once_with("complete")


def test_interrupted_upload_does_not_claim_candidate_durable(spool):
    run_report.begin_delivery()
    spool[0].put_object.side_effect = EndpointConnectionError(endpoint_url="https://s3.test")
    with pytest.raises(run_report.RunReportError, match="report_spool_unavailable"):
        run_report.spool_candidate({"head_sha": "a" * 40})
    assert run_report.read_spool()["phase"] == "executing"


def test_wrong_scope_spool_is_refused_and_missing_is_distinct(spool):
    assert run_report.read_spool() is None
    run_report.begin_delivery()
    key = next(iter(spool[1]))
    body = json.loads(spool[1][key])
    body["attempt"] = 2
    spool[1][key] = json.dumps(body).encode()
    with pytest.raises(run_report.RunReportError, match="report_spool_scope_mismatch"):
        run_report.read_spool()


def unstarted_snapshot():
    return {key: None for key in ("worker_receipt", "candidate_pr", "binding_receipt", "terminal_receipt", "review_receipt")}


def failed_start_marker(monkeypatch):
    def failed(*args):
        raise run_report.RunReportError("run_report_http_409", retryable=False)

    monkeypatch.setattr(run_report, "request", failed)
    with pytest.raises(run_report.RunReportError, match="run_report_http_409"):
        run_report.begin_delivery()


def test_refused_start_reuses_exact_marker_without_rewriting_or_new_attempt(spool, monkeypatch):
    failed_start_marker(monkeypatch)
    original = dict(spool[1])
    writes = spool[0].put_object.call_count
    calls = []

    def request(path="", body=None):
        calls.append((path, body))
        if path == "":
            return unstarted_snapshot()
        assert path == "/started"
        return {"worker_receipt": {"ownership_nonce": body["ownership_nonce"]}}

    monkeypatch.setattr(run_report, "request", request)
    assert resume_handoff() is False
    run_report.begin_delivery()
    assert [path for path, body in calls] == ["", "", "/started"]
    assert spool[0].put_object.call_count == writes
    assert spool[1] == original
    assert run_report._assignment["run_id"] == "run-1" and run_report._assignment["attempt"] == 1


@pytest.mark.parametrize("receipt", ["worker_receipt", "candidate_pr", "binding_receipt", "terminal_receipt", "review_receipt"])
def test_any_acknowledged_work_prevents_marker_reuse(spool, monkeypatch, receipt):
    failed_start_marker(monkeypatch)
    snapshot = unstarted_snapshot() | {receipt: {"recorded": True}}
    monkeypatch.setattr(run_report, "request", MagicMock(return_value=snapshot))
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        run_report.begin_delivery()
    run_report.request.assert_called_once_with()


def test_lost_start_response_with_committed_receipt_cannot_restart_execution(spool, monkeypatch):
    failed_start_marker(monkeypatch)
    snapshot = unstarted_snapshot() | {"worker_receipt": {"ownership_nonce": "original-winner"}}
    monkeypatch.setattr(run_report, "request", lambda *args: snapshot)
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        resume_handoff()
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        run_report.begin_delivery()


@pytest.mark.parametrize("change", [{"phase": "candidate"}, {"candidate_pr": {}}, {"unrecognized": True}])
def test_only_exact_executing_null_candidate_marker_can_retry(spool, monkeypatch, change):
    failed_start_marker(monkeypatch)
    key = next(iter(spool[1]))
    spool[1][key] = json.dumps(json.loads(spool[1][key]) | change).encode()
    request = MagicMock(return_value=unstarted_snapshot())
    monkeypatch.setattr(run_report, "request", request)
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        run_report.begin_delivery()
    assert all(call.args != ("/started",) for call in request.mock_calls)


@pytest.mark.parametrize("missing", ["worker_receipt", "candidate_pr", "binding_receipt", "terminal_receipt", "review_receipt"])
def test_missing_snapshot_field_does_not_mean_no_prior_work(spool, monkeypatch, missing):
    failed_start_marker(monkeypatch)
    snapshot = unstarted_snapshot()
    snapshot.pop(missing)
    monkeypatch.setattr(run_report, "request", lambda *args: snapshot)
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        resume_handoff()


def test_ownership_change_during_bootstrap_blocks_retry_before_started(spool, monkeypatch):
    failed_start_marker(monkeypatch)
    request = MagicMock(side_effect=[unstarted_snapshot(), unstarted_snapshot() | {"worker_receipt": {"ownership_nonce": "rival"}}])
    monkeypatch.setattr(run_report, "request", request)
    assert resume_handoff() is False
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        run_report.begin_delivery()
    assert request.call_args_list == [(), ()]


def test_spool_change_between_read_and_start_refuses(spool, monkeypatch):
    failed_start_marker(monkeypatch)
    original = run_report.read_spool()
    monkeypatch.setattr(run_report, "read_spool", MagicMock(side_effect=[original, original | {"phase": "candidate"}]))
    request = MagicMock(return_value=unstarted_snapshot())
    monkeypatch.setattr(run_report, "request", request)
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        run_report.begin_delivery()
    request.assert_called_once_with()


def test_rival_wins_started_after_empty_recheck_loser_never_acknowledges_start(spool, monkeypatch):
    failed_start_marker(monkeypatch)
    request = MagicMock(side_effect=[unstarted_snapshot(), run_report.RunReportError("run_report_http_409", retryable=False)])
    monkeypatch.setattr(run_report, "request", request)
    with pytest.raises(run_report.RunReportError, match="run_report_http_409"):
        run_report.begin_delivery()
    assert request.call_count == 2


def test_rival_receipt_is_rejected_even_if_started_returns_success(spool, monkeypatch):
    failed_start_marker(monkeypatch)
    request = MagicMock(side_effect=[unstarted_snapshot(), {"worker_receipt": {"ownership_nonce": "rival"}}])
    monkeypatch.setattr(run_report, "request", request)
    with pytest.raises(run_report.RunReportError, match="delivery_start_unacknowledged"):
        run_report.begin_delivery()


def test_two_retries_observe_empty_receipt_but_only_gateway_nonce_winner_can_continue(spool, monkeypatch):
    failed_start_marker(monkeypatch)
    original, writes = dict(spool[1]), spool[0].put_object.call_count
    # Separate modules reproduce separate workers with independent random nonces.
    # The gateway's locked /started receipt is the shared arbitration boundary.
    empty_reads = Barrier(2)
    gateway_lock = Lock()
    receipt = None
    workers = []
    for index in range(2):
        spec = importlib.util.spec_from_file_location(f"report_rival_{index}", run_report.__file__)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        worker._assignment = run_report._assignment | {"ownership_nonce": str(index) * 32}
        worker._spool_client = lambda: spool[0]
        workers.append(worker)

    def request_for(worker):
        def request(path="", body=None):
            nonlocal receipt
            if not path:
                empty_reads.wait(timeout=5)
                return unstarted_snapshot()
            assert path == "/started"
            with gateway_lock:
                if receipt is not None and receipt["ownership_nonce"] != body["ownership_nonce"]:
                    raise worker.RunReportError("run_report_http_409", retryable=False)
                receipt = {"ownership_nonce": body["ownership_nonce"]}
                return {"worker_receipt": dict(receipt)}
        return request

    def begin(worker):
        worker.request = request_for(worker)
        try:
            worker.begin_delivery()
            return "acknowledged"
        except worker.RunReportError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(begin, workers))
    assert sorted(outcomes) == ["acknowledged", "run_report_http_409"]
    assert spool[1] == original and spool[0].put_object.call_count == writes


def test_failed_owner_survives_terminal_outage_without_restarting_work(spool, monkeypatch):
    run_report.begin_delivery()
    run_report.spool_undelivered_failure()
    terminal = MagicMock(side_effect=[run_report.RunReportError("run_report_unavailable"), {}])
    monkeypatch.setattr(run_report, "terminal", terminal)
    original = dict(spool[1])
    monkeypatch.setattr(run_report, "_assignment", run_report._assignment | {"ownership_nonce": "b" * 32})
    with pytest.raises(run_report.RunReportError, match="run_report_unavailable"):
        resume_handoff()
    assert resume_handoff() is True
    assert [call.args for call in terminal.call_args_list] == [("failed",), ("failed",)]
    assert spool[1] == original
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        run_report.begin_delivery()
    assert "credential" not in next(iter(spool[1].values())).decode()


@pytest.mark.parametrize("owner", [None, "b" * 32])
def test_failure_spool_cannot_report_for_unacknowledged_or_other_owner(spool, monkeypatch, owner):
    run_report.begin_delivery()
    run_report.spool_undelivered_failure()
    monkeypatch.setattr(run_report, "request", lambda: {"worker_receipt": {"ownership_nonce": owner}})
    terminal = MagicMock()
    monkeypatch.setattr(run_report, "terminal", terminal)
    with pytest.raises(run_report.RunReportError, match="delivery_recovery_required"):
        resume_handoff()
    terminal.assert_not_called()


def test_failure_spool_preserves_existing_pr_candidate(spool):
    run_report.begin_delivery()
    run_report.spool_candidate({"head_sha": "a" * 40})
    original = dict(spool[1])
    run_report.spool_undelivered_failure()
    assert spool[1] == original


def test_failed_spool_upload_cannot_fabricate_terminal_evidence(spool):
    run_report.begin_delivery()
    spool[0].put_object.side_effect = EndpointConnectionError(endpoint_url="https://s3.test")
    with pytest.raises(run_report.RunReportError, match="report_spool_unavailable"):
        run_report.spool_undelivered_failure()
    assert run_report.read_spool()["phase"] == "executing"


@pytest.mark.parametrize("change", [{"candidate_pr": {}}, {"ownership_nonce": None}, {"ownership_nonce": "X" * 32}, {"outcome": "complete"}])
def test_failure_spool_rejects_malformed_or_success_claims(spool, change):
    run_report.begin_delivery()
    run_report.spool_undelivered_failure()
    key = next(iter(spool[1]))
    spool[1][key] = json.dumps(json.loads(spool[1][key]) | change).encode()
    with pytest.raises(run_report.RunReportError, match="report_spool_scope_mismatch"):
        run_report.read_spool()

"""Durable pending payloads survive outages without authorizing a second run."""

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

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

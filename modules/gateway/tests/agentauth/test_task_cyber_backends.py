import hashlib
import io
import json
from unittest.mock import Mock

import pytest

from src.agentauth.task_cyber_backends import BackendUnavailableError, CyberBackends, _NoRedirect


@pytest.fixture
def sample():
    return {
        "bucket": "samples",
        "key": "tenant/team/user/task/in/sample",
        "version": "v1",
        "sha256": hashlib.sha256(b"sample").hexdigest(),
        "size": 6,
        "org_id": "tenant",
        "team_id": "team",
        "user_id": "user",
        "sample_s3_uri": "s3://samples/tenant/team/user/task/in/sample",
    }


def backend(env=None, http=None):
    clients = {name: Mock() for name in ("s3", "sqs", "dynamodb", "secretsmanager")}
    clients["s3"].generate_presigned_url.return_value = "https://s3.example/object?X-Amz-Signature=secret"
    clients["s3"].get_object.return_value = {"Body": io.BytesIO(b"sample")}
    clients["secretsmanager"].get_secret_value.return_value = {"SecretString": "private-provider-token"}
    return CyberBackends(env or {}, clients, http=http or Mock(), clock=lambda: 1000)


def test_fifo_manifest_pins_sample_and_keeps_download_private(sample):
    b = backend({"CYBER_TRIAGE_QUEUE": "queue.fifo"})
    result = b.submit("triage", "cyber-test", sample, {}, 1100)
    call = b.clients["sqs"].send_message.call_args.kwargs
    manifest = json.loads(call["MessageBody"])
    assert manifest["sample_download"] == {
        "url": b.clients["s3"].generate_presigned_url.return_value,
        "sha256": sample["sha256"],
        "size": 6,
        "version": "v1",
    }
    assert manifest["expires_at"] == 1100
    assert manifest["org_id"] == "tenant" and manifest["team_id"] == "team"
    assert call["MessageGroupId"] == call["MessageDeduplicationId"] == "cyber-test"
    assert "X-Amz" not in json.dumps(result)
    assert result["status"] == "pending"


def test_ambiguous_sqs_send_remains_unknown(sample):
    b = backend({"CYBER_STATIC_QUEUE": "queue.fifo"})
    b.clients["sqs"].send_message.side_effect = TimeoutError()
    result = b.submit("static", "cyber-test", sample, {}, 1100)
    assert result["status"] == "unknown" and result["_job"]["job_id"] == "cyber-test"
    b.clients["sqs"].send_message.assert_called_once()


def test_missing_config_is_partial_without_external_calls(sample):
    b = backend()
    assert b.submit("dynamic", "cyber-test", sample, {}, 1100)["status"] == "partial"
    assert b.url_analysis("https://example.org", 1100)["reason"] == "browser_not_configured"
    assert b.enrich(sample["sha256"], 1100)["reason"] == "virustotal_not_configured"
    b.http.assert_not_called()


@pytest.mark.parametrize("field,value", [("version", "null"), ("sha256", "invalid"), ("size", 100000000)])
def test_unpinned_or_unbounded_sample_refused(sample, field, value):
    sample[field] = value
    b = backend({"CYBER_TRIAGE_QUEUE": "queue"})
    with pytest.raises(ValueError):
        b.submit("triage", "cyber-test", sample, {}, 1100)
    b.clients["sqs"].send_message.assert_not_called()


def test_dynamic_verifies_exact_version_hash_before_cape(sample):
    b = backend({"CYBER_CAPE_ALB": "https://cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"}, Mock(return_value={"data": {"task_id": 42}}))
    result = b.submit("dynamic", "cyber-test", sample, {}, 1100)
    assert result["_job"]["provider_job_id"] == "42"
    b.clients["s3"].get_object.assert_called_once_with(Bucket="samples", Key=sample["key"], VersionId="v1")
    assert b.http.call_args.args == ("POST", "https://cape.internal/apiv2/tasks/create/file/")
    assert b"sample" in b.http.call_args.kwargs["data"]
    assert "private-provider-token" not in json.dumps(result)


def test_dynamic_hash_mismatch_never_uploads(sample):
    sample["sha256"] = "0" * 64
    b = backend({"CYBER_CAPE_ALB": "https://cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"})
    assert b.submit("dynamic", "cyber-test", sample, {}, 1100)["status"] == "partial"
    b.http.assert_not_called()


def test_dynamic_ambiguous_upload_not_reported_as_failure_safe_to_retry(sample):
    b = backend({"CYBER_CAPE_ALB": "https://cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"}, Mock(side_effect=TimeoutError()))
    assert b.submit("dynamic", "cyber-test", sample, {}, 1100)["status"] == "unknown"
    b.http.assert_called_once()


def test_scoped_stage_result_and_wrong_scope_refusal(sample):
    b = backend({"CYBER_RESULTS_TABLE": "results"})
    row = {
        key: {"S": value}
        for key, value in {
            "org_id": "tenant",
            "team_id": "team",
            "user_id": "user",
            "stage": "triage",
            "status": "ok",
            "findings": json.dumps({"sha256": sample["sha256"]}),
        }.items()
    }
    b.clients["dynamodb"].query.return_value = {"Items": [row]}
    job = {"kind": "triage", "job_id": "cyber-test", "sample": sample, "deadline_epoch": 1100}
    assert b.result(job)["status"] == "completed"
    row["org_id"] = {"S": "other-tenant"}
    assert b.result(job)["status"] == "partial"


def test_cape_poll_is_bounded_and_sanitizes_report():
    http = Mock(
        side_effect=[
            {"data": {"status": "reported"}},
            {
                "behavior": {"token": "private-provider-token", "files": ["https://x?X-Amz-Signature=secret"]},
                "info": {"note": "private-provider-token"},
            },
        ]
    )
    b = backend({"CYBER_CAPE_ALB": "https://cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"}, http)
    result = b.result(
        {"kind": "dynamic", "provider_job_id": "42", "backend_endpoint": "https://cape.internal", "job_id": "cyber-test", "deadline_epoch": 1100}
    )
    assert result["status"] == "completed" and http.call_count == 2
    assert "private-provider-token" not in json.dumps(result) and "X-Amz-Signature" not in json.dumps(result)


def test_no_backend_cancellation_claim_without_confirmation():
    result = backend().cancel({"kind": "dynamic", "job_id": "cyber-test"})
    assert result["status"] == "unknown" and result["reason"] == "backend_cancellation_not_supported"


def test_browser_uses_configured_broker_and_tls_verification():
    b = backend({"TASK_CYBER_BROWSER_ENDPOINT": "http://browser.internal:8080"}, Mock(return_value={"title": "Example"}))
    assert b.url_analysis("https://example.org", 1100)["status"] == "completed"
    assert b.http.call_args.args[1] == "http://browser.internal:8080/v1/analyze"
    assert json.loads(b.http.call_args.kwargs["data"])["ignore_https_errors"] is False
    assert b.http.call_args.kwargs["timeout"] <= 8


def test_enrichment_only_fixed_hash_endpoint():
    b = backend(
        {"CYBER_VT_TOKEN_SECRET": "vt/token"},
        Mock(return_value={"data": {"attributes": {"last_analysis_stats": {"malicious": 0}, "download_url": "secret"}}}),
    )
    result = b.enrich("a" * 64, 1100)
    assert b.http.call_args.args == ("GET", "https://www.virustotal.com/api/v3/files/" + "a" * 64)
    assert result["findings"] == {"last_analysis_stats": {"malicious": 0}}


def test_http_facade_refuses_redirect_without_forwarding_authorization():
    with pytest.raises(BackendUnavailableError, match="redirect_refused"):
        _NoRedirect().redirect_request(None, None, 302, None, None, "https://external.example")


def test_cape_endpoint_change_cannot_read_another_backend_job():
    b = backend({"CYBER_CAPE_ALB": "https://new-cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"})
    result = b.result(
        {"kind": "dynamic", "provider_job_id": "42", "backend_endpoint": "https://old-cape.internal", "job_id": "cyber-test", "deadline_epoch": 1100}
    )
    assert result["status"] == "partial"
    b.http.assert_not_called()


def test_elapsed_deadline_does_not_send_stage_job(sample):
    b = backend({"CYBER_TRIAGE_QUEUE": "queue"})
    assert b.submit("triage", "cyber-test", sample, {}, 999)["status"] == "partial"
    b.clients["sqs"].send_message.assert_not_called()


def test_completed_job_can_be_observed_after_execution_deadline(sample):
    b = backend({"CYBER_RESULTS_TABLE": "results"})
    values = {"org_id": "tenant", "team_id": "team", "user_id": "user", "stage": "triage", "status": "ok", "findings": "{}"}
    b.clients["dynamodb"].query.return_value = {"Items": [{k: {"S": v} for k, v in values.items()}]}
    result = b.result({"kind": "triage", "job_id": "cyber-test", "sample": sample, "deadline_epoch": 999})
    assert result["status"] == "completed"
    b.clients["sqs"].send_message.assert_not_called()


def test_expired_cape_job_can_still_be_observed_but_not_resubmitted():
    http = Mock(return_value={"data": {"status": "running"}})
    b = backend({"CYBER_CAPE_ALB": "https://cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"}, http)
    result = b.result(
        {"kind": "dynamic", "job_id": "cyber-test", "provider_job_id": "42", "backend_endpoint": "https://cape.internal", "deadline_epoch": 999}
    )
    assert result["status"] == "pending"
    assert http.call_args.args == ("GET", "https://cape.internal/apiv2/tasks/status/42/")
    assert http.call_args.kwargs["timeout"] <= 8


def test_browser_response_loss_is_unknown_not_confirmed_stopped():
    b = backend({"TASK_CYBER_BROWSER_ENDPOINT": "http://browser.internal:8080"}, Mock(side_effect=TimeoutError()))
    assert b.url_analysis("https://example.org", 1100)["status"] == "unknown"
    b.http.assert_called_once()


def test_browser_expired_before_send_is_partial_not_started():
    b = backend({"TASK_CYBER_BROWSER_ENDPOINT": "http://browser.internal:8080"})
    assert b.url_analysis("https://example.org", 999)["status"] == "partial"
    b.http.assert_not_called()


@pytest.mark.parametrize("state", ["reported", "completed"])
@pytest.mark.parametrize("failure", [TimeoutError(), BackendUnavailableError("response_too_large")])
def test_cape_terminal_execution_evidence_survives_report_failure(state, failure):
    http = Mock(side_effect=[{"data": {"status": state}}, failure])
    b = backend({"CYBER_CAPE_ALB": "https://cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"}, http)
    result = b.result(
        {"kind": "dynamic", "job_id": "cyber-test", "provider_job_id": "42", "backend_endpoint": "https://cape.internal", "deadline_epoch": 999}
    )
    assert result["status"] == "partial"
    assert result["execution_status"] == "completed"
    assert "findings" not in result


@pytest.mark.parametrize("state", ["failed", "error", "running", "nonsense"])
def test_cape_execution_status_only_for_explicit_terminal_state(state):
    b = backend({"CYBER_CAPE_ALB": "https://cape.internal", "CYBER_CAPE_TOKEN_SECRET": "cape/token"}, Mock(return_value={"data": {"status": state}}))
    result = b.result(
        {"kind": "dynamic", "job_id": "cyber-test", "provider_job_id": "42", "backend_endpoint": "https://cape.internal", "deadline_epoch": 999}
    )
    if state in {"failed", "error"}:
        assert result["execution_status"] == "failed"
    else:
        assert "execution_status" not in result

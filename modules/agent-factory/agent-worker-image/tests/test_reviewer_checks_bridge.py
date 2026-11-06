"""The controller receives the safe refusal and retry disposition on both transports."""
import json
import sys

import pytest

from lib import reviewer_checks, run_report, status_gateway_client


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("code,retryable", [("spend_unknown", True), ("policy_expired", False)])
def test_checks_bridge_preserves_safe_refusal(monkeypatch, capsys, tmp_path, shared, code, retryable):
    monkeypatch.setattr(sys, "argv", ["reviewer_checks", "a" * 40])
    monkeypatch.setattr(run_report, "_assignment", None)
    if shared:
        credential = tmp_path / "credential"
        credential.write_text("private-test-credential")
        monkeypatch.setenv("ADP_RUN_REPORT_CREDENTIAL_FILE", str(credential))
        monkeypatch.setenv("ADP_MESSAGE_ID", "reviewer")
        monkeypatch.setenv("ADP_ORCHESTRATION_ATTEMPT", "1")
        def refuse(*args, **kwargs):
            raise run_report.RunReportError(code, retryable=retryable)
        monkeypatch.setattr(run_report, "request", refuse)
    else:
        monkeypatch.delenv("ADP_RUN_REPORT_CREDENTIAL_FILE", raising=False)
        def refuse(*args, **kwargs):
            raise status_gateway_client.StatusGatewayError("private body", code=code, retryable=retryable)
        monkeypatch.setattr(status_gateway_client, "_post_bytes", refuse)
    reviewer_checks.main()
    assert json.loads(capsys.readouterr().out) == {"error": code, "retryable": retryable}


@pytest.mark.parametrize("status,detail,retryable", [
    (409, "spend_unknown", True),
    (409, "policy_expired", False),
    (503, "review_checks_unavailable", True),
    (409, "private secret body", False),
])
def test_shared_transport_preserves_only_allowlisted_review_refusal(monkeypatch, status, detail, retryable):
    from unittest.mock import MagicMock
    from botocore.credentials import Credentials
    from adp_trigger import transport_identity

    monkeypatch.setattr(run_report, "_assignment", {"credential": "private-credential"})
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.example.test/agent")
    monkeypatch.setattr(transport_identity, "worker_credentials", lambda _: Credentials("test", "test"))
    monkeypatch.setattr(transport_identity, "gateway_signing_region", lambda _: "us-east-1")
    response = MagicMock(status_code=status)
    response.raw.read.return_value = json.dumps({"detail": detail}).encode()
    session = MagicMock()
    session.request.return_value.__enter__.return_value = response
    monkeypatch.setattr(run_report.requests, "Session", lambda: session)
    session.__enter__.return_value = session
    with pytest.raises(run_report.RunReportError) as caught:
        run_report.request("/review-checks", {"head_sha": "a" * 40})
    assert caught.value.code == (f"run_report_http_{status}" if "private" in detail else detail)
    assert caught.value.retryable is retryable
    session.request.assert_called_once()
    response.raw.read.assert_called_once_with(4097, decode_content=True)


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("code,retryable", [("spend_unknown", True), ("policy_expired", False)])
def test_evidence_bridge_preserves_refusal(monkeypatch, capsys, tmp_path, shared, code, retryable):
    import io
    from lib import codex_review_delivery, review_result

    monkeypatch.delenv("ADP_RUN_REPORT_CREDENTIAL_FILE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"result": {}, "cycle": {}})))
    monkeypatch.setenv(review_result.REVIEW_EXPECT_ENV, "{}")
    monkeypatch.setenv(review_result.AGENT_REPORT_PATH_ENV, str(tmp_path / "report.json"))
    monkeypatch.setenv(review_result.RESULT_PATH_ENV, str(tmp_path / "result.json"))

    def refuse(*args, **kwargs):
        if shared:
            raise run_report.RunReportError(code, retryable=retryable)
        raise status_gateway_client.StatusGatewayError("private body", code=code, retryable=retryable)

    monkeypatch.setattr(codex_review_delivery, "finish_engine_review", refuse)
    codex_review_delivery.main()
    assert json.loads(capsys.readouterr().out) == {"error": code, "retryable": retryable}


@pytest.mark.parametrize("status", [409, 503])
@pytest.mark.parametrize("failure", ["malformed", "interrupted"])
def test_unreadable_refusal_keeps_status_disposition(status, failure):
    from unittest.mock import MagicMock
    from urllib3.exceptions import ProtocolError
    from lib.reviewer_errors import review_refusal

    response = MagicMock(status_code=status)
    if failure == "malformed":
        response.raw.read.return_value = b"not json"
    else:
        response.raw.read.side_effect = ProtocolError("private transport detail")
    assert review_refusal(response, "/review-checks", "fallback") == ("fallback", status == 503)

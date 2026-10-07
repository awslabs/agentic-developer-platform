"""The worker's authenticated status/control writes through the gateway (#5028 AC4).

Two things are asserted, and they are different claims:

1. **The gateway path sends the right proofs and nothing else.** Notably it does
   *not* send the row key or the control address, because a request that cannot
   name a row cannot name someone else's — and that is what allows the worker's
   table-wide ``dynamodb:UpdateItem`` grant to be removed.
2. **A failure never falls back to DynamoDB.** A fallback would require keeping
   that grant in place to serve it, which is the vulnerability being removed, and
   it would engage exactly when something is already wrong.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import invocation_status, status_gateway_client
from lib.status_gateway_client import StatusGatewayError

ENDPOINT = "https://api.example.test/internal/v1/agent"
CREDENTIAL = "adpr1.eyJpbnZvY2F0aW9uX2lkIjoicnVuLWEifQ.a-signature"
WORKLOAD_TOKEN = "a-projected-workload-token"


@pytest.fixture
def credential_file(tmp_path):
    path = tmp_path / "credential"
    path.write_text(CREDENTIAL + "\n", encoding="ascii")
    return path


@pytest.fixture
def enabled(credential_file, monkeypatch):
    """Authority on, with a credential file and a readable workload token."""
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("ADP_RUN_CREDENTIAL_FILE", str(credential_file))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(status_gateway_client, "read_workload_token", lambda: WORKLOAD_TOKEN)
    # Exercise the real web-identity provider and signing without using a CI
    # runner's IRSA token or network. Task credentials must not sign control calls.
    monkeypatch.setenv("ADP_WORKER_IRSA_ROLE_ARN", "arn:aws:iam::123456789012:role/worker")
    monkeypatch.setenv("ADP_WORKER_IRSA_TOKEN_FILE", str(credential_file))
    monkeypatch.setenv("ADP_WORKER_AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "customer-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "customer-secret")
    session = MagicMock()
    session.create_client.return_value.assume_role_with_web_identity.return_value = {
        "Credentials": {
            "AccessKeyId": "platform-key",
            "SecretAccessKey": "platform-secret",
            "SessionToken": "platform-token",
            "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
        }
    }
    monkeypatch.setattr(status_gateway_client.botocore.session, "get_session", lambda: session)
    # Process-local, so it survives between tests and would otherwise let one
    # test's registration satisfy another's teardown.
    invocation_status._registered_generation = None


class FakeResponse:
    def __init__(self, *, status_code=200, body=None):
        self.status_code = status_code
        self.raw = MagicMock()
        self.raw.read.return_value = json.dumps(body if body is not None else {}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def http(monkeypatch):
    """Captures the outbound request instead of sending it."""
    calls = []

    class FakeSession:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, url, data=None, headers=None, **kwargs):
            calls.append(
                {
                    "url": url,
                    "body": json.loads(data),
                    "headers": headers,
                    "kwargs": kwargs,
                    "session": self,
                }
            )
            return FakeResponse(body=calls_response[0])

    calls_response = [{}]
    monkeypatch.setattr(status_gateway_client.requests, "Session", FakeSession)
    return calls, calls_response


class TestTheRequestCarriesBothProofs:
    def test_the_run_credential_and_workload_token_are_both_sent(self, enabled, http):
        calls, _ = http

        status_gateway_client.record_status("in_progress", {"run_id": "keda-job-1"})

        headers = calls[0]["headers"]
        assert headers["X-Adp-Run-Credential"] == CREDENTIAL
        assert headers["X-Adp-Workload-Token"] == WORKLOAD_TOKEN
        assert "Credential=platform-key/" in headers["Authorization"]
        assert "customer-key" not in headers["Authorization"]

    def test_the_credential_is_read_fresh_on_every_call(self, enabled, http, credential_file):
        # The refresh thread replaces this file via os.replace as the credential
        # rotates. A value cached at startup would keep presenting a superseded
        # epoch until the pod exited.
        calls, _ = http
        status_gateway_client.record_status("in_progress", {})
        credential_file.write_text("adpr1.rotated.signature\n", encoding="ascii")
        status_gateway_client.record_status("complete", {})

        assert calls[0]["headers"]["X-Adp-Run-Credential"] == CREDENTIAL
        assert calls[1]["headers"]["X-Adp-Run-Credential"] == "adpr1.rotated.signature"

    def test_the_workload_token_is_read_fresh_on_every_call(self, enabled, http, monkeypatch):
        reads = []

        def read():
            reads.append(1)
            return WORKLOAD_TOKEN

        monkeypatch.setattr(status_gateway_client, "read_workload_token", read)
        for _ in range(3):
            status_gateway_client.record_status("in_progress", {})

        assert len(reads) == 3

    def test_environment_proxies_are_not_trusted(self, enabled, http):
        # A proxy on this path would see the run credential and the control token
        # in plaintext.
        calls, _ = http

        status_gateway_client.record_status("in_progress", {})

        assert calls[0]["session"].trust_env is False

    def test_redirects_are_not_followed(self, enabled, http):
        # A redirect would resend the credential to a host the gateway named.
        calls, _ = http

        status_gateway_client.record_status("in_progress", {})

        assert calls[0]["kwargs"]["allow_redirects"] is False


class TestTheRequestCannotNameAnotherRun:
    def test_a_status_write_sends_no_row_key(self, enabled, http):
        calls, _ = http

        invocation_status.update_status(
            event_id="run-b",
            arrived_at="2026-09-13T11:30:00Z",
            status="complete",
            summary="done",
        )

        # event_id/arrived_at are the caller's own row key in the legacy path and
        # the whole attack surface. They must not reach the wire.
        body = calls[0]["body"]
        assert "event_id" not in body
        assert "arrived_at" not in body
        assert "run-b" not in json.dumps(body)
        assert body["status"] == "complete"

    def test_a_registration_sends_no_address_or_port(self, enabled, http):
        calls, response = http
        response[0] = {"control_generation": 3}

        invocation_status.register_control_endpoint(
            "run-a",
            "2026-09-13T11:00:00Z",
            address="10.9.9.9",
            port=9999,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        # A caller-supplied address is the control-channel redirect this path
        # exists to remove. The gateway uses the pod IP it verified itself.
        body = calls[0]["body"]
        assert "control_address" not in body
        assert "10.9.9.9" not in json.dumps(body)
        assert "control_port" not in body
        assert body["control_token"] == "t" * 40

    def test_the_endpoint_must_be_https(self, enabled, monkeypatch):
        # This URL receives a bearer credential on every request.
        monkeypatch.setenv(
            "ADP_AGENT_CONTROL_ENDPOINT", "http://api.example.test/internal/v1/agent"
        )

        with pytest.raises(StatusGatewayError):
            status_gateway_client.record_status("in_progress", {})

    @pytest.mark.parametrize(
        "endpoint",
        [
            "",
            "https://user:pass@api.example.test/internal/v1/agent",
            "https://api.example.test/internal/v1/agent?x=1",
            "https://api.example.test/internal/v1/agent#frag",
        ],
    )
    def test_a_malformed_endpoint_is_refused(self, enabled, monkeypatch, endpoint):
        monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", endpoint)

        with pytest.raises(StatusGatewayError):
            status_gateway_client.record_status("in_progress", {})

    def test_the_writes_target_the_self_scoped_routes(self, enabled, http):
        calls, response = http
        response[0] = {"control_generation": 1}

        status_gateway_client.record_status("in_progress", {})
        status_gateway_client.register_control(
            token="t" * 40, token_expires_at="2026-09-13T13:00:00Z"
        )
        status_gateway_client.clear_control(1)

        assert [call["url"] for call in calls] == [
            ENDPOINT + "/self/status",
            ENDPOINT + "/self/control/registration",
            ENDPOINT + "/self/control/registration/clear",
        ]


class TestThereIsNoDynamoDbFallback:
    """The load-bearing property: a failure here must not reach DynamoDB.

    Any fallback would require keeping the table-wide ``dynamodb:UpdateItem`` grant
    to serve it — the exact grant AC4 removes.
    """

    @pytest.fixture
    def ddb(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(invocation_status, "_get_client", lambda: client)
        monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "adp-dev-webhook-events")
        return client

    def test_a_refused_status_write_does_not_write_dynamodb(self, enabled, ddb, monkeypatch):
        monkeypatch.setattr(
            status_gateway_client,
            "record_status",
            lambda *a, **k: (_ for _ in ()).throw(
                StatusGatewayError("gateway refused the write (status 404)")
            ),
        )
        monkeypatch.setattr(invocation_status, "record_status", status_gateway_client.record_status)

        # Still fail-soft: a lost status transition must never abort the run.
        invocation_status.update_status(
            event_id="run-a", arrived_at="2026-09-13T11:00:00Z", status="complete"
        )

        ddb.update_item.assert_not_called()

    def test_an_unavailable_workload_token_does_not_write_dynamodb(self, enabled, ddb, monkeypatch):
        from lib.run_identity import RunIdentityError

        monkeypatch.setattr(
            status_gateway_client,
            "read_workload_token",
            lambda: (_ for _ in ()).throw(RunIdentityError("projected workload token unavailable")),
        )

        invocation_status.update_status(
            event_id="run-a", arrived_at="2026-09-13T11:00:00Z", status="complete"
        )

        ddb.update_item.assert_not_called()

    def test_a_missing_credential_file_does_not_write_dynamodb(self, enabled, ddb, monkeypatch):
        monkeypatch.setenv("ADP_RUN_CREDENTIAL_FILE", "/nonexistent/credential")

        invocation_status.update_status(
            event_id="run-a", arrived_at="2026-09-13T11:00:00Z", status="complete"
        )

        ddb.update_item.assert_not_called()

    def test_a_failed_registration_does_not_write_dynamodb(self, enabled, ddb, monkeypatch):
        monkeypatch.setattr(
            invocation_status,
            "register_control",
            lambda **k: (_ for _ in ()).throw(StatusGatewayError("unavailable")),
        )

        generation = invocation_status.register_control_endpoint(
            "run-a",
            "2026-09-13T11:00:00Z",
            address="10.0.1.5",
            port=8770,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        # None means the caller declines to start the listener — an unregistered
        # listener is an open port nothing can reach through the policy.
        assert generation is None
        ddb.update_item.assert_not_called()

    def test_a_failed_clear_does_not_write_dynamodb(self, enabled, ddb, monkeypatch):
        invocation_status._registered_generation = 2
        monkeypatch.setattr(
            invocation_status,
            "clear_control",
            lambda g: (_ for _ in ()).throw(StatusGatewayError("unavailable")),
        )

        assert invocation_status.clear_control_endpoint("run-a", "2026-09-13T11:00:00Z") is False
        ddb.update_item.assert_not_called()

    @pytest.mark.parametrize("operation", ["status", "register", "clear"])
    def test_unexpected_transport_failure_is_redacted_and_never_falls_back(
        self, enabled, ddb, monkeypatch, caplog, operation
    ):
        def fail(*args, **kwargs):
            raise RuntimeError("sensitive-request-header-value")

        monkeypatch.setattr(
            invocation_status,
            {"status": "record_status", "register": "register_control", "clear": "clear_control"}[
                operation
            ],
            fail,
        )
        invocation_status._registered_generation = 2
        if operation == "status":
            invocation_status.update_status("run-a", "2026-09-13T11:00:00Z", "complete")
        elif operation == "register":
            assert (
                invocation_status.register_control_endpoint(
                    "run-a",
                    "2026-09-13T11:00:00Z",
                    address="10.0.1.5",
                    port=8770,
                    token="t" * 40,
                    token_expires_at="2026-09-13T13:00:00Z",
                )
                is None
            )
        else:
            assert (
                invocation_status.clear_control_endpoint("run-a", "2026-09-13T11:00:00Z") is False
            )
        assert "sensitive-request-header-value" not in caplog.text
        assert "via gateway" in caplog.text
        ddb.update_item.assert_not_called()

    def test_missing_listener_token_starts_no_registration(self, enabled, ddb, http):
        calls, _ = http
        assert (
            invocation_status.register_control_endpoint(
                "run-a",
                "2026-09-13T11:00:00Z",
                address="10.0.1.5",
                port=8770,
                token="",
                token_expires_at="2026-09-13T13:00:00Z",
            )
            is None
        )
        assert calls == []
        assert invocation_status._registered_generation is None
        ddb.update_item.assert_not_called()

    def test_a_non_200_response_is_not_treated_as_success(self, enabled, monkeypatch):
        class FakeSession:
            trust_env = True

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def post(self, *a, **k):
                return FakeResponse(status_code=403, body={})

        monkeypatch.setattr(status_gateway_client.requests, "Session", FakeSession)

        with pytest.raises(StatusGatewayError):
            status_gateway_client.record_status("in_progress", {})


class TestGenerationHandling:
    def test_the_generation_comes_from_the_gateway_response(self, enabled, http):
        _, response = http
        response[0] = {"control_generation": 7, "control_address": "10.0.1.5", "control_port": 8770}

        generation = invocation_status.register_control_endpoint(
            "run-a",
            "2026-09-13T11:00:00Z",
            address="10.0.1.5",
            port=8770,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        assert generation == 7

    @pytest.mark.parametrize("value", [None, 0, -1, "3", True, 3.5])
    def test_an_unusable_generation_is_a_failure_not_a_guess(self, enabled, http, value):
        # A guessed generation would make the listener reject every command the
        # gateway sends, which is indistinguishable from an attack.
        _, response = http
        response[0] = {"control_generation": value}

        with pytest.raises(StatusGatewayError):
            status_gateway_client.register_control(
                token="t" * 40, token_expires_at="2026-09-13T13:00:00Z"
            )

    def test_teardown_clears_the_generation_this_process_registered(self, enabled, http):
        calls, response = http
        response[0] = {"control_generation": 4}
        invocation_status.register_control_endpoint(
            "run-a",
            "2026-09-13T11:00:00Z",
            address="10.0.1.5",
            port=8770,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        assert invocation_status.clear_control_endpoint("run-a", "2026-09-13T11:00:00Z") is True
        assert calls[-1]["body"] == {"control_generation": 4}

    def test_teardown_without_a_registration_does_not_guess(self, enabled, http):
        # A guessed generation is either a no-op or someone else's teardown.
        calls, _ = http
        invocation_status._registered_generation = None

        assert invocation_status.clear_control_endpoint("run-a", "2026-09-13T11:00:00Z") is False
        assert calls == []


class TestLegacyModeIsUnchanged:
    """Authority off is the default, so a rollback is a flag flip."""

    @pytest.fixture
    def ddb(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(invocation_status, "_get_client", lambda: client)
        monkeypatch.setattr(invocation_status, "_table_name", "adp-dev-webhook-events")
        return client

    def test_the_default_is_the_direct_dynamodb_path(self, ddb, monkeypatch):
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)

        invocation_status.update_status(
            event_id="run-a", arrived_at="2026-09-13T11:00:00Z", status="in_progress"
        )

        ddb.update_item.assert_called_once()
        key = ddb.update_item.call_args.kwargs["Key"]
        assert key == {"event_id": {"S": "run-a"}, "arrived_at": {"S": "2026-09-13T11:00:00Z"}}

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", ""])
    def test_only_an_explicit_true_enables_the_gateway_path(self, ddb, monkeypatch, value):
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", value)

        invocation_status.update_status(
            event_id="run-a", arrived_at="2026-09-13T11:00:00Z", status="in_progress"
        )

        ddb.update_item.assert_called_once()

    def test_the_direct_path_still_registers_control_itself(self, ddb, monkeypatch):
        monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)
        ddb.update_item.return_value = {"Attributes": {"control_generation": {"N": "1"}}}

        generation = invocation_status.register_control_endpoint(
            "run-a",
            "2026-09-13T11:00:00Z",
            address="10.0.1.5",
            port=8770,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        assert generation == 1
        assert "ADD control_generation" in ddb.update_item.call_args.kwargs["UpdateExpression"]


class TestNothingSensitiveIsLogged:
    def test_no_credential_or_control_token_is_logged(self, enabled, http, caplog):
        caplog.set_level("DEBUG")
        _, response = http
        response[0] = {"control_generation": 5}

        invocation_status.update_status(
            event_id="run-a", arrived_at="2026-09-13T11:00:00Z", status="in_progress"
        )
        invocation_status.register_control_endpoint(
            "run-a",
            "2026-09-13T11:00:00Z",
            address="10.0.1.5",
            port=8770,
            token="s" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert CREDENTIAL not in logged
        assert "s" * 40 not in logged
        assert WORKLOAD_TOKEN not in logged

    def test_a_transport_failure_does_not_log_the_url_or_headers(
        self, enabled, monkeypatch, caplog
    ):
        # requests' exceptions stringify to include the full URL and can carry
        # headers, which is where the credential would surface.
        caplog.set_level("DEBUG")

        class FakeSession:
            trust_env = True

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def post(self, *a, **k):
                raise status_gateway_client.requests.ConnectionError(
                    f"failed to reach {ENDPOINT} with {CREDENTIAL}"
                )

        monkeypatch.setattr(status_gateway_client.requests, "Session", FakeSession)

        invocation_status.update_status(
            event_id="run-a", arrived_at="2026-09-13T11:00:00Z", status="complete"
        )

        logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert CREDENTIAL not in logged


def test_customer_region_does_not_change_platform_status_signature(enabled, http, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    status_gateway_client.record_status("in_progress", {})
    calls, _ = http
    assert "/us-east-1/execute-api/aws4_request" in calls[0]["headers"]["Authorization"]


class TestTranscriptArchive:
    def test_real_signing_and_receipt_verification(self, enabled, monkeypatch):
        import hashlib
        content = "a transcript with café"
        sent = []

        class Session:
            trust_env = True
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def post(self, url, **kwargs):
                sent.append((url, kwargs, self.trust_env))
                return FakeResponse(body={"key": "runs/own/transcript.md", "sha256": hashlib.sha256(content.encode()).hexdigest()})

        monkeypatch.setattr(status_gateway_client.requests, "Session", Session)
        assert status_gateway_client.upload_transcript(content) == "runs/own/transcript.md"
        url, request, proxies = sent[0]
        assert url == ENDPOINT + "/self/artifacts/transcript"
        assert request["data"] == content.encode()
        assert request["headers"]["X-Adp-Run-Credential"] == CREDENTIAL
        assert request["headers"]["X-Adp-Workload-Token"] == WORKLOAD_TOKEN
        assert "platform-key/" in request["headers"]["Authorization"]
        assert request["timeout"] == 35 and request["allow_redirects"] is False and proxies is False

    def test_digest_mismatch_is_not_a_success(self, enabled, monkeypatch):
        monkeypatch.setattr(status_gateway_client, "_post_bytes", lambda *a, **k: {"key": "runs/own/transcript.md", "sha256": "wrong"})
        with pytest.raises(StatusGatewayError, match="invalid transcript"):
            status_gateway_client.upload_transcript("text")

    @pytest.mark.parametrize("size", [0, 8 * 1024 * 1024 + 1])
    def test_bounds_before_network(self, enabled, monkeypatch, size):
        send = MagicMock()
        monkeypatch.setattr(status_gateway_client, "_post_bytes", send)
        with pytest.raises(StatusGatewayError, match="maximum 8 MiB"):
            status_gateway_client.upload_transcript("x" * size)
        send.assert_not_called()

    def test_entrypoint_refusal_never_falls_back_to_s3(self, enabled, monkeypatch):
        import entrypoint
        upload = MagicMock(side_effect=StatusGatewayError("refused"))
        direct = MagicMock(side_effect=AssertionError("direct S3 forbidden"))
        monkeypatch.setattr(status_gateway_client, "upload_transcript", upload)
        monkeypatch.setattr(entrypoint.boto3, "client", direct)
        monkeypatch.setenv("AGENT_RUN_LOGS_BUCKET", "shared-bucket")
        assert entrypoint._upload_transcript_to_s3("text", "owner/repo", 1, "run", "today", "developer") is None
        upload.assert_called_once_with("text")
        direct.assert_not_called()


@pytest.mark.parametrize("recorded", [True, False])
def test_review_upload_uses_live_own_run_transport(enabled, http, monkeypatch, recorded):
    import hashlib

    monkeypatch.setenv("ADP_TENANT_ID", "tenant")
    monkeypatch.setenv("ADP_MESSAGE_ID", "reviewer")
    monkeypatch.setenv("ADP_RUN_ATTEMPT", "1")
    data = b'{"result_id":"review-one"}'
    digest = hashlib.sha256(data).hexdigest()
    tenant = hashlib.sha256(b"tenant").hexdigest()
    run = hashlib.sha256(b"reviewer").hexdigest()
    key = f"runs/{tenant}/{run}/attempt-1/review-result/{digest}.json"
    calls, responses = http
    responses[0] = {"key": key, "sha256": digest, "recorded": recorded}
    if recorded:
        assert status_gateway_client.upload_review_result(data) == key
    else:
        with pytest.raises(StatusGatewayError, match="receipt"):
            status_gateway_client.upload_review_result(data)
    assert len(calls) == 1
    assert calls[0]["url"] == ENDPOINT + "/self/artifacts/review-result"
    assert calls[0]["headers"]["X-Adp-Run-Credential"] == CREDENTIAL
    assert calls[0]["headers"]["X-Adp-Workload-Token"] == WORKLOAD_TOKEN
    assert "Credential=platform-key/" in calls[0]["headers"]["Authorization"]
    assert calls[0]["kwargs"]["allow_redirects"] is False
    assert calls[0]["session"].trust_env is False


def test_review_upload_shared_report_transport_checks_exact_byte_receipt(monkeypatch):
    import hashlib
    from lib import run_report

    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_TENANT_ID", "tenant")
    monkeypatch.setenv("ADP_MESSAGE_ID", "reviewer")
    monkeypatch.setenv("ADP_RUN_ATTEMPT", "1")
    monkeypatch.setattr(run_report, "enabled", lambda: True)
    data = b'{"result_id": "review-one"}\n'
    digest = hashlib.sha256(data).hexdigest()
    key = f"runs/{hashlib.sha256(b'tenant').hexdigest()}/{hashlib.sha256(b'reviewer').hexdigest()}/attempt-1/review-result/{digest}.json"
    calls = []

    def report(path, body):
        calls.append((path, body))
        return {"key": key, "sha256": digest, "recorded": True}

    monkeypatch.setattr(run_report, "request", report)
    assert status_gateway_client.upload_review_result(data) == key
    assert calls == [("/review-result", {"content": data.decode()})]
    monkeypatch.setattr(run_report, "request", lambda *args: {"key": key, "sha256": "wrong", "recorded": True})
    with pytest.raises(StatusGatewayError, match="receipt"):
        status_gateway_client.upload_review_result(data)


@pytest.mark.parametrize("status,retryable", [(403, False), (409, False), (429, True), (500, True), (503, True)])
def test_reviewer_transport_distinguishes_temporary_failures(enabled, http, monkeypatch, status, retryable):
    original = FakeResponse.__init__
    def response_init(self, **kwargs):
        original(self, **{**kwargs, "status_code": status})
    monkeypatch.setattr(FakeResponse, "__init__", response_init)
    with pytest.raises(StatusGatewayError) as caught:
        status_gateway_client._post("/review-checks", {"head_sha": "a" * 40})
    assert caught.value.retryable is retryable

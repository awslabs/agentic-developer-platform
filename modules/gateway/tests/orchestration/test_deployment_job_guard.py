"""The job-side probe does not disclose credentials or accept an unbound release."""

import json
import runpy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deployment-job-guard.py"
guard = runpy.run_path(str(SCRIPT))
TARGET = {
    "provider": "aws",
    "account_id": "000000000000",
    "region": "us-east-1",
    "resource_kind": "eks-namespace",
    "resource_id": "sample-cluster/example",
}


@pytest.fixture
def environment():
    return {
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.test/",
        "ADP_DEPLOY_CORRELATION": "c" * 64,
        "ADP_DEPLOY_SOURCE_REVISION": "a" * 40,
        "ADP_DEPLOY_INPUTS_JSON": '{"environment": "dev"}',
        "ADP_DEPLOY_TARGET_JSON": json.dumps(TARGET),
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.actions.githubusercontent.com/token?job=123",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token-sentinel",
    }


class Response:
    status = 200

    def __init__(self, value):
        self.data = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.data[:size]


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        assert timeout == 15
        self.requests.append(request)
        return self.responses.pop(0)


def test_exact_job_uses_oidc_and_checks_private_context(environment):
    opener = Opener(
        Response({"value": "oidc-token-sentinel"}),
        Response({"source_revision": "a" * 40, "inputs": {"environment": "dev"}, "target": TARGET}),
    )
    assert guard["check_job"](environment, opener=opener) is None
    oidc_request, context_request = opener.requests
    assert parse_qs(urlsplit(oidc_request.full_url).query)["audience"] == [guard["AUDIENCE"]]
    assert oidc_request.get_header("Authorization") == "Bearer request-token-sentinel"
    assert context_request.full_url == "https://gateway.example.test/orchestration/deployment-credentials/context"
    assert context_request.get_method() == "POST"
    assert context_request.get_header("Authorization") == "Bearer oidc-token-sentinel"
    assert json.loads(context_request.data) == {
        "correlation": "c" * 64,
        "source_revision": "a" * 40,
        "inputs": {"environment": "dev"},
    }


@pytest.mark.parametrize(
    "result",
    [
        {"source_revision": "b" * 40, "inputs": {"environment": "dev"}},
        {"source_revision": "a" * 40, "inputs": {"environment": "prod"}},
        {"source_revision": "a" * 40},
        {"source_revision": "a" * 40, "inputs": {"environment": "dev"}},
        {"source_revision": "a" * 40, "inputs": {"environment": "dev"}, "target": {**TARGET, "account_id": "111111111111"}},
        {"source_revision": "a" * 40, "inputs": {"environment": "dev"}, "target": {**TARGET, "resource_id": "other"}},
    ],
)
def test_mismatched_context_refuses_after_job_attestation(environment, result):
    opener = Opener(Response({"value": "signed-job"}), Response(result))
    with pytest.raises(guard["GuardRefusedError"]):
        guard["check_job"](environment, opener=opener)
    assert len(opener.requests) == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("ADP_GATEWAY_BASE_URL", "http://gateway.example.test"),
        ("ADP_GATEWAY_BASE_URL", "https://user:pass@gateway.example.test"),
        ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.githubusercontent.com.evil.invalid/token"),
        ("ADP_DEPLOY_CORRELATION", "forged"),
        ("ADP_DEPLOY_CORRELATION", ""),
        ("ADP_DEPLOY_CORRELATION", "orch:35a28578-f760-5af6-95c8-e1dbe8635703"),
        ("ADP_DEPLOY_SOURCE_REVISION", "b"),
        ("ADP_DEPLOY_INPUTS_JSON", '{"environment": 10}'),
        ("ADP_DEPLOY_TARGET_JSON", "{}"),
    ],
)
def test_invalid_origins_and_job_binding_refuse_before_any_request(environment, key, value):
    environment[key] = value
    opener = Opener()
    with pytest.raises(guard["GuardRefusedError"]):
        guard["check_job"](environment, opener=opener)
    assert opener.requests == []


def test_missing_oidc_refuses_without_posting_context(environment):
    opener = Opener(Response({"value": ""}))
    with pytest.raises(guard["GuardRefusedError"]):
        guard["check_job"](environment, opener=opener)
    assert len(opener.requests) == 1
    assert guard["NoRedirect"]().redirect_request(None, None, 302, None, None, "https://elsewhere.invalid") is None


def test_oversized_oidc_response_refuses_before_gateway_request(environment):
    opener = Opener(Response({"value": "x" * 65537}))
    with pytest.raises(guard["GuardRefusedError"]):
        guard["check_job"](environment, opener=opener)
    assert len(opener.requests) == 1


def test_oversized_gateway_response_refuses(environment):
    opener = Opener(Response({"value": "signed-job"}), Response({"source_revision": "a" * 40, "extra": "x" * 65537}))
    with pytest.raises(guard["GuardRefusedError"]):
        guard["check_job"](environment, opener=opener)
    assert len(opener.requests) == 2


def test_cli_failure_never_prints_environment_or_tokens(environment, monkeypatch, capsys):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ADP_GATEWAY_BASE_URL", "not a URL")
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert error.value.code == 1
    assert capsys.readouterr().err == "deployment_job_guard_refused\n"

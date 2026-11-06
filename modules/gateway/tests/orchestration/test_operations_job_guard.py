"""The standalone job probe refuses missing or forged release authorization."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "operations-job-guard.py"
probe = runpy.run_path(str(SCRIPT))


@pytest.fixture
def job():
    return {
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipelines.actions.githubusercontent.com/oidc?job=1",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-request-token",
        "ADP_RELEASE_CORRELATION": "orch:12345678-1234-1234-1234-123456789abc",
        "ADP_RELEASE_SOURCE_REVISION": "a" * 40,
        "ADP_RELEASE_DISPATCH_REVISION": "b" * 40,
        "ADP_RELEASE_WORKFLOW": "release.yml",
        "ADP_RELEASE_ROLE_ARN": "arn:aws:iam::000000000000:role/test",
    }


class Opener:
    def __init__(self, answers=None):
        self.requests = []
        self.answers = answers or [{"value": "test-oidc"}, {"authorized": True}]

    def open(self, request, timeout):
        self.requests.append(request)
        answer = self.answers[len(self.requests) - 1]

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self, size):
                return json.dumps(answer).encode()

        return Response()


def test_job_checks_distinct_audience_and_exact_release(job):
    opener = Opener()
    probe["check_job"](job, opener=opener)
    assert len(opener.requests) == 2
    assert "audience=adp-operations-release-job-v1" in opener.requests[0].full_url
    sent = opener.requests[1]
    assert sent.full_url == "https://gateway.example.invalid/orchestration/operations-release-job/check"
    assert sent.get_method() == "POST"
    assert json.loads(sent.data) == {
        "correlation": job["ADP_RELEASE_CORRELATION"],
        "source_revision": job["ADP_RELEASE_SOURCE_REVISION"],
        "dispatch_revision": job["ADP_RELEASE_DISPATCH_REVISION"],
        "workflow": job["ADP_RELEASE_WORKFLOW"],
        "role_arn": job["ADP_RELEASE_ROLE_ARN"],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("ADP_GATEWAY_BASE_URL", "http://gateway.example.invalid"),
        ("ADP_GATEWAY_BASE_URL", "https://gateway.example.invalid/other"),
        ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://notgithub.example.invalid/oidc"),
        ("ADP_RELEASE_CORRELATION", "wrong-run"),
        ("ADP_RELEASE_SOURCE_REVISION", "wrong-source"),
        ("ADP_RELEASE_DISPATCH_REVISION", "wrong-dispatch"),
        ("ADP_RELEASE_WORKFLOW", "../wrong.yml"),
        ("ADP_RELEASE_ROLE_ARN", "arn:aws:iam::000000000000:role/invalid role"),
    ],
)
def test_invalid_job_binding_never_requests_oidc(job, field, value):
    job[field] = value
    opener = Opener()
    with pytest.raises(probe["GuardRefusedError"]):
        probe["check_job"](job, opener=opener)
    assert not opener.requests


@pytest.mark.parametrize(
    "answers",
    [
        [{"value": ""}],
        [{"value": "bad token"}],
        [{"value": "test-oidc"}, {"authorized": False}],
        [{"value": "test-oidc"}, {"authorized": True, "target": "forged"}],
    ],
)
def test_missing_token_or_extra_authorization_is_refused(job, answers):
    with pytest.raises(probe["GuardRefusedError"]):
        probe["check_job"](job, opener=Opener(answers))

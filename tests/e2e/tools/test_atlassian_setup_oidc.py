"""Controlled fresh OIDC exchange; no real GitHub or AWS calls."""

import json
from urllib.parse import parse_qs

import pytest

from tests.e2e.tools.atlassian_setup_oidc import credential_process, main


ROLE = "arn:aws:iam::123456789012:role/adp-dev-atlassian-mcp-observer-core"


def context(monkeypatch):
    for key, value in {
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": (
            "aws-e/adp/.github/workflows/tools-live-evaluation.yml@refs/heads/main"
        ),
        "GITHUB_RUN_ID": "2345",
        "GITHUB_RUN_ATTEMPT": "2",
        "AWS_REGION": "us-east-1",
        "ACTIONS_ID_TOKEN_REQUEST_URL": (
            "https://pipelines.actions.githubusercontent.com/oidc?api-version=2.0"
        ),
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-request-token",
    }.items():
        monkeypatch.setenv(key, value)


def test_credential_process_exchanges_new_oidc_token_without_storing_it(
    monkeypatch,
):
    context(monkeypatch)
    calls = []

    class Response:
        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 16385
            return self.content

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            if len(calls) % 2:
                return Response(json.dumps({"value": "ephemeral-oidc-jwt"}).encode())
            assert request.full_url == "https://sts.us-east-1.amazonaws.com/"
            fields = parse_qs(request.data.decode())
            assert fields["RoleArn"] == [ROLE]
            assert fields["WebIdentityToken"] == ["ephemeral-oidc-jwt"]
            return Response(
                b"<AssumeRoleWithWebIdentityResponse><Credentials>"
                b"<AccessKeyId>temporary</AccessKeyId>"
                b"<SecretAccessKey>not-logged</SecretAccessKey>"
                b"<SessionToken>transient</SessionToken>"
                b"<Expiration>2026-10-26T01:00:00Z</Expiration>"
                b"</Credentials></AssumeRoleWithWebIdentityResponse>"
            )

    monkeypatch.setattr(
        "tests.e2e.tools.atlassian_setup_oidc.build_opener", lambda _: Opener()
    )
    assert credential_process(ROLE, environment="dev")["SessionToken"] == "transient"
    assert credential_process(ROLE, environment="dev")["AccessKeyId"] == "temporary"
    assert len(calls) == 4
    assert "audience=sts.amazonaws.com" in calls[0][0].full_url
    assert calls[0][0].get_header("Authorization") == "Bearer private-request-token"


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_EVENT_NAME", "push"),
        (
            "GITHUB_WORKFLOW_REF",
            "aws-e/adp/.github/workflows/other.yml@refs/heads/main",
        ),
        ("ACTIONS_ID_TOKEN_REQUEST_URL", "http://localhost/oidc"),
    ],
)
def test_credential_process_refuses_other_contexts(monkeypatch, key, invalid):
    context(monkeypatch)
    monkeypatch.setenv(key, invalid)
    with pytest.raises(ValueError):
        credential_process(ROLE, environment="dev")


def test_credential_process_never_prints_token_on_failure(monkeypatch, capsys):
    context(monkeypatch)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/untrusted")
    monkeypatch.setattr("sys.argv", ["observer-oidc", ROLE, "dev"])
    with pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-request-token" not in captured.err

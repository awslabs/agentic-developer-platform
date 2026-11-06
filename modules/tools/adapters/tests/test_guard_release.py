"""The native workflow guard never turns an unavailable or mismatched job into authority."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("adapter_release_guard", ROOT / "guard_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def environment():
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40,
        "ADP_CORRELATION": "orch:00000000-0000-0000-0000-000000000001",
        "ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-deployment-connection",
        "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
        "ADP_TOOLS_ADAPTER_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-tool-adapters",
        "ADP_TOOLS_EVAL_GATEWAY_URL": "https://gateway.example.test",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.actions.githubusercontent.com/token?request=one&audience=old",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-test-request-token",
    }


class Response:
    status = 200

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return json.dumps(self.value).encode()[:limit]


class Opener:
    def __init__(self, *, decision=None, identity="header.body.signature"):
        self.calls = []
        self.decision = {"authorized": True} if decision is None else decision
        self.identity = identity

    def open(self, request, timeout):
        assert timeout == (15 if not self.calls else 45)
        self.calls.append(request)
        return Response({"value": self.identity} if len(self.calls) == 1 else self.decision)


def test_signed_first_attempt_uses_exact_job_and_no_secret_in_body(guard, environment):
    opener = Opener()
    assert guard.authorize(environment, opener) is None
    identity, check = opener.calls
    assert "audience=adp-operations-release-job-v1" in identity.full_url
    assert "audience=old" not in identity.full_url
    assert identity.get_header("Authorization") == "Bearer private-test-request-token"
    assert check.full_url == "https://gateway.example.test/api/orchestration/operations-release-job/check"
    assert check.get_header("Authorization") == "Bearer header.body.signature"
    assert json.loads(check.data) == {
        "correlation": environment["ADP_CORRELATION"],
        "source_revision": environment["ADP_SOURCE_REVISION"],
        "dispatch_revision": environment["ADP_DEFINITION_REVISION"],
        "workflow": "tools-platform-deploy.yml",
        "role_arn": environment["ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN"],
        "mutation_role_arn": environment["ADP_TOOLS_DEPLOY_ROLE_ARN"],
    }
    assert environment["ADP_TOOLS_ADAPTER_ROLE_ARN"] != json.loads(check.data)["role_arn"]
    assert b"private-test-request-token" not in check.data
    assert b"header.body.signature" not in check.data


@pytest.mark.parametrize("elapsed,allowed", [(16.853, True), (46, False)])
@pytest.mark.parametrize(
    "kwargs,decision",
    [
        ({}, {"authorized": True}),
        ({"publication_digest": "sha256:" + "b" * 64}, {"recorded": True}),
        ({"publication_digest": "sha256:" + "b" * 64, "settlement": True}, {"settled": True}),
        ({"transition": {"action": "prepare", "plan": {}}}, {"state": None}),
    ],
)
def test_slow_authority_response_is_bounded_without_replaying_mutations(
    guard, environment, elapsed, allowed, kwargs, decision
):
    class SlowAuthority(Opener):
        def open(self, request, timeout):
            response = super().open(request, timeout)
            if len(self.calls) == 2 and elapsed > timeout:
                raise TimeoutError("authority response exceeded request timeout")
            return response

    opener = SlowAuthority(decision=decision)
    if allowed:
        assert guard.authorize(environment, opener, **kwargs) is None
    else:
        with pytest.raises(guard.GuardRefused):
            guard.authorize(environment, opener, **kwargs)
    assert len(opener.calls) == 2


def test_publication_intent_uses_same_signed_job_with_expected_digest(guard, environment):
    opener = Opener(decision={"recorded": True})
    digest = "sha256:" + "b" * 64
    assert guard.authorize(environment, opener, publication_digest=digest) is None
    request = opener.calls[-1]
    assert (
        request.full_url == "https://gateway.example.test/api/orchestration/operations-release-job/publication-intent"
    )
    assert json.loads(request.data)["image_digest"] == digest
    assert b"private-test-request-token" not in request.data
    with pytest.raises(guard.GuardRefused):
        guard.authorize(environment, Opener(), publication_digest="wrong")


@pytest.mark.parametrize(
    "digest,settlement", [(None, False), ("sha256:" + "b" * 64, False), ("sha256:" + "b" * 64, True)]
)
def test_identity_guard_carries_exact_generic_digest_on_each_protected_call(guard, environment, digest, settlement):
    environment["ADP_IDENTITY_CONTRACT_DIGEST"] = "f" * 64
    opener = Opener(decision={"settled": True} if settlement else {"recorded": True} if digest else None)
    if not digest:
        opener = Opener()
    guard.authorize(environment, opener, publication_digest=digest, settlement=settlement)
    assert json.loads(opener.calls[-1].data)["contract_digest"] == "f" * 64
    environment["ADP_IDENTITY_CONTRACT_DIGEST"] = "not-a-digest"
    with pytest.raises(guard.GuardRefused):
        guard.authorize(environment, Opener(), publication_digest=digest, settlement=settlement)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_REF", "refs/heads/other"),
        ("ADP_CORRELATION", "manual-run"),
        ("ADP_SOURCE_REVISION", "main"),
        ("ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN", "arn:aws:iam::123456789012:role/other?role"),
        ("ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN", ""),
        ("ADP_TOOLS_DEPLOY_ROLE_ARN", "arn:aws:iam::123456789012:role/other"),
        ("ADP_TOOLS_DEPLOY_ROLE_ARN", "arn:aws:iam::000000000000:role/adp-dev-trusted-deployment"),
        ("ADP_TOOLS_DEPLOY_ROLE_ARN", ""),
        ("ADP_TOOLS_EVAL_GATEWAY_URL", "http://gateway.example.test"),
        ("ADP_TOOLS_EVAL_GATEWAY_URL", "https://gateway.example.test/path"),
        ("ADP_TOOLS_EVAL_GATEWAY_URL", "https://attacker@gateway.example.test"),
        ("ADP_TOOLS_EVAL_GATEWAY_URL", "https://gateway.example.test:bad"),
        ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://oidc.other.example/token"),
        ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://oidc.actions.githubusercontent.com:bad/token"),
        ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://[broken/token"),
        ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "invalid token with spaces"),
    ],
)
def test_invalid_context_never_requests_identity(guard, environment, key, value):
    environment[key] = value
    opener = Opener()
    with pytest.raises(guard.GuardRefused, match="native_release_guard_refused"):
        guard.authorize(environment, opener)
    assert not opener.calls


@pytest.mark.parametrize("decision", [{"authorized": False}, {"authorized": True, "extra": "unexpected"}, {}])
def test_ambiguous_or_denied_job_refuses_without_leaking_tokens(guard, environment, decision):
    opener = Opener(decision=decision)
    with pytest.raises(guard.GuardRefused, match="native_release_guard_refused") as error:
        guard.authorize(environment, opener)
    assert len(opener.calls) == 2
    assert "private-test-request-token" not in str(error.value)
    assert "header.body.signature" not in str(error.value)


def test_redirects_are_never_followed(guard):
    with pytest.raises(guard.GuardRefused, match="native_release_guard_refused"):
        guard.NoRedirects().redirect_request(SimpleNamespace(), None, 302, "move", {}, "https://unexpected.test")


@pytest.mark.parametrize(
    "options",
    [
        {"workflow": "unapproved.yml"},
        {"workflow": "gateway-deploy.yml", "publication_digest": "sha256:" + "a" * 64},
        {"workflow": "gateway-deploy.yml", "settlement": True},
    ],
)
def test_shared_guard_does_not_expand_gateway_publication_authority(guard, environment, options):
    opener = Opener()
    with pytest.raises(guard.GuardRefused):
        guard.authorize(environment, opener, **options)
    assert not opener.calls


def test_publication_settlement_requires_signed_response(guard, environment):
    digest = "sha256:" + "b" * 64
    opener = Opener(decision={"settled": True})
    guard.authorize(environment, opener, publication_digest=digest, settlement=True)
    request = opener.calls[-1]
    assert (
        request.full_url == "https://gateway.example.test/api/orchestration/operations-release-job/publication-observed"
    )
    assert json.loads(request.data)["image_digest"] == digest
    with pytest.raises(guard.GuardRefused):
        guard.authorize(environment, Opener(decision={"recorded": True}), publication_digest=digest, settlement=True)
    with pytest.raises(guard.GuardRefused):
        guard.authorize(environment, Opener(), settlement=True)

"""The GitHub adapter boundary for tracker projection (#5284).

`test_tracker_projection.py` stubs the provider and covers which decision the pass
takes. This file covers the layer below it: the actual HTTP calls, the token those
calls are made with, and the refusals that happen before any request is sent.

Two things here are worth stating as *security* properties rather than as behaviour:

- The token is minted for one repository with `issues` at the least verb needed, so a
  projection cannot be turned into a lever on anything else in the installation.
  `test_read_asks_for_read_and_write_asks_for_write` fails if either scope widens.
- No provider response body reaches the error text. Token minting and app resolution
  can surface signed URLs and key material, so every failure is a fixed string.
  `test_failures_never_carry_provider_detail` is what keeps that true.

The transport is `httpx.MockTransport`, matching `test_pr_identity.py` — the same
boundary, stubbed the same way.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.tracker_provider import GitHubTrackerProvider, TrackerProviderError

REPO = "aws-e/adp"
ISSUE = 4910
ORG = "org-alpha"
INSTALLATION = 4242
SECRET = "https://signed.example/private-key?sig=do-not-log-this"


@pytest.fixture
def provider(monkeypatch):
    """A provider whose credentials and transport are both stubbed and recorded."""
    credentials = AsyncMock(return_value=("app-id", "private-key"))
    token = AsyncMock(return_value=("installation-token", "expiry"))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.knowledge.github_app_service.mint_installation_token_with_expiry", token)

    state = {"body": "## Intent\n\nbody text", "status": 200}
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(state["status"], json={"body": state["body"]})
        return httpx.Response(state["status"], json={})

    real = httpx.AsyncClient

    def client(**kwargs):
        # Pinned here rather than asserted once: an environment proxy must not be able
        # to reroute a credentialed call, and a redirect is how a credentialed request
        # reaches an owner it was never authorized for (GitHub 301s moved repos).
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("src.orchestration.tracker_provider.httpx.AsyncClient", client)
    return GitHubTrackerProvider(), state, requests, credentials, token


async def test_read_fetches_the_issue_body(provider):
    subject, state, requests, _, _ = provider
    body = await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE)
    assert body == state["body"]
    assert requests[0].method == "GET"
    assert str(requests[0].url) == f"https://api.github.com/repos/{REPO}/issues/{ISSUE}"


async def test_an_empty_issue_body_reads_as_empty_string_not_none(provider):
    """GitHub returns JSON null for an issue with no body — a legitimate state.

    Coerced at the boundary so every downstream string operation has a string. The
    caller then refuses it for having no sentinels, which is the correct outcome.
    """
    subject, state, *_ = provider
    state["body"] = None
    assert await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE) == ""


async def test_write_patches_only_the_body_field(provider):
    """The PATCH carries `body` alone: no title, no labels, no state, no assignees.

    A projection that sent any other field could close an issue or relabel it, which
    would make a display update into an engine action.
    """
    import json

    subject, _, requests, _, _ = provider
    await subject.write_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE, body="new body")
    assert requests[0].method == "PATCH"
    assert json.loads(requests[0].content) == {"body": "new body"}


async def test_read_asks_for_read_and_write_asks_for_write(provider):
    """Least privilege, and the read path deliberately does not get the write verb.

    The read happens on every examined flow while the write happens only when
    something changed, so minting the narrower token for the common call is the
    difference that matters.
    """
    subject, _, _, credentials, token = provider

    await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE)
    credentials.assert_awaited_once_with(ORG)
    assert token.await_args.kwargs == {"repositories": ["adp"], "permissions": {"issues": "read", "metadata": "read"}}
    assert token.await_args.args == ("app-id", "private-key", INSTALLATION)

    await subject.write_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE, body="x")
    assert token.await_args.kwargs == {"repositories": ["adp"], "permissions": {"issues": "write", "metadata": "read"}}


async def test_the_token_is_sent_as_a_bearer_credential(provider):
    subject, _, requests, _, _ = provider
    await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE)
    assert requests[0].headers["authorization"] == "Bearer installation-token"
    assert requests[0].headers["x-github-api-version"] == "2022-11-28"


@pytest.mark.parametrize("body", ["", "   ", "\n\t\n"])
async def test_writing_an_empty_body_is_refused_before_any_request(provider, body):
    """An empty body would erase the issue. Refused unconditionally.

    There is no legitimate projection that blanks the issue it reports on, so this is
    a refusal rather than a validation warning — and it happens before the request, so
    a bug upstream cannot cost a user their issue text.
    """
    subject, _, requests, _, _ = provider
    with pytest.raises(TrackerProviderError):
        await subject.write_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE, body=body)
    assert requests == []


async def test_an_oversize_body_is_refused_with_an_explanation(provider):
    """GitHub rejects a body past 65536 chars with a 422 that reads like an outage.

    Checked here so the operator sees a stated size refusal instead of a mystery
    failure that retries forever.
    """
    subject, _, requests, _, _ = provider
    with pytest.raises(TrackerProviderError, match="size limit"):
        await subject.write_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE, body="x" * 65537)
    assert requests == []


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "no-slash",
        "owner/repo/extra",
        "owner/..",
        "../owner",
        "owner/",
        "own er/repo",
        "owner/repo?x=1",
    ],
)
async def test_a_malformed_repository_is_refused_before_any_request(provider, repo):
    """Path traversal and injection are excluded at the boundary, not by the URL join.

    `owner/..` would otherwise resolve to a different API path once joined onto the
    base URL — the same check `pr_identity.py` makes, for the same reason.
    """
    subject, _, requests, _, token = provider
    with pytest.raises(TrackerProviderError):
        await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=repo, issue_number=ISSUE)
    assert requests == []
    # And no credential was minted for a target we already knew was unusable.
    token.assert_not_awaited()


@pytest.mark.parametrize("issue", [0, -1, None, "4910", 4910.0, True])
async def test_a_non_issue_number_is_refused_before_any_request(provider, issue):
    """`True` is in this list on purpose: it is an `int` subclass and would otherwise
    resolve to issue 1. The check is `type(...) is not int` for exactly that reason.
    """
    subject, _, requests, _, _ = provider
    with pytest.raises(TrackerProviderError):
        await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=issue)
    assert requests == []


@pytest.mark.parametrize(("org", "installation"), [("", INSTALLATION), (ORG, 0), (ORG, -5), (ORG, None), (ORG, "4242")])
async def test_an_unauthorized_target_is_refused(provider, org, installation):
    """AC3: a target that is not an authorized tenant/installation pair is refused."""
    subject, _, requests, _, _ = provider
    with pytest.raises(TrackerProviderError):
        await subject.read_issue_body(org_id=org, installation_id=installation, repo=REPO, issue_number=ISSUE)
    assert requests == []


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 500, 502])
async def test_a_provider_error_status_becomes_a_tracker_error(provider, status):
    """Every non-success is a `TrackerProviderError`, which the flush counts and retries.

    Notably including 404 and 403: an issue the app cannot see is not silently treated
    as "nothing to update", because that is indistinguishable from success and would
    hide a misconfigured installation forever.
    """
    subject, state, *_ = provider
    state["status"] = status
    with pytest.raises(TrackerProviderError):
        await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE)


async def test_failures_never_carry_provider_detail(provider):
    """A credential failure must not put the provider's response into the log.

    Token minting can return signed URLs and key material, and this text reaches logs.
    The assertion is against the *formatted* traceback rather than against
    `__context__`, because `raise ... from None` does not clear `__context__` — it sets
    `__cause__` to None and `__suppress_context__` to True, and the original exception
    object stays reachable on the raised one. What that flag guarantees is that
    `traceback` and therefore `logger.exception` will not render it, which is the
    property actually being relied on here.

    Worth knowing rather than glossing: the secret is still retrievable via
    `err.__context__` by anything that walks the chain explicitly and ignores the
    suppression flag. Standard logging does not, so this is the same exposure the
    existing `pr_identity.py` idiom accepts — but a future custom error handler that
    chases `__context__` would defeat it.
    """
    import traceback

    subject, _, _, credentials, _ = provider
    credentials.side_effect = RuntimeError(f"app lookup failed: {SECRET}")

    with pytest.raises(TrackerProviderError) as caught:
        await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE)

    error = caught.value
    assert str(error) == "Tracker credentials are unavailable."
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert SECRET not in rendered
    assert "app lookup failed" not in rendered


async def test_a_transport_failure_becomes_a_tracker_error(provider, monkeypatch):
    """A connection error is a retryable provider failure, not a crash in the tick."""
    subject, *_ = provider

    def failing(**kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("src.orchestration.tracker_provider.httpx.AsyncClient", failing)
    with pytest.raises(TrackerProviderError):
        await subject.read_issue_body(org_id=ORG, installation_id=INSTALLATION, repo=REPO, issue_number=ISSUE)

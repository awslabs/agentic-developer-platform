"""Resolve immutable PR identity using the tenant's scoped GitHub token."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.pr_identity import PrIdentityError, resolve_head_check_runs, resolve_pr_identity


@pytest.fixture
def provider(monkeypatch):
    credentials = AsyncMock(return_value=("app-id", "private-key"))
    token = AsyncMock(return_value=("installation-token", "expiry"))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.knowledge.github_app_service.mint_installation_token_with_expiry", token)
    record = {"number": 12, "node_id": "PR_provider", "head": {"sha": "a" * 40}, "base": {"repo": {"id": 99, "full_name": "owner/repo"}}}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=record)

    client_type = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("src.orchestration.pr_identity.httpx.AsyncClient", client)
    return record, requests, credentials, token


async def test_identity_is_fetched_with_tenant_and_repository_scope(provider):
    _, requests, credentials, token = provider
    identity = await resolve_pr_identity(org_id="tenant", installation_id=7, repo="owner/repo", pr_number=12)
    assert (identity.provider_repository_id, identity.provider_pr_node_id, identity.head_sha) == (99, "PR_provider", "a" * 40)
    credentials.assert_awaited_once_with("tenant")
    token.assert_awaited_once_with(
        "app-id",
        "private-key",
        7,
        repositories=["repo"],
        permissions={"metadata": "read", "pull_requests": "read"},
    )
    assert str(requests[0].url) == "https://api.github.com/repos/owner/repo/pulls/12"


@pytest.mark.parametrize("mutation", ["missing_id", "missing_node", "missing_head", "wrong_number", "wrong_repo"])
async def test_provider_identity_must_be_complete_and_match_request(provider, mutation):
    record, *_ = provider
    if mutation == "missing_id":
        record["base"]["repo"]["id"] = None
    elif mutation == "missing_node":
        record["node_id"] = ""
    elif mutation == "missing_head":
        record["head"]["sha"] = ""
    elif mutation == "wrong_number":
        record["number"] = 13
    else:
        record["base"]["repo"]["full_name"] = "other/repo"
    with pytest.raises(PrIdentityError):
        await resolve_pr_identity(org_id="tenant", installation_id=7, repo="owner/repo", pr_number=12)


async def test_token_failure_does_not_expose_provider_error(provider):
    *_, token = provider
    token.side_effect = RuntimeError("private provider response")
    with pytest.raises(PrIdentityError, match="^Pull-request identity could not be verified\\.$"):
        await resolve_pr_identity(org_id="tenant", installation_id=7, repo="owner/repo", pr_number=12)


HEAD = "b" * 40


@pytest.fixture
def check_runs(monkeypatch):
    """The provider's check-run listing for one commit, as pages.

    Pages are returned from a list so a test can describe a multi-page response
    without knowing how the function walks them, and every request is captured so a
    test can assert the read was scoped to the commit rather than to the pull request.
    """
    credentials = AsyncMock(return_value=("app-id", "private-key"))
    token = AsyncMock(return_value=("installation-token", "expiry"))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.knowledge.github_app_service.mint_installation_token_with_expiry", token)

    state = {"pages": [{"check_runs": [{"id": 105036077448}, {"id": 105036077449}]}], "status": 200}
    requests = []

    def respond(request):
        requests.append(request)
        index = int(dict(request.url.params).get("page", 1)) - 1
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"message": "Bad credentials"})
        page = state["pages"][index] if index < len(state["pages"]) else {"check_runs": []}
        return httpx.Response(200, json=page)

    client_type = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("src.orchestration.pr_identity.httpx.AsyncClient", client)
    return state, requests, credentials, token


class TestCheckRunsAreReadForTheExactCommit:
    """The read that makes a cited check run verifiable (#5146).

    A review citing `check-run:105036077448` is only evidence if that run exists, in
    this repository, against the commit under review. None of that is in the
    submitted document, and a reviewer can write a real check-run id belonging to a
    different commit. So the scoping in the request path — not a filter applied
    afterwards — is the substance of these tests.
    """

    async def test_the_request_is_scoped_to_the_commit_and_the_grant_is_narrow(self, check_runs):
        _, requests, credentials, token = check_runs
        refs = await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD)

        assert refs == frozenset({"check-run:105036077448", "check-run:105036077449"})
        credentials.assert_awaited_once_with("tenant")
        # `checks: read` and nothing else: a token minted to verify evidence must not
        # be able to write a check, a review or a comment.
        token.assert_awaited_once_with(
            "app-id",
            "private-key",
            7,
            repositories=["repo"],
            permissions={"metadata": "read", "checks": "read"},
        )
        assert requests[0].url.path == f"/repos/owner/repo/commits/{HEAD}/check-runs", (
            "the read must be scoped to the commit; a pull-request-scoped read would return runs from any of its commits"
        )

    async def test_the_reference_spelling_matches_the_contract(self, check_runs):
        """`check-run:<id>`, so the caller can intersect without re-deriving it.

        The one place the two halves must agree on a string. If this drifted from the
        contract's spelling, every honestly-cited check run would fail to intersect
        and every real review would be refused as `UNTRUSTED_ARTIFACT` — a
        total-refusal outage that no type would catch.
        """
        refs = await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD)
        assert all(ref.startswith("check-run:") for ref in refs)
        assert all(ref.split(":", 1)[1].isdigit() for ref in refs)

    async def test_a_commit_with_no_check_runs_is_an_empty_set_not_an_error(self, check_runs):
        """A real answer. Distinct from the failure case below, which raises."""
        state, *_ = check_runs
        state["pages"] = [{"check_runs": []}]
        assert await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD) == frozenset()

    async def test_later_pages_are_read(self, check_runs):
        """A full first page must not be mistaken for the whole answer.

        A busy repository's head carries more than one page of check runs. Truncating
        at the first would make a legitimately-cited run on page two unverifiable, and
        the review would be refused for citing evidence that exists.
        """
        state, requests, *_ = check_runs
        state["pages"] = [
            {"check_runs": [{"id": 1000 + index} for index in range(100)]},
            {"check_runs": [{"id": 2001}]},
        ]
        refs = await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD)
        assert "check-run:2001" in refs
        assert len(refs) == 101
        assert len(requests) >= 2

    async def test_a_conclusion_is_not_filtered_on(self, check_runs):
        """A failing check still resolves as a reference.

        This answers "does this reference resolve", not "did the check pass". Filtering
        failures out would turn a reviewer's cited failure into an unverifiable
        reference, converting a reasoned request-changes into `UNTRUSTED_ARTIFACT` —
        discarding exactly the evidence a blocking review rests on.
        """
        state, *_ = check_runs
        state["pages"] = [{"check_runs": [{"id": 55, "conclusion": "failure"}, {"id": 56, "conclusion": None}]}]
        refs = await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD)
        assert refs == frozenset({"check-run:55", "check-run:56"})

    async def test_malformed_entries_are_dropped_rather_than_trusted(self, check_runs):
        """An unusable id must not become a reference the validator then trusts."""
        state, *_ = check_runs
        state["pages"] = [{"check_runs": [{"id": None}, {"id": "105036077448"}, {"id": 0}, {"id": -1}, {}, {"id": 77}]}]
        refs = await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD)
        assert refs == frozenset({"check-run:77"})

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"org_id": ""},
            {"installation_id": 0},
            {"installation_id": True},
            {"repo": "owner"},
            {"repo": "../etc/passwd"},
            {"repo": "owner/../secrets"},
            {"head_sha": "main"},
            {"head_sha": "Z" * 40},
            {"head_sha": ""},
        ],
    )
    async def test_an_unusable_request_is_refused_before_a_token_is_minted(self, check_runs, kwargs):
        """Validated before any credential exists, so a bad ref cannot mint a token.

        `head_sha` must be a commit id and not a branch name in particular: the
        provider resolves this endpoint for a ref, so `main` would answer about
        whatever the branch points at *now* — re-introducing the moving target the
        whole issue exists to remove, while appearing to verify.
        """
        _, requests, credentials, token = check_runs
        request = {"org_id": "tenant", "installation_id": 7, "repo": "owner/repo", "head_sha": HEAD, **kwargs}
        with pytest.raises(PrIdentityError):
            await resolve_head_check_runs(**request)
        credentials.assert_not_awaited()
        token.assert_not_awaited()
        assert requests == []

    async def test_a_provider_failure_raises_and_redacts(self, check_runs):
        """Raises rather than returning empty, and carries no provider material.

        Both halves matter. The caller must be able to tell "asked, nothing there"
        from "could not ask", and this message reaches a reviewer, so a provider body
        or signed URL here would be a disclosure with no revocation path.
        """
        state, *_ = check_runs
        state["status"] = 401
        with pytest.raises(PrIdentityError, match="^Check runs could not be verified\\.$"):
            await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD)

    async def test_a_token_failure_does_not_expose_provider_error(self, check_runs):
        *_, token = check_runs
        token.side_effect = RuntimeError("private provider response")
        with pytest.raises(PrIdentityError, match="^Check runs could not be verified\\.$"):
            await resolve_head_check_runs(org_id="tenant", installation_id=7, repo="owner/repo", head_sha=HEAD)

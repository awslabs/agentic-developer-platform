"""Provider identities, current reviews and successful head checks gate completion."""

from copy import deepcopy

import pytest

from src.orchestration.results import GitHubEvidenceSource


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "withdrawn",
        "missing-check",
        "missing-review",
        "different-head",
        # Issue #5350. Every PR the engine opens is authored by the same GitHub App
        # that reviews it, and GitHub 422s APPROVE/REQUEST_CHANGES on your own PR.
        # The reviewer's only remaining option is a COMMENT review or a plain issue
        # comment, neither of which sets `reviewDecision`. These three cases pin the
        # arm that must therefore never pass: a verdict that could not be recorded is
        # not an approval. The behaviour is already correct — it was entirely
        # uncovered, so a refactor could have removed it silently and turned
        # "no verdict was possible" into "approved".
        "comment-only-review",
        "no-verdict-at-all",
        "self-approval",
    ],
)
async def test_provider_eligibility_uses_current_opinion_and_head_checks(monkeypatch, defect):
    head = "a" * 40
    record = {
        "id": "PR_123",
        "url": "https://github.com/acme/app/pull/1",
        "merged": True,
        "mergedAt": "2026-09-17T04:19:36Z",
        "mergeCommit": {"oid": "b" * 40},
        "headRefOid": head,
        "author": {"login": "developer"},
        "reviewDecision": "APPROVED",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        "reviews": {
            "pageInfo": {"hasPreviousPage": False},
            "nodes": [
                {"state": "APPROVED", "submittedAt": "2026-09-17T04:00:00Z", "commit": {"oid": head}, "author": {"login": "reviewer"}},
            ],
        },
    }
    if defect == "withdrawn":
        later = deepcopy(record["reviews"]["nodes"][0])
        later.update(state="CHANGES_REQUESTED", submittedAt="2026-09-17T04:10:00Z")
        record["reviews"]["nodes"].append(later)
    elif defect == "missing-check":
        record["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    elif defect == "missing-review":
        record["reviewDecision"] = "REVIEW_REQUIRED"
    elif defect == "different-head":
        record["reviews"]["nodes"][0]["commit"]["oid"] = "c" * 40
    elif defect == "comment-only-review":
        # What the engine's reviewer actually achieves today: GitHub accepted the
        # COMMENT it fell back to, so a review exists and carries a real verdict in
        # its body — but `state` is COMMENTED and reviewDecision stays empty.
        record["reviewDecision"] = None
        record["reviews"]["nodes"][0]["state"] = "COMMENTED"
    elif defect == "no-verdict-at-all":
        # The verdict was published as an ordinary issue comment, so the PR has no
        # review objects at all. An absent reviewDecision must not read as approval.
        record["reviewDecision"] = None
        record["reviews"]["nodes"] = []
    elif defect == "self-approval":
        # The impossible case, asserted anyway: were GitHub ever to accept a
        # self-approval, the author approving their own PR is still not independent
        # review, and the predicate must exclude it by login.
        record["reviews"]["nodes"][0]["author"] = {"login": "developer"}

    class App:
        def __init__(self, *args):
            pass

        async def get_installation_token(self, installation_id):
            assert installation_id == 4242
            return "test-token"

        async def aclose(self):
            pass

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"repository": {"databaseId": 987, "pullRequest": record}}}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            assert "states:APPROVED" not in kwargs["json"]["query"]
            assert "branchProtectionRule" not in kwargs["json"]["query"]
            return Response()

    async def credentials(org_id):
        assert org_id == "org-a"
        return 12, "test-key"

    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", App)
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.orchestration.results.httpx.AsyncClient", Client)
    evidence = await GitHubEvidenceSource().bound_pull_request(org_id="org-a", installation_id=4242, repo="acme/app", pr_number=1)
    assert evidence.provider_repository_id == 987
    assert evidence.provider_pr_node_id == "PR_123"
    assert evidence.head_sha == head
    assert evidence.checks_successful == (defect != "missing-check")
    assert evidence.approved_by_non_author == (defect in {None, "missing-check"})

"""Provider-authenticated current-head reviews and successful checks gate completion."""

from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from src.orchestration.pr_bindings import BindingRefusal, evidence_for_binding
from src.orchestration.results import GitHubEvidenceSource

HEAD = "a" * 40


def provider_record():
    return {
        "id": "PR_123",
        "url": "https://github.com/acme/app/pull/1",
        "merged": True,
        "mergedAt": "2026-09-17T04:19:36Z",
        "mergeCommit": {"oid": "b" * 40},
        "headRefOid": HEAD,
        "author": {"login": "developer"},
        "reviewDecision": "APPROVED",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        "reviews": {
            "pageInfo": {"hasPreviousPage": False},
            "nodes": [
                {"state": "APPROVED", "submittedAt": "2026-09-17T04:00:00Z", "commit": {"oid": HEAD}, "author": {"login": "reviewer"}},
            ],
        },
    }


def app_comment():
    return {
        "id": 1,
        "updated_at": "2026-09-17T04:10:00Z",
        "user": {"login": "developer", "type": "Bot"},
        "performed_via_github_app": {"id": 12},
        "body": f"## agent-codex-reviewer — APPROVE\n\n**Reviewed head:** `{HEAD}`\n**Blockers:** 0\n**Engine:** Codex SDK 0.155.1\n\nReviewed.\n",
    }


@pytest.fixture
def read_provider(monkeypatch):
    client_type = httpx.AsyncClient

    class App:
        def __init__(self, *args):
            pass

        async def get_installation_token(self, installation_id):
            assert installation_id == 4242
            return "test-token"

        async def aclose(self):
            pass

    async def credentials(org_id):
        assert org_id == "org-a"
        return 12, "test-key"

    async def read(record, comments=None, *, pages=None, endless=False, error=False):
        def respond(request):
            assert request.headers["Authorization"] == "Bearer test-token"
            if request.url.path == "/graphql":
                assert request.method == "POST"
                return httpx.Response(200, json={"data": {"repository": {"databaseId": 987, "pullRequest": record}}})
            assert request.method == "GET"
            assert request.url.path == "/repos/acme/app/issues/1/comments"
            assert request.url.params["per_page"] == "100"
            page = int(request.url.params["page"])
            if error:
                return httpx.Response(403)
            headers = {}
            if endless or (pages and page < len(pages)):
                headers["Link"] = f'<https://api.github.com/repos/acme/app/issues/1/comments?page={page + 1}>; rel="next"'
            return httpx.Response(200, json=pages[page - 1] if pages else comments or [], headers=headers)

        monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", App)
        monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
        monkeypatch.setattr(
            "src.orchestration.merge_evidence.httpx.AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(respond), **kw)
        )
        return await GitHubEvidenceSource().bound_pull_request(org_id="org-a", installation_id=4242, repo="acme/app", pr_number=1)

    return read


@pytest.mark.parametrize(
    "defect", [None, "withdrawn", "missing-check", "missing-review", "different-head", "comment-only-review", "no-verdict-at-all", "self-approval"]
)
async def test_provider_eligibility_uses_current_opinion_and_head_checks(read_provider, defect):
    record = provider_record()
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
        record["reviewDecision"] = None
        record["reviews"]["nodes"][0]["state"] = "COMMENTED"
    elif defect == "no-verdict-at-all":
        record["reviewDecision"] = None
        record["reviews"]["nodes"] = []
    elif defect == "self-approval":
        # GitHub does not permit formal self-approval; use an authenticated App verdict.
        record["reviews"]["nodes"][0]["author"] = {"login": "developer"}
    evidence = await read_provider(record)
    assert evidence.provider_repository_id == 987
    assert evidence.provider_pr_node_id == "PR_123"
    assert evidence.head_sha == HEAD
    assert evidence.checks_successful == (defect != "missing-check")
    assert evidence.review_approved == (defect in {None, "missing-check"})


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "fixes-reviewed",
        "human-author",
        "wrong-app",
        "human-comment",
        "no-app",
        "prose",
        "issue-verdict",
        "stale",
        "blockers",
        "missing-blockers",
        "withdrawn",
        "later-stale",
        "later-malformed",
        "github-required",
        "github-changes",
        "formal-changes",
        "incomplete-reviews",
        "failed-ci",
    ],
)
async def test_shared_identity_verdict_and_completion(read_provider, defect):
    record = provider_record()
    record["reviewDecision"] = None
    record["reviews"]["nodes"] = []
    comment = app_comment()
    comments = [comment]
    if defect == "fixes-reviewed":
        comment["body"] = comment["body"].replace("— APPROVE", "— FIXES PUSHED AND APPROVED")
    elif defect == "human-author":
        record["author"]["login"] = "human"
    elif defect == "wrong-app":
        comment["performed_via_github_app"]["id"] = 99
    elif defect == "human-comment":
        comment["user"]["type"] = "User"
    elif defect == "no-app":
        comment.pop("performed_via_github_app")
    elif defect == "prose":
        comment["body"] = "I APPROVE this PR.\n" + comment["body"]
    elif defect == "issue-verdict":
        comment["body"] = comment["body"].replace("— APPROVE", "— ISSUE READY")
    elif defect == "stale":
        comment["body"] = comment["body"].replace(HEAD, "c" * 40)
    elif defect == "blockers":
        comment["body"] = comment["body"].replace("**Blockers:** 0", "**Blockers:** 1")
    elif defect == "missing-blockers":
        comment["body"] = comment["body"].replace("**Blockers:** 0\n", "")
    elif defect in {"withdrawn", "later-stale", "later-malformed"}:
        later = deepcopy(comment)
        later.update(id=2, updated_at="2026-09-17T04:15:00Z")
        if defect == "withdrawn":
            later["body"] = later["body"].replace("— APPROVE", "— REQUEST CHANGES")
        elif defect == "later-stale":
            later["body"] = later["body"].replace(HEAD, "c" * 40)
        else:
            later["body"] = "## agent-codex-reviewer — APPROVE\nIncomplete publication"
        comments.append(later)
    elif defect in {"github-required", "github-changes"}:
        record["reviewDecision"] = "REVIEW_REQUIRED" if defect == "github-required" else "CHANGES_REQUESTED"
    elif defect == "formal-changes":
        review = provider_record()["reviews"]["nodes"][0]
        review["state"] = "CHANGES_REQUESTED"
        record["reviews"]["nodes"] = [review]
    elif defect == "incomplete-reviews":
        record["reviews"]["pageInfo"]["hasPreviousPage"] = True
    elif defect == "failed-ci":
        record["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
    evidence = await read_provider(record, comments)
    assert evidence.review_approved == (defect in {None, "fixes-reviewed", "human-author", "failed-ci"})
    bound = SimpleNamespace(provider_repository_id=987, provider_pr_node_id="PR_123", head_sha=HEAD, repo="acme/app", pr_number=1)
    url, refusal = evidence_for_binding(bound, evidence)
    if defect in {None, "fixes-reviewed", "human-author"}:
        assert url == record["url"] and refusal is None
    else:
        assert url is None
        assert refusal is (BindingRefusal.CHECKS_NOT_GREEN if defect == "failed-ci" else BindingRefusal.NO_INDEPENDENT_REVIEW)


async def test_later_page_withdraws_even_formal_approval(read_provider):
    withdrawal = app_comment()
    withdrawal.update(id=2, updated_at="2026-09-17T04:15:00Z")
    withdrawal["body"] = withdrawal["body"].replace("— APPROVE", "— REQUEST CHANGES")
    evidence = await read_provider(provider_record(), pages=[[app_comment()], [withdrawal]])
    assert not evidence.review_approved


async def test_edited_older_comment_withdraws_approval(read_provider):
    withdrawal = app_comment()
    withdrawal.update(id=1, updated_at="2026-09-17T04:15:00Z")
    withdrawal["body"] = withdrawal["body"].replace("— APPROVE", "— REQUEST CHANGES")
    approval = app_comment()
    approval["id"] = 2
    evidence = await read_provider(provider_record(), [withdrawal, approval])
    assert not evidence.review_approved


@pytest.mark.parametrize("failure", ["pagination", "provider-error"])
async def test_incomplete_comments_cannot_pass(read_provider, failure):
    with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        await read_provider(provider_record(), [app_comment()], endless=failure == "pagination", error=failure == "provider-error")


@pytest.mark.parametrize("state", ["FAILURE", "PENDING", "SUCCESS", None])
async def test_provider_preserves_check_diagnosis_and_mergeability(read_provider, state):
    record = provider_record()
    record.update(merged=False, isDraft=False, mergeable="MERGEABLE", mergeStateStatus="CLEAN")
    record["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {"state": state} if state else None
    evidence = await read_provider(record)
    assert evidence.checks_state == (state or "MISSING")
    assert evidence.checks_successful == (state == "SUCCESS")
    assert evidence.review_state == "approved"
    assert evidence.mergeable == "MERGEABLE" and evidence.merge_state == "CLEAN" and evidence.draft is False


@pytest.mark.parametrize(
    "verdict,expected", [("REQUEST CHANGES", "changes_requested"), ("APPROVE", "approved"), ("stale", "stale"), ("missing", "missing")]
)
async def test_provider_preserves_shared_app_review_diagnosis(read_provider, verdict, expected):
    record = provider_record()
    record["reviews"]["nodes"] = []
    record["reviewDecision"] = None
    comment = app_comment()
    if verdict == "stale":
        comment["body"] = comment["body"].replace(HEAD, "f" * 40)
    elif verdict == "REQUEST CHANGES":
        comment["body"] = comment["body"].replace("APPROVE", "REQUEST CHANGES")
    evidence = await read_provider(record, [] if verdict == "missing" else [comment])
    assert evidence.review_state == expected
    assert evidence.review_approved == (expected == "approved")

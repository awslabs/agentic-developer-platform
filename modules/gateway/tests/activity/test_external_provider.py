"""EXT01-t2: provider fixtures use immutable identities across aliases and repos."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from src.activity.external_events import classify_events
from src.activity.external_provider import PageBudget, read_github, read_gitlab

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 4, tzinfo=UTC)
TIME = "2026-10-02T11:00:00Z"


def run(coroutine):
    return asyncio.run(coroutine)


def gh_event(kind, event_id, repo, **fields):
    path = {
        "commit": f"commit/{event_id}",
        "pull": f"pull/{fields.get('number', event_id)}",
        "review": "pull/3#pullrequestreview-3",
        "comment": "issues/4#issuecomment-4",
    }[kind]
    return {
        "id": event_id,
        "sha": event_id,
        "html_url": f"https://github.com/{repo}/{path}",
        "user": {"id": 17, "login": "new-login"},
        "author": {"id": 17, "login": "old-login"},
        "created_at": TIME,
        "submitted_at": TIME,
        "updated_at": TIME,
        "commit": {"committer": {"date": TIME}},
        **fields,
    }


def test_github_reads_authored_events_reviews_old_prs_and_issue_comments_across_orgs():
    seen = []

    def handler(request):
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == "Bearer fixture-installation-token"
        seen.append(str(request.url))
        repo = "/".join(request.url.path.split("/")[2:4])
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[gh_event("commit", f"sha-{repo[-1]}", repo)])
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[gh_event("pull", 3, repo, number=3, created_at="2026-09-01T11:00:00Z")])
        if request.url.path.endswith("/pulls/3/reviews"):
            return httpx.Response(200, json=[gh_event("review", f"review-{repo[-1]}", repo)])
        if request.url.path.endswith("/issues/comments"):
            return httpx.Response(200, json=[gh_event("comment", f"comment-{repo[-1]}", repo)])
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_github("fixture-installation-token", ["org-a/repo1", "org-b/repo2"], START, END, client=client)

    result = run(collect())
    assert result.incomplete is False
    assert [(item.repository, item.kind) for item in result.events] == [
        (repo, kind) for repo in ("org-a/repo1", "org-b/repo2") for kind in ("commit", "review", "comment")
    ]
    assert all("fixture-installation-token" not in url for url in seen)
    assert len(classify_events(result.events, {"github": {"17"}})) == 6


def test_github_includes_prs_created_in_window_and_ignores_unlinked_commit_authors():
    def handler(request):
        repo = "org/repo"
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[gh_event("commit", "unlinked", repo, author=None)])
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[gh_event("pull", 12, repo, number=12)])
        return httpx.Response(200, json=[])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_github("token", ["org/repo"], START, END, client=client)

    assert [item.kind for item in run(collect()).events] == ["pull_request"]


@pytest.mark.parametrize("updated_at", [TIME, END.isoformat(), "2026-10-05T11:00:00Z"])
def test_github_historical_reviews_survive_later_pr_updates(updated_at):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[
                    gh_event("pull", 3, "org/repo", number=3, created_at="2026-09-01T11:00:00Z", updated_at=updated_at),
                    gh_event("pull", 9, "org/repo", number=9, created_at="2026-09-01T11:00:00Z", updated_at="2026-09-30T11:00:00Z"),
                ],
            )
        if request.url.path.endswith("/pulls/3/reviews"):
            return httpx.Response(
                200,
                json=[
                    gh_event(
                        "review",
                        identifier,
                        "org/repo",
                        submitted_at=timestamp,
                        html_url=f"https://github.com/org/repo/pull/3#pullrequestreview-{identifier}",
                    )
                    for identifier, timestamp in enumerate(
                        ["2026-09-30T11:00:00Z", START.isoformat(), TIME, END.isoformat(), "2026-10-05T11:00:00Z"], start=1
                    )
                ],
            )
        return httpx.Response(200, json=[])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_github("token", ["org/repo"], START, END, client=client)

    result = run(collect())
    assert [event.event_id for event in result.events] == ["2", "3"]
    assert all(event.kind == "review" for event in result.events)
    assert not result.incomplete
    assert "/repos/org/repo/pulls/9/reviews" not in requested


@pytest.mark.parametrize(
    "source_url,accepted",
    [
        ("https://github.com/org/repo/issues/4#issuecomment-4", True),
        ("https://github.com/org/repo/pull/4#issuecomment-4", True),
        ("https://github.com/org/repo/pull/4#pullrequestreview-4", False),
        ("https://github.com/org/repo/pull/4#discussion_r4", False),
        ("https://github.com/org/other/pull/4#issuecomment-4", False),
        ("https://evil.example.invalid/org/repo/pull/4#issuecomment-4", False),
        ("https://github.com/org/repo/pull/4?token=fixture-secret#issuecomment-4", False),
    ],
)
def test_github_issue_and_pr_discussion_links_remain_scoped(source_url, accepted):
    def handler(request):
        assert request.url.host == "api.github.com"
        rows = [gh_event("comment", 4, "org/repo", html_url=source_url)] if request.url.path.endswith("/issues/comments") else []
        return httpx.Response(200, json=rows)

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_github("fixture-secret", ["org/repo"], START, END, client=client)

    result = run(collect())
    assert [event.source_url for event in result.events] == ([source_url] if accepted else [])
    assert result.incomplete is not accepted
    assert result.gaps == (set() if accepted else {"source_invalid"})
    assert "fixture-secret" not in repr(result)


def gitlab_note_event(event_id, **note_fields):
    return {
        "id": event_id,
        "author": {"id": 42},
        "target_type": "Note",
        "target_iid": 601,
        "action_name": "commented on",
        "created_at": TIME,
        "note": {
            "id": 601,
            "noteable_type": "Issue",
            "noteable_iid": 4,
            "system": False,
            "body": "untrusted comment body",
            **note_fields,
        },
    }


@pytest.mark.parametrize("noteable_type,path", [("Issue", "issues"), ("MergeRequest", "merge_requests")])
def test_gitlab_reads_native_note_events_with_exact_parent_and_comment_links(noteable_type, path):
    note = gitlab_note_event(401, noteable_type=noteable_type)
    bot = {**gitlab_note_event(402, noteable_type=noteable_type, id=602), "author": {"id": 99, "bot": True}}

    def handler(request):
        rows = [note, bot, note] if request.url.path.endswith("/events") else []
        return httpx.Response(200, json=rows)

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab("token", "https://gitlab.example.invalid", [(7, "org/repo")], START, END, client=client, verified_emails={})

    result = run(collect())
    assert not result.incomplete
    assert [event.actor_kind for event in result.events] == ["human", "bot", "human"]
    classified = classify_events(result.events, {"gitlab": {"42"}})
    assert len(classified) == 1 and classified[0].human_work
    assert classified[0].event.source_url == f"https://gitlab.example.invalid/org/repo/-/{path}/4#note_601"
    assert classified[0].event.event_id == "401"
    assert "untrusted comment body" not in repr(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("noteable_iid", 0),
        ("noteable_iid", True),
        ("noteable_iid", "4"),
        ("noteable_iid", "../private"),
        ("id", 0),
        ("id", True),
        ("id", "token"),
        ("system", None),
    ],
)
def test_gitlab_malformed_note_references_report_incomplete_history(field, value):
    row = gitlab_note_event(401, **{field: value})

    def handler(request):
        return httpx.Response(200, json=[gitlab_note_event(402, id=602), row] if request.url.path.endswith("/events") else [])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab("token", "https://gitlab.example.invalid", [(7, "org/repo")], START, END, client=client, verified_emails={})

    result = run(collect())
    assert [event.event_id for event in result.events] == ["402"]
    assert result.incomplete and result.gaps == {"response_invalid"}


@pytest.mark.parametrize("note", [None, [], {}])
def test_gitlab_missing_note_payload_reports_incomplete_history(note):
    def handler(request):
        return httpx.Response(200, json=[{**gitlab_note_event(401), "note": note}] if request.url.path.endswith("/events") else [])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab("token", "https://gitlab.example.invalid", [(7, "org/repo")], START, END, client=client, verified_emails={})

    result = run(collect())
    assert result.events == []
    assert result.incomplete and result.gaps == {"response_invalid"}


def test_gitlab_system_notes_assignments_and_other_note_targets_are_not_work():
    rows = [
        gitlab_note_event(401, system=True),
        gitlab_note_event(402, noteable_type="Snippet"),
        {**gitlab_note_event(403), "action_name": "assigned"},
    ]

    def handler(request):
        return httpx.Response(200, json=rows if request.url.path.endswith("/events") else [])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab("token", "https://gitlab.example.invalid", [(7, "org/repo")], START, END, client=client, verified_emails={})

    result = run(collect())
    assert not result.incomplete and result.events == []


def test_gitlab_reads_verified_email_commit_mr_review_and_issue_comment():
    requested = []

    def handler(request):
        assert request.url.host == "gitlab.example.invalid"
        assert request.headers["private-token"] == "fixture-user-token"
        requested.append(request.url.path)
        project = request.url.path.split("/")[5]
        repo = f"org-{project}/repo"
        if request.url.path.endswith("/repository/commits"):
            return httpx.Response(
                200,
                json=[
                    {"id": f"sha-{project}", "author_email": "ALIAS@example.invalid", "committed_date": TIME},
                    {"id": f"unknown-{project}", "author_email": "other@example.invalid", "committed_date": TIME},
                ],
            )
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": int(project),
                        "author": {"id": 42, "username": "renamed"},
                        "created_at": TIME,
                        "web_url": f"https://gitlab.example.invalid/instance/{repo}/-/merge_requests/2",
                    }
                ],
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=[
                    {"id": 400, "author": {"id": 42}, "target_type": "MergeRequest", "target_iid": 2, "action_name": "approved", "created_at": TIME},
                    gitlab_note_event(401),
                    {"id": 402, "author": {"id": 42}, "target_type": "Issue", "target_iid": 4, "action_name": "assigned", "created_at": TIME},
                ],
            )
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab(
                "fixture-user-token",
                "https://gitlab.example.invalid/instance",
                [(7, "org-7/repo"), (8, "org-8/repo")],
                START,
                END,
                client=client,
                verified_emails={"alias@example.invalid": "42"},
            )

    result = run(collect())
    assert result.incomplete is True
    assert [item.kind for item in result.events] == ["commit", "pull_request", "review", "comment"] * 2
    assert len(classify_events(result.events, {"gitlab": {"42"}})) == 8
    assert len(requested) == 6
    assert all(item.actor_id == "42" and "fixture-user-token" not in item.source_url for item in result.events)


def test_github_exhausts_empty_continuation_and_reports_page_limit():
    seen = []

    def handler(request):
        page = int(request.url.params["page"])
        seen.append((request.url.path, page))
        if request.url.path.endswith("/commits"):
            rows = [gh_event("commit", f"sha-{page}", "org/repo")] if page == 2 else []
            return httpx.Response(200, json=rows, headers={"Link": '<https://api.github.com/next>; rel="next"'} if page < 3 else {})
        return httpx.Response(200, json=[])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_github("token", ["org/repo"], START, END, client=client, max_pages=2)

    result = run(collect())
    assert [event.event_id for event in result.events] == ["sha-2"]
    assert result.incomplete and result.gaps == {"page_limit"}
    assert seen[:2] == [("/repos/org/repo/commits", 1), ("/repos/org/repo/commits", 2)]
    assert all(page <= 2 for _, page in seen)


def test_gitlab_exhausts_empty_continuation_across_projects_with_shared_budget():
    seen = []

    def handler(request):
        page = int(request.url.params["page"])
        seen.append((request.url.path, page))
        if request.url.path.endswith("/repository/commits") and page == 1:
            return httpx.Response(200, json=[], headers={"X-Next-Page": "2"})
        if request.url.path.endswith("/repository/commits") and page == 2:
            return httpx.Response(200, json=[{"id": "sha", "author_email": "alias@example.invalid", "committed_date": TIME}])
        return httpx.Response(200, json=[])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab(
                "token",
                "https://gitlab.example.invalid",
                [(1, "one/repo"), (2, "two/repo")],
                START,
                END,
                client=client,
                verified_emails={"alias@example.invalid": "42"},
                budget=PageBudget(4),
            )

    result = run(collect())
    assert [event.kind for event in result.events] == ["commit"]
    assert result.incomplete and result.gaps == {"request_limit"}
    assert len(seen) == 4
    assert seen[:2] == [("/api/v4/projects/1/repository/commits", 1), ("/api/v4/projects/1/repository/commits", 2)]


@pytest.mark.parametrize(
    "status,headers",
    [
        (429, {"Retry-After": "60"}),
        (403, {"X-RateLimit-Remaining": "0", "Retry-After": "60"}),
        (403, {"X-RateLimit-Remaining": "0"}),
        (403, {"X-RateLimit-Remaining": "4999", "Retry-After": "60"}),
    ],
)
def test_github_rate_limit_after_page_preserves_events_and_continues_other_queries(status, headers):
    pages = []

    def handler(request):
        if request.url.path.endswith("/commits"):
            pages.append(request.url.params["page"])
            if request.url.params["page"] == "2":
                return httpx.Response(status, headers=headers)
            return httpx.Response(200, json=[gh_event("commit", "sha", "org/repo")], headers={"Link": '<https://api.github.com/next>; rel="next"'})
        if request.url.path.endswith("/issues/comments"):
            return httpx.Response(200, json=[gh_event("comment", 23, "org/repo")])
        return httpx.Response(200, json=[])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_github("token", ["org/repo"], START, END, client=client)

    result = run(collect())
    assert [event.kind for event in result.events] == ["commit", "comment"]
    assert result.incomplete and result.gaps == {"rate_limited"}
    assert pages == ["1", "2"]


@pytest.mark.parametrize("headers", [{}, {"X-RateLimit-Remaining": "4999"}, {"Retry-After": ""}])
def test_github_permission_denial_does_not_return_collected_activity_as_rate_limited(headers):
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[gh_event("commit", "sha", "org/repo")], headers={"Link": '<https://api.github.com/next>; rel="next"'})
        return httpx.Response(403, headers=headers)

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_github("token", ["org/repo"], START, END, client=client)

    with pytest.raises(httpx.HTTPStatusError) as error:
        run(collect())
    assert error.value.response.status_code == 403


def test_gitlab_forbidden_does_not_use_github_rate_limit_indicators():
    def handler(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "Retry-After": "60"})

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab("token", "https://gitlab.example.invalid", [(7, "org/repo")], START, END, client=client, verified_emails={})

    with pytest.raises(httpx.HTTPStatusError) as error:
        run(collect())
    assert error.value.response.status_code == 403


def test_gitlab_rate_limit_after_empty_page_retains_authorized_other_events():
    def handler(request):
        if request.url.path.endswith("/repository/commits"):
            if request.url.params["page"] == "2":
                return httpx.Response(429)
            return httpx.Response(200, json=[], headers={"X-Next-Page": "2"})
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 12,
                        "author": {"id": 42},
                        "target_type": "Issue",
                        "target_iid": 4,
                        "action_name": "commented on",
                        "created_at": TIME,
                    }
                ],
            )
        return httpx.Response(200, json=[])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab(
                "token",
                "https://gitlab.example.invalid",
                [(1, "org/repo")],
                START,
                END,
                client=client,
                verified_emails={},
            )

    result = run(collect())
    assert [event.kind for event in result.events] == ["comment"]
    assert result.incomplete and result.gaps == {"rate_limited"}

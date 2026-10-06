"""EXT03-t3: unsafe provider destinations must not expose scoped credentials."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import HTTPException

from src.activity.external_provider import read_github, read_gitlab

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 4, tzinfo=UTC)
TIME = "2026-10-02T11:00:00Z"
SECRET = "fixture-provider-secret"


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.example.invalid",
        "https://user:pass@gitlab.example.invalid",
        "https://gitlab.example.invalid/%2e%2e",
        "https://gitlab.example.invalid/../escape",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://localhost",
    ],
)
def test_gitlab_refuses_unsafe_provider_destinations_before_sending_token(url):
    observed = []

    def handler(request):
        observed.append(request)
        raise AssertionError("Unsafe provider destination was contacted")

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab(SECRET, url, [(1, "org/repo")], START, END, client=client, verified_emails={})

    with pytest.raises((HTTPException, ValueError)):
        run(collect())
    assert not observed


@pytest.mark.parametrize("repo", ["org/repo/../../escape", "org/%2e%2e", "org/../escape"])
def test_gitlab_refuses_unsafe_project_path(repo):
    observed = []

    def handler(request):
        observed.append(request)
        raise AssertionError("Unsafe project path was contacted")

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_gitlab(SECRET, "https://gitlab.example.invalid", [(1, repo)], START, END, client=client, verified_emails={})

    with pytest.raises(ValueError):
        run(collect())
    assert not observed


def test_github_refuses_redirect_and_untrusted_event_links_without_leaking_secret():
    observed = []

    def handler(request):
        observed.append(request.url.path)
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        if request.url.path.endswith("/commits"):
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "safe-sha",
                        "author": {"id": 17},
                        "commit": {"committer": {"date": TIME}},
                        "html_url": "https://github.com/org/repo/commit/safe-sha",
                    },
                    {
                        "sha": "evil",
                        "author": {"id": 17},
                        "commit": {"committer": {"date": TIME}},
                        "html_url": f"https://evil.example.invalid/leak?token={SECRET}",
                    },
                    {
                        "sha": "spoofed",
                        "author": {"id": 17},
                        "commit": {"committer": {"date": TIME}},
                        "html_url": f"https://github.com/org/repo/commit/spoofed?token={SECRET}",
                    },
                    {
                        "sha": SECRET,
                        "author": {"id": 17},
                        "commit": {"committer": {"date": TIME}},
                        "html_url": "https://github.com/org/repo/commit/safe-sha",
                    },
                ],
            )
        return httpx.Response(302, headers={"Location": f"https://evil.example.invalid/?token={SECRET}"})

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await read_github(SECRET, ["org/repo"], START, END, client=client)

    result = run(collect())
    assert [event.event_id for event in result.events] == ["safe-sha"]
    assert result.incomplete and {"source_invalid", "redirect_refused"} <= result.gaps
    assert SECRET not in repr(result)
    assert observed == ["/repos/org/repo/commits", "/repos/org/repo/pulls", "/repos/org/repo/issues/comments"]


def test_gitlab_refuses_redirect_and_untrusted_mr_link_without_leaking_secret():
    observed = []

    def handler(request):
        observed.append(str(request.url))
        assert request.url.host == "gitlab.example.invalid"
        assert request.headers["PRIVATE-TOKEN"] == SECRET
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 17,
                        "author": {"id": 42},
                        "created_at": TIME,
                        "web_url": "https://gitlab.example.invalid/instance/org/repo/-/merge_requests/17",
                    },
                    {
                        "id": 18,
                        "author": {"id": 42},
                        "created_at": TIME,
                        "web_url": f"https://gitlab.example.invalid/instance/org/repo/-/merge_requests/18#{SECRET}",
                    },
                    {
                        "id": SECRET,
                        "author": {"id": 42},
                        "created_at": TIME,
                        "web_url": "https://gitlab.example.invalid/instance/org/repo/-/merge_requests/19",
                    },
                ],
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(302, headers={"Location": "https://evil.example.invalid"})
        return httpx.Response(200, json=[])

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await read_gitlab(
                SECRET,
                "https://gitlab.example.invalid/instance",
                [(1, "org/repo")],
                START,
                END,
                client=client,
                verified_emails={},
            )

    result = run(collect())
    assert [event.event_id for event in result.events] == ["17"]
    assert result.incomplete and {"source_invalid", "redirect_refused"} <= result.gaps
    assert SECRET not in repr(result)
    assert len(observed) == 3

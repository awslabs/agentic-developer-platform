"""Bounded, read-only provider collection for repositories authorized by the caller."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.parse import quote, urlsplit

import httpx

from src.activity.external_events import ProviderEvent
from src.admin.connections.github_client import github_account_id
from src.gitlab.service import host as approved_gitlab_host

GITHUB_API = "https://api.github.com"
PAGE_SIZE = 100
MAX_PAGES = 3


def safe_gitlab_host(value: str) -> str:
    base_url = approved_gitlab_host(value)
    hostname = urlsplit(base_url).hostname or ""
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname == "localhost.localdomain":
        raise ValueError("Unsafe provider destination")
    try:
        address = ip_address(hostname)
    except ValueError:
        pass
    else:
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise ValueError("Unsafe provider destination")
    return base_url


def _source_valid(url: object, repo: str, kind: str, base_url: str, token: str) -> bool:
    if not isinstance(url, str) or not url.isascii() or any(ord(char) < 33 for char in url):
        return False
    if "\\" in url or "%" in url or token in url:
        return False
    try:
        source, base = urlsplit(url), urlsplit(base_url)
        if source.port != base.port or source.netloc != base.netloc or source.scheme != "https" or source.query:
            return False
    except ValueError:
        return False
    prefix = f"{base.path.rstrip('/')}/{repo}/"
    if not source.path.startswith(prefix):
        return False
    suffix = source.path[len(prefix) :]
    if base.netloc == "github.com":
        patterns = {
            "commit": r"commit/[A-Za-z0-9._-]+",
            "pull_request": r"pull/[0-9]+",
            "review": r"pull/[0-9]+",
            "comment": r"(?:issues|pull)/[0-9]+",
        }
        fragments = {
            "commit": "",
            "pull_request": "",
            "review": r"pullrequestreview-[0-9]+",
            "comment": r"issuecomment-[0-9]+",
        }
    else:
        patterns = {
            "commit": r"-/commit/[A-Za-z0-9._-]+",
            "pull_request": r"-/merge_requests/[0-9]+",
            "review": r"-/merge_requests/[0-9]+",
            "comment": r"-/(?:issues|merge_requests)/[0-9]+",
        }
        fragments = {"commit": "", "pull_request": "", "review": "", "comment": r"(?:note_[0-9]+)?"}
    return bool(re.fullmatch(patterns[kind], suffix) and re.fullmatch(fragments[kind], source.fragment))


@dataclass
class ProviderRead:
    events: list[ProviderEvent]
    incomplete: bool = False
    gaps: set[str] = field(default_factory=set)


@dataclass
class PageBudget:
    remaining: int = 50


def _instant(value: str | None, start: datetime, end: datetime | None) -> bool:
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and start <= stamp and (end is None or stamp < end)
    except (TypeError, ValueError):
        return False


def _window(start: datetime, end: datetime) -> tuple[str, str]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("An ordered, timezone-aware window is required")
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


async def _pages(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    params: dict,
    *,
    provider: str,
    page_size: int,
    max_pages: int,
    budget: PageBudget,
) -> tuple[list[dict], bool, str | None]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        if budget.remaining <= 0:
            return rows, True, "request_limit"
        budget.remaining -= 1
        headers = {"Authorization": f"Bearer {token}"} if provider == "github" else {"PRIVATE-TOKEN": token}
        try:
            response = await client.get(url, params={**params, "per_page": page_size, "page": page}, headers=headers, follow_redirects=False)
        except httpx.RequestError:
            return rows, True, "provider_unavailable"
        if 300 <= response.status_code < 400:
            return rows, True, "redirect_refused"
        if response.status_code == 429 or (
            provider == "github"
            and response.status_code == 403
            and (response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("retry-after"))
        ):
            return rows, True, "rate_limited"
        if response.status_code >= 500:
            return rows, True, "provider_unavailable"
        response.raise_for_status()
        try:
            batch = response.json()
        except ValueError:
            return rows, True, "response_invalid"
        if not isinstance(batch, list):
            return rows, True, "response_invalid"
        rows.extend(batch)
        if provider == "github":
            following = 'rel="next"' in response.headers.get("link", "")
        else:
            next_page = response.headers.get("x-next-page", "")
            if next_page and (not next_page.isascii() or not next_page.isdigit() or int(next_page) != page + 1):
                return rows, True, "pagination_invalid"
            following = bool(next_page)
        if not following:
            return rows, False, None
    return rows, True, "page_limit"


def _github_event(row: dict, *, kind: str, repo: str, timestamp: str, source: str, start: datetime, end: datetime):
    actor = row.get("author") if kind == "commit" else row.get("user")
    actor = actor or {}
    actor_id = github_account_id(actor.get("id"))
    when = row.get(timestamp)
    url = row.get(source)
    event_id = row.get("sha") if kind == "commit" else row.get("id")
    if not actor_id or not event_id or not isinstance(url, str) or not _instant(when, start, end):
        return None
    return ProviderEvent(
        provider="github",
        kind=kind,
        event_id=str(event_id),
        source_url=url,
        repository=repo,
        actor_id=actor_id,
        actor_kind="bot" if actor.get("type") == "Bot" else "human",
        occurred_at=when,
    )


async def read_github(
    token: str,
    repositories: list[str],
    start: datetime,
    end: datetime,
    *,
    client: httpx.AsyncClient,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    budget: PageBudget | None = None,
) -> ProviderRead:
    """Read only commits, PRs, PR reviews and issue comments from supplied repositories."""
    since, until = _window(start, end)
    if not token or not 1 <= page_size <= 100 or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError("Invalid provider read bound")
    result = ProviderRead(events=[])
    budget = budget or PageBudget()
    for repo in repositories:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(part in {".", ".."} for part in repo.split("/")):
            raise ValueError("Invalid repository")
        prefix = f"{GITHUB_API}/repos/{repo}"
        for path, params, kind, timestamp, source in (
            ("/commits", {"since": since, "until": until}, "commit", "commit.committer.date", "html_url"),
            ("/pulls", {"state": "all", "sort": "updated", "direction": "desc"}, "pull_request", "created_at", "html_url"),
            ("/issues/comments", {"since": since}, "comment", "created_at", "html_url"),
        ):
            rows, incomplete, gap = await _pages(
                client,
                prefix + path,
                token,
                params,
                provider="github",
                page_size=page_size,
                max_pages=max_pages,
                budget=budget,
            )
            result.incomplete |= incomplete
            if gap:
                result.gaps.add(gap)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if kind == "commit":
                    row = {**row, "commit.committer.date": ((row.get("commit") or {}).get("committer") or {}).get("date")}
                valid_source = _source_valid(row.get(source), repo, kind, "https://github.com", token)
                if valid_source and token not in str(row.get("id")) and token not in str(row.get("sha")):
                    item = _github_event(row, kind=kind, repo=repo, timestamp=timestamp, source=source, start=start, end=end)
                    if item:
                        result.events.append(item)
                else:
                    result.incomplete = True
                    result.gaps.add("source_invalid")
                if kind == "pull_request" and isinstance(row.get("number"), int) and _instant(row.get("updated_at"), start, None):
                    reviews, truncated, gap = await _pages(
                        client,
                        f"{prefix}/pulls/{row['number']}/reviews",
                        token,
                        {},
                        provider="github",
                        page_size=page_size,
                        max_pages=max_pages,
                        budget=budget,
                    )
                    result.incomplete |= truncated
                    if gap:
                        result.gaps.add(gap)
                    for review in reviews:
                        if isinstance(review, dict):
                            valid_source = _source_valid(review.get("html_url"), repo, "review", "https://github.com", token)
                            if valid_source and token not in str(review.get("id")):
                                item = _github_event(
                                    review,
                                    kind="review",
                                    repo=repo,
                                    timestamp="submitted_at",
                                    source="html_url",
                                    start=start,
                                    end=end,
                                )
                                if item:
                                    result.events.append(item)
                            else:
                                result.incomplete = True
                                result.gaps.add("source_invalid")
    return result


def _gitlab_event(row: dict, *, kind: str, repo: str, timestamp: str, source: str, start: datetime, end: datetime):
    actor = row.get("author") or {}
    actor_id = actor.get("id")
    when = row.get(timestamp)
    url = row.get(source)
    event_id = row.get("id")
    if not isinstance(actor_id, int) or actor_id <= 0 or not event_id or not isinstance(url, str) or not _instant(when, start, end):
        return None
    return ProviderEvent(
        provider="gitlab",
        kind=kind,
        event_id=str(event_id),
        source_url=url,
        repository=repo,
        actor_id=str(actor_id),
        actor_kind="bot" if actor.get("bot") is True else "human",
        occurred_at=when,
    )


async def read_gitlab(
    token: str,
    base_url: str,
    projects: list[tuple[int, str]],
    start: datetime,
    end: datetime,
    *,
    client: httpx.AsyncClient,
    verified_emails: Mapping[str, str],
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    budget: PageBudget | None = None,
) -> ProviderRead:
    """Read repository commits, MRs and historical issue/MR discussion events."""
    base_url = safe_gitlab_host(base_url)
    since, until = _window(start, end)
    if not token or not 1 <= page_size <= 100 or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError("Invalid provider read bound")
    result = ProviderRead(events=[])
    budget = budget or PageBudget()
    for project_id, repo in projects:
        if type(project_id) is not int or project_id <= 0:
            raise ValueError("Invalid project ID")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", repo) or any(part in {".", ".."} for part in repo.split("/")):
            raise ValueError("Invalid project repository")
        prefix = f"{base_url}/api/v4/projects/{project_id}"
        commits, incomplete, gap = await _pages(
            client,
            prefix + "/repository/commits",
            token,
            {"since": since, "until": until},
            provider="gitlab",
            page_size=page_size,
            max_pages=max_pages,
            budget=budget,
        )
        result.incomplete |= incomplete
        if gap:
            result.gaps.add(gap)
        for row in commits:
            if not isinstance(row, dict):
                continue
            author_id = verified_emails.get(str(row.get("author_email", "")).casefold())
            if not author_id:
                result.incomplete = True
                continue
            commit_url = f"{base_url}/{quote(repo, safe='/')}/-/commit/{quote(str(row.get('id')), safe='')}"
            if _source_valid(commit_url, repo, "commit", base_url, token):
                item = _gitlab_event(
                    {**row, "author": {"id": int(author_id)}, "web_url": commit_url},
                    kind="commit",
                    repo=repo,
                    timestamp="committed_date",
                    source="web_url",
                    start=start,
                    end=end,
                )
                if item:
                    result.events.append(item)
            else:
                result.incomplete = True
                result.gaps.add("source_invalid")
        requests, incomplete, gap = await _pages(
            client,
            prefix + "/merge_requests",
            token,
            {"state": "all", "order_by": "updated_at", "sort": "desc", "updated_after": since},
            provider="gitlab",
            page_size=page_size,
            max_pages=max_pages,
            budget=budget,
        )
        result.incomplete |= incomplete
        if gap:
            result.gaps.add(gap)
        for row in requests:
            if isinstance(row, dict):
                if _source_valid(row.get("web_url"), repo, "pull_request", base_url, token) and token not in str(row.get("id")):
                    item = _gitlab_event(row, kind="pull_request", repo=repo, timestamp="created_at", source="web_url", start=start, end=end)
                    if item:
                        result.events.append(item)
                else:
                    result.incomplete = True
                    result.gaps.add("source_invalid")
        events, incomplete, gap = await _pages(
            client,
            prefix + "/events",
            token,
            {"after": start.date().isoformat(), "before": end.date().isoformat()},
            provider="gitlab",
            page_size=page_size,
            max_pages=max_pages,
            budget=budget,
        )
        result.incomplete |= incomplete
        if gap:
            result.gaps.add(gap)
        for row in events:
            if not isinstance(row, dict):
                continue
            action = row.get("action_name")
            target_type = row.get("target_type")
            iid = row.get("target_iid")
            note_id = None
            if target_type == "Note" and action == "commented on":
                note = row.get("note")
                if not isinstance(note, dict) or not isinstance(note.get("noteable_type"), str) or type(note.get("system")) is not bool:
                    result.incomplete = True
                    result.gaps.add("response_invalid")
                    continue
                if note["system"] or note["noteable_type"] not in {"Issue", "MergeRequest"}:
                    continue
                target_type = note["noteable_type"]
                iid = note.get("noteable_iid")
                note_id = note.get("id")
                if type(iid) is not int or iid <= 0 or type(note_id) is not int or note_id <= 0:
                    result.incomplete = True
                    result.gaps.add("response_invalid")
                    continue
            if target_type not in {"Issue", "MergeRequest"}:
                continue
            kind = "review" if action == "approved" and target_type == "MergeRequest" else "comment" if action == "commented on" else None
            if not kind or type(iid) is not int or iid <= 0:
                continue
            issue_kind = "merge_requests" if target_type == "MergeRequest" else "issues"
            source_url = f"{base_url}/{quote(repo, safe='/')}/-/{issue_kind}/{iid}"
            if note_id is not None:
                source_url += f"#note_{note_id}"
            item = _gitlab_event(
                {**row, "web_url": source_url},
                kind=kind,
                repo=repo,
                timestamp="created_at",
                source="web_url",
                start=start,
                end=end,
            )
            if item and _source_valid(item.source_url, repo, kind, base_url, token) and token not in item.event_id:
                result.events.append(item)
            elif item:
                result.incomplete = True
                result.gaps.add("source_invalid")
    return result

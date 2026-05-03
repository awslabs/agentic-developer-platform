"""GitHub Checks API helpers for surfacing agent pod progress.

Creates a Check Run at pod start (status=in_progress) and finalises it at pod
exit (status=completed, conclusion=success|failure).  All functions are
best-effort: the caller should catch exceptions so that Check Run failures
never fail the pod.

Uses `requests` (already in the image; see #413).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
_HEADERS_TEMPLATE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _auth_headers(token: str) -> dict[str, str]:
    return {**_HEADERS_TEMPLATE, "Authorization": f"Bearer {token}"}


def create_check_run(
    repo: str,
    head_sha: str,
    persona: str,
    issue: int,
    token: str,
) -> dict[str, Any]:
    """Create a GitHub Check Run in status=in_progress.

    Args:
        repo: Full repo name, e.g. ``"acme-corp/flagship-app"``.
        head_sha: The commit SHA to attach the check to (default-branch HEAD).
        persona: Agent persona name, e.g. ``"developer"``.
        issue: Issue number the agent is working on.
        token: GitHub installation access token with ``checks:write`` scope.

    Returns:
        Dict with at least ``id`` (int) and ``html_url`` (str).

    Raises:
        RuntimeError: If the API call returns a non-201 status.
    """
    url = f"{GITHUB_API_URL}/repos/{repo}/check-runs"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    issue_url = f"https://github.com/{repo}/issues/{issue}"

    payload: dict[str, Any] = {
        "name": f"ADP Agent: {persona}",
        "head_sha": head_sha,
        "status": "in_progress",
        "started_at": now,
        "details_url": issue_url,
        "output": {
            "title": f"Agent {persona} is working on #{issue}",
            "summary": f"Agent `{persona}` started processing issue #{issue}.",
            "text": "",
        },
    }

    logger.info("Creating check run for %s sha=%s persona=%s issue=#%s", repo, head_sha[:7], persona, issue)
    resp = requests.post(url, headers=_auth_headers(token), json=payload, timeout=30)

    if resp.status_code != 201:
        raise RuntimeError(
            f"Failed to create check run: {resp.status_code} {resp.text}"
        )

    data: dict[str, Any] = resp.json()
    logger.info("Check run created: id=%s url=%s", data.get("id"), data.get("html_url"))
    return {"id": data["id"], "html_url": data["html_url"]}


def update_check_run(
    repo: str,
    check_run_id: int,
    token: str,
    *,
    status: str | None = None,
    conclusion: str | None = None,
    output: dict[str, str] | None = None,
) -> None:
    """Update an existing Check Run.

    Args:
        repo: Full repo name, e.g. ``"acme-corp/flagship-app"``.
        check_run_id: The integer ID returned by :func:`create_check_run`.
        token: GitHub installation access token with ``checks:write`` scope.
        status: New status (``"in_progress"`` or ``"completed"``).
        conclusion: Required when ``status="completed"`` — one of ``"success"``,
            ``"failure"``, ``"neutral"``, ``"cancelled"``, ``"skipped"``,
            ``"timed_out"``, ``"action_required"``.
        output: Optional dict with ``title`` and ``summary`` keys.

    Raises:
        RuntimeError: If the API call returns a non-200 status.
    """
    url = f"{GITHUB_API_URL}/repos/{repo}/check-runs/{check_run_id}"
    payload: dict[str, Any] = {}

    if status is not None:
        payload["status"] = status
    if conclusion is not None:
        payload["conclusion"] = conclusion
    if status == "completed":
        payload["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if output is not None:
        payload["output"] = output

    logger.info(
        "Updating check run %s: status=%s conclusion=%s",
        check_run_id,
        status,
        conclusion,
    )
    resp = requests.patch(url, headers=_auth_headers(token), json=payload, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to update check run {check_run_id}: {resp.status_code} {resp.text}"
        )

    logger.info("Check run %s updated successfully", check_run_id)

#!/usr/bin/env python3
"""Derive per-repo ACLs at ingestion time (#5658).

The gap this closes
-------------------
``ingest-repo.py`` stamped ``allowed_principals = ["*"]`` on every repository it
ingested — public sentinel, unconditionally, private repos included. Combined with
``db.ensure_repo_exists``'s ``ON CONFLICT`` (which only overwrites when the existing
value is ``'[]'`` or NULL), that meant a private repo was published readable to
every principal and re-ingestion could never tighten it.

``door/acl.py`` already had ``derive_acl_from_github``, which asks GitHub for the
repo's visibility and enumerates the collaborators and teams with push+ access. It
had **zero production callers**, because ``door/`` is not in the ingestion image —
the logic existed and was simply never reachable from the code that does the
stamping. So this module makes it reachable rather than reimplementing it.

Why the function below is a verbatim copy
-----------------------------------------
Same constraint as ``url_denylist.py``: separate Docker build contexts, no
cross-module imports at runtime. ``tests/unit/test_repo_acl_vendoring.py`` pins it
AST-identical to ``door/acl.py``, so the Door's view of who may read a repo and
ingestion's view of who may read it cannot drift apart. Two components disagreeing
about the same ACL is worse than either being wrong consistently: the Door would
filter on one rule while ingestion stamped another.

Fail-closed
-----------
``derive_acl_from_github`` returns ``[]`` when the API cannot answer. An empty list
denies everyone, which is the correct direction: an unknown ACL is not permission.
``resolve_allowed_principals`` keeps that property and refuses to fall back to the
public sentinel on any error path.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("repo-acl")

# Must match door/acl.py. The Door compares against this exact value.
PUBLIC_SENTINEL = "*"


def derive_acl_from_github(
    repo_full_name: str,
    github_token: str,
    *,
    request_timeout: int = 30,
) -> list[str]:
    """Derive the allowed_principals list for a repo from the GitHub API.

    For public repos: returns ["*"].
    For private/internal repos: returns user logins + team slugs with push+ access.

    Parameters
    ----------
    repo_full_name:
        Full repo name, e.g. "aws-e/adp".
    github_token:
        GitHub App installation token with repo + read:org scope.
    request_timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    List of principal strings. Empty list means no one can see the repo
    (safe default on API failure for new repos).

    Raises
    ------
    Does NOT raise on API failure — returns [] for new repos (fail-closed)
    or preserves caller's previous value via the upsert logic.
    """
    import requests as http_requests

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = "https://api.github.com"
    owner = repo_full_name.split("/")[0] if "/" in repo_full_name else ""

    # Step 1: Check repo visibility
    try:
        resp = http_requests.get(
            f"{base_url}/repos/{repo_full_name}",
            headers=headers,
            timeout=request_timeout,
        )
        if resp.status_code != 200:
            log.warning(
                "derive_acl: GET /repos/%s returned %d — cannot derive ACL",
                repo_full_name,
                resp.status_code,
            )
            return []

        repo_data = resp.json()
        visibility = repo_data.get("visibility", "private")

        if visibility == "public":
            return [PUBLIC_SENTINEL]

    except Exception as e:
        log.warning("derive_acl: failed to check repo visibility for %s: %s", repo_full_name, e)
        return []

    # Step 2: Enumerate collaborators (direct, with push+ access)
    principals: list[str] = []
    try:
        page = 1
        while True:
            resp = http_requests.get(
                f"{base_url}/repos/{repo_full_name}/collaborators",
                headers=headers,
                params={"affiliation": "direct", "per_page": 100, "page": page},
                timeout=request_timeout,
            )
            if resp.status_code == 403:
                log.warning(
                    "derive_acl: 403 on collaborators for %s (missing admin:read scope?)",
                    repo_full_name,
                )
                break
            if resp.status_code != 200:
                log.warning(
                    "derive_acl: collaborators returned %d for %s",
                    resp.status_code,
                    repo_full_name,
                )
                break

            collabs = resp.json()
            if not collabs:
                break

            for c in collabs:
                permissions = c.get("permissions", {})
                if permissions.get("push") or permissions.get("admin"):
                    login = c.get("login", "").lower()
                    if login:
                        principals.append(login)

            # Check for next page
            if len(collabs) < 100:
                break
            page += 1

    except Exception as e:
        log.warning("derive_acl: collaborators fetch failed for %s: %s", repo_full_name, e)

    # Step 3: Enumerate teams (with push/maintain/admin access)
    try:
        page = 1
        while True:
            resp = http_requests.get(
                f"{base_url}/repos/{repo_full_name}/teams",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=request_timeout,
            )
            if resp.status_code == 403:
                log.warning(
                    "derive_acl: 403 on teams for %s (missing read:org scope?)",
                    repo_full_name,
                )
                break
            if resp.status_code != 200:
                log.warning(
                    "derive_acl: teams returned %d for %s",
                    resp.status_code,
                    repo_full_name,
                )
                break

            teams = resp.json()
            if not teams:
                break

            for t in teams:
                perm = t.get("permission", "")
                if perm in ("push", "maintain", "admin"):
                    slug = t.get("slug", "")
                    if slug and owner:
                        principals.append(f"{owner}/{slug}".lower())

            if len(teams) < 100:
                break
            page += 1

    except Exception as e:
        log.warning("derive_acl: teams fetch failed for %s: %s", repo_full_name, e)

    return principals


def resolve_allowed_principals(
    org_repo: str,
    *,
    token: str | None = None,
    token_path: str = "/tmp/github-token",
) -> list[str]:
    """Return the principals allowed to read ``org_repo``, failing closed.

    Replaces the unconditional ``["*"]`` at the stamping site. The contract is
    deliberately blunt: this function NEVER returns the public sentinel unless
    GitHub affirmatively reported the repository as public.

    With no token available we cannot distinguish public from private, so we return
    ``[]`` (deny) rather than ``["*"]`` (publish). That is a visible failure — the
    repo's content is stored but not readable — which is recoverable. The other
    direction silently publishes a private repo, which is not.
    """
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or _read_token_file(token_path)

    if not token:
        log.warning(
            "No GitHub token available to derive an ACL for %s — denying by default. "
            "The repo is ingested but not readable until its ACL is derived.",
            org_repo,
        )
        return []

    principals = derive_acl_from_github(org_repo, token)

    if not principals:
        log.warning(
            "Derived an empty ACL for %s — the repo will not be readable. This is the "
            "fail-closed path (GitHub API unavailable, or no collaborators with push "
            "access), not a public repo.",
            org_repo,
        )

    return principals


def _read_token_file(path: str) -> str:
    """Read the minted installation token from disk.

    ``github_auth.mint_github_token`` writes it to /tmp/github-token for the git
    credential helper. Never logged, and the value is not echoed on failure.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""

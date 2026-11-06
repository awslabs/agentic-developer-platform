"""GitHub ACL derivation vendored from ingestion/repo_acl.py.

Only the trusted gateway resolves ACLs. Content workers cannot supply them.
The parity test prevents the gateway and Door ACL rules from drifting.
"""

import logging

log = logging.getLogger(__name__)
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

"""
Allowlist check for GitHub users — reuses org-membership logic from pre-signup Lambda.

Issue #520: Lambda broker for GitHub sign-in.
"""

import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def check_org_membership(github_login: str, allowed_orgs: list[str], github_token: str) -> bool:
    """
    Check if a GitHub user is a member of at least one allowed organization.

    Mirrors the logic in modules/gateway/lambda/pre-signup/handler.py.

    Args:
        github_login: GitHub username to check.
        allowed_orgs: List of org names the user must belong to.
        github_token: GitHub API token with read:org scope.

    Returns:
        True if the user belongs to at least one allowed org.
    """
    if not allowed_orgs:
        logger.info("No allowed_orgs configured; skipping org check")
        return True

    if not github_token:
        logger.error("No GitHub token available; cannot verify org membership")
        return False

    for org in allowed_orgs:
        if _is_org_member(org, github_login, github_token):
            logger.info("User %s is a member of org %s", github_login, org)
            return True

    logger.warning("User %s is not a member of any allowed org", github_login)
    return False


def _is_org_member(org: str, username: str, token: str) -> bool:
    """Check membership in a single GitHub organization."""
    url = f"https://api.github.com/orgs/{org}/members/{username}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status == 204
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return True
        if e.code == 404:
            return False
        if e.code == 302:
            logger.warning("GitHub 302 for org %s; token may lack org:read scope", org)
            return False
        logger.error("GitHub API error for org %s: %s %s", org, e.code, e.reason)
        return False
    except (urllib.error.URLError, OSError) as e:
        logger.error("Network error checking org %s membership: %s", org, e)
        return False

"""
Allowlist check for GitHub users — reuses org-membership logic from pre-signup Lambda.

Issue #520: Lambda broker for GitHub sign-in.
Issue #3986: fail closed. An empty ``allowed_orgs`` used to return True
(allow-everyone), inverting the pre-signup helper's behaviour for the same
input. It now denies, and callers can tell "not a member" apart from "could not
verify" so a misconfigured org token does not look like a legitimate denial.
"""

import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Result of an org-membership check.
ALLOWED = "allowed"
DENIED = "denied"
# The check could not be completed (no token, bad scope, GitHub/network error).
# Callers must still deny, but should report it distinctly from DENIED.
UNVERIFIED = "unverified"


def check_org_membership(github_login: str, allowed_orgs: list[str], github_token: str) -> str:
    """
    Check if a GitHub user is a member of at least one allowed organization.

    Mirrors the logic in modules/gateway/lambda/pre-signup/handler.py, which
    denies on both an empty config and an empty parsed list.

    Args:
        github_login: GitHub username to check.
        allowed_orgs: List of org names the user must belong to.
        github_token: GitHub API token with read:org scope.

    Returns:
        ALLOWED if the user belongs to at least one allowed org, DENIED if they
        provably belong to none, UNVERIFIED if membership could not be checked.
    """
    orgs = [org.strip() for org in allowed_orgs if org and org.strip()]
    if not orgs:
        logger.error("No allowed_orgs configured; denying (org mode requires at least one org)")
        return UNVERIFIED

    if not github_token:
        logger.error("No GitHub token available; cannot verify org membership")
        return UNVERIFIED

    unverified_orgs: list[str] = []
    for org in orgs:
        member = _is_org_member(org, github_login, github_token)
        if member is True:
            logger.info("User %s is a member of org %s", github_login, org)
            return ALLOWED
        if member is None:
            unverified_orgs.append(org)

    if unverified_orgs:
        logger.error(
            "Could not verify membership for user %s in org(s) %s; denying",
            github_login,
            ",".join(unverified_orgs),
        )
        return UNVERIFIED

    logger.warning("User %s is not a member of any allowed org", github_login)
    return DENIED


def _is_org_member(org: str, username: str, token: str) -> bool | None:
    """Check membership in a single GitHub organization.

    Returns True (member), False (provably not a member), or None when the
    check could not be completed.
    """
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
            return None
        logger.error("GitHub API error for org %s: %s %s", org, e.code, e.reason)
        return None
    except (urllib.error.URLError, OSError) as e:
        logger.error("Network error checking org %s membership: %s", org, e)
        return None

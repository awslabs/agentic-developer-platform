"""
GitHub OAuth helpers — code exchange and user info fetch.

Issue #520: Lambda broker for GitHub sign-in.
"""

import logging
from typing import TypedDict

import requests

logger = logging.getLogger(__name__)

GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_USER_EMAILS_URL = "https://api.github.com/user/emails"


class GitHubUser(TypedDict):
    id: int
    login: str
    email: str
    name: str
    avatar_url: str


def exchange_code_for_token(code: str, client_id: str, client_secret: str) -> str:
    """
    Exchange a GitHub OAuth authorization code for an access token.

    Args:
        code: Authorization code from GitHub callback.
        client_id: GitHub OAuth App client ID.
        client_secret: GitHub OAuth App client secret.

    Returns:
        GitHub access token string.

    Raises:
        ValueError: If the token exchange fails.
    """
    response = requests.post(
        GITHUB_TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        error_desc = data.get("error_description", data["error"])
        logger.error("GitHub token exchange error: %s", error_desc)
        raise ValueError(f"GitHub token exchange failed: {error_desc}")

    access_token = data.get("access_token")
    if not access_token:
        raise ValueError("No access_token in GitHub response")

    return access_token


def get_github_user(access_token: str) -> GitHubUser:
    """
    Fetch the authenticated user's profile from GitHub.

    Args:
        access_token: GitHub OAuth access token.

    Returns:
        GitHubUser dict with id, login, email, name, avatar_url.

    Raises:
        ValueError: If the user fetch fails.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    response = requests.get(GITHUB_USER_URL, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    user_id = data.get("id")
    if not user_id:
        raise ValueError("GitHub /user response missing 'id'")

    email = data.get("email") or ""
    # If the user has their email set to private, /user returns email=null even
    # with user:email scope. Fall back to /user/emails (requires user:email
    # scope — which we request) and pick the primary verified address.
    if not email:
        try:
            email = _fetch_primary_email(headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch /user/emails fallback: %s", exc)

    return GitHubUser(
        id=int(user_id),
        login=data.get("login", ""),
        email=email,
        name=data.get("name") or data.get("login", ""),
        avatar_url=data.get("avatar_url", ""),
    )


def _fetch_primary_email(headers: dict) -> str:
    """Call /user/emails and return the primary verified email, or empty string.

    Returns empty when the scope is missing, the response shape is unexpected,
    or no verified primary is present. The caller decides how to treat an
    empty result (in our case: let the Cognito provisioner skip the email
    attribute rather than set email_verified=true without an email).
    """
    resp = requests.get(GITHUB_USER_EMAILS_URL, headers=headers, timeout=10)
    if resp.status_code != 200:
        logger.info("/user/emails returned %d — cannot recover email", resp.status_code)
        return ""
    payload = resp.json()
    if not isinstance(payload, list):
        return ""
    # Prefer primary + verified; fall back to any verified; else any entry.
    for entry in payload:
        if entry.get("primary") and entry.get("verified") and entry.get("email"):
            return entry["email"]
    for entry in payload:
        if entry.get("verified") and entry.get("email"):
            return entry["email"]
    for entry in payload:
        if entry.get("email"):
            return entry["email"]
    return ""

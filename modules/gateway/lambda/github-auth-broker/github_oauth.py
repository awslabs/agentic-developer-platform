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
    response = requests.get(
        GITHUB_USER_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()

    user_id = data.get("id")
    if not user_id:
        raise ValueError("GitHub /user response missing 'id'")

    return GitHubUser(
        id=int(user_id),
        login=data.get("login", ""),
        email=data.get("email") or "",
        name=data.get("name") or data.get("login", ""),
        avatar_url=data.get("avatar_url", ""),
    )

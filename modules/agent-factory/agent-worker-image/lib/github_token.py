"""GitHub App installation token minting.

Generates a JWT signed with the App private key, then exchanges it
for a short-lived installation access token via the GitHub API.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import jwt
import requests

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"


def generate_jwt(app_id: str, private_key: str) -> str:
    """Generate a JWT for GitHub App authentication.

    Args:
        app_id: The GitHub App ID.
        private_key: PEM-encoded RSA private key.

    Returns:
        Encoded JWT string (valid for 10 minutes).
    """
    now = int(time.time())
    payload = {
        "iat": now - 60,  # 60s clock skew allowance
        "exp": now + (10 * 60),  # 10 minute expiry (GitHub max)
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def mint_installation_token(app_id: str, private_key: str, installation_id: int) -> str:
    """Mint a GitHub App installation access token.

    Args:
        app_id: The GitHub App ID.
        private_key: PEM-encoded RSA private key.
        installation_id: The GitHub App installation ID.

    Returns:
        Installation access token string (valid for 1 hour).

    Raises:
        RuntimeError: If the token exchange fails.
    """
    token = generate_jwt(app_id, private_key)

    url = f"{GITHUB_API_URL}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    logger.info("Minting installation token for installation %s", installation_id)
    resp = requests.post(url, headers=headers, timeout=30)

    if resp.status_code != 201:
        raise RuntimeError(f"Failed to mint token: {resp.status_code} {resp.text}")

    data: dict[str, Any] = resp.json()
    logger.info("Token minted, expires at %s", data.get("expires_at", "unknown"))
    return data["token"]

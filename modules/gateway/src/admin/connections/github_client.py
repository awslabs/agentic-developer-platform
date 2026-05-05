"""Thin async wrapper around GitHub App REST API endpoints.

Issue #465: Used by the connections service to fetch/delete installation metadata.

JWT minting follows the GitHub App authentication spec:
  - RS256-signed JWT with iss=<app_id>, iat=now-60s, exp=now+600s
  - Exchanged for an installation access token or used directly for app-level endpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
_APP_JWT_EXPIRY_SECONDS = 600  # 10 min max per GitHub spec
_APP_JWT_BACKDATE_SECONDS = 60  # clock-skew buffer


def _mint_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Generate a short-lived RS256 JWT to authenticate as the GitHub App."""
    try:
        import jwt as pyjwt
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyJWT is required for GitHub App JWT minting") from exc

    now = int(time.time())
    payload = {
        "iat": now - _APP_JWT_BACKDATE_SECONDS,
        "exp": now + _APP_JWT_EXPIRY_SECONDS,
        "iss": app_id,
    }
    return pyjwt.encode(payload, private_key_pem, algorithm="RS256")


class GitHubAppClient:
    """Async client for GitHub App-level API calls.

    Args:
        app_id:          GitHub App numeric ID (string form).
        private_key_pem: RSA private key in PEM format.
        http_client:     Optional pre-built httpx.AsyncClient (injected in tests).
    """

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._http_client = http_client or httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10.0,
        )

    def _auth_headers(self) -> dict[str, str]:
        token = _mint_app_jwt(self._app_id, self._private_key_pem)
        return {"Authorization": f"Bearer {token}"}

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        """Fetch installation metadata from GitHub.

        Returns the raw GitHub API response dict, e.g.:
          {
            "id": 124731131,
            "account": {"type": "Organization", "login": "sophos-test", "id": 98765},
            "repository_selection": "selected",
            "repositories_url": "...",
            "installed_at": "2026-05-01T10:00:00Z",
            ...
          }
        """
        resp = await self._http_client.get(
            f"/app/installations/{installation_id}",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_installation(self, installation_id: int) -> None:
        """Revoke the GitHub App installation (removes access from the org/user)."""
        resp = await self._http_client.delete(
            f"/app/installations/{installation_id}",
            headers=self._auth_headers(),
        )
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    async def list_installation_repositories(self, installation_id: int) -> int:
        """Return the count of repositories accessible via this installation.

        Uses the installation's own access token (not the app JWT) so that
        the scope is limited to the installation's granted repos.

        Returns 0 on any error (repository count is informational only).
        """
        try:
            # First, get an installation access token
            resp = await self._http_client.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            token = resp.json().get("token", "")

            # List repositories for this installation
            repos_resp = await self._http_client.get(
                "/installation/repositories",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 1},  # we only need the total_count
            )
            repos_resp.raise_for_status()
            return repos_resp.json().get("total_count", 0)
        except Exception as exc:
            logger.warning("Could not fetch repository count for installation %d: %s", installation_id, exc)
            return 0

    async def aclose(self) -> None:  # pragma: no cover
        await self._http_client.aclose()

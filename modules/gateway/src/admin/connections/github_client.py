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

        Thin wrapper over list_installation_repository_names for callers that
        only need the count (kept for backward compatibility).
        """
        return len(await self.list_installation_repository_names(installation_id))

    async def list_installation_repository_names(self, installation_id: int) -> list[str]:
        """Return the ``full_name`` (owner/repo) of every repo this install can access.

        Uses the installation's own access token (not the app JWT) so the scope
        is limited to the installation's granted repos. Paginates so "all"-type
        installs with many repos are fully listed. Returns [] on any error
        (repository info is informational only — never fail the install for it).
        """
        try:
            # First, get an installation access token
            resp = await self._http_client.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            token = resp.json().get("token", "")

            names: list[str] = []
            page = 1
            while True:
                repos_resp = await self._http_client.get(
                    "/installation/repositories",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    params={"per_page": 100, "page": page},
                )
                repos_resp.raise_for_status()
                body = repos_resp.json()
                repos = body.get("repositories", []) or []
                names.extend(r.get("full_name") or r.get("name", "") for r in repos)
                # Stop when we've collected everything (total_count) or a short page.
                total = body.get("total_count")
                if not repos or (total is not None and len(names) >= total) or len(repos) < 100:
                    break
                page += 1
            return [n for n in names if n]
        except Exception as exc:
            logger.warning("Could not fetch repositories for installation %d: %s", installation_id, exc)
            return []

    async def get_installation_token(self, installation_id: int) -> str:
        """Exchange the App JWT for a short-lived installation access token.

        Returns the token string. Raises on HTTP errors.
        """
        resp = await self._http_client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json().get("token", "")

    async def check_org_membership(
        self,
        installation_id: int,
        org_login: str,
        username: str,
    ) -> bool:
        """Return True if `username` is a member of `org_login`.

        Uses an installation token for `installation_id`. Per GitHub docs:
        GET /orgs/{org}/members/{username} returns 204 for members, 404 for non-members.

        Issue #3035: Logs a diagnostic WARNING on 302/403 responses. These indicate
        the App lacks the ``Organization members: read`` permission — expected for
        the current permission set. The warning aids debugging for the future
        teammate-auto-join path without failing silently.
        """
        token = await self.get_installation_token(installation_id)
        resp = await self._http_client.get(
            f"/orgs/{org_login}/members/{username}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if resp.status_code in (302, 403):
            logger.warning(
                "check_org_membership: %d for %s in %s (installation %d) — "
                "app likely lacks 'Organization members: read' permission. "
                "Membership verification will return False.",
                resp.status_code,
                username,
                org_login,
                installation_id,
            )
        return resp.status_code == 204

    async def aclose(self) -> None:  # pragma: no cover
        await self._http_client.aclose()

"""Fresh provider-side repository permission checks before reading personal activity."""

import re
from urllib.parse import quote

import httpx

from src.admin.connections.github_client import github_account_id
from src.gitlab.service import host as approved_gitlab_host

GITHUB_API = "https://api.github.com"
MAX_IDENTITIES = 10
READ_PERMISSIONS = frozenset({"read", "triage", "write", "maintain", "admin"})


class ProviderAccessUnavailableError(Exception):
    """Current repository entitlement could not be established."""


def _identity_ids(values: frozenset[str]) -> list[str]:
    if not values or len(values) > MAX_IDENTITIES or any(github_account_id(value) != value for value in values):
        raise ProviderAccessUnavailableError("identity_scope_unavailable")
    return sorted(values, key=int)


def _github_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(segment in {".", ".."} for segment in repo.split("/")):
        raise ProviderAccessUnavailableError("repository_invalid")
    return repo


async def _response(client: httpx.AsyncClient, url: str, token: str, *, provider: str) -> dict | None:
    headers = {"Authorization": f"Bearer {token}"} if provider == "github" else {"PRIVATE-TOKEN": token}
    try:
        response = await client.get(url, headers=headers, follow_redirects=False)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProviderAccessUnavailableError("permission_check_unavailable")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid provider response")
        return payload
    except (httpx.HTTPError, ValueError):
        raise ProviderAccessUnavailableError("permission_check_unavailable") from None


async def github_repository_users(
    client: httpx.AsyncClient,
    token: str,
    repo: str,
    verified_ids: frozenset[str],
) -> frozenset[str]:
    """Return only linked accounts with *current* read permission on this repo.

    The installation token is not a member credential: `/user/{id}` resolves
    today's login from an immutable proven ID before the repo-specific check.
    No result or denial is cached across requests.
    """
    _github_repo(repo)
    if not token:
        raise ProviderAccessUnavailableError("provider_disconnected")
    allowed: set[str] = set()
    for identity in _identity_ids(verified_ids):
        account = await _response(client, f"{GITHUB_API}/user/{identity}", token, provider="github")
        if account is None:
            continue
        login = account.get("login")
        if github_account_id(account.get("id")) != identity or account.get("type") != "User" or not isinstance(login, str):
            continue
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            continue
        permission = await _response(
            client,
            f"{GITHUB_API}/repos/{repo}/collaborators/{quote(login, safe='')}/permission",
            token,
            provider="github",
        )
        if permission is None:
            continue
        permission_user = permission.get("user")
        if not isinstance(permission_user, dict):
            raise ProviderAccessUnavailableError("permission_check_unavailable")
        account_id = github_account_id(permission_user.get("id"))
        if account_id == identity and permission.get("permission") in READ_PERMISSIONS:
            allowed.add(identity)
    return frozenset(allowed)


async def gitlab_repository_users(
    client: httpx.AsyncClient,
    token: str,
    instance: str,
    project_id: int,
    repo: str,
    verified_ids: frozenset[str],
) -> frozenset[str]:
    """Check the exact project through a credential owned by the chatting user."""
    base_url = approved_gitlab_host(instance)
    if not token or type(project_id) is not int or project_id <= 0 or not isinstance(repo, str):
        raise ProviderAccessUnavailableError("provider_disconnected")
    identities = _identity_ids(verified_ids)
    account = await _response(client, f"{base_url}/api/v4/user", token, provider="gitlab")
    if account is None or type(account.get("id")) is not int or str(account["id"]) not in identities:
        raise ProviderAccessUnavailableError("credential_identity_mismatch")
    project = await _response(client, f"{base_url}/api/v4/projects/{project_id}", token, provider="gitlab")
    if project is None or type(project.get("id")) is not int or project["id"] != project_id or project.get("path_with_namespace") != repo:
        return frozenset()
    permissions = project.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise ProviderAccessUnavailableError("permission_check_unavailable")
    levels = [permissions.get(name) or {} for name in ("project_access", "group_access")]
    if any(not isinstance(entry, dict) for entry in levels):
        raise ProviderAccessUnavailableError("permission_check_unavailable")
    access = max((entry.get("access_level", 0) for entry in levels), default=0)
    if type(access) is not int or access < 10:
        return frozenset()
    return frozenset({str(account["id"])})

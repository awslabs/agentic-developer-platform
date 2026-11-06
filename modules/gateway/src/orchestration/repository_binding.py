"""Verify the repository and issue of a tenant's story before granting work."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from src.admin.connections.github_client import GitHubAppClient
from src.admin.installations.resolver import OwnerState, resolve_installation_owner
from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials
from src.shared.models.vault import ChannelTenantMap

_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MAX_CONNECTIONS = 32


class RepositoryBindingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VerifiedRepository:
    provider: str
    repository_id: int
    full_name: str
    installation_id: int
    accepted_name: str
    issue_number: int | None
    operation: str
    issue_id: int | None = None
    issue_node_id: str | None = None


async def _provider_read(tenant_id: str, installation_id: int, name: str, repository_id: int | None, issue: int | None):
    app_id, private_key = await resolve_tenant_app_credentials(tenant_id)
    async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
        github = GitHubAppClient(app_id, private_key, http_client=client)
        token, _ = await mint_installation_token_with_expiry(
            app_id,
            private_key,
            installation_id,
            repositories=None if repository_id else [name.split("/")[1]],
            repository_ids=[repository_id] if repository_id else None,
            permissions={"metadata": "read", "issues": "read"},
            http_client=client,
        )
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        response = await client.get(f"/repositories/{repository_id}" if repository_id else f"/repos/{name}", headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        repository = response.json()
        actual_name = repository.get("full_name")
        if not isinstance(actual_name, str) or not _NAME.fullmatch(actual_name):
            raise RepositoryBindingError("repository_verification_unavailable")
        if repository_id is not None and repository.get("id") != repository_id:
            raise RepositoryBindingError("repository_identity_mismatch")
        if repository_id is not None and actual_name.casefold() != name.casefold():
            try:
                name_token, _ = await mint_installation_token_with_expiry(
                    app_id,
                    private_key,
                    installation_id,
                    repositories=[name.split("/")[1]],
                    permissions={"metadata": "read"},
                    http_client=client,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (404, 422):
                    raise
            else:
                name_response = await client.get(
                    f"/repos/{name}", headers={"Authorization": f"Bearer {name_token}", "Accept": "application/vnd.github+json"}
                )
                if name_response.status_code not in (301, 302, 307, 308, 404):
                    name_response.raise_for_status()
                    if name_response.json().get("id") != repository_id:
                        raise RepositoryBindingError("repository_identity_mismatch")
        if issue is None:
            return repository, await github.get_installation(installation_id), None
        issue_response = await client.get(f"/repos/{actual_name}/issues/{issue}", headers=headers)
        if issue_response.status_code == 404:
            raise RepositoryBindingError("repository_identity_mismatch")
        issue_response.raise_for_status()
        issue_metadata = issue_response.json()
        if (
            type(issue_metadata.get("id")) is not int
            or issue_metadata["id"] <= 0
            or not isinstance(issue_metadata.get("node_id"), str)
            or not issue_metadata["node_id"]
            or type(issue_metadata.get("number")) is not int
            or issue_metadata["number"] != issue
            or issue_metadata.get("repository_url", "").casefold() != f"https://api.github.com/repos/{actual_name}".casefold()
            or issue_metadata.get("html_url", "").casefold() != f"https://github.com/{actual_name}/issues/{issue}".casefold()
            or "pull_request" in issue_metadata
        ):
            raise RepositoryBindingError("repository_identity_mismatch")
        return repository, await github.get_installation(installation_id), issue_metadata


async def resolve_story_repository(
    session,
    *,
    tenant_id: str,
    name: str,
    issue: int | None,
    operation: str,
    repository_id: int | None = None,
    installation_id: int | None = None,
    permitted_names: tuple[str, ...] | None = None,
) -> VerifiedRepository:
    """Resolve registered tenant connections without trusting cached repo lists."""
    if not isinstance(name, str) or not _NAME.fullmatch(name) or any(part in {".", ".."} for part in name.split("/")):
        raise RepositoryBindingError("repository_invalid")
    if (issue is None and operation != "evaluate") or (issue is not None and (type(issue) is not int or issue <= 0)):
        raise RepositoryBindingError("repository_invalid")
    if repository_id is not None and (type(repository_id) is not int or repository_id <= 0):
        raise RepositoryBindingError("repository_invalid")
    if installation_id is not None and (type(installation_id) is not int or installation_id <= 0):
        raise RepositoryBindingError("repository_invalid")

    connections = (
        await session.scalars(
            select(ChannelTenantMap).where(
                ChannelTenantMap.org_id == tenant_id,
                ChannelTenantMap.provider == "github",
            )
        )
    ).all()
    if len(connections) > _MAX_CONNECTIONS:
        raise RepositoryBindingError("repository_verification_unavailable")
    candidates = [
        row
        for row in connections
        if row.installation_id and row.installation_id.isdecimal() and (installation_id is None or int(row.installation_id) == installation_id)
    ]
    if not candidates:
        raise RepositoryBindingError("connection_not_owned" if installation_id else "repository_unavailable")

    found: list[VerifiedRepository] = []
    revoked = False
    for connection in candidates:
        selected_installation = int(connection.installation_id)
        try:
            owner, state = await resolve_installation_owner(selected_installation, db=session)
        except Exception as exc:
            raise RepositoryBindingError("repository_verification_unavailable") from exc
        if state is not OwnerState.RESOLVED or owner is None or owner.tenant_id != tenant_id:
            if installation_id is not None:
                raise RepositoryBindingError("connection_revoked" if state is OwnerState.REVOKED else "connection_not_owned")
            revoked = revoked or state is OwnerState.REVOKED
            continue
        try:
            metadata = await _provider_read(tenant_id, selected_installation, name, repository_id, issue)
        except RepositoryBindingError:
            raise
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (404, 422):
                if installation_id is not None:
                    raise RepositoryBindingError("repository_unavailable") from exc
                continue
            raise RepositoryBindingError("repository_verification_unavailable") from exc
        if metadata is None:
            continue
        repository, installation, issue_metadata = metadata
        actual_id, actual_name = repository.get("id"), repository.get("full_name")
        if type(actual_id) is not int or actual_id <= 0 or not isinstance(actual_name, str):
            raise RepositoryBindingError("repository_verification_unavailable")
        if repository_id is not None and actual_id != repository_id or repository_id is None and actual_name.casefold() != name.casefold():
            raise RepositoryBindingError("repository_identity_mismatch")
        if (installation.get("account") or {}).get("id") != int(owner.github_account_id or 0):
            raise RepositoryBindingError("connection_not_owned")
        if permitted_names is not None and name not in permitted_names and actual_name not in permitted_names:
            raise RepositoryBindingError("repository_not_permitted")
        found.append(
            VerifiedRepository(
                "github",
                actual_id,
                actual_name,
                selected_installation,
                name,
                issue,
                operation,
                issue_metadata["id"] if issue_metadata else None,
                issue_metadata["node_id"] if issue_metadata else None,
            )
        )

    if not found:
        raise RepositoryBindingError("connection_revoked" if revoked else "repository_unavailable")
    if len(found) > 1:
        raise RepositoryBindingError("repository_connection_ambiguous")
    return found[0]

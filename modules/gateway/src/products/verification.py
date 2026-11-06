"""Bounded read-only observation of a selected, existing source connection."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.products.models import ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap, UserCredential

OBSERVATION_LIFETIME = timedelta(minutes=15)
OBSERVATION_TIMEOUT = 30


def unavailable(reason: str) -> dict:
    return {"coverage": "unavailable", "reason": reason, "status": "unverified"}


def denied() -> dict:
    return {"coverage": "denied", "reason": "source_access_denied", "status": "revoked"}


async def observe_binding(
    binding: ProductSourceBinding,
    installation: ChannelTenantMap | UserCredential | None,
    *,
    github_client: GitHubAppClient | None = None,
    now: datetime | None = None,
) -> dict:
    """Return observed metadata only after a live read proves both native IDs."""
    if binding.provider in ("sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack"):
        from src.auth.vault_routes import get_secrets_manager
        from src.product_onboarding.expanded_provider import observe

        return await observe(binding, installation, get_secrets_manager(), now=now)
    if binding.provider == "jenkins" and binding.kind == "ci_job":
        from src.auth.vault_routes import get_secrets_manager
        from src.product_onboarding.jenkins import observe_jenkins

        return await observe_jenkins(binding, installation, get_secrets_manager(), now=now)
    if binding.kind == "environment" and binding.provider == "aws" and binding.credential_id is not None:
        from src.products.aws_environment_verification import observe_aws_environment

        return await observe_aws_environment(binding, installation, now=now)
    if binding.provider == "jira" and binding.kind == "tracker" and binding.credential_id is not None:
        from src.auth.vault_routes import get_secrets_manager
        from src.internal.connector_source_broker import jira_boards
        from src.shared.models.vault import UserCredential

        if not isinstance(installation, UserCredential) or installation.id != binding.credential_id or installation.org_id != binding.tenant_id:
            return denied()
        try:
            rows, _, coverage = await jira_boards(installation, get_secrets_manager(), board_id=binding.native_id)
            if coverage != "complete" or len(rows) != 1 or rows[0]["namespace"] != binding.namespace:
                return denied()
            observed_at = now or datetime.now(UTC)
            return {
                "coverage": "complete",
                "status": "healthy",
                "observed": {
                    "observed_at": observed_at.isoformat(),
                    "stale_after": (observed_at + OBSERVATION_LIFETIME).isoformat(),
                    "source_revision": rows[0]["source_revision"],
                    "native_id": binding.native_id,
                    "namespace": binding.namespace,
                },
            }
        except HTTPException:
            return unavailable("jira_read_unavailable")
    if binding.kind == "document" and binding.provider == "github" and binding.installation_id is not None:
        from src.products.github_documents import read_document_bytes

        try:
            _, revision = await read_document_bytes(binding, installation, client=github_client)
        except HTTPException as error:
            return denied() if error.status_code in (401, 403, 404) else unavailable("document_unavailable")
        except httpx.HTTPStatusError as error:
            return denied() if error.response.status_code in (401, 403, 404) else unavailable("provider_read_unavailable")
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return unavailable("document_unavailable")
        observed_at = now or datetime.now(UTC)
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": observed_at.isoformat(),
                "stale_after": (observed_at + OBSERVATION_LIFETIME).isoformat(),
                "source_revision": revision,
                "native_id": binding.native_id,
                "namespace": binding.namespace,
            },
        }
    if binding.kind != "repository" or binding.provider != "github" or binding.installation_id is None:
        return unavailable("unsupported_adapter")
    if (
        installation is None
        or installation.id != binding.installation_id
        or installation.org_id != binding.tenant_id
        or installation.provider != "github"
        or installation.ownership_disputed
        or not installation.installation_id
    ):
        return denied()
    installation_id = github_account_id(installation.installation_id)
    repository_id = github_account_id(binding.native_id)
    namespace = github_account_id(binding.namespace)
    if installation_id is None or repository_id is None or namespace is None:
        return denied()
    desired = binding.desired if binding.desired is not None else {}
    if not isinstance(desired, dict):
        return unavailable("source_revision_invalid")
    pinned_revision = desired.get("source_revision")
    if pinned_revision is not None and (not isinstance(pinned_revision, str) or re.fullmatch(r"[a-f0-9]{40}", pinned_revision) is None):
        return unavailable("source_revision_invalid")
    if github_client is None:
        from src.admin.connections.service import _get_github_app_credentials

        app_id, private_key = _get_github_app_credentials()
        if not app_id or not private_key:
            return unavailable("github_app_unavailable")
        github_client = GitHubAppClient(app_id, private_key)
        close_client = True
    else:
        close_client = False
    try:
        async with asyncio.timeout(OBSERVATION_TIMEOUT):
            installation_data = await github_client.get_installation(int(installation_id))
            account = installation_data.get("account") or {}
            account_id = github_account_id(account.get("id"))
            if (
                github_account_id(installation_data.get("id")) != installation_id
                or account_id != namespace
                or installation.provider_scope_id not in (account_id, account.get("login"))
            ):
                return denied()
            repository = await github_client.get_installation_repository(int(installation_id), int(repository_id))
            if github_account_id(repository.get("id")) != repository_id or github_account_id((repository.get("owner") or {}).get("id")) != account_id:
                return denied()
            owner = repository.get("owner") or {}
            login, name, branch = owner.get("login"), repository.get("name"), repository.get("default_branch")
            source_ref = pinned_revision if pinned_revision is not None else branch
            if (
                not isinstance(login, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
                or name in (".", "..")
                or not isinstance(source_ref, str)
                or not 0 < len(source_ref) <= 1024
                or source_ref in (".", "..")
                or any(ord(character) < 32 for character in source_ref)
            ):
                return unavailable("source_revision_unavailable")
            commit = await github_client.installation_request(
                int(installation_id),
                "GET",
                f"/repos/{quote(login, safe='')}/{quote(name, safe='')}/commits/{quote(source_ref, safe='')}",
            )
            revision = commit.get("sha") if isinstance(commit, dict) else None
            if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{40}", revision) is None:
                return unavailable("source_revision_unavailable")
            if pinned_revision is not None and revision != pinned_revision:
                return unavailable("source_revision_mismatch")
            observed_at = now or datetime.now(UTC)
            return {
                "coverage": "complete",
                "status": "healthy",
                "observed": {
                    "observed_at": observed_at.isoformat(),
                    "stale_after": (observed_at + OBSERVATION_LIFETIME).isoformat(),
                    "source_revision": revision,
                    "display_name": f"{login}/{name}",
                    "native_id": binding.native_id,
                    "namespace": binding.namespace,
                },
            }
    except (TimeoutError, httpx.HTTPStatusError, httpx.TimeoutException, ValueError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403, 404):
            return denied()
        return unavailable("provider_read_unavailable")
    finally:
        if close_client:
            await github_client.aclose()

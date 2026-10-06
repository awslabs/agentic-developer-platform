"""Authorization boundary for provider reads in a verified chat work request."""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.activity.external_authorization import ProviderAccessUnavailableError, _github_repo, github_repository_users, gitlab_repository_users
from src.activity.external_events import ClassifiedEvent, classify_events
from src.activity.external_identity import resolve_provider_identities
from src.activity.external_provider import GITHUB_API, PageBudget, ProviderRead, read_github, read_gitlab, safe_gitlab_host
from src.admin.connections.github_client import github_account_id
from src.agentauth.chat_capability import ChatLaunch
from src.gitlab.service import approved_project
from src.gitlab.service import providers as gitlab_providers
from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials, verify_installation_ownership
from src.shared.database import get_session_factory
from src.shared.models.organization import Organization
from src.shared.models.vault import UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper

MAX_INSTALLATIONS = 2
MAX_REPOSITORIES = 10
GITHUB_READ_PERMISSIONS = {"metadata": "read", "contents": "read", "pull_requests": "read", "issues": "read"}


@dataclass
class ExternalRead:
    events: list[ClassifiedEvent] = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)


def _coverage(result: ExternalRead, provider: str, status: str, reason: str) -> None:
    previous = [entry for entry in result.coverage if entry["source"] == provider]
    if status == "available" and previous:
        for entry in previous:
            if entry["status"] == "unavailable":
                entry["status"] = "partial"
        return
    if status != "available" and any(entry["status"] == "available" for entry in previous):
        result.coverage[:] = [entry for entry in result.coverage if entry["source"] != provider or entry["status"] != "available"]
    if status == "unavailable" and (
        any(entry["status"] in {"partial", "available"} for entry in previous) or any(item.event.provider == provider for item in result.events)
    ):
        status = "partial"
    entry = {"source": provider, "status": status, "reason": reason}
    if entry not in result.coverage:
        result.coverage.append(entry)


def _read_coverage(result: ExternalRead, provider: str, read: ProviderRead) -> None:
    if read.incomplete:
        for reason in sorted(read.gaps or {"history_incomplete"}):
            _coverage(result, provider, "partial", reason)


def _finish_coverage(result: ExternalRead, provider: str, queried: bool) -> None:
    if queried:
        return
    previous = [entry for entry in result.coverage if entry["source"] == provider]
    if previous:
        for entry in previous:
            entry["status"] = "unavailable"
    else:
        _coverage(result, provider, "unavailable", "no_authorized_repositories")


async def _github(
    db,
    client: httpx.AsyncClient,
    launch: ChatLaunch,
    identities: frozenset[str],
    start: datetime,
    end: datetime,
    result: ExternalRead,
):
    org = await db.get(Organization, launch.tenant_id)
    installations = [int(value) for value in (org.github_installation_ids or []) if github_account_id(value)]
    if len(installations) > MAX_INSTALLATIONS:
        _coverage(result, "github", "partial", "installation_limit")
    app_id, private_key = await resolve_tenant_app_credentials(launch.tenant_id)
    budget = PageBudget()
    queried = False
    for installation_id in installations[:MAX_INSTALLATIONS]:
        if not await verify_installation_ownership(launch.tenant_id, installation_id, db=db):
            _coverage(result, "github", "partial", "installation_not_owned")
            continue
        enumeration_token, _ = await mint_installation_token_with_expiry(
            app_id,
            private_key,
            installation_id,
            permissions={"metadata": "read"},
            http_client=client,
        )
        response = await client.get(
            f"{GITHUB_API}/installation/repositories",
            headers={"Authorization": f"Bearer {enumeration_token}"},
            params={"per_page": MAX_REPOSITORIES + 1, "page": 1},
            follow_redirects=False,
        )
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict) or not isinstance(document.get("repositories"), list):
            raise ValueError("Invalid repository listing")
        repositories = document["repositories"]
        if len(repositories) > MAX_REPOSITORIES or document.get("total_count", 0) > MAX_REPOSITORIES:
            _coverage(result, "github", "partial", "repository_limit")
        for repository in repositories[:MAX_REPOSITORIES]:
            if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str):
                _coverage(result, "github", "partial", "repository_invalid")
                continue
            repo = repository["full_name"]
            try:
                _github_repo(repo)
                token, _ = await mint_installation_token_with_expiry(
                    app_id,
                    private_key,
                    installation_id,
                    repositories=[repo.split("/")[-1]],
                    permissions=GITHUB_READ_PERMISSIONS,
                    http_client=client,
                )
                granted = await github_repository_users(client, token, repo, identities)
                if not granted:
                    _coverage(result, "github", "partial", "repository_not_authorized")
                    continue
                read = await read_github(token, [repo], start, end, client=client, budget=budget)
                queried = True
                result.events.extend(classify_events(read.events, {"github": set(granted)}))
                _coverage(result, "github", "available", "queried")
                _read_coverage(result, "github", read)
            except (ProviderAccessUnavailableError, httpx.HTTPError, ValueError):
                _coverage(result, "github", "unavailable", "provider_failure")
    _finish_coverage(result, "github", queried)


async def _gitlab_emails(client: httpx.AsyncClient, base_url: str, token: str, account_id: str) -> dict[str, str]:
    headers = {"PRIVATE-TOKEN": token}
    user_response = await client.get(f"{base_url}/api/v4/user", headers=headers, follow_redirects=False)
    user_response.raise_for_status()
    user = user_response.json()
    if not isinstance(user, dict) or github_account_id(user.get("id")) != account_id:
        raise ProviderAccessUnavailableError("credential_identity_mismatch")
    emails = {}
    if isinstance(user.get("email"), str) and "@" in user["email"]:
        emails[user["email"].casefold()] = account_id
    response = await client.get(f"{base_url}/api/v4/user/emails", headers=headers, follow_redirects=False)
    if response.status_code == 200 and isinstance(response.json(), list):
        for item in response.json():
            if isinstance(item, dict) and item.get("confirmed_at") and isinstance(item.get("email"), str) and "@" in item["email"]:
                emails[item["email"].casefold()] = account_id
    return emails


async def _gitlab(
    db,
    client: httpx.AsyncClient,
    launch: ChatLaunch,
    ids: dict[str, frozenset[str]],
    start: datetime,
    end: datetime,
    result: ExternalRead,
):
    org = await db.get(Organization, launch.tenant_id)
    config = (org.settings or {}).get("gitlab_cli_v1") or {}
    projects = config.get("projects") or {}
    credentials = (
        await db.scalars(
            select(UserCredential).where(
                UserCredential.org_id == launch.tenant_id,
                UserCredential.user_id == launch.user_id,
                UserCredential.service == "gitlab",
                UserCredential.credential_type.in_(("api_key", "bearer", "oauth_token")),
            )
        )
    ).all()
    approved_instances = {row["url"] for row in await gitlab_providers() if row.get("id") == config.get("provider_id")}
    candidates = [
        project
        for project in projects.values()
        if isinstance(project, dict) and project.get("active") and project.get("owner") == launch.user_id and project.get("instance") in ids
    ]
    budget = PageBudget()
    queried = False
    if len(candidates) > MAX_REPOSITORIES or len(credentials) > 2:
        _coverage(result, "gitlab", "partial", "query_limit")
    if not credentials:
        _coverage(result, "gitlab", "unavailable", "credential_disconnected")
        return
    for project in candidates[:MAX_REPOSITORIES]:
        base_url = project["instance"]
        repo = project.get("repo")
        project_id = project.get("project_id")
        if type(project_id) is not int or project_id <= 0 or not isinstance(repo, str):
            _coverage(result, "gitlab", "partial", "repository_invalid")
            continue
        if base_url not in approved_instances:
            _coverage(result, "gitlab", "partial", "provider_not_approved")
            continue
        try:
            safe_gitlab_host(base_url)
            approved_project(SimpleNamespace(org_id=launch.tenant_id), base_url, project_id, repo)
        except (HTTPException, ValueError):
            _coverage(result, "gitlab", "partial", "repository_not_approved")
            continue
        found = False
        failed = False
        for credential in credentials[:2]:
            expiry = credential.expires_at
            if expiry is not None and expiry.replace(tzinfo=expiry.tzinfo or UTC) <= datetime.now(UTC):
                continue
            try:
                token = await run_in_threadpool(SecretsManagerHelper().get_secret, credential.secret_arn)
                if not isinstance(token, str) or not token or len(token) > 65536:
                    continue
                granted = await gitlab_repository_users(client, token, base_url, project_id, repo, ids[base_url])
                if not granted:
                    continue
                found = True
                verified_emails = await _gitlab_emails(client, base_url, token, next(iter(granted)))
                read = await read_gitlab(
                    token,
                    base_url,
                    [(project_id, repo)],
                    start,
                    end,
                    client=client,
                    verified_emails=verified_emails,
                    budget=budget,
                )
                queried = True
                result.events.extend(classify_events(read.events, {"gitlab": set(granted)}))
                _coverage(result, "gitlab", "available", "queried")
                _read_coverage(result, "gitlab", read)
                break
            except (ProviderAccessUnavailableError, httpx.HTTPError, ValueError):
                failed = True
                _coverage(result, "gitlab", "unavailable", "provider_failure")
        if not found and not failed:
            _coverage(result, "gitlab", "partial", "repository_not_authorized")
    _finish_coverage(result, "gitlab", queried)


async def read_external_work(request: Request, launch: ChatLaunch, start: datetime, end: datetime) -> ExternalRead:
    """The run supplies only a verified launch; never a provider URL, token or repo."""
    result = ExternalRead()
    if os.environ.get("ADP_EXTERNAL_ACTIVITY_ENABLED") != "true":
        for provider in ("github", "gitlab"):
            _coverage(result, provider, "unavailable", "authorization_not_configured")
        return result
    try:
        async with get_session_factory()() as db:
            identities = await resolve_provider_identities(db, user_id=launch.user_id, tenant_id=launch.tenant_id)
            async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10, follow_redirects=False, trust_env=False) as client:
                for provider, scope, reader in (
                    ("github", identities.github_ids, _github),
                    ("gitlab", identities.gitlab_ids, _gitlab),
                ):
                    if not scope:
                        _coverage(result, provider, "unavailable", identities.unavailable.get(provider, "identity_unverified"))
                        continue
                    try:
                        await reader(db, client, launch, scope, start, end, result)
                    except Exception:
                        _coverage(result, provider, "unavailable", "provider_failure")
    except Exception:
        for provider in ("github", "gitlab"):
            if not any(entry["source"] == provider for entry in result.coverage):
                _coverage(result, provider, "unavailable", "identity_unavailable")
    return result

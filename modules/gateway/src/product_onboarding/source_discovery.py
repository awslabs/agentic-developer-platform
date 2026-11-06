"""Read-only projections of authorized source connections and repository metadata."""

import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from sqlalchemy import or_, select

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by
from src.auth.aws_connection_authority import connection_material, verified_connection_evidence
from src.auth.vault_service import _visible_credential_filter
from src.shared.identity.workspaces import workspace_user
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap, UserCredential

_UNSAFE_TEXT = re.compile(
    r"(?i)(?:https?://|\bBearer\s+\S+|\b(?:gh[pousr]_|glpat-)[A-Za-z0-9_-]+"
    r"|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)"
)

DEVLAKE_CREDENTIAL_TYPES = {
    "jenkins": {"basic_auth"},
    "sonarqube": {"api_key", "bearer"},
    "argocd": {"bearer", "api_key"},
    "gitlab": {"api_key", "bearer", "oauth_token"},
    "pagerduty": {"api_key", "bearer"},
    "linear": {"api_key", "bearer"},
    "slack": {"oauth_token", "bearer"},
}


def connector_credential(provider, credential):
    if credential.credential_type not in DEVLAKE_CREDENTIAL_TYPES[provider]:
        return False
    if provider == "slack":
        scopes = (credential.scopes or {}).get("oauth_scopes", [])
        return isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes) and {"channels:read", "groups:read"} <= set(scopes)
    return True


def safe_native(value, limit=512):
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value == value.strip()
        and all(character.isprintable() for character in value)
        and not _UNSAFE_TEXT.search(value)
    )


def safe_name(value):
    if not isinstance(value, str) or not value.strip():
        return None
    if _UNSAFE_TEXT.search(value):
        return "[redacted]"
    return "".join(character if character.isprintable() else " " for character in value).strip()[:200]


async def aws_environment(credential, secrets_manager):
    """Read identity and configured region from one version-bound AWS connection."""
    try:
        attempt, version, binding, verified_at = verified_connection_evidence(credential)
        current = await asyncio.to_thread(secrets_manager.current_version_id, credential.secret_arn)
        if current != version:
            return None
        payload, served = await asyncio.to_thread(secrets_manager.get_secret_at_version, credential.secret_arn, version)
        if served != version:
            return None
        secret = json.loads(payload)
        role, _, account = connection_material(secret, credential.scopes or {}, credential)
        region = secret.get("default_region")
        if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
            return None
        if await asyncio.to_thread(secrets_manager.current_version_id, credential.secret_arn) != version:
            return None
        if verified_connection_evidence(credential)[:3] != (attempt, version, binding):
            return None
        observed_at = verified_at.isoformat().replace("+00:00", "Z")
        return {
            "kind": "environment",
            "namespace": account,
            "native_id": region,
            "name": f"{account} · {region}",
            "source_revision": version,
            "observed_at": observed_at,
            "verified_environment": {
                "provider": "aws",
                "account_id": account,
                "regions": [region],
                "role_ref": role,
                "permission_tier": "unknown",
                "verified_at": observed_at,
                "source_revision": version,
            },
        }
    except (HTTPException, BotoCoreError, ClientError, OSError, ValueError, TypeError, KeyError):
        return None


async def connection_actor(db, actor):
    if actor.account_type != "human":
        return actor
    member = await workspace_user(db, actor.user_id, actor.org_id, username=actor.cognito_username, refresh=True)
    if member is None:
        return None
    return actor.model_copy(update={"user_id": member.id})


async def authorized_connections(db, actor):
    actor = await connection_actor(db, actor)
    if actor is None:
        return []
    mappings = (
        await db.scalars(select(ChannelTenantMap).where(ChannelTenantMap.org_id == actor.org_id, ChannelTenantMap.provider == "github"))
    ).all()
    visible = _visible_credential_filter(actor)
    credentials = (await db.scalars(select(UserCredential).where(UserCredential.org_id == actor.org_id, or_(*visible)))).all() if visible else []
    connections = []
    for mapping in mappings:
        if (
            not mapping.installation_id
            or mapping.ownership_disputed
            or (mapping.provider_scope_id.startswith("personal:") and mapping.installed_by_user_id != actor.user_id)
        ):
            continue
        try:
            await assert_installation_owned_by(actor.org_id, int(mapping.installation_id), db=db)
        except (InstallationOwnershipError, ValueError):
            continue
        connections.append(
            (
                {
                    "connection_ref": {"kind": "github_installation", "id": mapping.id},
                    "provider": "github",
                    "label": safe_name(mapping.provider_scope_id) or "[unavailable]",
                    "health": "unverified",
                    "capabilities": ["repository", "pipeline"],
                },
                mapping,
            )
        )
    for credential in credentials:
        provider = credential.provider or credential.service
        if provider not in ("gitlab", "github", "aws", "jira", "jenkins", "sonarqube", "argocd", "pagerduty", "linear", "slack"):
            continue
        if provider in ("jenkins", "sonarqube", "argocd", "pagerduty", "linear", "slack"):
            from src.product_onboarding.discovery import connector_enabled

            if not connector_credential(provider, credential) or not await connector_enabled(db, actor.org_id):
                continue
        expiry = credential.expires_at
        if expiry is not None and expiry.replace(tzinfo=expiry.tzinfo or UTC) <= utcnow():
            continue
        connections.append(
            (
                {
                    "connection_ref": {"kind": "credential", "id": credential.id},
                    "provider": provider,
                    "label": safe_name(credential.label) or "[unavailable]",
                    "health": "unverified",
                    "capabilities": {
                        "jenkins": ["ci_job"],
                        "sonarqube": ["quality_project"],
                        "argocd": ["deployment_application"],
                        "pagerduty": ["incident_service"],
                        "linear": ["tracker_team"],
                        "slack": ["communication_channel"],
                        "aws": ["environment"],
                        "jira": ["tracker"],
                    }.get(provider, ["repository"]),
                },
                credential,
            )
        )
    return sorted(connections, key=lambda pair: (pair[0]["connection_ref"]["kind"], pair[0]["connection_ref"]["id"]))


def repository_resource(repository, account_id, observed_at):
    owner = repository.get("owner") or {}
    if not isinstance(owner, dict):
        return None
    native_id = github_account_id(repository.get("id"))
    owner_id = github_account_id(owner.get("id"))
    revision = repository.get("updated_at")
    name = repository.get("name")
    if not native_id or owner_id != account_id or not safe_native(revision) or safe_name(name) is None:
        return None
    resource = {
        "kind": "repository",
        "namespace": owner_id,
        "native_id": native_id,
        "name": safe_name(name),
        "source_revision": revision,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }
    login = owner.get("login")
    if (
        isinstance(login, str)
        and re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
        and isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
        and name not in (".", "..")
    ):
        resource["full_name"] = f"{login}/{name}"
    branch = repository.get("default_branch")
    if isinstance(branch, str) and branch:
        resource["default_branch"] = safe_name(branch)
    return resource


async def github_resources(mapping, *, page=1, limit=100, client=None):
    """Only fetch fixed GitHub API paths using the installation's own authority."""
    from src.admin.connections.service import _get_github_app_credentials

    installation_id = github_account_id(mapping.installation_id)
    if not installation_id:
        return [], None, "denied"
    close_client = client is None
    if client is None:
        app_id, private_key = _get_github_app_credentials()
        if not app_id or not private_key:
            return [], None, "unavailable"
        client = GitHubAppClient(app_id, private_key)
    try:
        async with asyncio.timeout(30):
            installation = await client.get_installation(int(installation_id))
            if not isinstance(installation, dict):
                return [], None, "unavailable"
            account = installation.get("account") or {}
            if not isinstance(account, dict):
                return [], None, "unavailable"
            account_id = github_account_id(account.get("id"))
            if (
                github_account_id(installation.get("id")) != installation_id
                or not account_id
                or mapping.provider_scope_id not in (account_id, account.get("login"))
            ):
                return [], None, "denied"
            data = await client.list_installation_repositories_page(int(installation_id), page, limit)
            if not isinstance(data, dict):
                return [], None, "unavailable"
            repositories = data.get("repositories")
            if not isinstance(repositories, list) or len(repositories) > limit:
                return [], None, "unavailable"
            observed_at = utcnow()
            resources = [repository_resource(repository, account_id, observed_at) for repository in repositories if isinstance(repository, dict)]
            valid = [resource for resource in resources if resource is not None]
            total = data.get("total_count")
            next_cursor = str(page + 1) if len(repositories) == limit and (not isinstance(total, int) or page * limit < total) else None
            if len(valid) != len(repositories):
                return valid, str(page), "unavailable"
            return valid, next_cursor, "partial" if next_cursor else "complete"
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403, 404):
            return [], None, "denied"
        return [], None, "unavailable"
    finally:
        if close_client:
            await client.aclose()


async def github_pipeline_resources(mapping, repository_id, *, page=1, limit=100, workflow_id=None, client=None):
    """Read workflows only inside one repository proved accessible to this installation."""
    from src.admin.connections.service import _get_github_app_credentials

    installation_id = github_account_id(mapping.installation_id)
    repository_id = github_account_id(repository_id)
    if not installation_id or not repository_id:
        return [], None, "denied"
    close_client = client is None
    if client is None:
        app_id, private_key = _get_github_app_credentials()
        if not app_id or not private_key:
            return [], None, "unavailable"
        client = GitHubAppClient(app_id, private_key)
    try:
        async with asyncio.timeout(30):
            installation = await client.get_installation(int(installation_id))
            account = installation.get("account") or {}
            account_id = github_account_id(account.get("id"))
            if (
                github_account_id(installation.get("id")) != installation_id
                or not account_id
                or mapping.provider_scope_id not in (account_id, account.get("login"))
            ):
                return [], None, "denied"
            repository = await client.get_installation_repository(int(installation_id), int(repository_id))
            owner = repository.get("owner") or {}
            owner_login, name = owner.get("login"), repository.get("name")
            if (
                github_account_id(repository.get("id")) != repository_id
                or github_account_id(owner.get("id")) != account_id
                or not isinstance(owner_login, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner_login)
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
                or name in (".", "..")
            ):
                return [], None, "denied"
            base = f"/repos/{quote(owner_login, safe='')}/{quote(name, safe='')}/actions/workflows"
            if workflow_id is not None:
                workflow_id = github_account_id(workflow_id)
                if not workflow_id:
                    return [], None, "denied"
                data = await client.installation_request(int(installation_id), "GET", f"{base}/{workflow_id}")
                workflows, total = [data], 1
            else:
                data = await client.installation_request(int(installation_id), "GET", f"{base}?per_page={limit}&page={page}")
                workflows, total = data.get("workflows"), data.get("total_count")
            if not isinstance(workflows, list) or len(workflows) > limit:
                return [], None, "unavailable"
            observed_at = utcnow().isoformat().replace("+00:00", "Z")
            resources = []
            for workflow in workflows:
                if not isinstance(workflow, dict) or not github_account_id(workflow.get("id")) or not safe_native(workflow.get("updated_at")):
                    return [], None, "unavailable"
                if workflow_id is not None and github_account_id(workflow["id"]) != workflow_id:
                    return [], None, "denied"
                resources.append(
                    {
                        "kind": "pipeline",
                        "namespace": account_id,
                        "native_id": github_account_id(workflow["id"]),
                        "repository_id": repository_id,
                        "name": safe_name(workflow.get("name")) or "[unnamed]",
                        "source_revision": workflow["updated_at"],
                        "observed_at": observed_at,
                    }
                )
            continuation = (
                str(page + 1) if workflow_id is None and len(workflows) == limit and (not isinstance(total, int) or page * limit < total) else None
            )
            return resources, continuation, "partial" if continuation else "complete"
    except (TimeoutError, httpx.HTTPStatusError, httpx.TimeoutException, ValueError, TypeError, KeyError, AttributeError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403, 404):
            return [], None, "denied"
        return [], None, "unavailable"
    finally:
        if close_client:
            await client.aclose()


async def gitlab_resources(db, actor, credential, *, page=1, limit=100):
    """Read only previously connected GitLab projects on an approved instance."""
    from src.gitlab import service as gitlab

    config = gitlab.state(await gitlab.organization(db, actor))
    try:
        approved = next(provider for provider in await gitlab.providers() if provider["id"] == config.get("provider_id"))
    except (StopIteration, HTTPException, ValueError):
        return [], None, "unavailable"
    associations = sorted(
        (
            row
            for row in config.get("projects", {}).values()
            if isinstance(row, dict) and row.get("active") and row.get("owner") == actor.user_id and row.get("instance") == approved["url"]
        ),
        key=lambda row: str(row.get("project_id")),
    )
    selected = associations[(page - 1) * limit : page * limit]
    resources = []
    coverage = "complete"
    try:
        async with asyncio.timeout(30):
            for row in selected:
                observed = await gitlab.probe(db, actor, approved, credential.id, row["repo"], row["project_id"])
                if str(observed["project_id"]) != str(row["project_id"]) or observed["repo"] != row["repo"]:
                    return resources, str(page), "denied"
                if not safe_native(row["repo"]) or not safe_native(str(row["project_id"])) or not safe_native(str(row["revision"])):
                    return [], str(page), "unavailable"
                resources.append(
                    {
                        "kind": "repository",
                        "namespace": row["repo"],
                        "native_id": str(row["project_id"]),
                        "name": safe_name(row["repo"].rsplit("/", 1)[-1]),
                        "source_revision": str(row["revision"]),
                        "observed_at": utcnow().isoformat().replace("+00:00", "Z"),
                    }
                )
    except (TimeoutError, HTTPException, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, HTTPException) and exc.status_code in (401, 403, 404):
            coverage = "denied"
        else:
            coverage = "unavailable"
    continuation = str(page + 1) if page * limit < len(associations) else None
    if coverage != "complete" and selected:
        continuation = str(page)
    elif continuation:
        coverage = "partial"
    return resources, continuation, coverage


async def associated_records(db, actor, project, connection_id):
    """Project-confirmed refs are reused only with a currently authorized verified binding."""
    from src.products.models import ProductSourceBinding
    from src.products.source_routes import binding_view, connection_access

    basic = (project.configuration or {}).get("basic") or {}
    known = [
        (kind, reference)
        for field, kind in (("trackers", "tracker"), ("documents", "document"))
        for reference in (basic.get(field) or [])
        if isinstance(reference, dict)
        and reference.get("tenant_id") == actor.org_id
        and reference.get("project_id") == project.id
        and reference.get("source_connection_id") == connection_id
    ]
    if not known:
        return []
    bindings = (
        await db.scalars(
            select(ProductSourceBinding).where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.project_id == project.id,
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status == "healthy",
            )
        )
    ).all()
    authorized = {
        (binding.kind, binding.native_id): binding
        for binding in bindings
        if (binding.installation_id or binding.credential_id) == connection_id
        and binding_view(binding, True)["status"] == "healthy"
        and (binding.observed or {}).get("namespace") == binding.namespace
        and await connection_access(db, actor, binding) is not None
    }
    records = []
    for kind, reference in known:
        binding = authorized.get((kind, reference.get("record_id")))
        if binding is None or not isinstance(reference.get("record_revision"), str):
            continue
        observed_at = datetime.fromisoformat(binding.observed["observed_at"].replace("Z", "+00:00"))
        records.append(
            {
                "kind": kind,
                "namespace": binding.namespace,
                "native_id": binding.native_id,
                "name": binding.native_id[:200],
                "source_revision": reference["record_revision"],
                "observed_at": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "record_ref": reference,
            }
        )
    return records

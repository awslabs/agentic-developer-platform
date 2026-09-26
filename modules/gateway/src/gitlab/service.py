"""Human-owned associations never grant or rewrite protected producer authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.agentauth.external_roots import root_bindings
from src.auth.gitlab_sso import _discover_gitlab_url, _load_signing_key
from src.shared.models.organization import Organization
from src.shared.models.vault import UserCredential, UserIdentity
from src.shared.services.secrets_manager import SecretsManagerHelper

KEY = "gitlab_cli_v1"


def host(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(503, "Approved GitLab provider must be an HTTPS origin")
    return value.rstrip("/")


async def providers():
    platform = await asyncio.to_thread(_discover_gitlab_url)
    approved = {host(row.instance) for row in root_bindings() if row.source == "gitlab"}
    if platform:
        approved.add(host(platform))
    return [
        {
            "id": hashlib.sha256(value.encode()).hexdigest()[:24],
            "url": value,
            "kind": "platform" if value == platform else "external",
            "revision": hashlib.sha256(value.encode()).hexdigest(),
        }
        for value in sorted(approved)
    ]


async def organization(db, caller, lock=False):
    query = select(Organization).where(Organization.id == caller.org_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    org = await db.scalar(query)
    if org is None:
        raise HTTPException(404, "Tenant not found")
    return org


def state(org):
    return dict((org.settings or {}).get(KEY) or {})


async def provider(db, caller, provider_id=None):
    config = state(await organization(db, caller))
    selected = provider_id or config.get("provider_id")
    visible = await providers()
    found = [row for row in visible if row["id"] == selected]
    if len(found) != 1:
        raise HTTPException(409, "Select a deployment-approved provider with admin gitlab configure")
    return found[0], config


async def credential(db, caller, credential_id):
    # Only the caller's own PAT can authenticate their identity; org/bot and
    # workload credentials cannot impersonate an ordinary human here.
    row = await db.scalar(
        select(UserCredential).where(
            UserCredential.id == str(credential_id), UserCredential.org_id == caller.org_id, UserCredential.user_id == caller.user_id
        )
    )
    if row is None or row.service != "gitlab" or row.credential_type not in {"api_key", "bearer", "oauth_token"}:
        raise HTTPException(404, "Owned GitLab credential not found")
    expiry = row.expires_at
    if expiry is not None and expiry.replace(tzinfo=expiry.tzinfo or UTC) <= datetime.now(UTC):
        raise HTTPException(409, "GitLab credential expired")
    return row


async def probe(db, caller, approved, credential_id, repo, project_id=None):
    row = await credential(db, caller, credential_id)
    try:
        token = await asyncio.to_thread(SecretsManagerHelper().get_secret, row.secret_arn)
        if not isinstance(token, str) or not token or len(token) > 65536:
            raise ValueError
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:

            async def get(path):
                response = await client.get(approved["url"] + "/api/v4" + path, headers={"PRIVATE-TOKEN": token})
                if response.status_code != 200:
                    raise HTTPException(403 if response.status_code in {401, 403, 404} else 503, "GitLab access not verified")
                return response.json()

            user = await get("/user")
            project = await get("/projects/" + quote(str(project_id) if project_id else repo, safe=""))
        if type(user.get("id")) is not int or user["id"] <= 0 or type(project.get("id")) is not int or project["id"] <= 0:
            raise ValueError
        actual = project.get("path_with_namespace")
        if actual != repo or (project_id is not None and project["id"] != project_id):
            raise HTTPException(409, "Project moved or renamed; review its current numeric ID and namespace")
        permission = project.get("permissions") or {}
        access = max((permission.get(name) or {}).get("access_level", 0) for name in ("project_access", "group_access"))
        if access < 40:
            raise HTTPException(403, "GitLab Maintainer permission is required to manage this project association")
        return {"project_id": project["id"], "repo": actual, "provider_user_id": str(user["id"]), "username": user.get("username", "")}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "GitLab verification unavailable; credentials were not returned") from None


def approved_project(caller, instance, project_id, repo):
    rows = [row for row in root_bindings() if row.source == "gitlab" and row.instance == instance and row.project_id == project_id]
    if len(rows) != 1 or rows[0].tenant_id != caller.org_id or rows[0].repo != repo:
        raise HTTPException(409, "Operator approval for this exact tenant, instance, project ID and namespace is required")
    return rows[0]


async def describe(db, caller, *, repo=None, credential_id=None):
    org = await organization(db, caller)
    config = state(org)
    available = await providers()
    selected = next((p for p in available if p["id"] == config.get("provider_id")), None)
    result = {
        "contract": KEY,
        "org_id": caller.org_id,
        "providers": available,
        "configuration": {"provider_id": config.get("provider_id"), "revision": config.get("revision")},
        "sso": "unconfigured",
        "identity_linked": False,
        "project_access": "unverified",
        "webhook_delivery": "unverified",
        "agent_runtime": "unverified",
    }
    if selected:
        result["sso"] = "configured" if selected["kind"] == "platform" and await asyncio.to_thread(_load_signing_key) else "unverified"
        linked = await db.scalar(
            select(UserIdentity.id).where(
                UserIdentity.org_id == caller.org_id,
                UserIdentity.user_id == caller.user_id,
                UserIdentity.provider == "gitlab",
                UserIdentity.provider_user_id.startswith(selected["url"] + "#"),
                UserIdentity.verified_at.is_not(None),
            )
        )
        result["identity_linked"] = linked is not None
    if repo:
        matches = [
            r
            for r in config.get("projects", {}).values()
            if r["repo"] == repo and r["owner"] == caller.user_id and selected and r["instance"] == selected["url"]
        ]
        result["association"] = matches[0] if len(matches) == 1 else None
        if credential_id and selected:
            observed = await probe(db, caller, selected, credential_id, repo, matches[0]["project_id"] if matches else None)
            result.update(project_access="verified", project={k: observed[k] for k in ("project_id", "repo")})
    return result


async def mutate(db, caller, action, request):
    approved, initial = await provider(db, caller, request.provider_id if action == "configure" else None)
    if approved["revision"] != request.expected_provider_revision:
        raise HTTPException(409, "Provider configuration changed")
    verified = None
    if action == "connect":
        verified = await probe(db, caller, approved, request.credential_id, request.repo, request.project_id)
        approved_project(caller, approved["url"], request.project_id, request.repo)
    org = await organization(db, caller, lock=True)
    current = state(org)
    # Re-read selection after the network call and lock before persisting.
    if action != "configure" and current.get("provider_id") != approved["id"]:
        raise HTTPException(409, "Selected provider changed")
    operation = str(request.operation_id)
    digest = hashlib.sha256(
        json.dumps({"actor": caller.user_id, "action": action, **request.model_dump(mode="json")}, sort_keys=True).encode()
    ).hexdigest()
    operations = dict(current.get("operations", {}))
    if operation in operations:
        if operations[operation]["digest"] != digest:
            raise HTTPException(409, "Operation ID is bound to different input")
        return {**operations[operation]["result"], "replayed": True, "current_revision": current.get("revision")}
    if len(operations) >= 256:
        raise HTTPException(409, "Tenant operation history needs operator archival")
    if current.get("revision") != request.expected_revision:
        raise HTTPException(409, "Tenant GitLab revision changed; inspect status before retrying")
    projects = dict(current.get("projects", {}))
    if action == "configure":
        if any(row.get("active") for row in projects.values()) and current.get("provider_id") != approved["id"]:
            raise HTTPException(409, "Disconnect managed projects before changing provider")
        current["provider_id"] = approved["id"]
    else:
        key = approved["id"] + ":" + str(request.project_id)
        previous = projects.get(key)
        if previous and previous["owner"] != caller.user_id:
            raise HTTPException(403, "Project association is owned by another user")
        if action == "disconnect" and previous and previous["repo"] != request.repo:
            raise HTTPException(409, "Project namespace changed; inspect the owned association")
        if action == "disconnect" and (previous is None or not previous.get("active")):
            raise HTTPException(404, "Owned active project association not found")
        if action == "connect":
            external = approved["url"] + "#" + verified["provider_user_id"]
            identity = await db.scalar(
                select(UserIdentity).where(
                    UserIdentity.provider == "gitlab", UserIdentity.provider_user_id == external, UserIdentity.org_id == caller.org_id
                )
            )
            if identity and identity.user_id != caller.user_id:
                raise HTTPException(409, "GitLab identity is already linked to another human")
            if identity is None:
                db.add(
                    UserIdentity(
                        org_id=caller.org_id,
                        user_id=caller.user_id,
                        team_id=caller.team_id,
                        provider="gitlab",
                        provider_user_id=external,
                        provider_username=verified["username"],
                        verification_method="credential_verified",
                        verified_at=datetime.now(UTC),
                    )
                )
            elif identity.verified_at is None:
                identity.verified_at = datetime.now(UTC)
        projects[key] = {
            "project_id": request.project_id,
            "repo": request.repo,
            "owner": caller.user_id,
            "instance": approved["url"],
            "active": action == "connect",
            "revision": operation,
        }
        current["projects"] = projects
    current["revision"] = operation
    result = {
        "contract": KEY,
        "org_id": caller.org_id,
        "provider_id": approved["id"],
        "revision": operation,
        "operation_id": operation,
        "action": action,
        "replayed": False,
        "project_id": getattr(request, "project_id", None),
        "repo": getattr(request, "repo", None),
        "webhook_delivery": "unverified",
        "agent_runtime": "unverified",
    }
    operations[operation] = {"digest": digest, "result": result}
    current["operations"] = operations
    org.settings = {**(org.settings or {}), KEY: current}
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Identity or association changed concurrently") from None
    return result


async def require_managed_association(db, binding):
    """Managed tombstones deny new protected roots; untouched legacy roots remain unchanged."""
    org = await db.scalar(select(Organization).where(Organization.id == binding.tenant_id))
    if org is None:
        raise HTTPException(403, "model root refused")
    config = state(org)
    key = hashlib.sha256(binding.instance.encode()).hexdigest()[:24] + ":" + str(binding.project_id)
    managed = config.get("projects", {}).get(key)
    if managed is not None and (not managed.get("active") or managed["instance"] != binding.instance or managed["repo"] != binding.repo):
        raise HTTPException(403, "GitLab project association is disconnected or changed")

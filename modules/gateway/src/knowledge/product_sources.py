"""Product-owned GitHub source authority shared by UI, scheduler and workers.

The asset's typed product_source column is a bounded source grant. Only the
authenticated registration route can create it. The existing named scheduler
and signed per-attempt worker capabilities consume it; neither a queue message
nor user-editable metadata can select a connection. The grant does not depend
on the continued membership of the person who configured it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.admin.connections.service import _get_github_app_credentials
from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by
from src.knowledge.github_app_service import mint_installation_token_with_expiry
from src.products.models import Product, ProductProjectBinding, ProductSourceBinding, Project
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap

SERVICE_ID = "knowledge-ingestion"


def unavailable(message="This Product source is no longer available. Check the Product connection."):
    return HTTPException(409, detail={"code": "knowledge_source_unavailable", "message": message})


@dataclass(frozen=True)
class ProductSource:
    binding: ProductSourceBinding
    connection: ChannelTenantMap
    relationship: ProductProjectBinding
    project: Project


async def resolve_source(db, tenant_id, product_id, binding_id, *, lock=False) -> ProductSource:
    query = (
        select(ProductSourceBinding, ChannelTenantMap, ProductProjectBinding, Project)
        .join(ChannelTenantMap, ChannelTenantMap.id == ProductSourceBinding.installation_id)
        .join(Project, (Project.tenant_id == ProductSourceBinding.tenant_id) & (Project.id == ProductSourceBinding.project_id))
        .join(ProductProjectBinding, (ProductProjectBinding.tenant_id == Project.tenant_id) & (ProductProjectBinding.project_id == Project.id))
        .join(Product, (Product.tenant_id == ProductProjectBinding.tenant_id) & (Product.id == ProductProjectBinding.product_id))
        .where(
            ProductSourceBinding.tenant_id == tenant_id,
            ProductSourceBinding.id == binding_id,
            ProductSourceBinding.kind == "repository",
            ProductSourceBinding.provider == "github",
            ProductSourceBinding.unlinked_at.is_(None),
            ProductSourceBinding.status != "revoked",
            ChannelTenantMap.org_id == tenant_id,
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.ownership_disputed.is_(False),
            Product.id == product_id,
            Product.archived_at.is_(None),
            Product.team_id.is_not(None),
            ProductProjectBinding.valid_to.is_(None),
            Project.archived_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update(read=True)
    row = (await db.execute(query)).first()
    if row is None:
        raise unavailable()
    source = ProductSource(*row)
    mapping = source.connection
    # Personal installations retain their existing personal import path.
    # Product service grants may only use an organization-owned installation.
    if mapping.provider_scope_id.startswith("personal:") or not github_account_id(mapping.installation_id):
        raise unavailable("Choose an organization GitHub App connection for Product Context.")
    try:
        await assert_installation_owned_by(tenant_id, int(mapping.installation_id), db=db)
    except (InstallationOwnershipError, ValueError):
        raise unavailable() from None
    if not github_account_id(source.binding.native_id) or not github_account_id(source.binding.namespace):
        raise unavailable()
    # Knowledge follows the repository's current default branch. A pinned
    # assessment input is intentionally not widened into a moving source.
    if (source.binding.desired or {}).get("source_revision"):
        raise unavailable("This source is pinned to a commit. Select a repository that follows its default branch for automatic Context ingestion.")
    return source


def source_identity(source: ProductSource) -> dict:
    return {
        "service_id": SERVICE_ID,
        "binding_id": source.binding.id,
        "binding_generation": source.binding.generation,
        "project_id": source.project.id,
        "relationship_id": source.relationship.id,
        "connection_id": source.connection.id,
        "installation_id": int(source.connection.installation_id),
        "repository_id": int(source.binding.native_id),
        "namespace": source.binding.namespace,
    }


async def current_grant(db, tenant_id, product_id, grant, *, lock=True):
    if not isinstance(grant, dict):
        raise unavailable()
    source = await resolve_source(db, tenant_id, product_id, grant.get("binding_id"), lock=lock)
    if any(grant.get(key) != value for key, value in source_identity(source).items()):
        raise unavailable("The selected Product source changed. Add it again using its current connection.")
    app_id, _ = app_credentials()
    if grant.get("app_id") != app_id:
        raise unavailable("The ADP GitHub App changed. Reconnect this Context source.")
    return source


def app_credentials():
    app_id, key = _get_github_app_credentials()
    if not app_id or not key:
        raise HTTPException(503, detail={"code": "knowledge_app_unavailable", "message": "The ADP GitHub App is not configured."})
    return str(app_id), key


async def describe_source(source: ProductSource, *, client=None):
    """Attest immutable account/repository IDs using the selected ADP App."""
    app_id, key = app_credentials()
    own_client = client is None
    client = client or GitHubAppClient(app_id, key)
    try:
        installation_id = int(source.connection.installation_id)
        installation = await client.get_installation(installation_id)
        account = installation.get("account") or {}
        if (
            github_account_id(installation.get("id")) != str(installation_id)
            or github_account_id(account.get("id")) != source.binding.namespace
            or account.get("type") != "Organization"
            or source.connection.provider_scope_id not in (source.binding.namespace, account.get("login"))
            or installation.get("suspended_at")
        ):
            raise unavailable()
        repository = await client.get_installation_repository(installation_id, int(source.binding.native_id))
        name = repository.get("full_name")
        if (
            github_account_id(repository.get("id")) != source.binding.native_id
            or github_account_id((repository.get("owner") or {}).get("id")) != source.binding.namespace
            or not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", name) is None
            or name.split("/")[1] in {".", ".."}
        ):
            raise unavailable()
        return {"name": name, "source_ref": "https://github.com/" + name, "connection_name": str(account["login"]), "app_id": app_id}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403, 404):
            raise unavailable() from None
        raise HTTPException(503, detail={"code": "knowledge_source_unavailable", "message": "GitHub is unavailable. Retry this source."}) from None
    except (httpx.RequestError, ValueError, KeyError, TypeError):
        raise HTTPException(503, detail={"code": "knowledge_source_unavailable", "message": "Could not verify this GitHub source. Retry."}) from None
    finally:
        if own_client:
            await client.aclose()


async def grant_source(db, tenant_id, product_id, binding_id, actor_id):
    source = await resolve_source(db, tenant_id, product_id, binding_id, lock=True)
    details = await describe_source(source)
    return {
        **source_identity(source),
        **details,
        "authorized_by": actor_id,
        "authorized_at": utcnow().isoformat(),
    }


async def source_token(db, authority):
    source = await current_grant(db, authority.tenant_id, authority.product_id, authority.product_source)
    details = await describe_source(source)
    if details["source_ref"].casefold() != authority.source_ref.casefold():
        raise unavailable("The repository was renamed or moved. Reconnect this Context source.")
    app_id, key = app_credentials()
    token, _ = await mint_installation_token_with_expiry(
        app_id,
        key,
        int(source.connection.installation_id),
        repository_ids=[int(source.binding.native_id)],
        permissions={"contents": "read", "metadata": "read"},
    )
    return token

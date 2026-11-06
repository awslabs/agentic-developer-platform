"""Read one explicitly consented GitHub branch through current Product ownership."""

import asyncio
import re
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from src.admin.connections.github_client import GitHubAppClient, github_account_id
from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by
from src.auth.l5_transform_branch_scope import consent_for_trigger
from src.products.models import ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap


async def current_branch_binding(db, parent, trigger):
    """Branch authority is exact repository identity, never arbitrary history."""
    denied = HTTPException(
        403,
        detail={
            "code": "branch_source_unavailable",
            "message": (
                "Verify that the selected GitHub repository is still linked to this Product "
                "and its installation remains accessible to the consent owner."
            ),
        },
    )
    if not consent_for_trigger(parent.execution_scope, trigger):
        raise HTTPException(
            403,
            detail={
                "code": "branch_schedule_consent_required",
                "message": "Review delegation-scope and authorize this configuration's exact repository, branch and scheduled goal.",
            },
        )
    selector = trigger["branch_selector"]
    rows = (
        await db.scalars(
            select(ProductSourceBinding)
            .where(
                ProductSourceBinding.tenant_id == parent.tenant_id,
                ProductSourceBinding.project_id == parent.project_id,
                ProductSourceBinding.installation_id == selector["source_connection_id"],
                ProductSourceBinding.native_id == selector["record_id"],
                ProductSourceBinding.kind == "repository",
                ProductSourceBinding.provider == "github",
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status != "revoked",
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(rows) != 1:
        raise denied
    binding = rows[0]
    mapping = await db.get(ChannelTenantMap, binding.installation_id, populate_existing=True)
    if (
        mapping is None
        or mapping.org_id != parent.tenant_id
        or mapping.provider != "github"
        or mapping.ownership_disputed
        or not mapping.installation_id
        or not github_account_id(mapping.installation_id)
        or not github_account_id(binding.native_id)
        or not github_account_id(binding.namespace)
        or (mapping.provider_scope_id.startswith("personal:") and mapping.installed_by_user_id != parent.created_by)
    ):
        raise denied
    try:
        await assert_installation_owned_by(parent.tenant_id, int(mapping.installation_id), db=db)
    except (InstallationOwnershipError, ValueError):
        raise denied from None
    return binding, mapping


def branch_binding_identity(binding, mapping):
    return (
        binding.id,
        binding.generation,
        binding.installation_id,
        binding.native_id,
        binding.namespace,
        mapping.id,
        mapping.org_id,
        mapping.installation_id,
        mapping.provider_scope_id,
        mapping.installed_by_user_id,
    )


def repository_owned_by_mapping(repository, binding, mapping):
    if not isinstance(repository, dict) or not isinstance(repository.get("owner"), dict):
        return False
    owner = repository.get("owner") or {}
    owner_id = github_account_id(owner.get("id"))
    return bool(
        owner_id
        and github_account_id(repository.get("id")) == github_account_id(binding.native_id)
        and owner_id == github_account_id(binding.namespace)
        and mapping.provider_scope_id in {owner_id, owner.get("login"), f"personal:{owner_id}:{getattr(mapping, 'installed_by_user_id', None)}"}
    )


async def resolve_branch(binding, mapping, selector):
    """Bounded provider read, with one repository and contents/metadata read only."""
    from src.admin.connections.service import _get_github_app_credentials
    from src.knowledge.github_app_service import mint_installation_token_with_expiry

    unavailable = HTTPException(
        503,
        detail={
            "code": "branch_resolution_unavailable",
            "message": (
                "The selected GitHub branch could not be read. Repair may retry this occurrence within its schedule window; "
                "verify the GitHub connection if failures persist."
            ),
        },
    )
    try:
        app_id, private_key = _get_github_app_credentials()
    except Exception:
        raise unavailable from None
    if not app_id or not private_key:
        raise unavailable
    client = GitHubAppClient(app_id, private_key)
    try:
        async with asyncio.timeout(25):
            token, _ = await mint_installation_token_with_expiry(
                app_id,
                private_key,
                int(mapping.installation_id),
                repository_ids=[int(binding.native_id)],
                permissions={"contents": "read", "metadata": "read"},
                http_client=client._http_client,
            )
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            response = await client._http_client.get(f"/repositories/{binding.native_id}", headers=headers)
            response.raise_for_status()
            if len(response.content) > 128 * 1024:
                raise unavailable
            repository = response.json()
            if not isinstance(repository, dict):
                raise unavailable
            name = repository.get("full_name")
            if (
                not repository_owned_by_mapping(repository, binding, mapping)
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
            ):
                raise HTTPException(403, detail={"code": "branch_source_unavailable"})
            branch = selector["record_revision"]
            response = await client._http_client.get(f"/repos/{name}/git/ref/heads/{quote(branch, safe='')}", headers=headers)
            response.raise_for_status()
            if len(response.content) > 16 * 1024:
                raise unavailable
            result = response.json()
            value = result.get("object", {}) if isinstance(result, dict) else {}
            if (
                not isinstance(result, dict)
                or result.get("ref") != f"refs/heads/{branch}"
                or not isinstance(value, dict)
                or value.get("type") != "commit"
                or not isinstance(value.get("sha"), str)
                or not re.fullmatch(r"[a-f0-9]{40}", value["sha"])
            ):
                raise HTTPException(409, detail={"code": "branch_commit_unavailable"})
            return {**selector, "record_type": "repository_commit", "record_revision": value["sha"]}
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {401, 403, 404, 422}:
            raise HTTPException(403, detail={"code": "branch_source_unavailable"}) from None
        raise unavailable from None
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        raise unavailable from None
    finally:
        await client.aclose()

"""Resolve verified, connected provider account IDs for one human and tenant."""

import hashlib
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.connections.github_client import github_account_id
from src.gitlab.service import host as approved_gitlab_host
from src.shared.identity.verification import is_proven
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity


@dataclass
class LinkedProviderIdentities:
    github_ids: frozenset[str] = frozenset()
    gitlab_ids: dict[str, frozenset[str]] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)


def _gitlab_instances(org: Organization, user_id: str) -> set[str]:
    config = (org.settings or {}).get("gitlab_cli_v1") or {}
    if not isinstance(config, dict) or not isinstance(config.get("projects"), dict):
        return set()
    selected = config.get("provider_id")
    active = set()
    for project in config["projects"].values():
        if not isinstance(project, dict) or not project.get("active") or project.get("owner") != user_id:
            continue
        try:
            instance = approved_gitlab_host(project.get("instance"))
        except HTTPException:
            continue
        if selected == hashlib.sha256(instance.encode()).hexdigest()[:24]:
            active.add(instance)
    return active


async def resolve_provider_identities(db: AsyncSession, *, user_id: str, tenant_id: str) -> LinkedProviderIdentities:
    """Only immutable, proven IDs attached to the active human's tenant qualify.

    A linked identity is not repository access; callers must check current
    provider rights for each repository before collecting or returning events.
    """
    user = await db.scalar(select(User).where(User.id == user_id, User.org_id == tenant_id, User.user_kind == "human", User.is_shadow.is_(False)))
    org = await db.scalar(select(Organization).where(Organization.id == tenant_id))
    if user is None or org is None:
        return LinkedProviderIdentities(unavailable={"github": "user_not_available", "gitlab": "user_not_available"})

    github_connected = any(github_account_id(installation_id) for installation_id in (org.github_installation_ids or []))
    gitlab_instances = _gitlab_instances(org, user_id)
    identities = (
        await db.scalars(
            select(UserIdentity).where(
                UserIdentity.user_id == user_id,
                UserIdentity.org_id == tenant_id,
                UserIdentity.provider.in_(("github", "gitlab")),
            )
        )
    ).all()
    github_ids: set[str] = set()
    gitlab_ids: dict[str, set[str]] = {instance: set() for instance in gitlab_instances}
    for row in identities:
        verified = row.verified_at is not None and (
            is_proven(row.verification_method) or (row.provider == "gitlab" and row.verification_method == "credential_verified")
        )
        if not verified:
            continue
        if row.provider == "github" and github_connected:
            github_id = github_account_id(row.provider_user_id)
            if github_id:
                github_ids.add(github_id)
        elif row.provider == "gitlab":
            for instance in gitlab_instances:
                prefix = instance + "#"
                if row.provider_user_id.startswith(prefix):
                    gitlab_id = github_account_id(row.provider_user_id[len(prefix) :])
                    if gitlab_id:
                        gitlab_ids[instance].add(gitlab_id)
    unavailable = {}
    if not github_connected or not github_ids:
        unavailable["github"] = "disconnected" if not github_connected else "identity_unverified"
    if not gitlab_instances or not any(gitlab_ids.values()):
        unavailable["gitlab"] = "disconnected" if not gitlab_instances else "identity_unverified"
    return LinkedProviderIdentities(
        github_ids=frozenset(github_ids),
        gitlab_ids={instance: frozenset(values) for instance, values in gitlab_ids.items() if values},
        unavailable=unavailable,
    )

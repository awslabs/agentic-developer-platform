"""Bind an observed Atlassian account and sites to one current ADP owner."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.internal.github_oauth import binding_claim as account_claim
from src.internal.github_oauth import canonical, credential_digest
from src.internal.jira_provider import ACCOUNT_ID, CLOUD_ID, MAX_SITES, JiraSiteObservation, select_jira_site
from src.shared.config import get_settings
from src.shared.identity.oauth_providers import approved_oauth_scopes
from src.shared.models.base import new_uuid
from src.shared.models.vault import OAuthConnectionBinding, UserCredential


def protected_sites(binding):
    resources = binding.observed_resources
    if not isinstance(resources, list) or not 0 < len(resources) <= MAX_SITES:
        raise HTTPException(403, "Jira connection unqualified")
    sites = []
    for resource in resources:
        if not isinstance(resource, dict) or set(resource) != {"cloud_id", "scopes"} or not isinstance(resource["scopes"], list):
            raise HTTPException(403, "Jira connection unqualified")
        sites.append(JiraSiteObservation(resource["cloud_id"], tuple(resource["scopes"])))
    sites = tuple(sites)
    for site in sites:
        select_jira_site(sites, site.cloud_id)
    if len({site.cloud_id for site in sites}) != len(sites):
        raise HTTPException(403, "Jira connection unqualified")
    return sites


def new_jira_binding(credential, user, sites):
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id=credential.org_id,
        user_id=credential.user_id,
        provider="atlassian",
        revision=new_uuid(),
        credential_digest=credential_digest(credential),
        provider_account_id=user.account_id,
        # /me success establishes read:me; accessible-resources establishes
        # read:jira-work. offline_access is requested, not inferred as observed.
        observed_scopes=["read:jira-work", "read:me"],
        observed_resources=[{"cloud_id": site.cloud_id, "scopes": list(site.scopes)} for site in sorted(sites, key=lambda site: site.cloud_id)],
        verified_at=datetime.now(UTC),
    )
    protected_sites(binding)
    return binding


def binding_claim(binding):
    return {**account_claim(binding), "resources_sha256": hashlib.sha256(canonical(binding.observed_resources)).hexdigest()}


async def selected_jira_connection(db, *, user_id, tenant_id, expected=None):
    try:
        rows = (
            await db.execute(
                select(UserCredential, OAuthConnectionBinding)
                .join(OAuthConnectionBinding, OAuthConnectionBinding.credential_id == UserCredential.id)
                .where(
                    UserCredential.user_id == user_id,
                    UserCredential.org_id == tenant_id,
                    UserCredential.service == "jira",
                    UserCredential.provider == "agentcore-identity",
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).all()
    except SQLAlchemyError:
        raise HTTPException(503, "tool connection unavailable") from None
    if len(rows) != 1:
        raise HTTPException(403, "Jira connection unqualified")
    credential, binding = rows[0]
    scopes = approved_oauth_scopes("atlassian", credential.scopes)
    workload = get_settings().oauth_workload_name
    if (
        credential.team_id is not None
        or credential.domain_app_id is not None
        or credential.credential_type != "oauth_token"
        or credential.agent_consent is not True
        or not scopes
        or not {"read:jira-work", "read:me"} <= set(scopes)
        or not workload.endswith("-oauth-user")
        or credential.secret_arn != f"agentcore-identity:{workload}:atlassian"
        or credential.expires_at is not None
        and credential.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or binding.user_id != user_id
        or binding.org_id != tenant_id
        or binding.provider != "atlassian"
        or not isinstance(binding.provider_account_id, str)
        or not ACCOUNT_ID.fullmatch(binding.provider_account_id)
        or not isinstance(binding.revision, str)
        or not CLOUD_ID.fullmatch(binding.revision)
        or binding.observed_scopes != ["read:jira-work", "read:me"]
        or binding.verified_at.replace(tzinfo=UTC) > datetime.now(UTC)
        or binding.credential_digest != credential_digest(credential)
    ):
        raise HTTPException(403, "Jira connection unqualified")
    protected_sites(binding)
    claim = binding_claim(binding)
    if expected is not None and claim != expected:
        raise HTTPException(403, "Jira connection changed")
    return credential, binding, claim

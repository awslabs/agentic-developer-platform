"""Bind Slack provider observations to the current owned OAuth connection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.internal.github_oauth import canonical, credential_digest
from src.internal.slack_reader import TEAM_ID, USER_ID, SlackIdentityObservation, _scopes
from src.shared.config import get_settings
from src.shared.models.base import new_uuid
from src.shared.models.vault import OAuthConnectionBinding, UserCredential


def new_slack_binding(credential, observation: SlackIdentityObservation):
    return OAuthConnectionBinding(
        credential_id=credential.id,
        org_id=credential.org_id,
        user_id=credential.user_id,
        provider="slack",
        revision=new_uuid(),
        credential_digest=credential_digest(credential),
        provider_account_id=f"{observation.team_id}:{observation.user_id}",
        observed_scopes=list(observation.scopes),
        verified_at=datetime.now(UTC),
    )


def binding_claim(binding):
    return {
        "connection_id": binding.credential_id,
        "revision": binding.revision,
        "account_id": binding.provider_account_id,
        "scopes_sha256": hashlib.sha256(canonical(binding.observed_scopes)).hexdigest(),
        "credential_sha256": binding.credential_digest,
    }


async def selected_slack_connection(db, *, user_id, tenant_id, expected=None):
    try:
        rows = (
            await db.execute(
                select(UserCredential, OAuthConnectionBinding)
                .join(OAuthConnectionBinding, OAuthConnectionBinding.credential_id == UserCredential.id)
                .where(
                    UserCredential.user_id == user_id,
                    UserCredential.org_id == tenant_id,
                    UserCredential.service == "slack",
                    UserCredential.provider == "agentcore-identity",
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).all()
    except SQLAlchemyError:
        raise HTTPException(503, "tool connection unavailable") from None
    if len(rows) != 1:
        raise HTTPException(403, "Slack connection unqualified")
    credential, binding = rows[0]
    workload = get_settings().oauth_workload_name
    try:
        scopes = list(_scopes(",".join(binding.observed_scopes))) if isinstance(binding.observed_scopes, list) else None
    except (ValueError, TypeError, HTTPException):
        raise HTTPException(403, "Slack connection unqualified") from None
    saved = credential.scopes
    if (
        credential.team_id is not None
        or credential.domain_app_id is not None
        or credential.credential_type != "oauth_token"
        or credential.agent_consent is not True
        or not isinstance(saved, dict)
        or set(saved) != {"oauth_scopes"}
        or saved["oauth_scopes"] not in (["users:read"], ["users:read", "chat:write"])
        or not workload.endswith("-oauth-user")
        or credential.secret_arn != f"agentcore-identity:{workload}:slack"
        or credential.expires_at is not None
        and credential.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or binding.user_id != user_id
        or binding.org_id != tenant_id
        or binding.provider != "slack"
        or not isinstance(binding.provider_account_id, str)
        or ":" not in binding.provider_account_id
        or not TEAM_ID.fullmatch(binding.provider_account_id.split(":", 1)[0])
        or not USER_ID.fullmatch(binding.provider_account_id.split(":", 1)[1])
        or len(binding.provider_account_id) > 129
        or not isinstance(binding.revision, str)
        or len(binding.revision) != 36
        or not isinstance(binding.observed_scopes, list)
        or binding.observed_scopes != scopes
        or not set(saved["oauth_scopes"]) <= set(scopes or ())
        or binding.verified_at.replace(tzinfo=UTC) > datetime.now(UTC)
        or binding.credential_digest != credential_digest(credential)
    ):
        raise HTTPException(403, "Slack connection unqualified")
    claim = binding_claim(binding)
    if expected is not None and claim != expected:
        raise HTTPException(403, "Slack connection changed")
    return credential, binding, claim

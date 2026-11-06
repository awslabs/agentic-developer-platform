"""Compatibility evidence from an owner-bound completed Identity/MCP observation."""

import hashlib
import json
import re

from sqlalchemy import select

from src.internal.github_oauth import credential_digest
from src.shared.models.vault import OAuthConnectionAttempt


def observation_digest(binding):
    observed = {
        "credential_id": binding.credential_id,
        "tenant_id": binding.org_id,
        "user_id": binding.user_id,
        "provider": binding.provider,
        "credential_sha256": binding.credential_digest,
        "provider_account_id": binding.provider_account_id,
        "scopes": binding.observed_scopes,
        "resources": binding.observed_resources,
    }
    return hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def verified_connection(db, *, credential, binding, config_revision):
    if (
        credential is None
        or binding is None
        or not isinstance(config_revision, str)
        or not re.fullmatch(r"[0-9a-f]{64}", config_revision)
        or binding.provider != "atlassian-mcp"
        or credential.service != "atlassian-mcp"
        or credential.provider != "agentcore-identity"
        or credential.strict is not True
        or credential.expires_at is not None
        or binding.credential_id != credential.id
        or binding.org_id != credential.org_id
        or binding.user_id != credential.user_id
        or binding.credential_digest != credential_digest(credential)
        or credential.agent_consent is not True
    ):
        return False
    proof = await db.scalar(
        select(OAuthConnectionAttempt.state_digest)
        .where(
            OAuthConnectionAttempt.provider == "atlassian-mcp",
            OAuthConnectionAttempt.org_id == binding.org_id,
            OAuthConnectionAttempt.user_id == binding.user_id,
            OAuthConnectionAttempt.identity_completed_at.is_not(None),
            OAuthConnectionAttempt.consumed_at.is_not(None),
            OAuthConnectionAttempt.superseded_at.is_(None),
            OAuthConnectionAttempt.identity_completed_at <= OAuthConnectionAttempt.consumed_at,
            OAuthConnectionAttempt.connection_snapshot["config_revision"].as_string() == config_revision,
            OAuthConnectionAttempt.connection_snapshot["completed_connection"]["id"].as_string() == credential.id,
            OAuthConnectionAttempt.connection_snapshot["provider_observation_sha256"].as_string() == observation_digest(binding),
        )
        .limit(1)
    )
    return proof is not None

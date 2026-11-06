"""Current owned OAuth connection decisions shared by calls and discovery."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.shared.identity.oauth_providers import PROVIDER_SCOPES, VENDOR_TOOL_SCOPES, approved_oauth_scopes
from src.shared.models.vault import UserCredential


async def connection_scopes(db, *, tenant_id, owner_id, tool, workload_name):
    required = VENDOR_TOOL_SCOPES.get(tool)
    if required is None:
        return [], "missing_scope"
    provider = "atlassian" if tool.startswith("jira___") else tool.split("___", 1)[0]
    service, _ = PROVIDER_SCOPES[provider]
    try:
        credential = await db.scalar(
            select(UserCredential).where(
                UserCredential.org_id == tenant_id,
                UserCredential.user_id == owner_id,
                UserCredential.team_id.is_(None),
                UserCredential.domain_app_id.is_(None),
                UserCredential.service == service,
                UserCredential.provider == "agentcore-identity",
                UserCredential.credential_type == "oauth_token",
            )
        )
    except SQLAlchemyError:
        raise HTTPException(503, "tool connection unavailable") from None
    if (
        credential is None
        or credential.org_id != tenant_id
        or credential.user_id != owner_id
        or credential.team_id is not None
        or credential.domain_app_id is not None
        or credential.service != service
        or credential.provider != "agentcore-identity"
        or credential.credential_type != "oauth_token"
        or credential.agent_consent is not True
        or not workload_name.endswith("-oauth-user")
        or credential.secret_arn != f"agentcore-identity:{workload_name}:{provider}"
        or credential.expires_at is not None
        and credential.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
    ):
        return [], "disconnected"
    from src.internal.github_oauth import is_github_app_grant

    scopes = ["read:user"] if provider == "github" and is_github_app_grant(credential.scopes) else approved_oauth_scopes(provider, credential.scopes)
    if scopes is None or required not in scopes:
        return [], "missing_scope"
    return scopes, None

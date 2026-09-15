"""Resolve explicitly approved user credentials through the existing vault ACLs."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.vault import UserCredential
from src.shared.services.canonical_user import resolve_canonical_user
from src.shared.services.credential_resolver import CredentialNotFoundError, CredentialResolver

from .execution_policy import ExecutionPolicy


async def resolve_user_credential(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str,
    credential_id: str | None = None,
    service: str | None = None,
    label: str | None = None,
) -> UserCredential:
    user = await resolve_canonical_user(session, user_id, calling_endpoint="execution-policy-credential")
    if user is None or user.org_id != org_id:
        raise CredentialNotFoundError("credential unavailable")
    if credential_id is not None:
        row = await session.scalar(select(UserCredential).where(UserCredential.id == credential_id, UserCredential.org_id == org_id))
        if row is None:
            raise CredentialNotFoundError("credential unavailable")
        service, label = row.service, row.label
    if not isinstance(service, str) or not service:
        raise CredentialNotFoundError("credential unavailable")
    credential = await CredentialResolver(session).resolve(org_id=org_id, user_id=user.id, team_id=user.team_id or None, service=service, label=label)
    if credential_id is not None and credential.id != credential_id:
        raise CredentialNotFoundError("credential unavailable")
    expiry = credential.expires_at
    if expiry is not None and (expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=UTC)) <= datetime.now(UTC):
        raise CredentialNotFoundError("credential unavailable")
    return credential


async def validate_user_credential_authority(session: AsyncSession, *, policy: ExecutionPolicy, user_id: str) -> None:
    if policy.user_credentials is None:
        return
    for credential_id in policy.user_credentials.vault_credential_ids:
        await resolve_user_credential(session, org_id=policy.org_id, user_id=user_id, credential_id=credential_id)

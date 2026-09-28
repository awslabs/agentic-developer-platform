"""Owner-authorized lifecycle of workspace delegation and provider evidence."""

import asyncio
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import delete, select

from src.shared.models.audit import AuditLog
from src.shared.models.vault import CredentialValidationEvidence, CredentialWorkspaceDelegation, UserCredential

from .provider_validation import ValidationUnavailableError, configured_validator
from .vault_evidence import validation_digest
from .vault_service import _get_owned_credential


async def owned_current(session, caller, credential_id, *, lock=False):
    # Refresh before using the existing vault mutation gate. A lock serializes
    # short lifecycle writes; callers must not hold it across provider I/O.
    query = (
        select(UserCredential)
        .where(
            UserCredential.id == credential_id,
            UserCredential.org_id == caller.org_id,
        )
        .execution_options(populate_existing=True)
    )
    await session.scalar(query.with_for_update() if lock else query)
    return await _get_owned_credential(credential_id, session, caller)


async def set_workspace_delegation(session, caller, *, credential_id, workspace_id, active):
    credential = await owned_current(session, caller, credential_id, lock=True)
    row = await session.scalar(
        select(CredentialWorkspaceDelegation)
        .where(
            CredentialWorkspaceDelegation.credential_id == credential.id,
            CredentialWorkspaceDelegation.org_id == caller.org_id,
            CredentialWorkspaceDelegation.workspace_id == workspace_id,
        )
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if row is None:
        row = CredentialWorkspaceDelegation(
            credential_id=credential.id,
            org_id=caller.org_id,
            workspace_id=workspace_id,
            delegated_by=caller.user_id,
            delegated_at=now,
            revoked_at=None if active else now,
        )
        session.add(row)
    elif active and row.revoked_at is not None:
        row.delegated_by, row.delegated_at, row.revoked_at = caller.user_id, now, None
    elif not active:
        row.revoked_at = now
    if not active:
        await session.execute(
            delete(CredentialValidationEvidence).where(
                CredentialValidationEvidence.credential_id == credential.id,
                CredentialValidationEvidence.workspace_id == workspace_id,
                CredentialValidationEvidence.org_id == caller.org_id,
            )
        )
    session.add(
        AuditLog(
            org_id=caller.org_id,
            actor_id=caller.user_id,
            event_type="vault_workspace_delegated" if active else "vault_workspace_withdrawn",
            details={"credential_id": credential.id, "workspace_id": workspace_id},
        )
    )
    await session.commit()
    return {"credential_id": credential.id, "workspace_id": workspace_id, "active": active}


class ValidationConflictError(RuntimeError):
    pass


async def _delegated(session, caller, credential_id, workspace_id):
    row = await session.scalar(
        select(CredentialWorkspaceDelegation)
        .where(
            CredentialWorkspaceDelegation.credential_id == credential_id,
            CredentialWorkspaceDelegation.org_id == caller.org_id,
            CredentialWorkspaceDelegation.workspace_id == workspace_id,
            CredentialWorkspaceDelegation.revoked_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ValidationConflictError("active workspace delegation required")
    return row


def _snapshot(credential):
    return (
        credential.secret_arn,
        credential.service,
        credential.label,
        credential.credential_type,
        credential.user_id,
        credential.team_id,
        credential.domain_app_id,
        credential.expires_at,
    )


async def validate_workspace_credential(session, sm, settings, caller, *, credential_id, workspace_id):
    credential = await owned_current(session, caller, credential_id, lock=True)
    delegation = await _delegated(session, caller, credential_id, workspace_id)
    snapshot = _snapshot(credential)
    generation = (delegation.id, delegation.delegated_at)
    # Publish a new failed-closed generation before I/O. Failed revalidation must
    # not leave an older positive reading usable, and a late concurrent result
    # must never overwrite the newer attempt or restore a withdrawn delegation.
    await session.execute(
        delete(CredentialValidationEvidence).where(
            CredentialValidationEvidence.credential_id == credential_id,
            CredentialValidationEvidence.org_id == caller.org_id,
            CredentialValidationEvidence.workspace_id == workspace_id,
        )
    )
    attempt = str(uuid.uuid4())
    session.add(
        CredentialValidationEvidence(
            id=attempt,
            credential_id=credential_id,
            org_id=caller.org_id,
            workspace_id=workspace_id,
            credential_valid=False,
            permissions_sufficient=False,
            quota_available=False,
            checked_at=datetime.now(UTC),
            detail="validation in progress",
        )
    )
    await session.commit()
    try:
        validator = configured_validator(settings, org_id=caller.org_id, workspace_id=workspace_id, service=credential.service)
        version = await asyncio.to_thread(sm.current_version_id, snapshot[0])
        if not version:
            raise ValidationUnavailableError("current credential version unavailable")
        material, served_version = await asyncio.to_thread(sm.get_secret_at_version, snapshot[0], version)
        if served_version != version:
            raise ValidationConflictError("credential version changed")
        reading = await asyncio.to_thread(validator.validate, material, credential_type=snapshot[3], user_id=caller.user_id, label=snapshot[2])
        del material
        if await asyncio.to_thread(sm.current_version_id, snapshot[0]) != version:
            raise ValidationConflictError("credential version changed")
    except (ValidationUnavailableError, ValidationConflictError):
        raise
    except Exception:
        raise ValidationUnavailableError("provider validation unavailable") from None
    current = await owned_current(session, caller, credential_id, lock=True)
    current_delegation = await _delegated(session, caller, credential_id, workspace_id)
    row = await session.scalar(
        select(CredentialValidationEvidence)
        .where(
            CredentialValidationEvidence.id == attempt,
            CredentialValidationEvidence.org_id == caller.org_id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None or _snapshot(current) != snapshot or (current_delegation.id, current_delegation.delegated_at) != generation:
        raise ValidationConflictError("credential authority changed during validation")
    row.validated_version_id = version
    report = asdict(reading)
    row.provider_account_id = report.pop("provider_account_id")
    for key, value in report.items():
        setattr(row, key, value)
    session.add(
        AuditLog(
            org_id=caller.org_id,
            actor_id=caller.user_id,
            event_type="vault_workspace_validated",
            details={"credential_id": credential_id, "workspace_id": workspace_id, "validation_id": attempt, "version_id": version},
        )
    )
    await session.commit()
    digest = validation_digest(**{key: value for key, value in report.items() if key != "checked_at"})
    return {
        "credential_id": credential_id,
        "workspace_id": workspace_id,
        "validated_version_id": version,
        "provider_account_id": row.provider_account_id,
        "validation": report,
        "report_digest": digest,
    }

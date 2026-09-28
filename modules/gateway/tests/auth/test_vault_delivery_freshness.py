"""Independent-session revocation and current-version attestation regressions."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.vault_delivery import DELIVERY_PERMISSION, DeliveryRefusedError, OperationBinding, deliver_credential
from src.auth.vault_evidence import read_credential_evidence, validation_digest
from src.shared.models.vault import CredentialValidationEvidence, UserCredential
from tests.auth import test_vault_evidence as fixtures
from tests.auth.test_vault_evidence import ORG, VERSION, WORKSPACE, _cred, _delegate, _validation
from tests.operation_delivery_support import grant_operation, operation_storage

db, engine, sm = fixtures.db, fixtures.engine, fixtures.sm


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "rotated-version"])
async def test_old_or_unversioned_validation_cannot_attest_current_material(db, sm, version):
    credential = await _cred(db)
    await _delegate(db, credential)
    validation = await _validation(db, credential)
    digest = validation_digest(
        credential_valid=validation.credential_valid,
        permissions_sufficient=validation.permissions_sufficient,
        quota_available=validation.quota_available,
        observed_capacity=validation.observed_capacity,
        detail=validation.detail,
    )
    args = dict(
        org_id=ORG, workspace_id=WORKSPACE, credential_id=credential.id, service=None, label=None, principal="user:user-alice", report_digest=digest
    )
    assert await read_credential_evidence(db, sm, **args) is not None
    if version is None:
        await db.execute(update(CredentialValidationEvidence).values(validated_version_id=None))
        await db.commit()
    else:
        sm.current_version_id.return_value = version
    assert await read_credential_evidence(db, sm, **args) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["expires_at", "secret_arn", "service", "label"])
async def test_separate_session_revocation_or_binding_change_during_fetch_refuses(db, engine, mutation):
    credential = await _cred(db, expires_at=datetime.now(UTC) + timedelta(hours=1))
    await _delegate(db, credential)
    await _validation(db, credential)
    principal = "developer-invocation#3"
    await operation_storage(engine)
    await grant_operation(
        db,
        org=ORG,
        workspace=WORKSPACE,
        holder=principal,
        operation="operation",
        attempt="attempt",
        job="job",
        credential=credential.id,
        service=credential.service,
        label=credential.label,
    )
    await db.commit()
    entered, resume = threading.Event(), threading.Event()

    def fetch(_arn: str, _version_id: str) -> tuple[str, str]:
        entered.set()
        assert resume.wait(10)
        return "synthetic-material-never-returned", VERSION

    helper = MagicMock()
    helper.get_secret_at_version.side_effect = fetch
    helper.current_version_id.return_value = VERSION
    task = asyncio.create_task(
        deliver_credential(
            db,
            helper,
            binding=OperationBinding(
                operation_id="operation",
                attempt_id="attempt",
                job_id="job",
                org_id=ORG,
                workspace_id=WORKSPACE,
                provider="aws",
                provider_account_id="123456789012",
            ),
            credential_id=credential.id,
            service=credential.service,
            label=credential.label,
            recipient=principal,
            authenticated_recipient=principal,
            granted_permissions={DELIVERY_PERMISSION},
            refresh_executor=_unchanged_executor,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        value = datetime.now(UTC) - timedelta(seconds=5) if mutation == "expires_at" else "changed-binding"
        async with async_sessionmaker(engine, expire_on_commit=False)() as other:
            await other.execute(update(UserCredential).where(UserCredential.id == credential.id).values(**{mutation: value}))
            await other.commit()
    finally:
        resume.set()
    with pytest.raises(DeliveryRefusedError):
        await task


async def _unchanged_executor():
    """Unit tests hold executor authority fixed; paired HTTP tests revoke it live."""
    return frozenset({DELIVERY_PERMISSION})

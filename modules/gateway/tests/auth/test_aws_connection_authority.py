"""Provider-I/O interleavings must not revive revoked AWS connection evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth import aws_connect_routes as routes
from src.auth.vault_schemas import CredentialCreate, CredentialUpdate
from src.auth.vault_service import DuplicateCredentialError, create_credential, update_credential
from src.internal import sts_assume_service
from src.internal.sts_assume_service import STSAssumeError
from src.shared.models.vault import UserCredential
from tests.auth import test_vault_routes as fixtures
from tests.auth.test_vault_routes import ALICE, _insert_cred

db, engine = fixtures.db, fixtures.engine
ROLE = "arn:aws:iam::123456789012:role/Verify"
IDENTITY = "arn:aws:sts::123456789012:assumed-role/Verify/test"


def test_sts_result_preserves_provider_attested_identity(monkeypatch):
    client = MagicMock()
    client.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "synthetic-access",
            "SecretAccessKey": "synthetic-secret",
            "SessionToken": "synthetic-session",
            "Expiration": datetime.now(UTC) + timedelta(minutes=15),
        },
        "AssumedRoleUser": {"Arn": IDENTITY},
    }
    monkeypatch.setattr(sts_assume_service.boto3, "client", lambda *args, **kwargs: client)
    result = sts_assume_service.assume_role(
        role_arn=ROLE,
        external_id="synthetic-trust",
        session_duration_seconds=900,
        default_region="us-east-1",
        user_id=ALICE.user_id,
        agent_id="verify",
        task_id="verify",
        label="verify",
    )
    assert result.assumed_role_arn == IDENTITY


@pytest.fixture
async def connection(db, monkeypatch):
    credential = UserCredential(
        id="66666666-7777-4888-8999-aaaaaaaaaaaa",
        org_id=ALICE.org_id,
        user_id=ALICE.user_id,
        service="aws",
        label="verify",
        credential_type="aws_role",
        secret_arn="synthetic-connection",
        aws_external_id="synthetic-trust",
        scopes={"account_id": "123456789012", "role_arn": ROLE, "status": "pending"},
    )
    db.add(credential)
    await db.commit()
    monkeypatch.setattr(routes, "_resolve_user_id", AsyncMock(return_value=ALICE.user_id))
    monkeypatch.setattr(routes, "resolve_effective_org_id", AsyncMock(return_value=ALICE.org_id))
    monkeypatch.setattr(routes, "assume_role", MagicMock(return_value=SimpleNamespace(assumed_role_arn=IDENTITY)))
    monkeypatch.setattr(routes, "_probe_routing_capability", AsyncMock(return_value=(False, "user_pinned")))
    helper = MagicMock()
    helper.current_version_id.return_value = "version-1"
    helper.get_secret_at_version.return_value = (
        json.dumps({"account_id": "123456789012", "role_arn": ROLE, "external_id": "synthetic-trust"}),
        "version-1",
    )
    return credential, helper


async def verify(db, connection):
    credential, helper = connection
    return await routes.connect_verify(routes.ConnectVerifyRequest(credential_id=credential.id, fresh=True), ALICE, db, helper)


@pytest.mark.asyncio
async def test_late_success_cannot_overwrite_newer_failed_verification(db, engine, connection, monkeypatch):
    entered, resume = asyncio.Event(), asyncio.Event()
    credential_id = connection[0].id

    async def delayed_probe(**kwargs):
        entered.set()
        await resume.wait()
        return False, "user_pinned"

    monkeypatch.setattr(routes, "_probe_routing_capability", delayed_probe)
    routes.assume_role.side_effect = [SimpleNamespace(assumed_role_arn=IDENTITY), STSAssumeError("revoked", code="AccessDenied")]
    older = asyncio.create_task(verify(db, connection))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with async_sessionmaker(engine, expire_on_commit=False)() as newer:
            result = await verify(newer, connection)
        assert result.status == "failed"
    finally:
        resume.set()
    with pytest.raises(HTTPException) as conflict:
        await older
    assert conflict.value.status_code == 409
    await db.rollback()
    current = await db.get(UserCredential, credential_id)
    assert current.aws_verified_at is None
    assert current.scopes["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["label", "expires_at", "user_id", "delete", "version"])
async def test_verification_refuses_authority_changed_during_provider_io(db, engine, connection, monkeypatch, mutation):
    entered, resume = asyncio.Event(), asyncio.Event()

    async def delayed_probe(**kwargs):
        entered.set()
        await resume.wait()
        return False, "user_pinned"

    monkeypatch.setattr(routes, "_probe_routing_capability", delayed_probe)
    pending = asyncio.create_task(verify(db, connection))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if mutation == "version":
            connection[1].current_version_id.return_value = "version-2"
        else:
            async with async_sessionmaker(engine, expire_on_commit=False)() as other:
                if mutation == "delete":
                    statement = delete(UserCredential).where(UserCredential.id == connection[0].id)
                else:
                    value = {"label": "renamed", "user_id": "user-bob", "expires_at": datetime.now(UTC) - timedelta(seconds=1)}[mutation]
                    statement = update(UserCredential).where(UserCredential.id == connection[0].id).values(**{mutation: value})
                await other.execute(statement)
                await other.commit()
    finally:
        resume.set()
    with pytest.raises(HTTPException) as conflict:
        await pending
    assert conflict.value.status_code in {404, 409}


@pytest.mark.asyncio
async def test_forged_account_metadata_does_not_reach_sts(db, connection):
    credential, helper = connection
    credential.scopes = {**credential.scopes, "account_id": "999999999999"}
    await db.commit()
    material, version = helper.get_secret_at_version.return_value
    helper.get_secret_at_version.return_value = (json.dumps({**json.loads(material), "account_id": "999999999999"}), version)
    with pytest.raises(HTTPException) as conflict:
        await verify(db, connection)
    assert conflict.value.status_code == 409
    routes.assume_role.assert_not_called()
    assert credential.aws_verified_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [None, "arn:aws:sts::999999999999:assumed-role/Verify/test", "arn:aws:sts::123456789012:assumed-role/Other/test"],
)
async def test_sts_must_attest_expected_account_and_role(db, connection, identity):
    routes.assume_role.return_value = SimpleNamespace(assumed_role_arn=identity)
    with pytest.raises(HTTPException) as conflict:
        await verify(db, connection)
    assert conflict.value.status_code == 409
    assert connection[0].aws_verified_at is None


@pytest.mark.asyncio
async def test_expired_connection_is_refused_before_secret_read(db, connection):
    connection[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException):
        await verify(db, connection)
    connection[1].current_version_id.assert_not_called()
    routes.assume_role.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_patch_invalidates_verification_and_attempt(db, connection):
    assert (await verify(db, connection)).status == "verified"
    changed = await update_credential(connection[0].id, CredentialUpdate(label="renamed"), db, ALICE)
    assert changed.aws_verified_at is None
    assert changed.aws_verified_version_id is None
    assert changed.aws_verification_attempt is None
    assert changed.aws_verified_binding is None


@pytest.mark.asyncio
async def test_retry_committing_after_rollback_absence_check_keeps_its_secret(db, engine, monkeypatch):
    existing = await _insert_cred(db, user_id=ALICE.user_id, service="nebius", label="duplicate")
    existing_id = existing.id
    operation = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
    data = CredentialCreate(service="nebius", label="duplicate", credential_type="api_key", value="synthetic-value")
    helper = MagicMock()
    helper.create_secret.return_value = "shared-operation-secret"
    helper.get_secret.return_value = data.value
    original_execute = db.execute
    lookups = 0

    async def interleave(statement, *args, **kwargs):
        nonlocal lookups
        result = await original_execute(statement, *args, **kwargs)
        if operation in statement.compile().params.values():
            lookups += 1
            if lookups == 2:
                # A has captured the absent-row result. Remove the label conflict,
                # then B reuses A's deterministic secret and commits before A resumes.
                async with async_sessionmaker(engine, expire_on_commit=False)() as other:
                    await other.execute(update(UserCredential).where(UserCredential.id == existing_id).values(label="renamed"))
                    await other.commit()
                    await create_credential(data, other, ALICE, helper, credential_id=operation)
        return result

    monkeypatch.setattr(db, "execute", interleave)
    with pytest.raises(DuplicateCredentialError):
        await create_credential(data, db, ALICE, helper, credential_id=operation)
    helper.delete_secret.assert_not_called()
    await db.rollback()
    async with async_sessionmaker(engine, expire_on_commit=False)() as other:
        assert (await other.get(UserCredential, operation)).secret_arn == "shared-operation-secret"


@pytest.fixture(autouse=True)
def ownership_probe(monkeypatch):
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    monkeypatch.setattr(routes, "require_external_id_enforcement", MagicMock())

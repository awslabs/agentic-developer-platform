"""Durable capture subjects and retained deadlines exercise real authorization."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.models.base import Base as L5Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.models.preparation import AdmissionPreparation
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth import l5_delegation
from src.auth.l5_delegation import issue_l5_work_delegation
from src.l5_service_identity import worker_id
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.models import ProductOperation, ProductProjectBinding, ProductSourceBinding, ProductSourceSnapshot
from src.products.source_capture_jobs import CaptureSubject, current_capture, update_capture
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation, UserCredential
from tests.products.test_l5_snapshots import snapshot_fixture


def retained_subject(owner, *, scheduled=False):
    return CaptureSubject.model_validate_json(
        CaptureSubject(
            user_id="iam-agent:security" if scheduled else owner.user_id,
            org_id="platform-service" if scheduled else owner.org_id,
            team_id="" if scheduled else owner.team_id,
            account_type="service" if scheduled else "human",
            auth_source="iam" if scheduled else "jwt",
            scope="internal" if scheduled else "",
            agent_registry_id=worker_id("security") if scheduled else "",
        ).model_dump_json()
    )


@pytest.fixture
async def capture_authority(test_client, db_session, test_engine, app, source_member, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    grant, request, captured, _, _, _ = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    credential = await db_session.get(UserCredential, grant.credential_id)
    credential.aws_verified_version_id = "credential-version"
    mapping = ChannelTenantMap(org_id=grant.tenant_id, provider="github", provider_scope_id="789", installation_id="456", ownership_disputed=False)
    db_session.add(mapping)
    await db_session.flush()
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == grant.project_id))
    binding.kind, binding.provider, binding.credential_id, binding.installation_id = "repository", "github", None, mapping.id
    binding.namespace, binding.native_id = "789", "123"
    binding.desired = binding.observed = {"source_revision": "a" * 40}
    source = request.source_ref.model_copy(
        update={"source_connection_id": mapping.id, "record_type": "repository_commit", "record_id": "123", "record_revision": "a" * 40}
    )
    request = request.model_copy(update={"source_ref": source, "exclusions": []})
    captured = captured.model_copy(update={"source_ref": source, "exclusions": [], "expires_at": request.retain_until})
    connection_ref = {
        **source.model_dump(),
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": "credential-version",
    }
    grant.service_id = worker_id("security")
    grant.operations = ["product:read", "source:pin", "source:read", "credential:session"]
    grant.capabilities = ["code_review"]
    grant.sources = [{**connection_ref, "record_type": "credential"}, source.model_dump()]
    relationship = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.product_id == grant.product_id))
    configuration = Configuration(
        tenant_id=grant.tenant_id,
        configuration_id="capture-configuration",
        product_id=grant.product_id,
        project_id=grant.project_id,
        service_id="security",
        relationship_revision=str(relationship.relationship_revision),
        owner_id=grant.created_by,
        current_revision=1,
        handoff_state="linked",
    )
    version = ConfigurationVersion(
        tenant_id=grant.tenant_id,
        configuration_id=configuration.configuration_id,
        revision=1,
        settings={
            "enabled": True,
            "region": "us-east-1",
            "connection_ref": connection_ref,
            "source_refs": [source.model_dump()],
            "capabilities": ["code_review"],
            "visibility": grant.visibility,
        },
        inherited_fields=[],
        gaps=[],
        state="ready",
        hash="a" * 64,
    )
    grant.execution_scope = {
        **{
            field: getattr(grant, field)
            for field in (
                "tenant_id",
                "product_id",
                "project_id",
                "service_id",
                "credential_id",
                "created_by",
                "binding_generation",
                "visibility",
            )
        },
        "configuration_id": configuration.configuration_id,
        "configuration_revision": 1,
        "configuration_hash": version.hash,
        "operations": grant.operations,
        "capabilities": grant.capabilities,
        "sources": grant.sources,
        "region": "us-east-1",
        "provider_account_id": None,
        "provider_role_arn": None,
    }
    db_session.add_all([configuration, version])
    await db_session.commit()
    # Only connector I/O is replaced. Product membership, current consent,
    # source bindings, credential visibility and both model_copy chains are real.
    monkeypatch.setattr("src.products.source_routes.assert_installation_owned_by", AsyncMock())
    capture = AsyncMock(return_value=captured)
    monkeypatch.setattr("src.products.l5_admission_capture.capture_source", capture)
    monkeypatch.setattr("src.products.l5_admission_capture.get_snapshot_objects", lambda: object())
    return SimpleNamespace(
        grant=grant,
        request=request,
        captured=captured,
        capture=capture,
        credential=credential,
        source=source.model_dump(),
        configuration=configuration,
        relationship=relationship,
        owner=source_member,
        connection=connection_ref,
        binding=SimpleNamespace(
            project_id=grant.project_id,
            service_id="security",
            generation=grant.binding_generation,
            configuration_id=configuration.configuration_id,
            binding_id="capture-binding",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("revocation", [None, "before_capture", "during_capture"])
async def test_retained_subject_runs_real_capture_authority_and_observes_membership_revocation(
    capture_authority,
    db_session,
    scheduled,
    revocation,
):
    selected = capture_authority
    grant = selected.grant
    actor = retained_subject(selected.owner, scheduled=scheduled)
    frozen_actor = actor.model_dump()
    receipt = EventReceipt(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        service_id="security",
        trigger_id="nightly",
        event_id="schedule:" + datetime.now(UTC).isoformat(),
        event_kind="schedule",
        configuration_id=selected.configuration.configuration_id,
        configuration_revision=1,
        run_input={"capability": "code_review", "sources": [selected.source]},
        state="pending",
        received_at=datetime.now(UTC),
    )
    receipt.idempotency_key = semantic_key(receipt)
    key = receipt.idempotency_key if scheduled else "manual-capture"
    verified = {
        "scope": {
            "tenant_id": grant.tenant_id,
            "product_id": grant.product_id,
            "project_id": grant.project_id,
            "relationship_revision": str(selected.relationship.relationship_revision),
        },
        "service_id": "security",
        "actor_id": grant.created_by,
        "actor_type": "service" if scheduled else "human",
        "configuration_id": selected.configuration.configuration_id,
        "configuration_revision": 1,
        "event_id": receipt.event_id,
        "source_refs": [selected.source],
        "trigger": {"trigger_id": "nightly", "source_refs": [selected.source]},
        "exclusions": [],
        "max_bytes": selected.request.max_bytes,
        "max_files": selected.request.max_files,
    }
    if scheduled:
        db_session.add(receipt)
    db_session.add(
        AdmissionPreparation(
            tenant_id=grant.tenant_id,
            product_id=grant.product_id,
            project_id=grant.project_id,
            service_id="security",
            actor_id=grant.created_by,
            operation="submit",
            idempotency_key=key,
            request_hash="b" * 64,
            verified=verified,
            deadline_at=selected.request.retain_until,
        )
    )
    await db_session.commit()

    async def revoke():
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == selected.owner.user_id))
        await db_session.commit()

    if revocation == "before_capture":
        await revoke()
    elif revocation == "during_capture":

        async def capture_then_revoke(*_args):
            await revoke()
            return selected.captured

        selected.capture.side_effect = capture_then_revoke

    client = GatewayAdmissionSourceClient(
        db_session,
        actor,
        grant.id,
        verified,
        "submit",
        key,
        service_id="security",
        run_input=receipt.run_input,
        capture_fence=AsyncMock(),
    )
    if revocation:
        with pytest.raises(HTTPException) as denied:
            await client.pin_source(grant.product_id, grant.project_id, selected.request.model_dump(mode="json"), idempotency_key="retained-pin")
        assert denied.value.status_code == 404
        assert selected.capture.await_count == (revocation == "during_capture")
        assert await db_session.scalar(select(func.count()).select_from(ProductSourceSnapshot)) == 0
    else:
        snapshot = await client.pin_source(
            grant.product_id,
            grant.project_id,
            selected.request.model_dump(mode="json"),
            idempotency_key="retained-pin",
        )
        await db_session.commit()
        assert snapshot["source_ref"] == selected.source and selected.capture.await_count == 1
        assert await db_session.get(ProductSourceSnapshot, (grant.tenant_id, snapshot["snapshot_id"])) is not None
    assert actor.model_dump() == frozen_actor
    assert actor.is_admin is False and "expires_at" not in actor.model_dump()
    if scheduled:
        assert actor.account_type == "service" and actor.org_id == "platform-service"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "new_key", "wrong_grant", "changed_limit", "expired_preparation", "shortened_parent", "shortened_child"])
async def test_existing_consent_replay_uses_the_original_preparation_window(capture_authority, db_session, monkeypatch, failure):
    selected = capture_authority
    initial = datetime.now(UTC)
    clock = {"now": initial}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"] if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr(l5_delegation, "datetime", Clock)
    selected.grant.expires_at = initial + timedelta(seconds=3630)
    await db_session.commit()
    actor = retained_subject(selected.owner)

    async def issue(key="original-assessment"):
        return await issue_l5_work_delegation(
            db_session,
            actor,
            selected.grant,
            selected.credential,
            selected.binding,
            selected.connection,
            [selected.source],
            "code_review",
            3600,
            "submit",
            key,
        )

    child = await issue()
    child_id, child_expiry = child.id, child.expires_at
    original_deadline = initial + timedelta(seconds=3600)
    frozen = {
        "delegation_id": child.id,
        "delegation_version": child.revision,
        "binding_generation": selected.binding.generation,
        "limits": {"deadline_seconds": 3600},
        "trigger": {"source_refs": [selected.source]},
    }
    if failure == "wrong_grant":
        frozen["delegation_id"] = "another-grant"
    elif failure == "changed_limit":
        frozen["limits"] = {"deadline_seconds": 600}
    preparation = AdmissionPreparation(
        tenant_id=selected.grant.tenant_id,
        product_id=selected.grant.product_id,
        project_id=selected.grant.project_id,
        service_id="security",
        actor_id=selected.owner.user_id,
        operation="submit",
        idempotency_key="original-assessment",
        request_hash="c" * 64,
        verified=frozen,
        deadline_at=initial + timedelta(seconds=200) if failure == "expired_preparation" else original_deadline,
    )
    db_session.add(preparation)
    if failure == "shortened_parent":
        selected.grant.expires_at = initial + timedelta(seconds=3500)
    elif failure == "shortened_child":
        child.expires_at = initial + timedelta(seconds=3500)
    await db_session.commit()
    clock["now"] = initial + timedelta(seconds=300)
    if failure:
        with pytest.raises(HTTPException) as denied:
            await issue("new-assessment" if failure == "new_key" else "original-assessment")
        assert denied.value.status_code == 403 and denied.value.detail["code"] == "delegation_denied"
    else:
        replay = await issue()
        assert replay.id == child_id and replay.expires_at.replace(tzinfo=UTC) == child_expiry
        assert preparation.deadline_at.replace(tzinfo=UTC) == original_deadline
        assert selected.grant.expires_at.replace(tzinfo=UTC) == initial + timedelta(seconds=3630)
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["new_owner", "cancelled"])
@pytest.mark.parametrize("terminal_state", ["succeeded", "pending"])
async def test_stale_worker_cannot_write_after_lease_transfer_or_cancellation(
    capture_authority,
    db_session,
    test_engine,
    replacement,
    terminal_state,
):
    grant = capture_authority.grant
    now = datetime.now(UTC)
    job = ProductOperation(
        tenant_id=grant.tenant_id,
        id="cas-capture-job",
        actor_id=grant.created_by,
        product_id=grant.product_id,
        project_id=grant.project_id,
        operation="captureL5Source",
        key_hash="d" * 64,
        request_hash="e" * 64,
        request={},
        receipt={"stage": "capturing"},
        state="running",
        lease_owner="old-worker",
        lease_until=now + timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        attempt_count=1,
    )
    db_session.add(job)
    await db_session.commit()
    identity = grant.tenant_id, job.id, "old-worker"
    stale = await current_capture(db_session, identity)
    await db_session.commit()
    assert stale.lease_owner == "old-worker"
    values = (
        {"state": "running", "lease_owner": "new-worker", "attempt_count": 2}
        if replacement == "new_owner"
        else {
            "state": "cancelled",
            "lease_owner": None,
            "lease_until": None,
        }
    )
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        await independent.execute(update(ProductOperation).where(ProductOperation.id == identity[1]).values(**values))
        await independent.commit()
    # The old worker already passed its read check. Only the write's predicate
    # can stop it from overwriting this now-different durable row.
    assert stale.lease_owner == "old-worker"
    with pytest.raises(HTTPException) as denied:
        await update_capture(
            db_session,
            identity,
            {"state": terminal_state, "lease_owner": None, "lease_until": None},
            require_live=terminal_state == "succeeded",
        )
    assert denied.value.status_code == 409 and denied.value.detail["code"] == "source_capture_lease_changed"
    current = await db_session.get(ProductOperation, identity[:2], populate_existing=True)
    assert current.state == values["state"] and current.lease_owner == values["lease_owner"]
    assert current.attempt_count == (2 if replacement == "new_owner" else 1)


@pytest.mark.asyncio
async def test_sealing_rejects_an_expired_lease_even_without_a_replacement_worker(capture_authority, db_session):
    grant = capture_authority.grant
    job = ProductOperation(
        tenant_id=grant.tenant_id,
        id="expired-capture-job",
        actor_id=grant.created_by,
        product_id=grant.product_id,
        project_id=grant.project_id,
        operation="captureL5Source",
        key_hash="f" * 64,
        request_hash="a" * 64,
        request={},
        receipt={"stage": "capturing"},
        state="running",
        lease_owner="old-worker",
        lease_until=datetime.now(UTC) - timedelta(seconds=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        attempt_count=1,
    )
    db_session.add(job)
    await db_session.commit()
    identity = grant.tenant_id, job.id, "old-worker"
    with pytest.raises(HTTPException) as denied:
        await update_capture(db_session, identity, {"state": "succeeded", "lease_owner": None, "lease_until": None})
    assert denied.value.detail["code"] == "source_capture_lease_changed"
    current = await db_session.get(ProductOperation, identity[:2], populate_existing=True)
    assert current.state == "running" and current.lease_owner == "old-worker"

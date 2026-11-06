"""Real durable capture continuation for retries and scheduled Security runs."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.api.configuration import view
from l5_services.events.sources import admit_source_receipt
from l5_services.models.binding import Binding
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import select, update

from src.auth.aws_connection_authority import connection_binding
from src.l5_integration import GatewayConfigurationBackend
from src.l5_security_schedule import schedule_authority
from src.products.models import ProductOperation, ProductSourceBinding, ProductSourceSnapshot
from src.products.source_capture_jobs import claim_capture, run_capture
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_capture_continuation_authority import capture_authority, retained_subject  # noqa: F401


def aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC)


@pytest.fixture
async def durable_review(capture_authority, db_session, monkeypatch):  # noqa: F811
    """Extend the existing real Product/consent fixture with a runnable binding."""
    selected = capture_authority
    grant, credential, configuration = selected.grant, selected.credential, selected.configuration
    now = datetime.now(UTC)
    credential.service, credential.credential_type, credential.agent_consent = "aws", "aws_role", True
    credential.aws_external_id = "offline-external-id"
    credential.aws_verified_at, credential.aws_verification_attempt = now, "offline-verification"
    credential.scopes = {
        "account_id": "123456789012",
        "role_arn": "arn:aws:iam::123456789012:role/Selected",
        "source": "imported_role",
        "status": "verified",
    }
    credential.aws_verified_binding = connection_binding(credential)
    db_session.add(
        ProductSourceBinding(
            tenant_id=grant.tenant_id,
            project_id=grant.project_id,
            credential_id=credential.id,
            kind="environment",
            provider="aws",
            namespace="selected-account",
            native_id="us-east-1",
            desired={"region": "us-east-1"},
            observed={"source_revision": credential.aws_verified_version_id},
            status="healthy",
        )
    )
    grant.visibility = "only_me"
    grant.operation_id = grant.attempt_id = grant.lease_fence = None
    version = await db_session.get(ConfigurationVersion, (grant.tenant_id, configuration.configuration_id, 1))
    manual = {
        "trigger_id": "manual-review",
        "kind": "manual",
        "enabled": True,
        "capability": "code_review",
        "binding_id": selected.binding.binding_id,
        "source_refs": [selected.source],
    }
    scheduled = {**manual, "trigger_id": "scheduled-review", "kind": "schedule", "schedule": "* * * * *"}
    version.settings = {
        **version.settings,
        "service_id": "security",
        "purpose": "Review the retained repository",
        "classification": "internal",
        "visibility": grant.visibility,
        "profile_ref": selected.source,
        "triggers": [manual, scheduled],
        "limits": {
            "deadline_seconds": 3600,
            "max_provider_attempts": 1,
            "max_polls": 30,
            "concurrency": 1,
            "max_cost": "1.00",
            "currency": "USD",
            "policy_ref": {**selected.connection, "record_type": "policy", "record_id": "offline-budget"},
        },
    }
    identity = {**{field: credential.scopes[field] for field in ("account_id", "role_arn")}, "region": "us-east-1", "verified_at": now.isoformat()}
    native_ref = {**selected.connection, "record_type": "security_space", "record_id": "space-one", "record_revision": now.isoformat()}
    registration = {"service_role": "arn:aws:iam::123456789012:role/SourceReader", "bucket": "offline-source-staging"}
    binding = Binding(
        tenant_id=grant.tenant_id,
        binding_id=selected.binding.binding_id,
        configuration_id=configuration.configuration_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        service_id="security",
        scope=view(configuration, version)["scope"],
        revision=1,
        generation=grant.binding_generation,
        state="ready",
        ownership="managed",
        connection_ref=selected.connection,
        connection_key="c" * 64,
        native_resource_ref=native_ref,
        native_key="d" * 64,
        provider_identity=identity,
        gaps=[],
        readiness=[{"kind": "code_review_source_registration", "registration": registration, "observed_at": now.isoformat()}],
    )
    grant.execution_scope = {
        **grant.execution_scope,
        "visibility": grant.visibility,
        "binding_id": binding.binding_id,
        "provider_account_id": identity["account_id"],
        "provider_role_arn": identity["role_arn"],
    }
    db_session.add(binding)
    await db_session.commit()
    # Only external source bytes, provider reads and queue delivery are replaced.
    selected.capture.side_effect = lambda _binding, _mapping, body, _objects: selected.captured.model_copy(
        update={"source_ref": body.source_ref, "exclusions": body.exclusions, "expires_at": body.retain_until}
    )
    publish = AsyncMock()
    monkeypatch.setattr("src.l5_queue_registry.publisher", lambda service_id: publish)
    monkeypatch.setattr("src.product_onboarding.source_discovery.assert_installation_owned_by", AsyncMock())

    class Native:
        def batch_get_agent_spaces(self, **kwargs):
            assert kwargs == {"agentSpaceIds": [native_ref["record_id"]]}
            return {
                "agentSpaces": [
                    {
                        "agentSpaceId": native_ref["record_id"],
                        "name": "offline-space",
                        "createdAt": now,
                        "awsResources": {"iamRoles": [registration["service_role"]], "s3Buckets": [registration["bucket"]]},
                    }
                ]
            }

    monkeypatch.setattr(
        "src.l5_security_schedule._provider_session", AsyncMock(return_value=SimpleNamespace(client=lambda *_args, **_kwargs: Native()))
    )
    backend = GatewayConfigurationBackend(db_session, retained_subject(selected.owner), publish=publish)
    run_input = {
        "service_id": "security",
        "configuration_revision": 1,
        "binding_id": binding.binding_id,
        "capability": "code_review",
        "sources": [selected.source],
    }
    return SimpleNamespace(**vars(selected), backend=backend, publish=publish, run_input=run_input, scheduled=scheduled)


async def queued_capture(action, fixture, operation, key):
    with pytest.raises(HTTPException) as pending:
        await action
    if pending.value.status_code != 503:
        raise pending.value
    assert pending.value.status_code == 503 and pending.value.detail["code"] == "source_capture_preparing"
    async with fixture.backend.sessions() as reader:
        job = await reader.get(ProductOperation, (fixture.grant.tenant_id, pending.value.detail["preparation_id"]))
        preparation = await reader.get(
            AdmissionPreparation,
            (
                fixture.grant.tenant_id,
                fixture.grant.product_id,
                fixture.grant.project_id,
                "security",
                fixture.grant.created_by,
                operation,
                key,
            ),
        )
        assert job.state == "pending" and job.request["run_input"] == fixture.run_input
        assert job.request["admission_key"] == key and job.request["operation"] == operation
        assert aware(job.expires_at) == aware(preparation.deadline_at)
        return job.id, deepcopy(job.request), aware(preparation.deadline_at)


async def finish_capture(fixture, identifier):
    identity = await claim_capture(fixture.backend.sessions)
    assert identity[:2] == (fixture.grant.tenant_id, identifier)
    await run_capture(fixture.backend.sessions, identity)
    async with fixture.backend.sessions() as reader:
        completed = await reader.get(ProductOperation, identity[:2])
        assert completed.state == "succeeded", completed.receipt
        assert completed.attempt_count == 1 and completed.lease_owner is None
    assert await claim_capture(fixture.backend.sessions) is None


async def admitted_run(fixture, operation, key, deadline):
    grant = fixture.grant
    async with fixture.backend.sessions() as reader:
        receipt = await reader.get(RunReceipt, (grant.tenant_id, grant.product_id, grant.project_id, "security", grant.created_by, operation, key))
        assert receipt is not None
        run = await reader.get(Run, (grant.tenant_id, receipt.run_id))
        assert run.input == fixture.run_input and aware(run.deadline_at) == deadline
        assert datetime.fromisoformat(run.admission["expires_at"].replace("Z", "+00:00")) == deadline
        work = (await reader.scalars(select(Work).where(Work.run_id == run.run_id))).all()
        assert len(work) == 1 and work[0].kind == "execute"
        snapshot = run.admission["source_snapshots"][0]
        retained = await reader.get(ProductSourceSnapshot, (grant.tenant_id, snapshot["snapshot_id"]))
        assert retained.receipt == snapshot and aware(retained.retain_until) == deadline
        return run


@pytest.mark.asyncio
async def test_retry_capture_continues_original_retry_once(durable_review, db_session):
    fixture = durable_review
    backend, grant = fixture.backend, fixture.grant
    args = (grant.product_id, grant.project_id, "security")
    initial, _, deadline = await queued_capture(backend.submit(*args, fixture.run_input, "original-review"), fixture, "submit", "original-review")
    await finish_capture(fixture, initial)
    original = await admitted_run(fixture, "submit", "original-review", deadline)
    # Seed the worker-owned definitive failure that authorizes this retry.
    await db_session.execute(
        update(Run)
        .where(Run.run_id == original.run_id)
        .values(
            state="failed",
            provider_state="not_started",
            no_effect_proof={
                "classification": "definitive_no_effect",
                "evidence_ref": {**fixture.source, "record_type": "provider_observation", "record_id": "no-native-effect"},
            },
        )
    )
    await db_session.execute(update(Work).where(Work.run_id == original.run_id).values(completed_at=datetime.now(UTC)))
    await db_session.commit()
    fixture.publish.reset_mock()
    fixture.capture.reset_mock()
    reason, key = "Retry the verified no-effect failure", "retry-original-review"
    identifier, request, retry_deadline = await queued_capture(
        backend.retry_run(*args, original.run_id, reason, key),
        fixture,
        "retry",
        key,
    )
    assert request["retry_context"] == {"run_id": original.run_id, "reason": reason}
    assert request["source_request"]["delegation_id"] == original.admission["delegation_id"]
    fixture.publish.assert_not_awaited()
    await finish_capture(fixture, identifier)
    retried = await admitted_run(fixture, "retry", key, retry_deadline)
    assert retried.retry_of == original.run_id and retried.run_id != original.run_id
    replay = await backend.retry_run(*args, original.run_id, reason, key)
    assert replay["run_id"] == retried.run_id
    fixture.capture.assert_awaited_once()
    fixture.publish.assert_awaited_once()
    async with backend.sessions() as reader:
        assert len((await reader.scalars(select(Run))).all()) == 2
        child = await reader.get(CredentialServiceDelegation, retried.admission["delegation_id"])
        assert child.id == original.admission["delegation_id"] and child.parent_delegation_id == grant.id
        stored = await reader.get(ProductOperation, (grant.tenant_id, identifier))
        assert stored.request == request and aware(stored.expires_at) == retry_deadline


@pytest.mark.asyncio
async def test_scheduled_capture_continues_after_occurrence_window_once(durable_review, db_session, monkeypatch):
    fixture = durable_review
    grant = fixture.grant
    instant = datetime.now(UTC).replace(second=0, microsecond=0)
    stamp = instant.isoformat().replace("+00:00", "Z")
    receipt = EventReceipt(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        service_id="security",
        trigger_id=fixture.scheduled["trigger_id"],
        event_id=f"schedule:{stamp}",
        event_kind="schedule",
        configuration_id=fixture.configuration.configuration_id,
        configuration_revision=1,
        run_input=fixture.run_input,
        target={**fixture.scheduled, "configuration_revision": 1, "binding_generation": grant.binding_generation, "scheduled_at": stamp},
        state="pending",
        received_at=datetime.now(UTC),
    )
    receipt.idempotency_key = semantic_key(receipt)
    db_session.add(receipt)
    await db_session.commit()
    service = GatewayConfigurationBackend(db_session, retained_subject(fixture.owner, scheduled=True), publish=fixture.publish)

    async def admit():
        return await admit_source_receipt(
            db_session, service.sessions, receipt, lambda selected: schedule_authority(service, selected), fixture.publish
        )

    identifier, request, deadline = await queued_capture(admit(), fixture, "submit", receipt.idempotency_key)
    assert request["subject"]["account_type"] == "service" and request["verified"]["event_id"] == receipt.event_id
    assert request["verified"]["actor_id"] == grant.created_by
    fixture.publish.assert_not_awaited()

    # The captured occurrence must remain admissible after its five-minute
    # arrival window, while every resumed check still uses its frozen deadline.
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            value = instant + timedelta(minutes=6)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr("l5_services.events.sources.datetime", Later)
    monkeypatch.setattr("l5_services.admission.datetime", Later)
    monkeypatch.setattr("src.auth.l5_delegation.datetime", Later)
    await finish_capture(fixture, identifier)
    run = await admitted_run(fixture, "submit", receipt.idempotency_key, deadline)
    assert run.admission["event_id"] == receipt.event_id and run.admission["event_kind"] == "schedule"
    assert run.admission["causal_origin"] == "schedule"
    assert datetime.fromisoformat(run.admission["admitted_at"].replace("Z", "+00:00")) > instant + timedelta(minutes=5)
    replay = await admit()
    assert replay.run_id == run.run_id
    fixture.capture.assert_awaited_once()
    fixture.publish.assert_awaited_once()
    async with service.sessions() as reader:
        saved = await reader.get(
            EventReceipt, (grant.tenant_id, grant.product_id, grant.project_id, "security", receipt.trigger_id, receipt.event_id)
        )
        assert saved.state == "admitted" and saved.run_id == run.run_id
        assert len((await reader.scalars(select(Run))).all()) == 1
        stored = await reader.get(ProductOperation, (grant.tenant_id, identifier))
        assert stored.request == request and aware(stored.expires_at) == deadline

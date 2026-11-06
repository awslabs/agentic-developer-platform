"""The installed work authorizer requires live fenced rows, not a supplied permit."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.run import Run
from l5_services.models.work import Work

from src.auth.aws_connection_authority import connection_binding
from src.internal.l5_credential_routes import ConnectionRef, CredentialSessionRequest, get_l5_work_authorizer
from src.products.models import ProductSourceBinding
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


@pytest.fixture
async def fenced_run(test_engine, db_session):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    now = datetime.now(UTC)
    scope = dict(tenant_id="tenant", product_id="product", project_id="project", relationship_revision="1", owner_id="owner", visibility="only_me")
    connection_ref = dict(
        tenant_id="tenant",
        project_id="project",
        source_connection_id="credential",
        record_type="connection",
        record_id="credential",
        record_revision="v1",
    )
    credential_ref = {**connection_ref, "record_type": "credential"}
    native_space_ref = {**connection_ref, "record_type": "security_space", "record_id": "space-one"}
    native_model_ref = {**connection_ref, "record_type": "threat_model", "record_id": "model-one"}
    provider_identity = {
        "account_id": "123456789012",
        "role_arn": "arn:aws:iam::123456789012:role/ExampleSecurityRole",
        "region": "us-east-1",
    }
    credential = UserCredential(
        id="credential",
        org_id="tenant",
        user_id="owner",
        service="aws",
        credential_type="aws_role",
        label="selected",
        secret_arn="offline-test",
        aws_external_id="test-external-id",
        aws_verified_at=now,
        aws_verification_attempt="verification",
        aws_verified_version_id="v1",
        scopes={**provider_identity, "status": "verified"},
    )
    db_session.add(credential)
    await db_session.flush()
    credential.aws_verified_binding = connection_binding(credential)
    configuration = Configuration(
        tenant_id="tenant",
        configuration_id="config",
        product_id="product",
        project_id="project",
        service_id="security",
        relationship_revision="1",
        owner_id="owner",
        current_revision=1,
        handoff_state="linked",
    )
    version = ConfigurationVersion(
        tenant_id="tenant",
        configuration_id="config",
        revision=1,
        settings={"enabled": True, "connection_ref": connection_ref},
        inherited_fields=[],
        gaps=[],
        state="ready",
        hash="a" * 64,
    )
    binding = Binding(
        tenant_id="tenant",
        binding_id="binding",
        configuration_id="config",
        product_id="product",
        project_id="project",
        service_id="security",
        scope=scope,
        revision=1,
        generation=2,
        state="ready",
        ownership="managed",
        connection_ref=connection_ref,
        connection_key="a" * 64,
        native_resource_ref=native_space_ref,
        provider_identity=provider_identity,
        native_key=None,
        readiness=[
            {
                "kind": "code_review_source_registration",
                "observed_at": now.isoformat(),
                "registration": {"service_role": provider_identity["role_arn"], "bucket": "example-security-source"},
            },
            {"kind": "threat_model", "observed_at": now.isoformat(), "native_model_ref": native_model_ref},
        ],
        gaps=[],
    )
    grant = CredentialServiceDelegation(
        id="grant",
        tenant_id="tenant",
        credential_id="credential",
        service_id="l5-worker",
        product_id="product",
        project_id="project",
        created_by="owner",
        operations=["credential:session", "source:read"],
        sources=[credential_ref],
        capabilities=["threat_model"],
        visibility="only_me",
        revision=1,
        operation_id="run",
        attempt_id="work",
        lease_fence=1,
        binding_generation=2,
        expires_at=now + timedelta(hours=1),
    )
    run = Run(
        tenant_id="tenant",
        run_id="run",
        product_id="product",
        project_id="project",
        service_id="security",
        configuration_id="config",
        configuration_revision=1,
        binding_id="binding",
        trigger_id="manual",
        binding_generation=2,
        input={"capability": "threat_model"},
        admission={
            "admitted_at": now.isoformat(),
            "scope": scope,
            "connection_ref": connection_ref,
            "delegation_id": "grant",
            "delegation_version": 1,
            "binding_generation": 2,
            "actor_id": "owner",
            "source_snapshots": [],
            "native_space_ref": native_space_ref,
            "native_model_ref": native_model_ref,
            "provider_identity": provider_identity,
        },
        deadline_at=now + timedelta(hours=1),
        state="queued",
    )
    work = Work(
        tenant_id="tenant",
        work_id="work",
        run_id="run",
        kind="submit",
        generation=1,
        step=0,
        due_at=now,
        lease_owner="worker",
        lease_until=now + timedelta(seconds=45),
        fence=1,
    )
    attempt = WorkerAttempt(
        tenant_id="tenant",
        attempt_id="attempt",
        run_id="run",
        operation="submit",
        first_work_id="work",
        first_fence=1,
        idempotency_token="b" * 64,
        input_hash="c" * 64,
        started_at=now,
        status="prepared",
    )
    db_session.add_all([configuration, version, binding, grant, run, work, attempt])
    await db_session.commit()
    request = CredentialSessionRequest(
        delegation_id="grant",
        delegation_revision=1,
        operation_id="run",
        attempt_id="work",
        connection_ref=ConnectionRef.model_validate(credential_ref),
        lease_owner="worker",
        lease_fence=1,
    )
    return grant, request, now


@pytest.mark.asyncio
async def test_default_authorizer_requires_current_fenced_run(db_session, fenced_run):
    grant, request, now = fenced_run
    credential_ref = request.connection_ref.model_dump()
    run = await db_session.get(Run, ("tenant", "run"))
    work = await db_session.get(Work, ("tenant", "work"))
    configuration = await db_session.get(Configuration, ("tenant", "config"))
    authorize = get_l5_work_authorizer(db_session)
    permit = await authorize(grant, request)
    assert permit.lease_fence == 1 and permit.binding_generation == 2
    assert permit.expires_at == now + timedelta(seconds=45)
    parent = CredentialServiceDelegation(
        id="owner-grant",
        tenant_id="tenant",
        credential_id="credential",
        service_id="l5-worker",
        product_id="product",
        project_id="project",
        created_by="owner",
        operations=["credential:session", "source:read"],
        sources=[credential_ref],
        capabilities=["threat_model"],
        visibility="only_me",
        revision=1,
        binding_generation=2,
        expires_at=now + timedelta(hours=2),
    )
    db_session.add(parent)
    grant.parent_delegation_id = parent.id
    grant.operation_id = None
    grant.attempt_id = None
    grant.lease_fence = None
    await db_session.commit()
    bound = await authorize(grant, request)
    assert bound == permit
    assert (grant.operation_id, grant.attempt_id, grant.lease_fence) == ("run", "work", 1)
    parent.revoked_at = now
    await db_session.commit()
    with pytest.raises(HTTPException) as revoked_parent:
        await authorize(grant, request)
    assert revoked_parent.value.status_code == 409
    parent.revoked_at = None
    await db_session.commit()
    foreign = request.model_copy(update={"attempt_id": "other-attempt"})
    with pytest.raises(HTTPException) as wrong_attempt:
        await authorize(grant, foreign)
    assert wrong_attempt.value.status_code == 409
    source = {**credential_ref, "record_type": "document", "record_id": "source-document"}
    grant.sources = [credential_ref, source]
    parent.sources = [credential_ref, source]
    run.admission = {**run.admission, "source_snapshots": [{"snapshot_id": "snapshot", "source_ref": source}]}
    await db_session.commit()
    source_request = request.model_copy(update={"connection_ref": ConnectionRef.model_validate(source)})
    with pytest.raises(HTTPException) as missing_binding:
        await authorize(grant, source_request)
    assert missing_binding.value.status_code == 409
    product_binding = ProductSourceBinding(
        tenant_id="tenant",
        project_id="project",
        kind="document",
        provider="aws",
        credential_id="credential",
        namespace="selection",
        native_id="source-document",
        desired={"source_revision": "v1"},
        observed={"source_revision": "v1"},
        status="healthy",
    )
    db_session.add(product_binding)
    await db_session.commit()
    selected_source = await authorize(grant, source_request)
    assert selected_source.admitted_source_snapshot_ids == frozenset({"snapshot"})
    product_binding.observed = {"source_revision": "v2"}
    await db_session.commit()
    with pytest.raises(HTTPException) as drifted_source:
        await authorize(grant, source_request)
    assert drifted_source.value.status_code == 409
    work.fence = 2
    await db_session.commit()
    with pytest.raises(HTTPException) as stale:
        await authorize(grant, request)
    assert stale.value.status_code == 409
    work.fence = 1
    work.lease_until = now - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(HTTPException) as expired:
        await authorize(grant, request)
    assert expired.value.status_code == 409
    work.lease_until = now + timedelta(seconds=45)
    configuration.current_revision = 2
    await db_session.commit()
    with pytest.raises(HTTPException) as changed:
        await authorize(grant, request)
    assert changed.value.status_code == 409
    configuration.current_revision = 1
    grant.revoked_at = now
    await db_session.commit()
    with pytest.raises(HTTPException) as revoked:
        await authorize(grant, request)
    assert revoked.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("record_kind", ["code_review_source_registration", "threat_model"])
@pytest.mark.parametrize("invalid_observation", ["missing", "stale"])
async def test_default_authorizer_rejects_unverified_native_readiness(db_session, fenced_run, record_kind, invalid_observation):
    grant, request, now = fenced_run
    authorize = get_l5_work_authorizer(db_session)
    await authorize(grant, request)
    binding = await db_session.get(Binding, ("tenant", "binding"))
    if invalid_observation == "missing":
        binding.readiness = [record for record in binding.readiness if record["kind"] != record_kind]
    else:
        binding.readiness = [
            {**record, "observed_at": (now - timedelta(minutes=16)).isoformat()} if record["kind"] == record_kind else record
            for record in binding.readiness
        ]
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await authorize(grant, request)
    assert denied.value.status_code == 409
    assert denied.value.detail == {"code": "stale_delegation_or_attempt"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "field", "replacement"),
    [
        ("native_space_ref", "record_id", "other-space"),
        ("native_model_ref", "record_id", "other-model"),
        ("native_model_ref", "record_revision", "v2"),
        ("provider_identity", "role_arn", "arn:aws:iam::123456789012:role/OtherRole"),
    ],
)
async def test_default_authorizer_rejects_changed_native_admission(db_session, fenced_run, snapshot, field, replacement):
    grant, request, _ = fenced_run
    authorize = get_l5_work_authorizer(db_session)
    await authorize(grant, request)
    run = await db_session.get(Run, ("tenant", "run"))
    run.admission = {**run.admission, snapshot: {**run.admission[snapshot], field: replacement}}
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await authorize(grant, request)
    assert denied.value.status_code == 409
    assert denied.value.detail == {"code": "stale_delegation_or_attempt"}


def test_current_repository_binding_proves_only_a_pinned_commit():
    from types import SimpleNamespace

    from src.products.source_routes import allowed_source_ref

    commit = "a" * 40
    binding = SimpleNamespace(
        tenant_id="tenant",
        project_id="project",
        installation_id="installation",
        credential_id=None,
        kind="repository",
        native_id="repository",
        observed={"source_revision": commit},
        desired={},
    )
    reference = dict(
        tenant_id="tenant",
        project_id="project",
        source_connection_id="installation",
        record_type="repository_commit",
        record_id="repository",
        record_revision=commit,
    )
    grant = SimpleNamespace(tenant_id="tenant", project_id="project", sources=[reference], execution_scope=None)
    assert allowed_source_ref(grant, binding) == reference
    binding.observed = {"source_revision": "updated-at-is-not-a-commit"}
    assert allowed_source_ref(grant, binding) is None
    binding.observed = {"source_revision": "b" * 40}
    assert allowed_source_ref(grant, binding) is None

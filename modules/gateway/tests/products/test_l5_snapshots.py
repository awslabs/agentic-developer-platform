"""Pinned Product receipts preserve exact revisions without enabling provider content yet."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Request
from l5_services.models.base import Base as L5Base
from pydantic import ValidationError
from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import L5WorkPermit, get_l5_work_authorizer
from src.products.models import ProductSourceBinding, ProductSourceSnapshot
from src.products.source_snapshots import (
    ArtifactRef,
    CapturedSource,
    SourceRef,
    SourceSnapshotRequest,
    store_snapshot,
)
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.test_i01 import HEADERS, REQUEST


async def snapshot_fixture(test_client, db_session, app, source_member, monkeypatch):
    now = datetime.now(UTC)
    clock = {"now": now}
    monkeypatch.setattr("src.products.source_snapshots.utcnow", lambda: clock["now"])
    monkeypatch.setattr("src.internal.l5_snapshot_routes.utcnow", lambda: clock["now"])
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="document",
        credential_type="api_key",
        label="test-selected-source",
        secret_arn="test-secret",
    )
    db_session.add(credential)
    await db_session.flush()
    source_ref = SourceRef(
        tenant_id=source_member.org_id,
        project_id=project_id,
        source_connection_id=credential.id,
        record_type="document",
        record_id="document-a",
        record_revision="r1",
    )
    grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=credential.id,
        service_id="registered-l5-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=source_member.user_id,
        operations=["product:read", "source:pin", "source:read"],
        sources=[source_ref.model_dump()],
        capabilities=[],
        visibility="restricted",
        revision=2,
        operation_id="operation-a",
        attempt_id="attempt-a",
        lease_fence=2,
        binding_generation=1,
        expires_at=now + timedelta(hours=5),
    )
    db_session.add(grant)
    db_session.add(
        ProductSourceBinding(
            tenant_id=source_member.org_id,
            project_id=project_id,
            credential_id=credential.id,
            kind="document",
            provider="document",
            namespace="examples",
            native_id="document-a",
            desired={"name": "Document A", "source_revision": "r1"},
            observed={"source_revision": "r1"},
            status="healthy",
        )
    )
    await db_session.commit()
    request_body = SourceSnapshotRequest(
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        source_ref=source_ref,
        exclusions=["excluded-pattern"],
        max_bytes=1024,
        max_files=10,
        retain_until=now + timedelta(hours=1),
    )
    content = b"original document bytes"
    manifest = b'{"files":["document-a"]}'
    capture = CapturedSource(
        source_ref=source_ref,
        exclusions=request_body.exclusions,
        file_count=1,
        expires_at=now + timedelta(hours=2),
        content=ArtifactRef(
            artifact_id="fixture-content",
            version="version-r1",
            sha256=hashlib.sha256(content).hexdigest(),
            media_type="text/plain",
            bytes=len(content),
        ),
        file_manifest=ArtifactRef(
            artifact_id="fixture-manifest",
            version="version-r1",
            sha256=hashlib.sha256(manifest).hexdigest(),
            media_type="application/json",
            bytes=len(manifest),
        ),
    )
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:registered-l5-worker",
            "agent_registry_id": "registered-l5-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
        }
    )

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified
    body = {
        "delegation_id": grant.id,
        "delegation_revision": 2,
        "operation_id": "operation-a",
        "attempt_id": "attempt-a",
        "lease_fence": 2,
    }
    path = f"/internal/v1/products/{product_id}/projects/{project_id}/source-snapshots"
    return grant, request_body, capture, path, body, clock


@pytest.mark.asyncio
async def test_pinned_receipt_retains_original_revision_and_expires(
    test_client,
    db_session,
    test_engine,
    app,
    source_member,
    monkeypatch,
):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    grant, request_body, capture, path, body, clock = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    receipt, created = await store_snapshot(db_session, grant, request_body, capture, "pin-once")
    assert created is True
    await db_session.commit()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        row = await independent.scalar(select(ProductSourceSnapshot).where(ProductSourceSnapshot.id == receipt.snapshot_id))
        assert row.receipt["source_ref"] == request_body.source_ref.model_dump()
        assert row.receipt["content"]["sha256"] == capture.content.sha256
        assert row.receipt["file_manifest"]["sha256"] == capture.file_manifest.sha256
        assert row.request_hash and row.retain_until and row.expires_at
    async with test_engine.connect() as connection:
        columns = await connection.run_sync(lambda raw: {col["name"] for col in inspect(raw).get_columns("product_source_snapshots")})
        assert {"receipt", "source_ref", "retain_until", "expires_at", "request_hash"} <= columns

    replay, created = await store_snapshot(db_session, grant, request_body, capture, "pin-once")
    assert created is False and replay == receipt
    with pytest.raises(HTTPException) as conflict:
        await store_snapshot(db_session, grant, request_body.model_copy(update={"max_bytes": 512}), capture, "pin-once")
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as overflow:
        await store_snapshot(db_session, grant, request_body.model_copy(update={"max_bytes": 3}), capture, "another-key")
    assert overflow.value.status_code == 422
    with pytest.raises(HTTPException) as retention:
        await store_snapshot(
            db_session,
            grant,
            request_body.model_copy(update={"retain_until": clock["now"] + timedelta(days=91)}),
            capture,
            "long-retention",
        )
    assert retention.value.status_code == 422
    with pytest.raises(ValidationError):
        SourceSnapshotRequest.model_validate({**request_body.model_dump(mode="json"), "exclusions": [""]})
    with pytest.raises(ValidationError):
        SourceRef.model_validate({**request_body.source_ref.model_dump(), "record_revision": "HEAD"})

    read_path = f"{path}/{receipt.snapshot_id}/read"
    unavailable = await test_client.post(read_path, json=body)
    assert unavailable.status_code == 409, unavailable.text
    permit = L5WorkPermit(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        source_connection_id=grant.credential_id,
        operation_id="operation-a",
        attempt_id="attempt-a",
        lease_fence=2,
        binding_generation=1,
        expires_at=clock["now"] + timedelta(hours=4),
        admitted_source_snapshot_ids=frozenset({receipt.snapshot_id}),
    )
    checks = {"count": 0}

    async def authorize(grant, selection):
        assert selection.connection_ref.record_revision == "r1"
        checks["count"] += 1
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    read = await test_client.post(read_path, json=body)
    assert read.status_code == 200, read.text
    assert read.headers["Cache-Control"] == "no-store"
    assert checks["count"] == 2
    assert read.json() == receipt.model_dump(mode="json")

    clock["now"] = clock["now"] + timedelta(hours=3)
    await db_session.execute(
        update(ProductSourceBinding)
        .where(ProductSourceBinding.native_id == "document-a")
        .values(desired={"name": "Document A changed", "source_revision": "r2"}, observed={"source_revision": "r2"})
    )
    await db_session.commit()
    expired = await test_client.post(read_path, json=body)
    assert expired.status_code == 410, expired.text
    assert expired.json()["detail"]["code"] == "snapshot_expired"
    with pytest.raises(HTTPException) as expired_replay:
        await store_snapshot(db_session, grant, request_body, capture, "pin-once")
    assert expired_replay.value.status_code == 410
    assert (await db_session.scalar(select(ProductSourceSnapshot.id).where(ProductSourceSnapshot.id == receipt.snapshot_id))) == receipt.snapshot_id


@pytest.mark.asyncio
async def test_pinned_receipt_needs_current_admission_and_grant(test_client, db_session, app, source_member, monkeypatch):
    grant, request_body, capture, path, body, clock = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    receipt, _ = await store_snapshot(db_session, grant, request_body, capture, "pin-once")
    await db_session.commit()
    read_path = f"{path}/{receipt.snapshot_id}/read"
    permit = L5WorkPermit(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        source_connection_id=grant.credential_id,
        operation_id="operation-a",
        attempt_id="attempt-a",
        lease_fence=2,
        binding_generation=1,
        expires_at=clock["now"] + timedelta(hours=4),
    )

    async def authorize(grant, selection):
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    denied = await test_client.post(read_path, json=body)
    assert denied.status_code == 404
    assert "document-a" not in denied.text
    app.dependency_overrides[get_l5_work_authorizer] = lambda: (
        lambda grant, selection: authorize_with_membership(grant, selection, permit, receipt.snapshot_id)
    )
    stale = await test_client.post(read_path, json={**body, "attempt_id": "forged-attempt"})
    assert stale.status_code == 409
    calls = 0

    async def revoke_during_read(grant, selection):
        nonlocal calls
        calls += 1
        if calls == 2:
            await db_session.execute(
                update(CredentialServiceDelegation)
                .where(CredentialServiceDelegation.id == grant.id)
                .values(revoked_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            await db_session.commit()
        return replace(permit, admitted_source_snapshot_ids=frozenset({receipt.snapshot_id}))

    app.dependency_overrides[get_l5_work_authorizer] = lambda: revoke_during_read
    interrupted = await test_client.post(read_path, json=body)
    assert calls == 2
    assert interrupted.status_code == 404, interrupted.text
    assert "document-a" not in interrupted.text
    revoked = await test_client.post(read_path, json=body)
    assert revoked.status_code == 404
    assert "document-a" not in revoked.text


async def authorize_with_membership(grant, selection, permit, snapshot_id):
    return replace(permit, admitted_source_snapshot_ids=frozenset({snapshot_id}))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "missing_owner", "wrong_work", "unadmitted_snapshot"])
async def test_first_snapshot_read_binds_only_verified_admission(
    test_client,
    db_session,
    app,
    source_member,
    monkeypatch,
    failure,
):
    parent, request_body, capture, path, body, clock = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    child = CredentialServiceDelegation(
        **{
            name: getattr(parent, name)
            for name in (
                "tenant_id",
                "credential_id",
                "service_id",
                "product_id",
                "project_id",
                "created_by",
                "operations",
                "sources",
                "capabilities",
                "visibility",
                "revision",
                "binding_generation",
                "expires_at",
            )
        },
        parent_delegation_id=parent.id,
    )
    db_session.add(child)
    await db_session.flush()
    request_body = request_body.model_copy(update={"delegation_id": child.id})
    receipt, _ = await store_snapshot(db_session, child, request_body, capture, "first-read")
    await db_session.commit()
    calls = []

    async def authorize(grant, selection):
        calls.append(selection)
        if failure == "wrong_work":
            raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
        # The real verifier atomically binds the admission after checking the
        # live Work lease and Run. Model that transition, not an always-bound grant.
        grant.operation_id = selection.operation_id
        grant.attempt_id = selection.attempt_id
        grant.lease_fence = selection.lease_fence
        await db_session.commit()
        return L5WorkPermit(
            tenant_id=grant.tenant_id,
            product_id=grant.product_id,
            project_id=grant.project_id,
            source_connection_id=grant.credential_id,
            operation_id=selection.operation_id,
            attempt_id=selection.attempt_id,
            lease_owner=selection.lease_owner,
            lease_fence=selection.lease_fence,
            binding_generation=grant.binding_generation,
            expires_at=clock["now"] + timedelta(hours=1),
            admitted_source_snapshot_ids=frozenset() if failure == "unadmitted_snapshot" else frozenset({receipt.snapshot_id}),
        )

    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    body = {**body, "delegation_id": child.id, "lease_owner": "worker-owner"}
    if failure == "missing_owner":
        del body["lease_owner"]
    response = await test_client.post(f"{path}/{receipt.snapshot_id}/read", json=body)
    assert response.status_code == ({None: 200, "missing_owner": 409, "wrong_work": 409, "unadmitted_snapshot": 404}[failure]), response.text
    if failure is None:
        assert len(calls) == 2
        assert response.json() == receipt.model_dump(mode="json")
    elif failure == "missing_owner":
        assert not calls

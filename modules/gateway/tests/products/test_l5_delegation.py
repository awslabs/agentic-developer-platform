"""Product reads require a current grant bound to a verified service."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.dependencies import get_current_user
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import L5WorkPermit, get_l5_work_authorizer
from src.products import routes as product_routes
from src.products import source_routes
from src.products.models import Product, ProductChange, ProductSourceBinding, ProductSourceSnapshot, Project
from src.products.source_snapshots import store_snapshot
from src.shared.models.organization import Organization, User
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.membership import add_team_member
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_l5_snapshots import snapshot_fixture


@pytest.mark.asyncio
async def test_scoped_profile_read(test_client, db_session, test_engine, app, source_member, monkeypatch):
    clock = datetime.now(UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    other = await test_client.post(
        "/v1/products", json={**REQUEST, "name": "Other Product"}, headers={"Idempotency-Key": "other_product_scope_123456"}
    )
    assert other.status_code == 201, other.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"
    other_path = f"/v1/products/{other.json()['product_id']}/projects/{other.json()['project_id']}"
    foreign_member = source_member.model_copy(update={"org_id": "foreign-tenant", "user_id": "foreign-reader", "team_id": "foreign-team"})
    db_session.add(Organization(id=foreign_member.org_id, name="Isolated workspace"))
    db_session.add(
        User(
            id=foreign_member.user_id,
            org_id=foreign_member.org_id,
            team_id=foreign_member.team_id,
            email="foreign-reader@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, foreign_member)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign_member
    foreign = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Private Foreign Product"},
        headers={"Idempotency-Key": "foreign_product_scope_123456"},
    )
    assert foreign.status_code == 201, foreign.text
    foreign_path = f"/v1/products/{foreign.json()['product_id']}/projects/{foreign.json()['project_id']}"
    assert (await test_client.get(foreign_path)).status_code == 200
    assert (await test_client.get(path)).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: source_member
    assert (await test_client.get(foreign_path)).status_code == 404

    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="document",
        credential_type="api_key",
        label="synthetic-delegated-source",
        secret_arn="test-secret",
    )
    db_session.add(credential)
    await db_session.flush()
    source_ref = {
        "tenant_id": source_member.org_id,
        "project_id": created.json()["project_id"],
        "source_connection_id": credential.id,
        "record_type": "document",
        "record_id": "private-document",
        "record_revision": "source-revision-1",
    }
    db_session.add(
        ProductSourceBinding(
            tenant_id=source_member.org_id,
            project_id=created.json()["project_id"],
            credential_id=credential.id,
            kind="document",
            provider="document",
            namespace="private",
            native_id="private-document",
            desired={"name": "Private document", "source_revision": "source-revision-1"},
            observed={
                "observed_at": clock.isoformat(),
                "stale_after": (clock + timedelta(minutes=15)).isoformat(),
                "source_revision": "source-revision-1",
            },
            status="healthy",
        )
    )
    project = await db_session.get(Project, (source_member.org_id, created.json()["project_id"]))
    project.configuration = {"basic": {"documents": [source_ref]}}
    grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=credential.id,
        service_id="registered-l5-worker",
        product_id=created.json()["product_id"],
        project_id=created.json()["project_id"],
        created_by=source_member.user_id,
        operations=["product:read"],
        sources=[],
        capabilities=[],
        visibility="restricted",
        expires_at=clock + timedelta(hours=1),
    )
    db_session.add(grant)
    await db_session.commit()
    human = await test_client.get(path)
    assert human.status_code == 200, human.text
    assert human.json()["configuration"]["basic"]["documents"] == [source_ref]
    human_bindings = await test_client.get(path + "/bindings")
    human_projects = await test_client.get(path + "/project-bindings")
    assert human_bindings.status_code == human_projects.status_code == 200
    assert len(human_bindings.json()["items"]) == 1
    assert human_bindings.json()["items"][0]["selection"]["name"] == "Private document"
    assert [item["revision"] for item in human_projects.json()["items"]] == [1]
    assert (await db_session.scalar(select(Product).where(Product.id == grant.product_id))).revision == 1

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
    app.dependency_overrides[get_current_user] = lambda: service
    assert (await test_client.get(path)).status_code == 404
    assert (await test_client.get(other_path, headers={"X-L5-Delegation-ID": grant.id})).status_code == 404
    response = await test_client.get(path, headers={"X-L5-Delegation-ID": grant.id})
    assert response.status_code == 200, response.text
    assert response.headers["Profile-Revision"] == human.headers["Profile-Revision"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["product_id"] == human.json()["product_id"]
    assert response.json()["project_id"] == human.json()["project_id"]
    assert response.json()["configuration"]["basic"]["documents"] == []
    assert "private-document" not in response.text

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified
    service_path = "/internal/v1" + path.removeprefix("/v1")
    headers = {"X-L5-Delegation-ID": grant.id}
    current = await test_client.get(service_path, headers=headers)
    revision = await test_client.get(service_path + "/revisions/1", headers=headers)
    changes = await test_client.get(service_path + "/changes", headers=headers)
    assert current.status_code == revision.status_code == changes.status_code == 200
    assert current.json() == response.json()
    assert revision.json()["revision"] == 1
    assert current.headers["Profile-Revision"] == revision.headers["Profile-Revision"] == "1"
    assert [item["revision"] for item in changes.json()["items"]] == [1]
    service_bindings = await test_client.get(service_path + "/bindings", headers=headers)
    service_projects = await test_client.get(service_path + "/project-bindings", headers=headers)
    service_sources = await test_client.get(service_path + "/sources", headers=headers)
    assert service_bindings.status_code == service_projects.status_code == service_sources.status_code == 200
    assert service_bindings.json()["items"] == service_sources.json()["items"] == []
    assert service_projects.json() == human_projects.json()
    assert "private-document" not in revision.text + changes.text + service_bindings.text + service_sources.text

    for denied_path in (other_path, foreign_path):
        for suffix in ("", "/revisions/1", "/changes", "/project-bindings", "/bindings", "/sources"):
            attempt = await test_client.get("/internal/v1" + denied_path.removeprefix("/v1") + suffix, headers=headers)
            assert attempt.status_code == 404, (denied_path, suffix, attempt.text)
            assert "Private Foreign Product" not in attempt.text and "Other Product" not in attempt.text
            assert "items" not in attempt.text

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as audit:
        rows = (
            await audit.scalars(
                select(Product).where(
                    Product.id.in_(
                        [
                            created.json()["product_id"],
                            other.json()["product_id"],
                            foreign.json()["product_id"],
                        ]
                    )
                )
            )
        ).all()
        assert {(row.id, row.tenant_id, row.revision) for row in rows} == {
            (created.json()["product_id"], source_member.org_id, 1),
            (other.json()["product_id"], source_member.org_id, 1),
            (foreign.json()["product_id"], foreign_member.org_id, 1),
        }
        stored_change = await audit.scalar(select(ProductChange).where(ProductChange.product_id == created.json()["product_id"]))
        assert stored_change.revision == revision.json()["revision"] == 1
        stored_project = await audit.get(Project, (source_member.org_id, created.json()["project_id"]))
        assert stored_project.configuration["basic"]["documents"] == [source_ref]
        stored_bindings = (
            await audit.scalars(
                select(ProductSourceBinding).where(
                    ProductSourceBinding.project_id == created.json()["project_id"],
                )
            )
        ).all()
        assert len(stored_bindings) == 1 and stored_bindings[0].native_id == source_ref["record_id"]

    original_read = product_routes.get_profile

    async def unavailable(*args, **kwargs):
        raise HTTPException(503, detail={"code": "transport_unavailable"})

    monkeypatch.setattr(product_routes, "get_profile", unavailable)
    interrupted = await test_client.get(service_path, headers=headers)
    assert interrupted.status_code == 503 and "private-document" not in interrupted.text
    monkeypatch.setattr(product_routes, "get_profile", original_read)
    recovered = await test_client.get(service_path, headers=headers)
    assert recovered.status_code == 200 and recovered.json() == current.json()

    app.dependency_overrides[get_current_user] = lambda: source_member
    assert (await test_client.get(path)).json()["configuration"]["basic"]["documents"] == [source_ref]
    assert (await test_client.get(other_path)).status_code == 200
    assert (await test_client.get(foreign_path)).status_code == 404


@pytest.mark.asyncio
async def test_foreign_and_revoked_read(test_client, db_session, test_engine, app, source_member, monkeypatch):
    grant, pin_request, capture, source_path, read_body, clock = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock["now"])
    monkeypatch.setattr("src.shared.identity.product_scope.utcnow", lambda: clock["now"])
    other = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Other Tenant A Product"},
        headers={"Idempotency-Key": "scn02_other_a_123456"},
    )
    assert other.status_code == 201, other.text
    foreign_member = source_member.model_copy(update={"org_id": "scn02-tenant-b", "user_id": "scn02-human-b", "team_id": "scn02-team-b"})
    db_session.add(Organization(id=foreign_member.org_id, name="Separate workspace"))
    db_session.add(
        User(id=foreign_member.user_id, org_id=foreign_member.org_id, team_id=foreign_member.team_id, email="human-b@example.test", user_kind="human")
    )
    await add_team_member(db_session, foreign_member)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign_member
    foreign = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Private Tenant B Product"},
        headers={"Idempotency-Key": "scn02_other_b_123456"},
    )
    assert foreign.status_code == 201, foreign.text
    foreign_credential = UserCredential(
        org_id=foreign_member.org_id,
        user_id=foreign_member.user_id,
        service="document",
        credential_type="api_key",
        label="foreign-connection",
        secret_arn="test-secret",
    )
    db_session.add(foreign_credential)
    await db_session.flush()
    foreign_grant = CredentialServiceDelegation(
        tenant_id=foreign_member.org_id,
        credential_id=foreign_credential.id,
        service_id="different-worker",
        product_id=foreign.json()["product_id"],
        project_id=foreign.json()["project_id"],
        created_by=foreign_member.user_id,
        operations=["product:read"],
        sources=[],
        capabilities=[],
        visibility="restricted",
        expires_at=clock["now"] + timedelta(hours=2),
    )
    db_session.add(foreign_grant)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: source_member
    receipt, created = await store_snapshot(db_session, grant, pin_request, capture, "scn02-pinned")
    assert created
    db_session.add(
        ProductSourceBinding(
            tenant_id=grant.tenant_id,
            project_id=grant.project_id,
            credential_id=grant.credential_id,
            kind="document",
            provider="document",
            namespace="examples",
            native_id="not-delegated",
            desired={"name": "Hidden source", "source_revision": "r2"},
            observed={"source_revision": "r2"},
            status="healthy",
        )
    )
    await db_session.commit()

    permit = L5WorkPermit(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        source_connection_id=grant.credential_id,
        operation_id=grant.operation_id,
        attempt_id=grant.attempt_id,
        lease_fence=grant.lease_fence,
        binding_generation=grant.binding_generation,
        expires_at=clock["now"] + timedelta(hours=2),
        admitted_source_snapshot_ids=frozenset({receipt.snapshot_id}),
    )
    work_checks = []

    async def authorize(current, selection):
        work_checks.append((current.id, selection.attempt_id, selection.connection_ref.record_revision))
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    product_path = source_path.removesuffix("/source-snapshots")
    service_path_a = f"/internal/v1/products/{other.json()['product_id']}/projects/{other.json()['project_id']}"
    service_path_b = f"/internal/v1/products/{foreign.json()['product_id']}/projects/{foreign.json()['project_id']}"
    snapshot_read = f"{source_path}/{receipt.snapshot_id}/read"
    headers = {"X-L5-Delegation-ID": grant.id}
    scoped = await test_client.get(product_path, headers=headers)
    historical = await test_client.get(product_path + "/revisions/1", headers=headers)
    sources = await test_client.get(product_path + "/sources", headers=headers)
    pinned = await test_client.post(snapshot_read, json=read_body)
    assert scoped.status_code == historical.status_code == sources.status_code == pinned.status_code == 200
    assert scoped.json()["product_id"] == grant.product_id and historical.json()["revision"] == 1
    assert sources.json()["items"][0]["resources"] == [pin_request.source_ref.model_dump()]
    assert len(sources.json()["items"]) == 1 and "Hidden source" not in sources.text
    assert pinned.json() == receipt.model_dump(mode="json") and len(work_checks) == 2
    stale_attempt = await test_client.post(snapshot_read, json={**read_body, "attempt_id": "forged-attempt"})
    assert stale_attempt.status_code == 409 and "document-a" not in stale_attempt.text
    assert len(work_checks) == 2

    forged = {
        **headers,
        "X-User-ID": foreign_member.user_id,
        "X-Org-ID": foreign_member.org_id,
        "X-Agent-Registry-ID": "different-worker",
        "X-Initiating-Human-ID": foreign_member.user_id,
        "X-Source-Connection-ID": foreign_credential.id,
        "X-L5-Source-Ref": "not-delegated",
    }
    for target in (service_path_a, service_path_b):
        for suffix in ("", "/revisions/1", "/changes", "/project-bindings", "/bindings", "/sources"):
            denied = await test_client.get(target + suffix, headers=forged)
            assert denied.status_code == 404, (target, suffix, denied.text)
            assert denied.json() == {"detail": {"code": "not_found"}}
            assert "Private Tenant B Product" not in denied.text and "Other Tenant A Product" not in denied.text
            assert "items" not in denied.text and "Hidden source" not in denied.text
    impersonated = await test_client.get(service_path_b, headers={**forged, "X-L5-Delegation-ID": foreign_grant.id})
    assert impersonated.status_code == 404 and impersonated.json() == {"detail": {"code": "not_found"}}
    forged_source = await test_client.get(product_path + "/sources", headers=forged)
    assert forged_source.status_code == 200 and forged_source.json() == sources.json()
    for foreign_path in (service_path_a, service_path_b):
        wrong_snapshot = await test_client.post(f"{foreign_path}/source-snapshots/{receipt.snapshot_id}/read", json=read_body)
        assert wrong_snapshot.status_code == 404 and wrong_snapshot.json() == {"detail": {"code": "not_found"}}
    assert len(work_checks) == 2

    original_sources = source_routes.service_sources

    async def unavailable(*args, **kwargs):
        raise HTTPException(503, detail={"code": "transport_unavailable"})

    with monkeypatch.context() as failure:
        failure.setattr(source_routes, "service_sources", unavailable)
        interrupted_source = await test_client.get(product_path + "/sources", headers=forged)
        assert interrupted_source.status_code == 503 and "items" not in interrupted_source.text
    recovered_source = await test_client.get(product_path + "/sources", headers=forged)
    assert recovered_source.status_code == 200 and recovered_source.json() == forged_source.json()
    assert source_routes.service_sources is original_sources

    original_revision = product_routes.get_revision

    async def revoke_during_read(*args, **kwargs):
        read = await original_revision(*args, **kwargs)
        await db_session.execute(
            update(CredentialServiceDelegation)
            .where(CredentialServiceDelegation.id == grant.id)
            .values(revoked_at=clock["now"])
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        return read

    monkeypatch.setattr(product_routes, "get_revision", revoke_during_read)
    interrupted = await test_client.get(product_path + "/revisions/1", headers=forged)
    assert interrupted.status_code == 404 and interrupted.json() == {"detail": {"code": "not_found"}}
    monkeypatch.setattr(product_routes, "get_revision", original_revision)
    for suffix in ("", "/revisions/1", "/changes", "/project-bindings", "/bindings", "/sources"):
        denied = await test_client.get(product_path + suffix, headers=forged)
        assert denied.status_code == 404 and denied.json() == {"detail": {"code": "not_found"}}
    attempts = len(work_checks)
    inaccessible = await test_client.post(snapshot_read, json=read_body)
    assert inaccessible.status_code == 404 and inaccessible.json() == {"detail": {"code": "not_found"}}
    assert len(work_checks) == attempts

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as audit:
        stored = await audit.scalar(select(ProductSourceSnapshot).where(ProductSourceSnapshot.id == receipt.snapshot_id))
        revoked = await audit.scalar(select(CredentialServiceDelegation).where(CredentialServiceDelegation.id == grant.id))
        assert stored.receipt == receipt.model_dump(mode="json") and revoked.revoked_at is not None
        stored_history = await audit.scalar(
            select(ProductChange).where(
                ProductChange.product_id == grant.product_id,
                ProductChange.revision == 1,
            )
        )
        assert stored_history.revision == historical.json()["revision"]
        tenants = (
            await audit.scalars(
                select(Product).where(
                    Product.id.in_(
                        [
                            grant.product_id,
                            other.json()["product_id"],
                            foreign.json()["product_id"],
                        ]
                    )
                )
            )
        ).all()
        assert {(row.id, row.tenant_id) for row in tenants} == {
            (grant.product_id, grant.tenant_id),
            (other.json()["product_id"], grant.tenant_id),
            (foreign.json()["product_id"], foreign_member.org_id),
        }
    human = await test_client.get(f"/v1/products/{grant.product_id}/projects/{grant.project_id}/revisions/1")
    assert human.status_code == 200 and human.json()["revision"] == 1
    app.dependency_overrides[get_current_user] = lambda: foreign_member
    foreign_human = await test_client.get(f"/v1/products/{foreign.json()['product_id']}/projects/{foreign.json()['project_id']}")
    assert foreign_human.status_code == 200 and foreign_human.json()["product"]["name"] == "Private Tenant B Product"
    assert (await test_client.get(f"/v1/products/{grant.product_id}/projects/{grant.project_id}")).status_code == 404


@pytest.mark.asyncio
async def test_human_and_revision_compatibility(test_client, db_session, test_engine, app, source_member, monkeypatch):
    grant, pin_request, capture, source_path, read_body, clock = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock["now"])
    monkeypatch.setattr("src.shared.identity.product_scope.utcnow", lambda: clock["now"])
    receipt, created = await store_snapshot(db_session, grant, pin_request, capture, "scn03-original")
    assert created
    await db_session.commit()
    path = f"/v1/products/{grant.product_id}/projects/{grant.project_id}"
    service_path = "/internal" + path
    read_path = f"{source_path}/{receipt.snapshot_id}/read"
    edit_headers = {"Idempotency-Key": "scn03_human_edit_123456", "If-Profile-Revision": "1"}
    original = await test_client.get(path + "/revisions/1")
    edit = await test_client.patch(path, json={"purpose": "Purpose after pinned source"}, headers=edit_headers)
    replay = await test_client.patch(path, json={"purpose": "Purpose after pinned source"}, headers=edit_headers)
    current_human = await test_client.get(path)
    assert original.status_code == edit.status_code == replay.status_code == current_human.status_code == 200
    assert original.json()["revision"] == 1 and original.json()["product"]["purpose"] == REQUEST["purpose"]
    assert edit.json() == replay.json() and edit.json()["revision"] == current_human.json()["revision"] == 2
    assert current_human.json()["product"]["purpose"] == "Purpose after pinned source"

    foreign_member = source_member.model_copy(update={"org_id": "scn03-tenant-b", "user_id": "scn03-reader-b", "team_id": "scn03-team-b"})
    db_session.add(Organization(id=foreign_member.org_id, name="Isolated workspace"))
    db_session.add(
        User(
            id=foreign_member.user_id, org_id=foreign_member.org_id, team_id=foreign_member.team_id, email="reader-b@example.test", user_kind="human"
        )
    )
    await add_team_member(db_session, foreign_member)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign_member
    foreign = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Foreign private Product"},
        headers={"Idempotency-Key": "scn03_foreign_123456"},
    )
    assert foreign.status_code == 201 and (await test_client.get(path)).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: source_member

    permit = L5WorkPermit(
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        source_connection_id=grant.credential_id,
        operation_id=grant.operation_id,
        attempt_id=grant.attempt_id,
        lease_fence=grant.lease_fence,
        binding_generation=grant.binding_generation,
        expires_at=clock["now"] + timedelta(hours=4),
        admitted_source_snapshot_ids=frozenset({receipt.snapshot_id}),
    )
    work_checks = []

    async def authorize(current, selection):
        work_checks.append((current.id, selection.attempt_id, selection.connection_ref.record_revision))
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    headers = {"X-L5-Delegation-ID": grant.id}
    service = await test_client.get(service_path, headers=headers)
    historical = await test_client.get(service_path + "/revisions/1", headers=headers)
    pinned = await test_client.post(read_path, json=read_body)
    assert service.status_code == historical.status_code == pinned.status_code == 200
    assert service.json()["product"]["purpose"] == "Purpose after pinned source"
    assert historical.json()["product"]["purpose"] == REQUEST["purpose"]
    assert historical.headers["Profile-Revision"] == "1" and service.headers["Profile-Revision"] == "2"
    assert pinned.json() == receipt.model_dump(mode="json") and len(work_checks) == 2

    original_profile = product_routes.get_profile

    async def unavailable(*args, **kwargs):
        raise HTTPException(503, detail={"code": "transport_unavailable"})

    with monkeypatch.context() as fault:
        fault.setattr(product_routes, "get_profile", unavailable)
        interrupted = await test_client.get(service_path, headers=headers)
        assert interrupted.status_code == 503 and "Purpose after pinned source" not in interrupted.text
    recovered = await test_client.get(service_path, headers=headers)
    assert product_routes.get_profile is original_profile and recovered.status_code == 200
    assert recovered.json() == service.json()

    clock["now"] += timedelta(hours=3)
    await db_session.execute(
        update(ProductSourceBinding)
        .where(ProductSourceBinding.tenant_id == grant.tenant_id, ProductSourceBinding.native_id == "document-a")
        .values(desired={"name": "Document A updated", "source_revision": "r2"}, observed={"source_revision": "r2"})
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    expired = await test_client.post(read_path, json=read_body)
    assert expired.status_code == 410 and expired.json() == {"detail": {"code": "snapshot_expired"}}
    from src.products.source_capture import get_snapshot_objects

    class NeverReadStorage:
        def get(self, artifact):
            pytest.fail("Expired source bytes must not be fetched from storage")

    app.dependency_overrides[get_snapshot_objects] = lambda: NeverReadStorage()
    expired_content = await test_client.post(f"{source_path}/{receipt.snapshot_id}/content", json=read_body)
    assert expired_content.status_code == 410 and expired_content.json() == {"detail": {"code": "snapshot_expired"}}
    current_service = await test_client.get(service_path, headers=headers)
    still_historical = await test_client.get(service_path + "/revisions/1", headers=headers)
    assert current_service.status_code == still_historical.status_code == 200
    assert current_service.json()["revision"] == 2 and still_historical.json()["revision"] == 1
    assert still_historical.json()["product"]["purpose"] == REQUEST["purpose"]
    assert (await test_client.get(service_path + "/sources", headers=headers)).json()["items"] == []
    resumed_edit = await test_client.patch(path, json={"purpose": "Purpose after pinned source"}, headers=edit_headers)
    assert resumed_edit.status_code == 200 and resumed_edit.json()["revision"] == 2

    await db_session.execute(
        update(ProductChange)
        .where(ProductChange.product_id == grant.product_id, ProductChange.revision == 1)
        .values(recorded_at=clock["now"] - timedelta(days=91))
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    for endpoint, endpoint_headers in ((path + "/revisions/1", {}), (service_path + "/revisions/1", headers)):
        gone = await test_client.get(endpoint, headers=endpoint_headers)
        assert gone.status_code == 410 and gone.json() == {"detail": {"code": "revision_expired"}}
        assert REQUEST["purpose"] not in gone.text and "Purpose after pinned source" not in gone.text
    service_after = await test_client.get(service_path, headers=headers)
    human_after = await test_client.get(path)
    assert service_after.status_code == human_after.status_code == 200
    assert service_after.json()["product"]["purpose"] == human_after.json()["product"]["purpose"] == "Purpose after pinned source"

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as audit:
        stored = await audit.scalar(select(ProductSourceSnapshot).where(ProductSourceSnapshot.id == receipt.snapshot_id))
        changes = (
            await audit.scalars(select(ProductChange).where(ProductChange.product_id == grant.product_id).order_by(ProductChange.revision))
        ).all()
        assert stored.receipt == receipt.model_dump(mode="json")
        assert [change.revision for change in changes] == [1, 2]
        assert [change.snapshot["product"]["purpose"] for change in changes] == [REQUEST["purpose"], "Purpose after pinned source"]
        assert (await audit.scalar(select(Product.tenant_id).where(Product.id == foreign.json()["product_id"]))) == foreign_member.org_id
        source_binding = await audit.scalar(
            select(ProductSourceBinding).where(
                ProductSourceBinding.tenant_id == grant.tenant_id,
                ProductSourceBinding.native_id == "document-a",
            )
        )
        assert source_binding.observed["source_revision"] == "r2" and stored.receipt["source_ref"]["record_revision"] == "r1"
    app.dependency_overrides[get_current_user] = lambda: foreign_member
    foreign_path = f"/v1/products/{foreign.json()['product_id']}/projects/{foreign.json()['project_id']}"
    assert (await test_client.get(foreign_path)).status_code == 200
    assert (await test_client.get(path)).status_code == 404
    denied_service = await test_client.get("/internal" + foreign_path, headers=headers)
    assert denied_service.status_code == 404 and denied_service.json() == {"detail": {"code": "not_found"}}


@pytest.mark.asyncio
async def test_wired_service_read_routes(test_client, db_session, app, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    path = f"/internal/v1/products/{product_id}/projects/{project_id}"
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="document",
        credential_type="api_key",
        label="synthetic-document-connection",
        secret_arn="test-secret",
    )
    db_session.add(credential)
    await db_session.flush()
    permitted = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "document",
        "record_id": "doc-a",
        "record_revision": "r1",
    }
    grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=credential.id,
        service_id="registered-l5-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=[permitted],
        capabilities=[],
        visibility="restricted",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(grant)
    for native_id, name in (("doc-a", "Delegated document"), ("doc-b", "Foreign document")):
        db_session.add(
            ProductSourceBinding(
                tenant_id=source_member.org_id,
                project_id=project_id,
                credential_id=credential.id,
                kind="document",
                provider="document",
                namespace="examples",
                native_id=native_id,
                desired={"name": name, "source_revision": "r1"},
                observed={"source_revision": "r1"},
                status="healthy",
            )
        )
    await db_session.commit()
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
    headers = {"X-L5-Delegation-ID": grant.id}
    denied = await test_client.get(path, headers=headers)
    assert denied.status_code == 403

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified
    current = await test_client.get(path, headers=headers)
    assert current.status_code == 200, current.text
    assert current.json()["product_id"] == product_id
    assert current.headers["Profile-Revision"] == "1"
    old = await test_client.get(path + "/revisions/1", headers=headers)
    assert old.status_code == 200, old.text
    assert old.json()["revision"] == 1
    changes = await test_client.get(path + "/changes", headers=headers)
    assert changes.status_code == 200, changes.text
    assert [item["revision"] for item in changes.json()["items"]] == [1]
    assert changes.json()["items"][0]["source_revisions"] == []
    project_bindings = await test_client.get(path + "/project-bindings", headers=headers)
    assert project_bindings.status_code == 200, project_bindings.text
    bindings = await test_client.get(path + "/bindings", headers=headers)
    assert bindings.status_code == 200, bindings.text
    assert len(bindings.json()["items"]) == 1
    assert "Foreign document" not in bindings.text
    sources = await test_client.get(path + "/sources", headers=headers)
    assert sources.status_code == 200, sources.text
    assert sources.json()["items"][0]["resources"] == [permitted]
    assert sources.json()["items"][0]["event_support"] == {"document_revision": "unavailable"}
    assert "Foreign document" not in sources.text
    assert (await test_client.get(path, headers={"X-L5-Delegation-ID": "foreign"})).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,revocation",
    [
        ("revisions/1", "grant"),
        ("sources", "grant"),
        ("sources", "binding"),
    ],
)
async def test_history_and_sources_recheck_revoked_grant(test_client, db_session, app, source_member, monkeypatch, route, revocation):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    other = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Foreign Product"},
        headers={"Idempotency-Key": "foreign_revocation_123456"},
    )
    assert created.status_code == other.status_code == 201
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="document",
        credential_type="api_key",
        label="test-source",
        secret_arn="test-secret",
    )
    db_session.add(credential)
    await db_session.flush()
    allowed = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "document",
        "record_id": "doc-a",
        "record_revision": "r1",
    }
    grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=credential.id,
        service_id="registered-l5-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=[allowed],
        capabilities=[],
        visibility="restricted",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
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
            native_id="doc-a",
            desired={"name": "Delegated document", "source_revision": "r1"},
            observed={"source_revision": "r1"},
            status="healthy",
        )
    )
    await db_session.commit()
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
    prefix = f"/internal/v1/products/{product_id}/projects/{project_id}"
    headers = {"X-L5-Delegation-ID": grant.id}
    foreign_prefix = f"/internal/v1/products/{other.json()['product_id']}/projects/{other.json()['project_id']}"
    foreign = await test_client.get(f"{foreign_prefix}/{route}", headers=headers)
    assert foreign.status_code == 404
    assert "Foreign Product" not in foreign.text
    first = await test_client.get(f"{prefix}/{route}", headers=headers)
    assert first.status_code == 200, first.text
    if route == "sources":
        assert first.json()["items"][0]["resources"] == [allowed]
        module, method = source_routes, "service_visible_bindings"
    else:
        assert first.json()["revision"] == 1
        module, method = product_routes, "projected_profile"
    original = getattr(module, method)
    calls = 0

    async def revoke_during_read(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == (2 if route == "sources" else 1):
            if revocation == "grant":
                mutation = update(CredentialServiceDelegation).where(CredentialServiceDelegation.id == grant.id).values(revoked_at=datetime.now(UTC))
            else:
                mutation = update(ProductSourceBinding).where(ProductSourceBinding.native_id == "doc-a").values(status="revoked")
            await db_session.execute(mutation.execution_options(synchronize_session=False))
            await db_session.commit()
        return result

    monkeypatch.setattr(module, method, revoke_during_read)
    late = await test_client.get(f"{prefix}/{route}", headers=headers)
    assert calls == (2 if route == "sources" and revocation == "grant" else 3 if route == "sources" else 1)
    assert late.status_code == 404, late.text
    assert "Delegated document" not in late.text
    if revocation == "grant":
        assert await db_session.scalar(select(CredentialServiceDelegation.revoked_at).where(CredentialServiceDelegation.id == grant.id)) is not None
    else:
        assert await db_session.scalar(select(ProductSourceBinding.status).where(ProductSourceBinding.native_id == "doc-a")) == "revoked"
    again = await test_client.get(f"{prefix}/{route}", headers=headers)
    if revocation == "grant":
        assert again.status_code == 404
        assert "items" not in again.text
    else:
        assert again.status_code == 200
        assert again.json()["items"] == []
    monkeypatch.setattr(module, method, original)
    if route == "revisions/1":
        human = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}/{route}")
        assert human.status_code == 200, human.text
        assert human.json()["revision"] == 1


@pytest.mark.asyncio
async def test_forged_selectors_and_source_permissions(test_client, db_session, app, source_member):
    own = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    other = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Foreign Product"},
        headers={"Idempotency-Key": "foreign_selector_123456"},
    )
    assert own.status_code == other.status_code == 201
    product_id, project_id = own.json()["product_id"], own.json()["project_id"]
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="document",
        credential_type="api_key",
        label="selected-source",
        secret_arn="test-secret",
    )
    db_session.add(credential)
    await db_session.flush()
    permitted = {
        "tenant_id": source_member.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "document",
        "record_id": "selected-doc",
        "record_revision": "r1",
    }
    grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=credential.id,
        service_id="registered-l5-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=[permitted],
        capabilities=[],
        visibility="restricted",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    foreign_grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=credential.id,
        service_id="foreign-worker",
        product_id=other.json()["product_id"],
        project_id=other.json()["project_id"],
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=[],
        capabilities=[],
        visibility="restricted",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add_all([grant, foreign_grant])
    db_session.add(
        ProductSourceBinding(
            tenant_id=source_member.org_id,
            project_id=project_id,
            credential_id=credential.id,
            kind="document",
            provider="document",
            namespace="examples",
            native_id="selected-doc",
            desired={"name": "Allowed document", "source_revision": "r1"},
            observed={"source_revision": "r1"},
            status="healthy",
        )
    )
    await db_session.commit()
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
    prefix = f"/internal/v1/products/{product_id}/projects/{project_id}"
    foreign_prefix = f"/internal/v1/products/{other.json()['product_id']}/projects/{other.json()['project_id']}"
    grant_headers = {"X-L5-Delegation-ID": grant.id}
    baseline = await test_client.get(prefix, headers=grant_headers)
    assert baseline.status_code == 200, baseline.text
    granted_sources = await test_client.get(prefix + "/sources", headers=grant_headers)
    assert granted_sources.status_code == 200, granted_sources.text
    assert granted_sources.json()["items"][0]["resources"] == [permitted]
    granted_bindings = await test_client.get(prefix + "/bindings", headers=grant_headers)
    assert granted_bindings.status_code == 200, granted_bindings.text
    assert len(granted_bindings.json()["items"]) == 1

    db_session.add(
        ProductSourceBinding(
            tenant_id=source_member.org_id,
            project_id=project_id,
            credential_id=credential.id,
            kind="repository",
            provider="document",
            namespace="examples",
            native_id="foreign-repo",
            desired={"name": "Foreign repository", "source_revision": "r9"},
            observed={"source_revision": "r9"},
            status="healthy",
        )
    )
    await db_session.commit()
    spoofed = {
        **grant_headers,
        "X-User-ID": source_member.user_id,
        "X-Org-ID": source_member.org_id,
        "X-Agent-Registry-ID": "foreign-worker",
        "X-Initiating-Human-ID": source_member.user_id,
        "X-Source-Connection-ID": credential.id,
        "X-L5-Source-Ref": "foreign-repo",
    }
    for suffix in ("", "/revisions/1", "/changes", "/project-bindings", "/bindings", "/sources"):
        for target, headers in ((foreign_prefix, spoofed), (foreign_prefix, {**spoofed, "X-L5-Delegation-ID": foreign_grant.id})):
            denied = await test_client.get(target + suffix, headers=headers)
            assert denied.status_code == 404, (suffix, denied.text)
            assert "Foreign Product" not in denied.text
            assert "Foreign repository" not in denied.text
            assert "items" not in denied.text
    current = await test_client.get(prefix, headers=spoofed)
    assert current.status_code == 200, current.text
    assert current.json() == baseline.json()
    for suffix in ("/sources", "/bindings"):
        restricted = await test_client.get(prefix + suffix, headers=spoofed)
        assert restricted.status_code == 200, restricted.text
        assert restricted.json() == (granted_sources if suffix == "/sources" else granted_bindings).json()
        assert "Foreign repository" not in restricted.text
        assert "foreign-repo" not in restricted.text
    foreign_ref = {**permitted, "record_type": "repository", "record_revision": "r9"}
    foreign_product_ref = {
        **permitted,
        "source_connection_id": "adp.profile",
        "record_type": "product",
        "record_id": other.json()["product_id"],
        "record_revision": "1",
    }
    db_session.add(
        ProductSourceBinding(
            tenant_id=source_member.org_id,
            project_id=project_id,
            credential_id=credential.id,
            kind="repository",
            provider="document",
            namespace="examples",
            native_id="selected-doc",
            desired={"name": "Colliding foreign repository", "source_revision": "r9"},
            observed={"source_revision": "r9"},
            status="healthy",
        )
    )
    project = await db_session.get(Project, (source_member.org_id, project_id))
    project.configuration = {"basic": {"repositories": [foreign_ref], "roadmap": [foreign_product_ref]}}
    await db_session.commit()
    collision = await test_client.get(prefix, headers=spoofed)
    assert collision.status_code == 200, collision.text
    assert foreign_ref not in collision.json()["configuration"]["basic"]["repositories"]
    assert foreign_product_ref not in collision.json()["configuration"]["basic"]["roadmap"]
    assert collision.json()["progress"] == baseline.json()["progress"]
    assert "Colliding foreign repository" not in collision.text

    grant.sources = []
    grant.revision += 1
    await db_session.commit()
    revoked = await test_client.get(prefix + "/sources", headers=spoofed)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["items"] == []
    assert "Foreign repository" not in revoked.text

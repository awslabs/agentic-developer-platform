"""Product mutations commit receipts and their dependent journal rows together."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductOperation, ProductProjectBinding, Project
from src.products.routes import router
from src.shared.database import get_db
from src.shared.models.organization import Organization, User
from tests.migrations.conftest_postgres import to_async_url, upgrade
from tests.products.membership import add_team_member


@pytest.mark.parametrize(
    ("operation_name", "create_project"),
    [("bindProject", False), ("bindProject", True), ("unbindProject", False), ("updateProject", False)],
    ids=["bindProject", "createProject", "unbindProject", "updateProject"],
)
async def test_product_mutation_commits_receipt_and_journal(pg_url, valid_token_context, monkeypatch, operation_name, create_project):
    upgrade(pg_url, "head")
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    changed_at = datetime(2026, 2, 2, tzinfo=UTC)
    bound_at = datetime(2026, 2, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: changed_at)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = valid_token_context
    product_id = "receipt-order-product"
    configuration = {"governance": {"criticality": "low"}}
    try:
        async with sessions() as session:
            session.add(Organization(id=actor.org_id, name="Receipt fixture"))
            await session.flush()
            session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="owner@example.test", user_kind="human"))
            await add_team_member(session, actor)
            session.add(
                Product(
                    tenant_id=actor.org_id,
                    id=product_id,
                    name="Planner",
                    purpose="Verify receipts",
                    intended_users=["Maintainers"],
                    owner_user_id=actor.user_id,
                    team_id=actor.team_id,
                    created_by=actor.user_id,
                    starting_point="idea",
                )
            )
            session.add_all(
                [
                    Project(tenant_id=actor.org_id, id=project_id, name=project_id, owner_user_id=actor.user_id, configuration=configuration)
                    for project_id in ("first-scope", "second-scope", "unbound-scope")
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ProductProjectBinding(
                        tenant_id=actor.org_id,
                        product_id=product_id,
                        project_id=project_id,
                        relationship_revision=7,
                        valid_from=bound_at,
                        recorded_at=bound_at,
                        provenance={"source": "test"},
                    )
                    for project_id in ("first-scope", "second-scope")
                ]
            )
            await session.commit()

        async def database():
            async with sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: actor
        path = f"/v1/products/{product_id}/projects"
        project_id = "unbound-scope" if operation_name == "bindProject" else "second-scope"
        request_headers = {"Idempotency-Key": f"postgres_receipt_{operation_name}", "If-Profile-Revision": "1"}
        if operation_name == "bindProject":
            method, url, body = "POST", path, {"project_id": project_id}
            if create_project:
                body = {"mode": "create", "name": "Created scope"}
        elif operation_name == "unbindProject":
            method, url, body = "DELETE", f"{path}/{project_id}", None
        else:
            method, url, body = "PATCH", f"{path}/{project_id}/configuration", {"name": "Updated", "configuration": {"basic": {}}}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            result = await client.request(method, url, json=body, headers=request_headers)
            expected_status = 201 if create_project else 200
            assert result.status_code == expected_status, result.text
            assert result.json()["revision"] == 2
            if create_project:
                project_id = result.json()["project_id"]
            replay = await client.request(method, url, json=body, headers=request_headers)
            assert replay.status_code == expected_status and replay.json() == result.json()

        async with sessions() as readback:
            operation = (await readback.scalars(select(ProductOperation))).one()
            change = (await readback.scalars(select(ProductChange))).one()
            product = await readback.get(Product, (actor.org_id, product_id))
            project = await readback.get(Project, (actor.org_id, project_id))
            bindings = (await readback.scalars(select(ProductProjectBinding).where(ProductProjectBinding.valid_to.is_(None)))).all()
            if create_project:
                assert len((await readback.scalars(select(Project))).all()) == 4
                assert project.name == "Created scope"
            assert product.revision == change.revision == operation.receipt["revision"] == 2
            assert operation.operation == operation_name
            assert operation.actor_id == change.actor_id == actor.user_id
            assert change.operation_id == operation.id
            # Receipts retain configuration, while reads add an authorized, live catalogue.
            persisted_profile = dict(result.json())
            shared = persisted_profile.pop("shared_profile")
            assert shared["product_id"] == product_id and shared["revision"] == product.revision
            assert shared["resources"] == []
            persisted_profile["profile_configuration"] = product.profile_configuration or {}
            assert operation.receipt["profile"] == persisted_profile
            assert operation.project_id == change.project_id == project_id
            if operation_name == "unbindProject":
                remaining = await readback.get(Project, (actor.org_id, "first-scope"))
                assert result.json()["project_id"] == remaining.id
                assert result.json()["project"] == {
                    "id": remaining.id,
                    "tenant_id": remaining.tenant_id,
                    "name": remaining.name,
                    "owner_user_id": remaining.owner_user_id,
                    "state": remaining.state,
                }
                assert result.json()["configuration"] == remaining.configuration
                assert result.json()["provenance"] == remaining.provenance
                assert change.snapshot == {
                    **persisted_profile,
                    "project_id": project_id,
                    "project": {
                        "id": project.id,
                        "tenant_id": project.tenant_id,
                        "name": project.name,
                        "owner_user_id": project.owner_user_id,
                        "state": project.state,
                    },
                    "configuration": project.configuration,
                    "provenance": project.provenance,
                }
            else:
                assert change.snapshot == persisted_profile
                assert change.snapshot["project_id"] == project_id
            expected_count = {"bindProject": 3, "unbindProject": 1, "updateProject": 2}[operation_name]
            assert len(bindings) == expected_count
            if operation_name == "updateProject":
                assert project.name == "Updated" and project.configuration == {"basic": {}}
                assert change.changed_paths == ["/project/name", "/project/configuration"]
            else:
                assert project.configuration == ({} if create_project else configuration)
                binding = (
                    await readback.scalars(
                        select(ProductProjectBinding).where(
                            ProductProjectBinding.tenant_id == actor.org_id,
                            ProductProjectBinding.product_id == product_id,
                            ProductProjectBinding.project_id == project_id,
                        )
                    )
                ).one()
                reference = {
                    "tenant_id": actor.org_id,
                    "project_id": project_id,
                    "source_connection_id": "adp.profile",
                    "record_revision": change.id,
                }
                product_ref = {**reference, "record_type": "product", "record_id": product_id}
                project_ref = {**reference, "record_type": "project", "record_id": project_id}
                expected_version = {
                    "relationship_id": binding.id,
                    "relationship_type": "product_project",
                    "from_ref": product_ref,
                    "to_ref": project_ref,
                    "valid_from": "2026-02-02T00:00:00Z" if operation_name == "bindProject" else "2026-02-01T00:00:00Z",
                    "recorded_at": "2026-02-02T00:00:00Z",
                    "mapping_version": "profile-registry-1",
                    "source_refs": [product_ref, project_ref],
                    "source_connection_id": "adp.profile",
                    "relationship_revision": "1" if operation_name == "bindProject" else "8",
                }
                assert binding.mapping_version == "profile-registry-1"
                assert binding.source_connection_id == "adp.profile"
                assert change.recorded_at == changed_at
                if operation_name == "unbindProject":
                    expected_version.update(valid_to="2026-02-02T00:00:00Z", supersedes_revision="7")
                    assert binding.relationship_revision == 8 and binding.supersedes_revision == 7
                    assert binding.valid_from == bound_at and binding.valid_to == changed_at
                else:
                    assert binding.relationship_revision == 1 and binding.supersedes_revision is None
                    assert binding.valid_from == changed_at and binding.valid_to is None
                assert change.relationship_versions == [expected_version]
    finally:
        await engine.dispose()

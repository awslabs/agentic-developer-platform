"""Scope registry relationships cannot be mixed across Projects or tenants."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from src.products.models import ConnectorCell, ConnectorCollectionGrant, ConnectorPolicyRealm, ProductSourceBinding, Project


@pytest.mark.asyncio
async def test_cell_registry_enforces_realm_project_and_unique_route(db_session):
    db_session.add_all(
        [Project(tenant_id="tenant-a", id=project_id, name=project_id, owner_user_id="owner-a") for project_id in ("project-a", "project-b")]
    )
    db_session.add(
        ConnectorPolicyRealm(
            tenant_id="tenant-a",
            project_id="project-a",
            id="realm-a",
            revision=1,
            descriptor_digest="a" * 64,
            descriptor={"visibility": "private"},
            source_policy_ref="source-a",
            evidence_policy_ref="evidence-a",
        )
    )
    await db_session.commit()
    await db_session.execute(text("PRAGMA foreign_keys=ON"))

    route = dict(
        tenant_id="tenant-a",
        project_id="project-a",
        realm_id="realm-a",
        realm_revision=1,
        database_ref="admitted-db",
        role_ref="admitted-role",
        object_prefix="admitted-prefix/",
        workload_id="registered-workload",
        audience="private",
    )
    db_session.add(ConnectorCell(id="cell-a", **route))
    await db_session.commit()
    assert (await db_session.scalar(select(ConnectorCell.id))) == "cell-a"

    for invalid in ({**route, "project_id": "project-b"}, route):
        db_session.add(ConnectorCell(id="another-cell", **invalid))
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


@pytest.mark.asyncio
async def test_grant_cannot_cross_project_even_within_tenant(db_session):
    db_session.add_all(
        [Project(tenant_id="tenant-a", id=project_id, name=project_id, owner_user_id="owner-a") for project_id in ("project-a", "project-b")]
    )
    db_session.add(
        ProductSourceBinding(
            tenant_id="tenant-a",
            id="binding-a",
            project_id="project-a",
            kind="repository",
            provider="github",
            credential_id="credential-a",
            namespace="example",
            native_id="repository-a",
            desired={},
        )
    )
    db_session.add(
        ConnectorPolicyRealm(
            tenant_id="tenant-a",
            project_id="project-b",
            id="realm-b",
            revision=1,
            descriptor_digest="b" * 64,
            descriptor={"visibility": "private"},
            source_policy_ref="source-b",
            evidence_policy_ref="evidence-b",
        )
    )
    await db_session.commit()
    db_session.add(
        ConnectorCell(
            tenant_id="tenant-a",
            id="cell-b",
            project_id="project-b",
            realm_id="realm-b",
            realm_revision=1,
            database_ref="db-b",
            role_ref="role-b",
            object_prefix="prefix-b/",
            workload_id="registered-workload",
            audience="private",
        )
    )
    await db_session.commit()
    await db_session.execute(text("PRAGMA foreign_keys=ON"))

    db_session.add(
        ConnectorCollectionGrant(
            tenant_id="tenant-a",
            id="grant-b",
            project_id="project-b",
            binding_id="binding-a",
            binding_generation=1,
            cell_id="cell-b",
            consumer="insights",
            purpose="collection",
            source_selections=["repository-a"],
            granting_actor_id="member-b",
            policy_revision=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    assert (await db_session.scalars(select(ConnectorCollectionGrant))).all() == []

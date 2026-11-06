"""Scoped source selections retain immutable provider identity, not credentials."""

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from src.products.models import ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.mark.asyncio
async def test_source_binding_schema(test_engine):
    async with test_engine.connect() as connection:
        columns = await connection.run_sync(lambda raw: {column["name"] for column in inspect(raw).get_columns("product_source_bindings")})
    assert {"tenant_id", "project_id", "installation_id", "credential_id", "namespace", "native_id", "desired", "observed"} <= columns
    assert not {"secret", "token", "password"} & columns


@pytest.mark.asyncio
async def test_source_binding_exact_identity_and_unique_active_selection(test_client, db_session, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    project_id = created.json()["project_id"]
    mapping = ChannelTenantMap(provider="github", provider_scope_id="example-workspace", org_id=source_member.org_id)
    db_session.add(mapping)
    await db_session.flush()
    selection = {
        "kind": "repository",
        "provider": "github",
        "namespace": "example-workspace",
        "native_id": "0004321",
        "connection_ref": {"kind": "github_installation", "id": mapping.id},
    }
    binding = ProductSourceBinding(
        tenant_id=source_member.org_id,
        project_id=project_id,
        kind=selection["kind"],
        provider=selection["provider"],
        namespace=selection["namespace"],
        native_id=selection["native_id"],
        installation_id=mapping.id,
        desired=selection,
    )
    db_session.add(binding)
    await db_session.commit()
    mapping_id = mapping.id
    db_session.expire_all()
    saved = (await db_session.scalars(select(ProductSourceBinding))).one()
    assert saved.native_id == "0004321" and saved.namespace == "example-workspace"
    assert saved.desired == selection and saved.status == "unverified" and saved.observed is None
    assert saved.installation_id == mapping_id and saved.credential_id is None

    db_session.add(
        ProductSourceBinding(
            tenant_id=source_member.org_id,
            project_id=project_id,
            kind="repository",
            provider="github",
            namespace="example-workspace",
            native_id="0004321",
            installation_id=mapping_id,
            desired=selection,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_source_binding_requires_one_connection(test_client, db_session, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201
    db_session.add(
        ProductSourceBinding(
            tenant_id=source_member.org_id,
            project_id=created.json()["project_id"],
            kind="repository",
            provider="github",
            namespace="example-workspace",
            native_id="0004321",
            desired={},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

"""Retained native-agent identity cannot cross project scope."""

import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.api.bindings import claim_retained_agent_scope
from l5_services.migrations.versions import l5_finops_retained_scope
from l5_services.models.binding import RetainedAgentScope


IDENTITY = {"account_id": "123456789012", "region": "us-east-1"}
AGENT = {"record_type": "finops_agent", "record_id": "agent-example"}


async def claim(session, tenant_id, project_id, binding_id="binding-example"):
    await claim_retained_agent_scope(
        session, tenant_id=tenant_id, product_id="product-example", project_id=project_id,
        binding_id=binding_id, provider_identity=IDENTITY, native_resource_ref=AGENT,
    )


@pytest.mark.asyncio
async def test_retained_agent_scope_masks_cross_project_and_tenant_collisions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(RetainedAgentScope.__table__.create)
    try:
        async with sessions() as session:
            await claim(session, "tenant-one", "project-one")
            await session.commit()
        async with sessions() as session:
            await claim(session, "tenant-one", "project-one")
            await session.commit()
        for tenant_id, project_id in (("tenant-one", "project-two"),
                                      ("tenant-two", "project-one")):
            async with sessions() as session:
                with pytest.raises(HTTPException) as failure:
                    await claim(session, tenant_id, project_id)
                assert failure.value.status_code == 409
                assert failure.value.detail == {"code": "native_scope_conflict"}
        async with sessions() as session:
            with pytest.raises(HTTPException) as failure:
                await claim(session, "tenant-one", "project-one", "other-binding")
            assert failure.value.detail == {"code": "native_scope_conflict"}
            with pytest.raises(HTTPException) as failure:
                await claim_retained_agent_scope(
                    session, tenant_id="tenant-one", product_id="product-example",
                    project_id="project-one", binding_id="binding-example",
                    provider_identity=IDENTITY,
                    native_resource_ref={"record_type": "finops_agent", "record_id": ""},
                )
            assert failure.value.detail == {"code": "native_identity_unconfirmed"}
        async with sessions() as reader:
            rows = (await reader.scalars(sa.select(RetainedAgentScope))).all()
            assert len(rows) == 1
            assert rows[0].tenant_id == "tenant-one" and rows[0].project_id == "project-one"
    finally:
        await engine.dispose()


def test_migration_quarantines_historical_cross_scope_collision():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    bindings = sa.Table("l5_service_bindings", metadata,
                        sa.Column("tenant_id", sa.String()), sa.Column("product_id", sa.String()),
                        sa.Column("project_id", sa.String()), sa.Column("binding_id", sa.String()),
                        sa.Column("service_id", sa.String()),
                        sa.Column("provider_identity", sa.JSON()),
                        sa.Column("native_resource_ref", sa.JSON()))
    with engine.begin() as connection:
        metadata.create_all(connection)
        for tenant_id, project_id in (("tenant-one", "project-one"),
                                      ("tenant-two", "project-two")):
            connection.execute(bindings.insert().values(
                tenant_id=tenant_id, product_id="product-example", project_id=project_id,
                binding_id=f"binding-{tenant_id}", service_id="finops",
                provider_identity=IDENTITY, native_resource_ref=AGENT,
            ))
        with Operations.context(MigrationContext.configure(connection)):
            l5_finops_retained_scope.upgrade()
        row = connection.execute(sa.select(RetainedAgentScope.__table__)).mappings().one()
        assert row["identity_digest"] and row["conflicted"] is True
        with Operations.context(MigrationContext.configure(connection)):
            l5_finops_retained_scope.downgrade()
    engine.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_cross_tenant_claim():
    url = os.environ.get("L5_FINOPS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires an existing dedicated PostgreSQL test database")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        exists = await connection.run_sync(lambda sync: sa.inspect(sync).has_table(
            RetainedAgentScope.__tablename__))
        if exists:
            pytest.skip("dedicated test database must not contain the retained scope table")
        await connection.run_sync(RetainedAgentScope.__table__.create)
    try:
        async def contender(tenant_id):
            async with sessions() as session:
                try:
                    await claim(session, tenant_id, "project-example")
                    await session.commit()
                    return "won"
                except HTTPException as failure:
                    assert failure.status_code == 409
                    return "denied"

        import asyncio
        assert sorted(await asyncio.gather(contender("tenant-one"), contender("tenant-two"))) == [
            "denied", "won"]
        async with sessions() as reader:
            assert len((await reader.scalars(sa.select(RetainedAgentScope))).all()) == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(RetainedAgentScope.__table__.drop)
        await engine.dispose()

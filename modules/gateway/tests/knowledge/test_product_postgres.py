"""Registry routes and ownership migrations exercised on isolated PostgreSQL 16."""

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.knowledge import routes
from src.knowledge.accessibility import AccessibilityResult
from src.knowledge.schemas import AssetCreateRequest, BulkCommitItem, BulkCommitRequest
from src.products.models import Product
from src.shared.models.base import Base, utcnow
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

MIGRATIONS = Path(__file__).resolve().parents[4] / "modules/agent-context/alembic/versions"
A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def migrate(conn, name, method="upgrade"):
    path = MIGRATIONS / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(conn)):
        getattr(module, method)()


@pytest.fixture
async def registry(pg_url, monkeypatch):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    tables = [Organization.__table__, Team.__table__, User.__table__, TeamMembership.__table__, TenantMembership.__table__, Product.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
        for name in (
            "007_knowledge_assets.py",
            "008_knowledge_assets_registry_columns.py",
            "012_partial_unique_index_exclude_removed.py",
            "013_ingestion_callback_attempt.py",
        ):
            await conn.run_sync(lambda c, name=name: migrate(c, name))
        await conn.execute(text("CREATE TABLE index_runs (id uuid PRIMARY KEY, status text, started_at timestamptz)"))
        await conn.execute(
            text(
                "CREATE TABLE index_run_stages (run_id uuid, repo text, stage text, status text, artifact_ref text, error text, "
                "started_at timestamptz, completed_at timestamptz, metrics jsonb, worker_pod text)"
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all([Organization(id="org-a", name="A"), Organization(id="org-b", name="B")])
        db.add_all([Team(id=t, org_id="org-a", department_id="d", name=t) for t in ("team-a", "team-b")])
        db.add_all(
            [
                User(id=u, org_id="org-a", team_id=t, email=u + "@example.test", cognito_sub="login-" + u)
                for u, t in (("owner", "team-b"), ("member", "team-a"), ("peer", "team-a"), ("outsider", "team-b"))
            ]
        )
        await db.flush()
        db.add_all(
            [
                TeamMembership(user_id=u, team_id=t, org_id="org-a")
                for u, t in (("owner", "team-b"), ("member", "team-a"), ("peer", "team-a"), ("outsider", "team-b"))
            ]
        )
        db.add_all(
            [
                Product(
                    id=p,
                    tenant_id="org-a",
                    name=p,
                    purpose="test",
                    intended_users=[],
                    owner_user_id="owner",
                    team_id=t,
                    starting_point="new",
                    created_by="owner",
                )
                for p, t in ((A, "team-a"), (B, "team-b"))
            ]
        )
        await db.commit()
    monkeypatch.setenv("SCOPED_KNOWLEDGE_IMPORTS_ENABLED", "true")
    monkeypatch.setattr(routes, "admit_source", AsyncMock())
    monkeypatch.setattr(routes, "dispatch_ingestion", AsyncMock())
    monkeypatch.setattr(routes, "validate_repo_accessibility", AsyncMock(return_value=AccessibilityResult(allowed=True, shared=True)))

    async def upgrade():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: migrate(c, "015_product_knowledge.py"))

    try:
        yield SimpleNamespace(engine=engine, sessions=sessions, upgrade=upgrade)
    finally:
        await engine.dispose()


def actor(uid="member", admin=False, org="org-a"):
    return SimpleNamespace(user_id="login-" + uid, org_id=org, is_admin=admin, account_type="human", username="")


async def create(r, uid="member", scope="product", product=A, source="https://example.com/guide", title=None):
    async with r.sessions() as db, r.sessions() as gateway:
        return await routes.register_asset(
            AssetCreateRequest(
                asset_type="url", source_ref=source, display_name=title, scope=scope, product_id=product if scope == "product" else None
            ),
            db,
            gateway,
            actor(uid),
        )


async def test_migration_preserves_ownership_and_null_duplicate_semantics(registry):
    r = registry
    old = await create(r, scope="personal")
    await r.upgrade()
    one = await create(r)
    two = await create(r, uid="owner", product=B)
    assert len({old.id, one.id, two.id}) == 3
    async with r.sessions() as db:
        row = (await db.execute(text("SELECT tenant_id,owner_sub,product_id FROM knowledge_assets WHERE id=:id"), {"id": old.id})).one()
        assert tuple(row) == ("org-a", "member", None)
        for product, owner in ((A, None), (None, "member")):
            with pytest.raises(IntegrityError):
                await db.execute(
                    text(
                        "INSERT INTO knowledge_assets(id,asset_type,source_ref,tenant_id,owner_sub,product_id) VALUES (:id,'url','https://example.com/guide','org-a',:owner,:p)"
                    ),
                    {"id": uuid.uuid4(), "owner": owner, "p": product},
                )
            await db.rollback()
        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    "INSERT INTO knowledge_assets(id,asset_type,source_ref,tenant_id,owner_sub,product_id) VALUES (:id,'url','https://example.com/invalid','org-a','member',:p)"
                ),
                {"id": uuid.uuid4(), "p": A},
            )
        await db.rollback()
        await db.execute(text("UPDATE knowledge_assets SET status='removed' WHERE id=:id"), {"id": one.id})
        await db.commit()
    assert (await create(r)).id != one.id
    async with r.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="Cannot remove Product ownership"):
            await conn.run_sync(lambda c: migrate(c, "015_product_knowledge.py", "downgrade"))


async def test_old_schema_routes_work_and_product_writes_explain_readiness(registry):
    r = registry
    own = await create(r, scope="personal")
    async with r.sessions() as db, r.sessions() as gateway:
        listing = await routes.list_assets(db, gateway, actor(), scope="personal")
        assert [x.id for x in listing.items] == [own.id]
        assert listing.quota.urls.used == 1
        assert (await routes.get_asset_detail(own.id, db, gateway, actor())).can_manage
    with pytest.raises(HTTPException) as exc:
        await create(r)
    assert exc.value.status_code == 503 and "storage" in exc.value.detail


async def test_current_product_member_owner_and_nonmember_list_detail_status(registry):
    r = registry
    await r.upgrade()
    own = await create(r)
    await create(r, uid="owner", product=B)
    await create(r, scope="personal")
    for uid in ("member", "peer"):
        async with r.sessions() as db, r.sessions() as gateway:
            listing = await routes.list_assets(db, gateway, actor(uid), scope="product", product_id=A)
            assert [x.id for x in listing.items] == [own.id]
            assert listing.quota.urls.used == 1
            assert (await routes.get_asset_detail(own.id, db, gateway, actor(uid))).can_manage is (uid != "peer")
            assert (await routes.get_asset_status(own.id, db, gateway, actor(uid))).asset_id == own.id
    for uid in ("owner", "outsider"):
        async with r.sessions() as db, r.sessions() as gateway:
            for fn in (routes.get_asset_detail, routes.get_asset_status):
                with pytest.raises(HTTPException) as exc:
                    await fn(own.id, db, gateway, actor(uid))
                assert exc.value.status_code == 404
            with pytest.raises(HTTPException) as exc:
                await routes.list_assets(db, gateway, actor(uid), scope="product", product_id=A)
            assert exc.value.status_code == 404
    async with r.sessions() as db, r.sessions() as gateway:
        administrator = actor("outsider", admin=True)
        assert [x.id for x in (await routes.list_assets(db, gateway, administrator, scope="product", product_id=A)).items] == [own.id]
        assert not (await routes.get_asset_detail(own.id, db, gateway, administrator)).can_manage
        assert (await routes.get_asset_status(own.id, db, gateway, administrator)).asset_id == own.id
        assert all(x.product_id is None for x in (await routes.list_assets(db, gateway, administrator)).items)
    async with r.sessions() as gateway:
        await gateway.execute(update(TeamMembership).where(TeamMembership.user_id == "member").values(team_id="team-b"))
        await gateway.commit()
    async with r.sessions() as db, r.sessions() as gateway:
        with pytest.raises(HTTPException):
            await routes.list_assets(db, gateway, actor(), scope="product", product_id=A)
        with pytest.raises(HTTPException):
            await routes.get_asset_detail(own.id, db, gateway, actor())


async def test_manage_gate_reindex_provenance_delete_and_removed_visibility(registry, monkeypatch):
    r = registry
    await r.upgrade()
    own = await create(r)
    async with r.sessions() as db:
        await db.execute(text("UPDATE knowledge_assets SET status='complete' WHERE id=:id"), {"id": own.id})
        await db.commit()
    async with r.sessions() as db, r.sessions() as gateway:
        for fn in (routes.delete_asset, routes.reindex_asset):
            with pytest.raises(HTTPException) as exc:
                await fn(own.id, db, gateway, actor("peer"))
            assert exc.value.status_code == 403
        monkeypatch.setenv("SCOPED_KNOWLEDGE_IMPORTS_ENABLED", "false")
        with pytest.raises(HTTPException) as exc:
            await routes.reindex_asset(own.id, db, gateway, actor())
        assert exc.value.status_code == 503
        monkeypatch.setenv("SCOPED_KNOWLEDGE_IMPORTS_ENABLED", "true")
        await routes.reindex_asset(own.id, db, gateway, actor())
        assert routes.dispatch_ingestion.await_args.kwargs["product_id"] == A
        gateway.add(TeamMembership(user_id="owner", team_id="team-a", org_id="org-a"))
        await gateway.commit()
        await routes.delete_asset(own.id, db, gateway, actor("owner"))
        for fn in (routes.get_asset_detail, routes.get_asset_status, routes.reindex_asset):
            with pytest.raises(HTTPException) as exc:
                await fn(own.id, db, gateway, actor())
            assert exc.value.status_code == 404


async def test_revoked_tenant_and_archived_product(registry):
    r = registry
    await r.upgrade()
    own = await create(r)
    async with r.sessions() as gateway:
        gateway.add(TeamMembership(user_id="owner", team_id="team-a", org_id="org-a"))
        gateway.add(TenantMembership(user_id="member", tenant_id="org-a", revoked_at=utcnow()))
        await gateway.execute(update(Product).where(Product.id == A).values(archived_at=utcnow()))
        await gateway.commit()
    async with r.sessions() as db, r.sessions() as gateway:
        with pytest.raises(HTTPException):
            await routes.get_asset_detail(own.id, db, gateway, actor())
        assert not (await routes.get_asset_detail(own.id, db, gateway, actor("owner"))).can_manage
    with pytest.raises(HTTPException) as exc:
        await create(r, uid="owner", source="https://example.com/new")
    assert exc.value.status_code == 409


async def test_product_knowledge_revokes_cached_team_owner_and_org_admin_access(registry):
    r = registry
    await r.upgrade()
    own = await create(r)
    async with r.sessions() as gateway:
        await gateway.execute(delete(TeamMembership).where(TeamMembership.user_id == "member"))
        # A stale primary-team pointer and an organization role confer no access.
        await gateway.execute(update(User).where(User.id == "member").values(team_id="team-a", role="org_admin"))
        await gateway.execute(update(Product).where(Product.id == A).values(owner_user_id="member"))
        await gateway.commit()
    async with r.sessions() as db, r.sessions() as gateway:
        for fn in (routes.get_asset_detail, routes.get_asset_status, routes.delete_asset, routes.reindex_asset):
            with pytest.raises(HTTPException) as exc:
                await fn(own.id, db, gateway, actor())
            assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await create(r, source="https://example.com/blocked")
    assert exc.value.status_code == 404


async def test_product_knowledge_teamless_is_platform_admin_only_and_tenant_bound(registry):
    r = registry
    await r.upgrade()
    own = await create(r)
    async with r.sessions() as gateway:
        await gateway.execute(update(Product).where(Product.id == A).values(team_id=None))
        await gateway.commit()
    async with r.sessions() as db, r.sessions() as gateway:
        for uid in ("member", "owner"):
            with pytest.raises(HTTPException) as exc:
                await routes.get_asset_detail(own.id, db, gateway, actor(uid))
            assert exc.value.status_code == 404
        assert (await routes.get_asset_detail(own.id, db, gateway, actor("outsider", admin=True))).id == own.id
        # Platform authority does not substitute a different requested tenant.
        with pytest.raises(HTTPException) as exc:
            await routes.get_asset_detail(own.id, db, gateway, actor("outsider", admin=True, org="org-b"))
        assert exc.value.status_code == 404


async def test_personal_search_and_admin_privacy_and_cross_tenant(registry):
    r = registry
    await r.upgrade()
    own = await create(r, scope="personal", title="100% guide")
    await create(r, uid="peer", scope="personal", source="https://example.com/peer")
    await create(r, title="100% Product guide")
    async with r.sessions() as db, r.sessions() as gateway:
        result = await routes.list_assets(db, gateway, actor(admin=True), scope="personal", search="100%")
        assert [x.id for x in result.items] == [own.id] and result.quota.urls.used == 1
        assert [x.id for x in (await routes.list_assets(db, gateway, actor(admin=True))).items] == [own.id]
        assert (await routes.list_assets(db, gateway, actor(), scope="personal", search="' OR 1=1 --")).total == 0
        for user in (actor("outsider", admin=True), actor(org="org-b")):
            with pytest.raises(HTTPException) as exc:
                await routes.get_asset_detail(own.id, db, gateway, user)
            assert exc.value.status_code == 404


async def test_public_repository_retains_personal_scope(registry):
    r = registry
    await r.upgrade()
    async with r.sessions() as db, r.sessions() as gateway:
        own = await routes.register_asset(
            AssetCreateRequest(asset_type="repo", source_ref="https://github.com/example/repo", scope="personal"), db, gateway, actor()
        )
        assert own.tenant_id == "org-a" and own.owner_sub == "member" and own.product_id is None


async def test_product_bulk_and_rollout_flag(registry, monkeypatch):
    r = registry
    await r.upgrade()
    monkeypatch.setenv("SCOPED_KNOWLEDGE_IMPORTS_ENABLED", "false")
    with pytest.raises(HTTPException) as exc:
        await create(r)
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc:
        await create(r, scope="personal")
    assert exc.value.status_code == 503
    monkeypatch.setenv("SCOPED_KNOWLEDGE_IMPORTS_ENABLED", "true")
    async with r.sessions() as db, r.sessions() as gateway:
        body = BulkCommitRequest(
            scope="product",
            product_id=A,
            items=[
                BulkCommitItem(asset_type="url", source_ref="https://example.com/one"),
                BulkCommitItem(asset_type="url", source_ref="https://example.com/two"),
            ],
        )
        result = await routes.bulk_commit(body, db, gateway, actor())
        assert result.created == 2 and all(x.product_id == A and x.owner_sub is None for x in result.assets)
        assert all(x.kwargs["product_id"] == A for x in routes.dispatch_ingestion.await_args_list)


async def test_dispatch_binds_scope_and_generation_before_publish(registry, monkeypatch):
    from src.knowledge import dispatch
    from tests.knowledge.test_dispatch import FakeSQSClient

    r = registry
    await r.upgrade()
    own = await create(r)
    client = FakeSQSClient()
    monkeypatch.setattr(dispatch, "admit_source", AsyncMock())
    monkeypatch.setattr(dispatch, "get_sqs_client", lambda: client)
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-dispatch-signing-key" * 3)
    monkeypatch.setenv("INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/test")
    args = dict(asset_id=own.id, asset_type="url", source_ref=own.source_ref, tenant_id="org-a", owner_sub=None, project_id=None)
    async with r.sessions() as db:
        assert await dispatch.dispatch_ingestion(**args, product_id=B, db=db) is False
        assert client.messages == []
        assert await dispatch.dispatch_ingestion(**args, product_id=A, db=db) is True
        body = client.messages[0]["MessageBody"]
        row = (await db.execute(text("SELECT status, ingestion_attempt_id FROM knowledge_assets WHERE id=:id"), {"id": own.id})).one()
        assert body["scope"]["product_id"] == A
        assert body["scope"]["visibility"] == "product"
        assert body["ingestion_attempt_id"] == str(row.ingestion_attempt_id)
        assert row.status == "queued"
        assert await dispatch.dispatch_ingestion(**args, product_id=A, db=db) is False
        assert len(client.messages) == 1

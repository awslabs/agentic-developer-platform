"""Real SQL coverage of Product source selection and unattended refresh authority."""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from sqlalchemy import delete, text, update

from src.auth.dependencies import get_current_user
from src.internal import ingestion_control_routes as control
from src.internal import ingestion_refresh_routes as refresh
from src.knowledge import dispatch, routes
from src.knowledge import product_source_routes as api
from src.knowledge import product_sources as sources
from src.knowledge.schemas import AssetCreateRequest
from src.products.models import Product, ProductProjectBinding, ProductSourceBinding, Project
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db
from src.shared.models.base import Base, utcnow
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import ChannelTenantMap, InstallationOwnershipConflict, InstallationRevocation, UserCredential
from tests.knowledge.test_product_postgres import A, B, actor, migrate, registry  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

BINDING = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
async def connected(registry, monkeypatch):  # noqa: F811
    r = registry
    await r.upgrade()
    tables = [
        ChannelTenantMap.__table__,
        InstallationRevocation.__table__,
        InstallationOwnershipConflict.__table__,
        UserCredential.__table__,
        Project.__table__,
        ProductProjectBinding.__table__,
        ProductSourceBinding.__table__,
    ]
    async with r.engine.begin() as db:
        await db.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
        await db.run_sync(lambda conn: migrate(conn, "017_product_source_refresh.py"))
    async with r.sessions() as db:
        db.add(ChannelTenantMap(id="connection-a", org_id="org-a", provider="github", provider_scope_id="101", installation_id="202"))
        for product, project, binding in ((A, "project-a", BINDING), (B, "project-b", OTHER)):
            db.add(Project(id=project, tenant_id="org-a", name=project, owner_user_id="owner"))
            await db.flush()
            db.add(ProductProjectBinding(id="relationship-" + project, tenant_id="org-a", product_id=product, project_id=project, provenance={}))
            db.add(
                ProductSourceBinding(
                    id=binding,
                    tenant_id="org-a",
                    project_id=project,
                    kind="repository",
                    provider="github",
                    installation_id="connection-a",
                    namespace="101",
                    native_id="303",
                    desired={},
                    status="healthy",
                )
            )
        await db.commit()
    monkeypatch.setattr(sources, "app_credentials", lambda: ("505", "synthetic-app-key"))
    monkeypatch.setattr(
        sources,
        "describe_source",
        AsyncMock(
            return_value={
                "name": "fixture/service",
                "source_ref": "https://github.com/fixture/service",
                "connection_name": "fixture",
                "app_id": "505",
            }
        ),
    )
    monkeypatch.setattr(api, "describe_source", sources.describe_source)
    monkeypatch.setattr(control, "admit_source", AsyncMock())
    monkeypatch.setattr(dispatch, "admit_source", AsyncMock())
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "synthetic-product-source-grant-key")
    monkeypatch.setenv("INGESTION_QUEUE_URL", "https://queue.invalid/knowledge")
    r.messages = []
    queue = Mock()
    queue.send_message.side_effect = lambda **kw: r.messages.append(json.loads(kw["MessageBody"])) or {"MessageId": "fixture"}
    monkeypatch.setattr(dispatch, "get_sqs_client", lambda: queue)
    monkeypatch.setattr(routes, "dispatch_ingestion", dispatch.dispatch_ingestion)
    monkeypatch.setattr(refresh, "dispatch_ingestion", dispatch.dispatch_ingestion)
    return r


async def add(r, *, binding=BINDING, keep_updated=False, user="member", product=A):
    async with r.sessions() as db, r.sessions() as gateway:
        return await routes.register_asset(
            AssetCreateRequest(
                asset_type="repo",
                source_ref="https://github.com/fixture/service",
                scope="product",
                product_id=product,
                source_binding_id=binding,
                keep_updated=keep_updated,
            ),
            db,
            gateway,
            actor(user),
        )


async def finish(r, asset):
    async with r.sessions() as db:
        await db.execute(text("UPDATE knowledge_assets SET status='complete',updated_at=NOW()-INTERVAL '2 days' WHERE id=:id"), {"id": asset.id})
        await db.commit()


async def tick(r):
    async with r.sessions() as db, r.sessions() as gateway:
        return await refresh.refresh_registered(refresh.RefreshRequest(), db, gateway)


async def listing(r, user="member"):
    async with r.sessions() as db, r.sessions() as gateway:
        return await api.list_product_sources(A, Response(), None, db, gateway, actor(user))


async def test_selected_connection_owns_grant_and_worker_gets_only_one_repo_token(connected, monkeypatch):
    r = connected
    asset = await add(r, keep_updated=True)
    assert asset.product_id == A and asset.owner_sub is None
    routes.validate_repo_accessibility.assert_not_awaited()
    message = r.messages[0]
    assert message["installation_id"] == 202
    assert message["scope"] == {"tenant_id": "org-a", "owner_sub": None, "project_id": None, "product_id": A, "visibility": "product"}
    mint = AsyncMock(return_value=("synthetic-one-repo-token", "2099-01-01T00:00:00Z"))
    monkeypatch.setattr(sources, "mint_installation_token_with_expiry", mint)
    legacy = AsyncMock(side_effect=AssertionError("Product sources must use their selected ADP App"))
    monkeypatch.setattr(control, "_get_ops_app_credentials", legacy)
    async with r.sessions() as db, r.sessions() as gateway:
        result = await control.ingestion_control(
            control.IngestionCommand(
                callback_grant=message["callback_grant"],
                operation="bootstrap",
                arguments={"receipt_handle": "synthetic-receipt"},
            ),
            db,
            gateway,
        )
    assert result["result"]["github_token"] == "synthetic-one-repo-token"
    mint.assert_awaited_once_with("505", "synthetic-app-key", 202, repository_ids=[303], permissions={"contents": "read", "metadata": "read"})
    legacy.assert_not_awaited()
    async with r.sessions() as db:
        row = (await db.execute(text("SELECT product_source,refresh_enabled FROM knowledge_assets WHERE id=:id"), {"id": asset.id})).one()
        assert row.product_source["authorized_by"] == "member"
        assert row.product_source["service_id"] == "knowledge-ingestion"
        assert row.refresh_enabled is True


async def test_refresh_is_explicit_and_survives_importer_membership_removal(connected):
    r = connected
    asset = await add(r)
    await finish(r, asset)
    assert (await tick(r))["queued"] == 0
    async with r.sessions() as db, r.sessions() as gateway:
        await api.update_refresh(asset.id, api.RefreshSettings(enabled=True, revision=1), db, gateway, actor())
    async with r.sessions() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "member"))
        await db.commit()
    assert (await tick(r)) == {"queued": 1, "blocked": 0, "after": None}
    assert (await tick(r))["queued"] == 0
    with pytest.raises(HTTPException):
        await listing(r)
    # Another current Product member still sees Product-owned information.
    view = await listing(r, "peer")
    assert view["items"][0]["asset_id"] == asset.id
    assert view["items"][0]["keep_updated"] is True


@pytest.mark.parametrize(
    "change", ["unlink", "archive_product", "archive_project", "move_project", "connection_tenant", "installation", "generation", "revoke"]
)
async def test_changed_source_stops_refresh_and_worker_operations(connected, change):
    r = connected
    asset = await add(r, keep_updated=True)
    async with r.sessions() as db:
        changes = {
            "unlink": update(ProductSourceBinding).where(ProductSourceBinding.id == BINDING).values(unlinked_at=utcnow(), status="unlinked"),
            "archive_product": update(Product).where(Product.id == A).values(archived_at=utcnow()),
            "archive_project": update(Project).where(Project.id == "project-a").values(archived_at=utcnow()),
            "move_project": update(ProductProjectBinding).where(ProductProjectBinding.project_id == "project-a").values(valid_to=utcnow()),
            "connection_tenant": update(ChannelTenantMap).values(org_id="org-b"),
            "installation": update(ChannelTenantMap).values(installation_id="999"),
            "generation": update(ProductSourceBinding).where(ProductSourceBinding.id == BINDING).values(generation=2),
            "revoke": update(ProductSourceBinding).where(ProductSourceBinding.id == BINDING).values(status="revoked"),
        }
        await db.execute(changes[change])
        await db.commit()
    async with r.sessions() as db, r.sessions() as gateway:
        with pytest.raises(HTTPException):
            await control.ingestion_control(
                control.IngestionCommand(
                    callback_grant=r.messages[0]["callback_grant"],
                    operation="vectors.put",
                    arguments={},
                ),
                db,
                gateway,
            )
    await finish(r, asset)
    result = await tick(r)
    assert result["queued"] == 0 and result["blocked"] == 1
    async with r.sessions() as db:
        error = await db.scalar(text("SELECT refresh_error FROM knowledge_assets WHERE id=:id"), {"id": asset.id})
        assert error and "source" in error.lower()


async def test_revoked_installation_and_personal_connection_are_not_service_authority(connected):
    r = connected
    async with r.sessions() as db:
        db.add(InstallationRevocation(installation_id="202", org_id="org-a"))
        await db.commit()
    with pytest.raises(HTTPException):
        await add(r)
    async with r.sessions() as db:
        await db.execute(delete(InstallationRevocation))
        await db.execute(update(ChannelTenantMap).values(provider_scope_id="personal:101:member", installed_by_user_id="member"))
        await db.commit()
    with pytest.raises(HTTPException):
        await add(r)
    assert not r.messages


async def test_product_and_human_boundaries_are_checked_before_source_use(connected):
    r = connected
    with pytest.raises(HTTPException):
        await add(r, binding=OTHER)
    with pytest.raises(HTTPException):
        await add(r, user="outsider")
    assert not r.messages
    result = await listing(r)
    assert [item["binding_id"] for item in result["items"]] == [BINDING]


async def test_refresh_settings_are_versioned_and_disabled_source_stays_disabled(connected):
    r = connected
    asset = await add(r, keep_updated=True)
    async with r.sessions() as db, r.sessions() as gateway:
        result = await api.update_refresh(asset.id, api.RefreshSettings(enabled=False, revision=1), db, gateway, actor())
        assert result == {"enabled": False, "revision": 2}
    async with r.sessions() as db, r.sessions() as gateway:
        with pytest.raises(HTTPException) as error:
            await api.update_refresh(asset.id, api.RefreshSettings(enabled=True, revision=1), db, gateway, actor())
        assert error.value.status_code == 409
    await finish(r, asset)
    assert (await tick(r))["queued"] == 0
    source = (await listing(r))["items"][0]
    assert source["keep_updated"] is False and source["revision"] == 2


async def test_existing_product_import_is_connected_without_duplicate_or_reindex(connected, monkeypatch):
    r = connected
    async with r.sessions() as db, r.sessions() as gateway:
        old = await routes.register_asset(
            AssetCreateRequest(
                asset_type="repo",
                source_ref="https://github.com/fixture/service",
                scope="product",
                product_id=A,
            ),
            db,
            gateway,
            actor(),
        )
    await finish(r, old)
    monkeypatch.setenv("ASSET_QUOTA_PRODUCT_REPO", "1")  # Reusing an entry consumes no additional quota.
    connected_asset = await add(r, keep_updated=True)
    assert connected_asset.id == old.id
    assert len(r.messages) == 1
    again = await add(r, keep_updated=True)
    assert again.id == old.id
    assert (await listing(r))["items"][0]["last_refreshed_at"] is not None


async def test_source_grants_cannot_be_injected_through_user_metadata(connected):
    r = connected
    async with r.sessions() as db, r.sessions() as gateway:
        asset = await routes.register_asset(
            AssetCreateRequest(
                asset_type="repo",
                source_ref="https://github.com/fixture/service",
                scope="product",
                product_id=A,
                metadata={"product_source": {"binding_id": OTHER}, "refresh_enabled": True},
            ),
            db,
            gateway,
            actor(),
        )
        assert await db.scalar(text("SELECT product_source FROM knowledge_assets WHERE id=:id"), {"id": asset.id}) is None


async def test_source_routes_share_registration_and_refresh_authority(connected):
    r = connected
    app = FastAPI()
    app.include_router(routes._router)

    async def session():
        async with r.sessions() as db:
            yield db

    app.dependency_overrides[get_agent_context_db] = session
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.get(f"/agent-context/products/{A}/sources")
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["name"] == "fixture/service"
        response = await client.post(
            f"/agent-context/products/{A}/sources/{BINDING}",
            json={
                "source_ref": "https://github.com/fixture/service",
                "keep_updated": True,
            },
        )
        assert response.status_code == 201, response.text
        asset = response.json()
        assert asset["product_id"] == A and asset["owner_sub"] is None
        response = await client.patch(f"/agent-context/assets/{asset['id']}/refresh", json={"enabled": False, "revision": 1})
        assert response.status_code == 200, response.text
        assert response.json() == {"enabled": False, "revision": 2}
        response = await client.get(f"/agent-context/products/{A}/sources")
        assert response.status_code == 200
        assert response.json()["items"][0]["keep_updated"] is False


async def test_one_revoked_source_does_not_block_other_product_refreshes(connected, monkeypatch):
    r = connected
    first = await add(r, keep_updated=True)
    async with r.sessions() as db:
        await db.execute(update(ProductProjectBinding).where(ProductProjectBinding.project_id == "project-b").values(product_id=A))
        await db.execute(update(ProductSourceBinding).where(ProductSourceBinding.id == OTHER).values(native_id="304"))
        await db.commit()
    monkeypatch.setattr(
        sources,
        "describe_source",
        AsyncMock(
            return_value={
                "name": "fixture/second",
                "source_ref": "https://github.com/fixture/second",
                "connection_name": "fixture",
                "app_id": "505",
            }
        ),
    )
    async with r.sessions() as db, r.sessions() as gateway:
        second = await routes.register_asset(
            AssetCreateRequest(
                asset_type="repo",
                source_ref="https://github.com/fixture/second",
                scope="product",
                product_id=A,
                source_binding_id=OTHER,
                keep_updated=True,
            ),
            db,
            gateway,
            actor(),
        )
    await finish(r, first)
    await finish(r, second)
    async with r.sessions() as db:
        await db.execute(update(ProductSourceBinding).where(ProductSourceBinding.id == BINDING).values(status="revoked"))
        await db.commit()
    assert await tick(r) == {"queued": 1, "blocked": 1, "after": None}
    assert r.messages[-1]["source"] == "fixture/second"


async def test_source_migration_is_required_before_new_routes_are_available(registry):  # noqa: F811
    r = registry
    await r.upgrade()
    with pytest.raises(HTTPException) as error:
        await listing(r)
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "knowledge_source_setup_required"


async def test_changed_binding_can_be_explicitly_reconnected_without_losing_knowledge(connected):
    r = connected
    asset = await add(r, keep_updated=True)
    await finish(r, asset)
    async with r.sessions() as db:
        await db.execute(update(ProductSourceBinding).where(ProductSourceBinding.id == BINDING).values(generation=2))
        await db.commit()
    before = (await listing(r))["items"][0]
    assert before["eligible"] is False and before["needs_reconnect"] is True
    assert (await tick(r))["blocked"] == 1
    reconnected = await add(r, keep_updated=True)
    assert reconnected.id == asset.id
    current = (await listing(r))["items"][0]
    assert current["eligible"] is True and current["revision"] == 2
    assert (await tick(r))["queued"] == 1


async def test_repository_rename_reconnects_the_same_asset(connected, monkeypatch):
    r = connected
    asset = await add(r, keep_updated=True)
    await finish(r, asset)
    description = AsyncMock(
        return_value={
            "name": "fixture/renamed",
            "source_ref": "https://github.com/fixture/renamed",
            "connection_name": "fixture",
            "app_id": "505",
        }
    )
    monkeypatch.setattr(sources, "describe_source", description)
    monkeypatch.setattr(api, "describe_source", description)
    entry = (await listing(r))["items"][0]
    assert entry["needs_reconnect"] is True and entry["source_ref"] == "https://github.com/fixture/renamed"
    async with r.sessions() as db, r.sessions() as gateway:
        result = await api.include_source(A, BINDING, api.IncludeSource(source_ref=entry["source_ref"], keep_updated=True), db, gateway, actor())
        assert result.id == asset.id and result.source_ref == entry["source_ref"]
        assert await db.scalar(text("SELECT COUNT(*) FROM knowledge_assets")) == 1

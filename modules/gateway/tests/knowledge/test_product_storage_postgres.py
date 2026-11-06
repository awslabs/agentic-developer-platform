"""Product storage API against real PostgreSQL, with mocked AWS SDK calls."""

import base64
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, text, update

from src.auth.dependencies import get_current_user
from src.knowledge import product_storage as storage
from src.knowledge import routes
from src.products.models import Product
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db
from src.shared.models.base import Base, utcnow
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import UserIdentity
from tests.knowledge.test_product_postgres import A, B, actor, migrate, registry  # noqa: F401
from tests.knowledge.test_product_storage import KEY, OTHER_KEY, ROLE
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401


@pytest.fixture
async def configured(registry, monkeypatch):  # noqa: F811
    r = registry
    await r.upgrade()
    async with r.engine.begin() as db:
        await db.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[UserIdentity.__table__]))
        for name in ("017_product_source_refresh.py", "018_product_context_storage.py"):
            await db.run_sync(lambda conn, name=name: migrate(conn, name))
    monkeypatch.setenv("PRODUCT_CONTEXT_STORAGE_ROLE_ARN", ROLE)
    monkeypatch.setenv("PRODUCT_CONTEXT_DEFAULT_KMS_KEY_ARN", KEY)
    monkeypatch.setenv("AGENT_CONTEXT_S3_BUCKET", "fixture-context")
    r.s3 = Mock()
    r.s3.generate_presigned_url.return_value = "https://fixture-context.s3.amazonaws.com/object?signature=private"
    r.s3.head_object.return_value = {
        "ContentLength": 17,
        "ChecksumSHA256": base64.b64encode(bytes.fromhex("ab" * 32)).decode(),
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": KEY,
        "BucketKeyEnabled": False,
        "VersionId": "verified-v1",
    }
    monkeypatch.setattr(storage, "s3_session", Mock(return_value=r.s3))
    r.actor = actor()
    app = FastAPI()
    app.include_router(routes._router)

    async def session():
        async with r.sessions() as db:
            yield db

    app.dependency_overrides[get_agent_context_db] = session
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: r.actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        r.client = client
        yield r


def path(product=A):
    return f"/agent-context/products/{product}/storage"


async def begin(r):
    response = await r.client.post(path() + "/uploads", json={"filename": "guide.md", "content_length": 17, "sha256": "ab" * 32})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def complete(r, upload):
    return await r.client.post(path() + f"/uploads/{upload['upload_id']}/complete")


async def test_access_and_config_revision_history(configured, monkeypatch):
    r = configured
    view = await r.client.get(path())
    assert view.status_code == 200 and view.json()["can_configure"] is False
    assert view.json()["prefix"] == f"tenants/org-a/products/{A}/"
    assert (await r.client.get(path(B))).status_code == 404
    assert (await r.client.put(path(), json={"encryption": "shared", "revision": 0})).status_code == 403
    r.actor = actor("outsider", admin=True)
    result = await r.client.put(path(), json={"encryption": "shared", "revision": 0})
    assert result.status_code == 200 and result.json()["revision"] == 1
    assert (await r.client.put(path(), json={"encryption": "shared", "revision": 0})).status_code == 409
    monkeypatch.setattr(storage, "validate_key", lambda *a, **kw: OTHER_KEY)
    result = await r.client.put(path(), json={"encryption": "customer", "revision": 1, "kms_key_arn": OTHER_KEY})
    assert result.status_code == 200 and result.json()["kms_key_arn"] == OTHER_KEY
    async with r.sessions() as db:
        assert set((await storage.load_storage(db, "org-a", A)).read_key_arns) == {KEY, OTHER_KEY}
    r.actor = actor("outsider", admin=True, org="org-b")
    assert (await r.client.get(path())).status_code == 404


async def test_upload_verified_version_download_and_retry(configured):
    r = configured
    upload = await begin(r)
    response = await complete(r, upload)
    assert response.status_code == 200, response.text
    asset = response.json()
    assert asset["product_id"] == A and asset["owner_sub"] is None and asset["can_download"] is True
    assert (await complete(r, upload)).json()["id"] == asset["id"]
    routes.dispatch_ingestion.assert_awaited_once()
    async with r.sessions() as db:
        row = (await db.execute(text("SELECT source_object_version,refresh_enabled FROM knowledge_assets WHERE id=:id"), {"id": asset["id"]})).one()
        assert tuple(row) == ("verified-v1", False)
    r.actor = actor("peer")
    endpoint = path() + f"/assets/{asset['id']}/download"
    assert (await r.client.post(endpoint)).status_code == 200
    assert r.s3.generate_presigned_url.call_args.kwargs["Params"]["VersionId"] == "verified-v1"
    async with r.sessions() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "peer"))
        await db.commit()
    assert (await r.client.post(endpoint)).status_code == 404
    r.actor = actor()
    async with r.sessions() as db:
        await db.execute(text("UPDATE knowledge_assets SET status='removed' WHERE id=:id"), {"id": asset["id"]})
        await db.commit()
    assert (await r.client.post(endpoint)).status_code == 404
    assert (await complete(r, upload)).status_code == 410


@pytest.mark.parametrize(
    "field,value", [("ChecksumSHA256", "wrong"), ("ContentLength", 18), ("SSEKMSKeyId", OTHER_KEY), ("BucketKeyEnabled", True), ("VersionId", "null")]
)
async def test_bad_objects_never_register(configured, field, value):
    r = configured
    upload = await begin(r)
    r.s3.head_object.return_value[field] = value
    assert (await complete(r, upload)).status_code in {409, 503}
    routes.dispatch_ingestion.assert_not_awaited()
    async with r.sessions() as db:
        assert await db.scalar(text("SELECT COUNT(*) FROM knowledge_assets")) == 0


async def test_receipt_cannot_cross_product_user_or_be_forged(configured):
    r = configured
    upload = await begin(r)
    r.actor = actor("peer")
    assert (await complete(r, upload)).status_code == 403
    r.actor = actor("outsider", admin=True)
    assert (await r.client.post(path(B) + f"/uploads/{upload['upload_id']}/complete")).status_code == 404
    r.actor = actor()
    body = {
        "asset_type": "doc",
        "source_ref": upload["source_ref"],
        "scope": "product",
        "product_id": A,
        "metadata": {"source_object_version": "forged", "source_upload_id": upload["upload_id"]},
    }
    assert (await r.client.post("/agent-context/assets", json=body)).status_code == 409
    body["source_upload_id"] = upload["upload_id"]
    assert (await r.client.post("/agent-context/assets", json=body)).status_code == 409
    async with r.sessions() as db:
        await db.execute(text("UPDATE product_context_uploads SET expires_at=NOW()-INTERVAL '1 day'"))
        await db.commit()
    assert (await complete(r, upload)).status_code == 410
    routes.dispatch_ingestion.assert_not_awaited()


async def test_archive_stops_upload_and_service_ingestion(configured):
    r = configured
    upload = await begin(r)
    async with r.sessions() as db:
        await db.execute(update(Product).where(Product.id == A).values(archived_at=utcnow()))
        await db.commit()
    assert (await complete(r, upload)).status_code == 409
    async with r.sessions() as db:
        with pytest.raises(HTTPException):
            await storage.require_service_product(db, "org-a", A)
    assert (await r.client.get(path())).json()["can_upload"] is False


async def test_upload_size_filename_and_pending_limits(configured):
    r = configured
    for name, size in (("../guide.md", 17), ("guide.exe", 17), ("guide.md", storage.MAX_UPLOAD_BYTES + 1)):
        assert (await r.client.post(path() + "/uploads", json={"filename": name, "content_length": size, "sha256": "ab" * 32})).status_code in {
            400,
            422,
        }
    for _ in range(20):
        await begin(r)
    assert (await r.client.post(path() + "/uploads", json={"filename": "guide.md", "content_length": 17, "sha256": "ab" * 32})).status_code == 429


async def test_disabled_rollout_and_old_schema_fail_closed(registry, monkeypatch):  # noqa: F811
    monkeypatch.delenv("PRODUCT_CONTEXT_STORAGE_ROLE_ARN", raising=False)
    async with registry.sessions() as db:
        with pytest.raises(HTTPException) as error:
            await storage.load_storage(db, "org-a", A)
        assert error.value.status_code == 503
        monkeypatch.setenv("PRODUCT_CONTEXT_STORAGE_ROLE_ARN", ROLE)
        monkeypatch.setenv("PRODUCT_CONTEXT_DEFAULT_KMS_KEY_ARN", KEY)
        monkeypatch.setenv("AGENT_CONTEXT_S3_BUCKET", "fixture-context")
        with pytest.raises(HTTPException) as error:
            await storage.load_storage(db, "org-a", A)
        assert "database update" in error.value.detail["message"]


async def test_absent_bucket_key_header_is_compatible_with_disabled_bucket_keys(configured):
    r = configured
    upload = await begin(r)
    del r.s3.head_object.return_value["BucketKeyEnabled"]
    assert (await complete(r, upload)).status_code == 200


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("source_kind", ["original", "folder", "product_root", "encoded_folder"])
async def test_all_import_paths_require_verified_uploads(configured, bulk, source_kind):
    r = configured
    upload = await begin(r)
    source = upload["source_ref"]
    if source_kind == "folder":
        source = source.rsplit("/", 1)[0] + "/"
    elif source_kind == "product_root":
        source = source.split("/uploads/")[0] + "/"
    elif source_kind == "encoded_folder":
        source = source.rsplit("/", 1)[0].replace("/uploads/", "/%75ploads/") + "/"
    item = {"asset_type": "doc", "source_ref": source}
    payload = {"scope": "product", "product_id": A, **({"items": [item]} if bulk else item)}
    endpoint = "/agent-context/assets" + ("/bulk/commit" if bulk else "")
    response = await r.client.post(endpoint, json=payload)
    assert response.status_code == 409, response.text
    routes.dispatch_ingestion.assert_not_awaited()
    async with r.sessions() as db:
        assert await db.scalar(text("SELECT COUNT(*) FROM knowledge_assets")) == 0

"""Mounted retained-source reads preserve current run visibility after work ends."""

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from l5_services.models.base import Base as L5Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import select

from src import l5_integration
from src.products.models import ProductProjectBinding, ProductSourceBinding, ProductSourceSnapshot
from src.products.source_capture import MAX_RANGE_BYTES, SnapshotObjects
from src.products.source_snapshots import store_snapshot
from tests.products.test_l5_snapshots import snapshot_fixture


@pytest.fixture
async def retained(test_client, db_session, test_engine, app, source_member, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    grant, request, captured, _, _, _ = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    captured = captured.model_copy(
        update={
            "content": captured.content.model_copy(update={"artifact_id": "products/source-snapshots/content"}),
            "file_manifest": captured.file_manifest.model_copy(update={"artifact_id": "products/source-snapshots/manifest"}),
        }
    )
    receipt, _ = await store_snapshot(db_session, grant, request, captured, "retained-source")
    relationship = await db_session.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.product_id == grant.product_id,
            ProductProjectBinding.project_id == grant.project_id,
        )
    )
    configuration_id, run_id = str(uuid4()), str(uuid4())
    scope = {
        "tenant_id": grant.tenant_id,
        "product_id": grant.product_id,
        "project_id": grant.project_id,
        "owner_id": source_member.user_id,
        "visibility": "only_me",
        "relationship_revision": str(relationship.relationship_revision),
    }
    db_session.add(
        Configuration(
            tenant_id=grant.tenant_id,
            configuration_id=configuration_id,
            product_id=grant.product_id,
            project_id=grant.project_id,
            service_id="security",
            relationship_revision=scope["relationship_revision"],
            owner_id=source_member.user_id,
            current_revision=1,
            handoff_state="linked",
        )
    )
    db_session.add(
        ConfigurationVersion(
            tenant_id=grant.tenant_id,
            configuration_id=configuration_id,
            revision=1,
            settings={},
            inherited_fields=[],
            gaps=[],
            state="ready",
            hash="a" * 64,
        )
    )
    await db_session.flush()
    sources = [request.source_ref.model_dump()]
    run = Run(
        tenant_id=grant.tenant_id,
        run_id=run_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        service_id="security",
        configuration_id=configuration_id,
        configuration_revision=1,
        binding_id="selected-binding",
        trigger_id="manual",
        binding_generation=1,
        input={"sources": sources},
        admission={"scope": scope, "delegation_id": grant.id},
        state="succeeded",
        provider_state="completed",
        deadline_at=datetime.now(UTC) - timedelta(hours=1),
        manifest={"scope": scope, "run_id": run_id, "sources": sources, "source_snapshot_ids": [receipt.snapshot_id], "files": []},
    )
    db_session.add(run)
    await db_session.flush()
    work = Work(
        tenant_id=grant.tenant_id,
        work_id=str(uuid4()),
        run_id=run_id,
        kind="execute",
        generation=1,
        step=0,
        due_at=datetime.now(UTC) - timedelta(hours=2),
        completed_at=datetime.now(UTC) - timedelta(hours=1),
        lease_owner=None,
        lease_until=None,
    )
    db_session.add(work)
    await db_session.commit()
    payloads = {"products/source-snapshots/content": b"original document bytes", "products/source-snapshots/manifest": b'{"files":["document-a"]}'}
    s3 = Mock()
    s3.get_object.side_effect = lambda **kwargs: {"VersionId": "version-r1", "Body": io.BytesIO(payloads[kwargs["Key"]])}
    monkeypatch.setattr("src.products.l5_retained_sources.get_snapshot_objects", lambda: SnapshotObjects(s3, "example-private-bucket"))
    path = f"/v1/products/{grant.product_id}/projects/{grant.project_id}/services/security/runs/{run_id}"
    return SimpleNamespace(
        path=path, run=run, work=work, grant=grant, receipt=receipt, s3=s3, relationship=relationship, payloads=payloads, member=source_member
    )


@pytest.mark.asyncio
async def test_completed_run_reads_original_snapshot_without_execution_lease(test_client, db_session, retained):
    manifest = await test_client.get(retained.path + "/source-manifest")
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["source_snapshot_ids"] == [retained.receipt.snapshot_id]
    path = retained.path + "/source-snapshots/" + retained.receipt.snapshot_id
    metadata = await test_client.get(path)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json() == retained.receipt.model_dump(mode="json")
    for part, expected in (("content", b"original document bytes"), ("manifest", b'{"files":["document-a"]}')):
        response = await test_client.get(path + "/" + part)
        assert response.status_code == 200, response.text
        assert response.content == expected
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert retained.s3.get_object.call_count == 2
    assert all(call.kwargs["VersionId"] == "version-r1" for call in retained.s3.get_object.call_args_list)
    await db_session.refresh(retained.work)
    assert retained.work.completed_at is not None and retained.work.lease_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "foreign_run",
        "foreign_product",
        "foreign_service",
        "foreign_snapshot",
        "revoked_source",
        "unbound_project",
        "wrong_manifest_scope",
        "wrong_manifest_run",
        "wrong_snapshot_source",
        "wrong_snapshot_project",
        "wrong_snapshot_grant",
        "expired_snapshot",
        "changed_receipt",
        "unknown_part",
    ],
)
async def test_unavailable_or_foreign_sources_do_not_reach_storage(test_client, db_session, retained, fault):
    path = retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/content"
    snapshot = await db_session.get(ProductSourceSnapshot, (retained.grant.tenant_id, retained.receipt.snapshot_id))
    if fault == "foreign_run":
        path = path.replace(retained.run.run_id, str(uuid4()))
    elif fault == "foreign_product":
        path = path.replace(retained.run.product_id, str(uuid4()))
    elif fault == "foreign_service":
        path = path.replace("/services/security/", "/services/devops/")
    elif fault == "foreign_snapshot":
        path = path.replace(retained.receipt.snapshot_id, str(uuid4()))
    elif fault == "revoked_source":
        binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == retained.run.project_id))
        binding.status = "revoked"
    elif fault == "unbound_project":
        retained.relationship.valid_to = datetime.now(UTC)
    elif fault == "wrong_manifest_scope":
        retained.run.manifest = {**retained.run.manifest, "scope": {**retained.run.manifest["scope"], "tenant_id": "foreign"}}
    elif fault == "wrong_manifest_run":
        retained.run.manifest = {**retained.run.manifest, "run_id": str(uuid4())}
    elif fault == "wrong_snapshot_source":
        snapshot.source_ref = {**snapshot.source_ref, "record_id": "another-document"}
    elif fault == "wrong_snapshot_project":
        snapshot.project_id = str(uuid4())
    elif fault == "wrong_snapshot_grant":
        snapshot.delegation_id = str(uuid4())
    elif fault == "expired_snapshot":
        snapshot.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        snapshot.receipt = {**snapshot.receipt, "expires_at": snapshot.expires_at.isoformat()}
    elif fault == "changed_receipt":
        snapshot.receipt = {**snapshot.receipt, "snapshot_id": str(uuid4())}
    elif fault == "unknown_part":
        path = path.removesuffix("content") + "arbitrary-object"
    await db_session.commit()
    response = await test_client.get(path)
    assert response.status_code == (410 if fault == "expired_snapshot" else 409 if fault == "changed_receipt" else 404), response.text
    retained.s3.get_object.assert_not_called()


@pytest.mark.asyncio
async def test_corrupt_original_bytes_are_not_returned(test_client, retained):
    retained.payloads["products/source-snapshots/content"] = b"modified bytes"
    response = await test_client.get(retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/content")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_integrity_mismatch"


@pytest.mark.asyncio
async def test_source_revocation_during_read_suppresses_bytes(test_client, retained, monkeypatch):
    allowed = l5_integration.permitted_source_refs
    revoked = {"value": False}

    async def rights(*args):
        return set() if revoked["value"] else await allowed(*args)

    def read(**kwargs):
        revoked["value"] = True
        return {"VersionId": "version-r1", "Body": io.BytesIO(retained.payloads[kwargs["Key"]])}

    monkeypatch.setattr(l5_integration, "permitted_source_refs", rights)
    retained.s3.get_object.side_effect = read
    response = await test_client.get(retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/content")
    assert response.status_code == 404, response.text
    assert b"original document bytes" not in response.content


async def large_artifact(db_session, retained, part):
    data = b"retained-original-" * (450_000 if part == "content" else 400_000)
    row = await db_session.get(ProductSourceSnapshot, (retained.grant.tenant_id, retained.receipt.snapshot_id))
    field = "content" if part == "content" else "file_manifest"
    artifact = {**row.receipt[field], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    row.receipt = {**row.receipt, field: artifact}
    await db_session.commit()

    def read(**kwargs):
        start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
        return {
            "VersionId": artifact["version"],
            "ContentLength": end - start + 1,
            "ContentRange": f"bytes {start}-{end}/{len(data)}",
            "Metadata": {"sha256": artifact["sha256"]},
            "Body": io.BytesIO(data[start : end + 1]),
        }

    retained.s3.get_object.side_effect = read
    return data, artifact


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["content", "manifest"])
async def test_large_public_artifacts_require_ranges_and_reassemble_exactly(test_client, db_session, retained, part):
    data, artifact = await large_artifact(db_session, retained, part)
    path = retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/" + part
    whole = await test_client.get(path)
    assert whole.status_code == 416
    assert whole.json()["detail"]["code"] == "source_range_required"
    assert whole.headers["content-range"] == f"bytes */{len(data)}"
    assert whole.headers["accept-ranges"] == "bytes"
    retained.s3.get_object.assert_not_called()
    chunks = []
    for start in range(0, len(data), MAX_RANGE_BYTES):
        end = min(start + MAX_RANGE_BYTES, len(data)) - 1
        response = await test_client.get(path, headers={"Range": f"bytes={start}-{end}"})
        assert response.status_code == 206, response.text
        assert len(response.content) <= MAX_RANGE_BYTES
        assert response.headers["content-range"] == f"bytes {start}-{end}/{len(data)}"
        assert response.headers["x-artifact-size"] == str(len(data))
        assert response.headers["x-artifact-sha256"] == artifact["sha256"]
        assert response.headers["x-artifact-version"] == artifact["version"]
        assert response.headers["etag"] == '"' + artifact["sha256"] + '"'
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        chunks.append(response.content)
    assert b"".join(chunks) == data
    assert hashlib.sha256(b"".join(chunks)).hexdigest() == artifact["sha256"]
    assert all(call.kwargs["VersionId"] == artifact["version"] for call in retained.s3.get_object.call_args_list)
    await db_session.refresh(retained.work)
    assert retained.work.completed_at is not None and retained.work.lease_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["bytes=0-", "bytes=-10", "bytes=1-0", "bytes=0-23", "bytes=0-1,3-4", "items=0-1", "bytes=0-1000000000000"])
async def test_invalid_public_ranges_do_not_reach_storage(test_client, retained, header):
    response = await test_client.get(retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/content", headers={"Range": header})
    assert response.status_code == 416
    assert response.json()["detail"]["code"] == "invalid_source_range"
    assert response.headers["content-range"] == "bytes */23"
    retained.s3.get_object.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_and_duplicate_public_ranges_do_not_reach_storage(test_client, db_session, retained):
    await large_artifact(db_session, retained, "content")
    path = retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/content"
    for headers in ({"Range": f"bytes=0-{MAX_RANGE_BYTES}"}, [("Range", "bytes=0-1"), ("Range", "bytes=2-3")]):
        response = await test_client.get(path, headers=headers)
        assert response.status_code == 416
    retained.s3.get_object.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["VersionId", "ContentRange", "ContentLength", "Metadata"])
async def test_public_ranges_reject_changed_storage_identity(test_client, db_session, retained, fault):
    await large_artifact(db_session, retained, "content")
    original = retained.s3.get_object.side_effect

    def corrupt(**kwargs):
        result = original(**kwargs)
        result[fault] = {"sha256": "b" * 64} if fault == "Metadata" else "changed"
        return result

    retained.s3.get_object.side_effect = corrupt
    response = await test_client.get(retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/content", headers={"Range": "bytes=0-9"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_integrity_mismatch"


@pytest.mark.asyncio
async def test_public_range_revocation_during_read_suppresses_bytes(test_client, db_session, retained, monkeypatch):
    await large_artifact(db_session, retained, "content")
    allowed, original = l5_integration.permitted_source_refs, retained.s3.get_object.side_effect
    revoked = {"value": False}

    async def rights(*args):
        return set() if revoked["value"] else await allowed(*args)

    def read(**kwargs):
        revoked["value"] = True
        return original(**kwargs)

    monkeypatch.setattr(l5_integration, "permitted_source_refs", rights)
    retained.s3.get_object.side_effect = read
    path = retained.path + "/source-snapshots/" + retained.receipt.snapshot_id + "/content"
    response = await test_client.get(path, headers={"Range": "bytes=0-9"})
    assert response.status_code == 404
    assert b"retained-o" not in response.content


@pytest.mark.asyncio
async def test_large_run_manifest_pages_preserve_every_file_and_the_full_digest(test_client, db_session, retained):
    files = [{"path": f"directory/{index:05d}/" + "a" * 700, "sha256": "a" * 64, "bytes": index} for index in range(14_000)]
    retained.run.manifest = {**retained.run.manifest, "files": files}
    original = json.loads(json.dumps(retained.run.manifest))
    await db_session.commit()
    path = retained.path + "/source-manifest"
    response = await test_client.get(path)
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "source_manifest_pagination_required"
    assert detail["file_count"] == len(files)
    assert detail["source_snapshot_ids"] == [retained.receipt.snapshot_id]
    digest = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert detail["manifest_sha256"] == digest
    assert detail["manifest_bytes"] > 10 * 1024 * 1024
    page_path, collected, offset = detail["next_page"], [], 0
    while page_path:
        page = await test_client.get(page_path)
        assert page.status_code == 200, page.text
        assert len(page.content) <= MAX_RANGE_BYTES
        body = page.json()
        assert body["file_count"] == len(files)
        assert body["manifest_sha256"] == digest
        assert body["offset"] == offset
        assert body["manifest"]["source_snapshot_ids"] == original["source_snapshot_ids"]
        collected.extend(body["manifest"]["files"])
        offset = len(collected)
        page_path = path + "?limit=1000&cursor=" + body["next_cursor"] if body["next_cursor"] else None
    assert {**body["manifest"], "files": collected} == original
    retained.s3.get_object.assert_not_called()


@pytest.mark.asyncio
async def test_manifest_cursor_cannot_cross_identity_and_revocation_still_applies(test_client, db_session, retained):
    retained.run.manifest = {**retained.run.manifest, "files": [{"path": "one"}, {"path": "two"}]}
    await db_session.commit()
    path = retained.path + "/source-manifest"
    first = await test_client.get(path + "?limit=1")
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    retained.run.manifest = {**retained.run.manifest, "files": [{"path": "changed"}, {"path": "two"}]}
    await db_session.commit()
    changed = await test_client.get(path, params={"cursor": cursor})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "source_manifest_changed"
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == retained.run.project_id))
    binding.status = "revoked"
    await db_session.commit()
    denied = await test_client.get(path + "?limit=1")
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_manifest_rechecks_current_access_after_serialization(test_client, retained, monkeypatch):
    from src.products import l5_retained_sources

    allowed, original = l5_integration.permitted_source_refs, l5_retained_sources.manifest_identity
    revoked = {"value": False}

    async def rights(*args):
        return set() if revoked["value"] else await allowed(*args)

    def digest(manifest):
        result = original(manifest)
        revoked["value"] = True
        return result

    monkeypatch.setattr(l5_integration, "permitted_source_refs", rights)
    monkeypatch.setattr(l5_retained_sources, "manifest_identity", digest)
    response = await test_client.get(retained.path + "/source-manifest?limit=1")
    assert response.status_code == 404
    assert "source_snapshot_ids" not in response.text


def test_manifest_page_byte_ceiling_can_reduce_the_requested_file_count():
    from src.products.l5_retained_sources import manifest_page

    files = [{"path": "a" * 1024, "sha256": "a" * 64, "bytes": index} for index in range(1000)]
    raw = manifest_page({"files": files, "source_snapshot_ids": ["snapshot-one"]}, "a" * 64, 1000, None)
    body = json.loads(raw)
    assert len(raw) <= MAX_RANGE_BYTES
    assert 0 < len(body["manifest"]["files"]) < 1000
    assert body["file_count"] == 1000
    assert body["next_cursor"] == "a" * 64 + "." + str(len(body["manifest"]["files"]))

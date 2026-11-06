"""The Transform qualifier consumes the mounted, currently authorized source API."""

import io
import json
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

pytest.importorskip("src.products.l5_retained_sources", reason="requires the shared retained-source API")

from l5_services.adapters.transform.qualification_source import RetainedProductSource, TransformQualificationAuthority
from l5_services.models import Run, Work

from src.products.models import ProductSourceSnapshot
from src.products.source_capture import SnapshotObjects
from src.shared.models.vault import CredentialServiceDelegation
from tests.products import test_transform_backend

transform_gateway = test_transform_backend.transform_gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "revoked", "corrupt"])
async def test_retained_transform_source_uses_current_product_rights(transform_gateway, test_client, db_session, monkeypatch, failure):
    scenario = transform_gateway
    response = await test_client.post(scenario.path + "/runs", json=scenario.body, headers={"Idempotency-Key": "retained-source-request-0001"})
    assert response.status_code == 202, response.text
    run = await db_session.get(Run, (scenario.template.tenant_id, response.json()["run_id"]))
    snapshot = (
        await db_session.scalars(select(ProductSourceSnapshot).where(ProductSourceSnapshot.delegation_id == run.admission["delegation_id"]))
    ).one()
    contents = b"original source\n"
    source_ref = snapshot.source_ref
    entry = {"path": "src/app.py", "bytes": len(contents), "sha256": sha256(contents).hexdigest(), "mode": "100644", "source_ref": source_ref}
    inventory = json.dumps({"source_ref": source_ref, "exclusions": [".git"], "files": [entry]}).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("src/app.py")
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(info, contents)
    payloads = {"content": buffer.getvalue(), "manifest": inventory}
    receipt = {**snapshot.receipt, "exclusions": [".git"], "file_count": 1}
    for part, field in (("content", "content"), ("manifest", "file_manifest")):
        receipt[field] = {
            **receipt[field],
            "artifact_id": "products/source-snapshots/" + part,
            "bytes": len(payloads[part]),
            "sha256": sha256(payloads[part]).hexdigest(),
        }
    snapshot.receipt = receipt
    run.manifest = {
        "scope": run.admission["scope"],
        "run_id": run.run_id,
        "sources": [source_ref],
        "source_snapshot_ids": [snapshot.id],
        "files": [entry],
        "archive": receipt["content"],
        "exclusions": [".git"],
    }
    run.state = "succeeded"
    run.deadline_at = datetime.now(UTC) - timedelta(hours=1)
    work = (await db_session.scalars(select(Work).where(Work.run_id == run.run_id))).one()
    work.completed_at, work.lease_owner, work.lease_until = datetime.now(UTC), None, None
    await db_session.commit()
    read_calls = []

    def get_object(**request):
        read_calls.append(request)
        return {"VersionId": request["VersionId"], "Body": io.BytesIO(payloads[request["Key"].rsplit("/", 1)[1]])}

    monkeypatch.setattr(
        "src.products.l5_retained_sources.get_snapshot_objects",
        lambda: SnapshotObjects(SimpleNamespace(get_object=get_object), "example-source-bucket"),
    )
    source = RetainedProductSource(test_client, run.admission["scope"], run.run_id)
    authority = TransformQualificationAuthority(run, source)
    assert not hasattr(authority, "provider_credentials")
    manifest = json.loads(json.dumps(run.manifest))
    assert await source.read_sealed_source(manifest=manifest, authority=authority) == {"src/app.py": contents}
    assert len(read_calls) == 2
    if failure == "revoked":
        grant = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
        grant.revoked_at = datetime.now(UTC)
        await db_session.commit()
        with pytest.raises(httpx.HTTPStatusError) as error:
            await source.read_sealed_source(manifest=manifest, authority=authority)
        assert error.value.response.status_code == 404
        assert len(read_calls) == 2
    elif failure == "corrupt":
        payloads["content"] = b"changed source"
        with pytest.raises(httpx.HTTPStatusError) as error:
            await source.read_sealed_source(manifest=manifest, authority=authority)
        assert error.value.response.status_code == 409
    await db_session.refresh(work)
    assert work.completed_at is not None and work.lease_owner is None

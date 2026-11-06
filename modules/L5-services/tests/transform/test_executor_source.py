"""Product snapshot projection for isolated Transform jobs, with no provider effects."""

import io
import json
import stat
import zipfile
from hashlib import sha256
from types import SimpleNamespace

import pytest

from l5_services.adapters.transform.source import ProductSealedSource
from l5_services.source_manifest import SourceIntegrityError


SOURCE = {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "repository",
          "record_type": "repository_commit", "record_id": "example/project", "record_revision": "a" * 40}
SCOPE = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"}


def fixture(data=b"original\n"):
    path = "src/example.py"
    entry = {"path": path, "sha256": sha256(data).hexdigest(), "bytes": len(data), "source_ref": SOURCE}
    inventory = {"files": [entry], "source_ref": SOURCE, "exclusions": []}
    manifest_bytes = json.dumps(inventory).encode()
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        info = zipfile.ZipInfo(path)
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(info, data)
    contents = archive_buffer.getvalue()
    content_ref = {"bytes": len(contents), "sha256": sha256(contents).hexdigest()}
    receipt = {"snapshot_id": "sealed-a", "source_ref": SOURCE, "content": content_ref,
               "file_manifest": {"bytes": len(manifest_bytes), "sha256": sha256(manifest_bytes).hexdigest()},
               "file_count": 1, "exclusions": []}
    manifest = {"run_id": "run-a", "scope": SCOPE, "sources": [SOURCE], "source_snapshot_ids": ["sealed-a"],
                "archive": content_ref, "files": [{**entry, "mode": "100644"}], "exclusions": []}
    authority = SimpleNamespace(scope=SCOPE, source_request={"delegation_id": "delegation-a",
        "delegation_revision": 1, "operation_id": "run-a", "attempt_id": "work-a",
        "lease_owner": "worker-a", "lease_fence": 1},
        capture_limits={"max_files": 2, "max_bytes": len(data)})

    class Client:
        calls = []

        async def read_source(self, product_id, project_id, snapshot_id, request):
            self.calls.append(("receipt", product_id, project_id, snapshot_id, request))
            return receipt

        async def read_source_manifest(self, product_id, project_id, snapshot_id, request):
            self.calls.append(("manifest", product_id, project_id, snapshot_id, request))
            return manifest_bytes

        async def stream_source(self, product_id, project_id, snapshot_id, request):
            self.calls.append(("bytes", product_id, project_id, snapshot_id, request))
            yield contents

    return Client(), manifest, authority, data


@pytest.mark.asyncio
async def test_product_scoped_source_projection_requires_sealed_original_bytes():
    client, manifest, authority, data = fixture()
    files = await ProductSealedSource(client).read_sealed_source(manifest=manifest, authority=authority)
    assert files == {"src/example.py": data}
    assert [call[0] for call in client.calls] == ["receipt", "manifest", "bytes"]
    assert all(call[1:4] == ("product-a", "project-a", "sealed-a")
               and call[4] is authority.source_request for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("alter", ["foreign_scope", "stale_work", "foreign_source", "wrong_snapshot",
                                   "changed_inventory", "changed_bytes", "wrong_mode", "foreign_file"])
async def test_product_projection_refuses_foreign_or_changed_sources(alter):
    client, manifest, authority, _ = fixture()
    if alter == "foreign_scope":
        authority.scope = {**SCOPE, "project_id": "other-project"}
    elif alter == "stale_work":
        authority.source_request["operation_id"] = "other-run"
    elif alter == "foreign_source":
        manifest["sources"] = [{**SOURCE, "project_id": "other-project"}]
    elif alter == "wrong_snapshot":
        manifest["source_snapshot_ids"] = ["other-snapshot"]
    elif alter == "changed_inventory":
        manifest["files"][0]["sha256"] = "0" * 64
    elif alter == "changed_bytes":
        original_stream = client.stream_source

        async def changed(*args):
            async for chunk in original_stream(*args):
                yield chunk + b"substitution"

        client.stream_source = changed
    elif alter == "wrong_mode":
        manifest["files"][0]["mode"] = "100755"
    else:
        manifest["files"][0]["path"] = "src/other.py"
    with pytest.raises(SourceIntegrityError):
        await ProductSealedSource(client).read_sealed_source(manifest=manifest, authority=authority)
    if alter in {"foreign_scope", "stale_work", "foreign_source"}:
        assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,mode", [("../escape.py", stat.S_IFREG | 0o644),
                                       ("src/example.py", stat.S_IFLNK | 0o777)])
async def test_product_projection_refuses_unsafe_archive_even_with_matching_digest(name, mode):
    client, manifest, authority, data = fixture()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        archive.writestr(info, data)
    contents = output.getvalue()
    reference = {"bytes": len(contents), "sha256": sha256(contents).hexdigest()}
    manifest["archive"] = reference
    original_read = client.read_source

    async def read_source(*args):
        return {**await original_read(*args), "content": reference}

    async def stream_source(*args):
        yield contents

    client.read_source = read_source
    client.stream_source = stream_source
    with pytest.raises(SourceIntegrityError):
        await ProductSealedSource(client).read_sealed_source(manifest=manifest, authority=authority)

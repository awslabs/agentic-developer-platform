"""Full repository bytes stay on disk and immutable transfers fail closed."""

import hashlib
import io
import json
import shutil
import tracemalloc
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError

from l5_services.adapters.security.code_review import _stage_archive
from l5_services.product_client import ProductClient
from l5_services.runtime_support import ArchiveStore
from l5_services.source_manifest import SourceIntegrityError, materialize_inputs


def reference(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return {"artifact_id": "fixture", "version": "version-one", "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(), "media_type": "application/zip"}


async def test_full_repository_over_old_guards_has_bounded_memory_and_complete_coverage(tmp_path):
    now = datetime.now(UTC)
    source = {"tenant_id": "tenant", "project_id": "project", "source_connection_id": "connection",
              "record_type": "repository_commit", "record_id": "repo", "record_revision": "a" * 40}
    path = tmp_path / "repository.zip"
    block = b"source content\n" * 2048
    entries = []
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1001):
            name = f"src/{index}.py"
            archive.writestr(name, block)
            entries.append({"source_ref": source, "source_revision": source["record_revision"],
                            "path": name, "bytes": len(block), "sha256": hashlib.sha256(block).hexdigest()})
    listing = json.dumps({"source_ref": source, "files": entries, "exclusions": []}).encode()
    snapshot = {"snapshot_id": "snapshot", "source_ref": source, "exclusions": [], "file_count": len(entries),
                "content": reference(path), "file_manifest": {"bytes": len(listing),
                "sha256": hashlib.sha256(listing).hexdigest()}, "expires_at": (now + timedelta(hours=1)).isoformat()}
    run = SimpleNamespace(service_id="security", tenant_id="tenant", project_id="project", product_id="product",
                          run_id=str(uuid4()), configuration_revision=1, binding_generation=1,
                          capture_limits={"max_files": 1001, "max_bytes": path.stat().st_size},
                          input={"capability": "code_review", "sources": [source]}, admission={
                              "source_snapshots": [snapshot], "scope": {"tenant_id": "tenant"},
                              "delegation_id": "grant", "delegation_version": 1, "configuration_hash": "f" * 64,
                              "profile_ref": source, "adapter_revision": "1", "exclusions": []})

    class Sources:
        async def read_source(self, *args):
            return snapshot

        async def read_source_manifest(self, *args):
            return listing

        async def stream_source(self, *args):
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    yield chunk

    stored = tmp_path / "stored.zip"
    materialized_path = None

    class Store:
        async def put_archive_file(self, run_id, archive_path):
            nonlocal materialized_path
            materialized_path = archive_path
            assert isinstance(archive_path, Path)
            shutil.copyfile(archive_path, stored)
            return reference(stored)

    tracemalloc.start()
    try:
        manifest, contents = await materialize_inputs(run, SimpleNamespace(work_id="work", fence=1), Sources(),
                                                      archive_store=Store(), now=now)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024 < path.stat().st_size
    assert contents == [] and manifest["files"] == entries
    assert manifest["coverage_mode"] == "full_snapshot"
    assert manifest["archive"]["sha256"] == snapshot["content"]["sha256"]
    assert not materialized_path.exists()
    with zipfile.ZipFile(stored) as archive:
        assert archive.namelist() == [entry["path"] for entry in entries]


class BoundedBody(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 65536
        return super().read(size)


class ObjectStore:
    def __init__(self, *, corrupt=False, wrong_version=False):
        self.data = None
        self.corrupt, self.wrong_version = corrupt, wrong_version
        self.uploads, self.bodies = [], []

    def put_object(self, **request):
        self.uploads.append(request)
        assert request["IfNoneMatch"] == "*"
        assert hasattr(request["Body"], "read")
        if self.data is not None:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.data = request["Body"].read()
        assert request["ContentLength"] == len(self.data)
        self.metadata = request["Metadata"]
        return {"VersionId": "version-one"}

    def head_object(self, **request):
        return {"VersionId": "version-one", "ContentLength": len(self.data), "Metadata": self.metadata}

    def get_object(self, **request):
        assert request["VersionId"] == "version-one"
        data = b"X" + self.data[1:] if self.corrupt else self.data
        body = BoundedBody(data)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(data),
                "VersionId": "other-version" if self.wrong_version else "version-one"}


@pytest.mark.parametrize("corrupt,wrong_version", [(False, False), (True, False), (False, True)])
async def test_archive_store_file_readback_and_collision_are_verified(tmp_path, corrupt, wrong_version):
    path = tmp_path / "source.zip"
    path.write_bytes(b"PK" + b"x" * 200000)
    client = ObjectStore()
    store = ArchiveStore("private-fixture", client)
    run_id = str(uuid4())
    artifact = await store.put_archive_file(run_id, path)
    assert artifact["version"] == "version-one"
    async with store.get_archive_file(run_id, artifact) as loaded:
        assert loaded.read_bytes() == path.read_bytes()
        temporary = loaded
    assert not temporary.exists()
    client.corrupt, client.wrong_version = corrupt, wrong_version
    if corrupt or wrong_version:
        with pytest.raises(ValueError, match="changed"):
            await store.put_archive_file(run_id, path)
    else:
        assert await store.put_archive_file(run_id, path) == artifact
    assert all(body.closed for body in client.bodies)
    assert len(client.uploads) == 2


@pytest.mark.parametrize("corrupt", [False, True])
def test_native_staging_collision_reads_actual_bytes_and_never_overwrites(tmp_path, corrupt):
    path = tmp_path / "source.zip"
    path.write_bytes(b"PK" + b"x" * 200000)
    client = ObjectStore()
    artifact = reference(path)
    _stage_archive(client, "bucket", "key", path, artifact)
    original = client.data
    client.corrupt = corrupt
    if corrupt:
        with pytest.raises(SourceIntegrityError, match="content changed"):
            _stage_archive(client, "bucket", "key", path, artifact)
    else:
        _stage_archive(client, "bucket", "key", path, artifact)
    assert client.data == original
    assert all(body.closed for body in client.bodies)


@pytest.mark.parametrize("fault", [None, "status", "range", "sha", "version", "length", "body", "short"])
async def test_product_ranges_verify_identity_length_and_final_digest(fault):
    data = b"x" * (1024 * 1024 + 37)
    digest = hashlib.sha256(data).hexdigest()
    artifact = {"bytes": len(data), "sha256": digest, "version": "v1"}
    observed = []

    def respond(request):
        if request.url.path.endswith("/read"):
            return httpx.Response(200, json={"content": artifact})
        selected = request.headers["Range"]
        observed.append(selected)
        start, end = map(int, selected.removeprefix("bytes=").split("-"))
        headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}", "X-Artifact-Sha256": digest,
                   "X-Artifact-Version": "v1", "X-Artifact-Size": str(len(data)), "Content-Length": str(end - start + 1)}
        body = data[start:end + 1]
        if start:
            if fault == "range":
                headers["Content-Range"] = f"bytes 0-{end}/{len(data)}"
            if fault == "sha":
                headers["X-Artifact-Sha256"] = "0" * 64
            if fault == "version":
                headers["X-Artifact-Version"] = "v2"
            if fault == "length":
                headers["Content-Length"] = "0"
            if fault == "body":
                body = b"y" + body[1:]
            if fault == "short":
                body = body[:-1]
        return httpx.Response(200 if fault == "status" else 206, content=body, headers=headers)

    async with httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond)) as transport:
        client = ProductClient(transport, "grant")
        async def read():
            return b"".join([part async for part in client.stream_source(
                "product", "project", "snapshot", {"delegation_id": "grant"})])
        if fault:
            with pytest.raises(ValueError):
                await read()
        else:
            assert await read() == data
            assert observed == ["bytes=0-1048575", "bytes=1048576-1048612"]


def test_zip_directory_is_bounded_before_parser_allocation_and_accepts_zip64(tmp_path):
    import struct
    from l5_services.source_manifest import _bounded_zip_directory

    forged = tmp_path / "forged.zip"
    forged.write_bytes(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 300 * 1024 * 1024, 0, 0))
    with pytest.raises(SourceIntegrityError, match="oversized directory"):
        _bounded_zip_directory(forged, 100000)
    large = tmp_path / "zip64.zip"
    with zipfile.ZipFile(large, "w") as archive:
        for index in range(65536):
            archive.writestr(f"src/{index}", b"")
    _bounded_zip_directory(large, 100000)
    with pytest.raises(SourceIntegrityError, match="too many entries"):
        _bounded_zip_directory(large, 1000)

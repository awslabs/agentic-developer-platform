"""Full repository capture is complete, bounded, immutable, and disk-backed."""

import base64
import hashlib
import io
import json
import stat
import struct
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.products import source_capture_disk, source_snapshot_routes
from src.products.source_capture import MAX_RANGE_BYTES, SnapshotObjects, capture_source
from src.products.source_capture_disk import canonical_repository_file, check_zip_directory
from src.products.source_capture_metadata import MAX_CONTENT, REPOSITORY_MAX_BYTES, REPOSITORY_MAX_FILES, repository_capture_preflight
from src.products.source_snapshots import ArtifactRef, SourceSnapshotRequest


def capture_request(**extra):
    return SourceSnapshotRequest(
        delegation_id="delegation",
        delegation_revision=2,
        source_ref={
            "tenant_id": "tenant",
            "project_id": "project",
            "source_connection_id": "installation-map",
            "record_type": "repository",
            "record_id": "123",
            "record_revision": "a" * 40,
        },
        exclusions=extra.pop("exclusions", []),
        max_bytes=extra.pop("max_bytes", REPOSITORY_MAX_BYTES),
        max_files=extra.pop("max_files", REPOSITORY_MAX_FILES),
        retain_until=datetime.now(UTC) + timedelta(hours=1),
        **extra,
    )


def blob(path, content, **extra):
    return {
        "path": path,
        "size": len(content),
        "type": "blob",
        "mode": "100644",
        "sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
        **extra,
    }


def upstream(path, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, content)
    return path


def test_large_capture_preserves_every_byte_and_mode_and_bounds_reads(tmp_path, monkeypatch):
    content = b"x" * (MAX_CONTENT + 1)
    archive = upstream(tmp_path / "input.zip", [("root/large.bin", content, 0o100644), ("root/run", b"#!/bin/sh\n", 0o100755)])
    selected = [blob("run", b"#!/bin/sh\n", mode="100755"), blob("large.bin", content)]
    read = zipfile.ZipExtFile.read
    sizes = []

    def bounded(self, size=-1):
        sizes.append(size)
        assert 0 <= size <= MAX_RANGE_BYTES
        return read(self, size)

    with monkeypatch.context() as patch:
        patch.setattr(zipfile.ZipExtFile, "read", bounded)
        captured, manifest, count = canonical_repository_file(archive, tmp_path, selected, capture_request())
    assert count == 2 and len(sizes) > 6
    assert stat.S_IMODE(captured.stat().st_mode) == stat.S_IMODE(manifest.stat().st_mode) == 0o600
    with zipfile.ZipFile(captured) as result:
        assert result.namelist() == ["large.bin", "run"]
        assert result.read("large.bin") == content
        assert result.getinfo("run").external_attr >> 16 == 0o100755
    document = json.loads(manifest.read_bytes())
    assert document["exclusions"] == []
    assert [(entry["path"], entry["bytes"]) for entry in document["files"]] == [("large.bin", len(content)), ("run", 10)]
    assert document["files"][0]["sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("failure", ["missing", "extra", "size", "hash", "mode", "duplicate", "traversal", "symlink", "root", "encrypted"])
def test_archive_must_match_the_entire_selected_tree(tmp_path, failure):
    entries = [("root/app.py", b"same", 0o100644)]
    selected = [blob("app.py", b"same")]
    if failure == "missing":
        selected.append(blob("lost.py", b"lost"))
    elif failure == "extra":
        entries.append(("root/extra.py", b"extra", 0o100644))
    elif failure == "size":
        selected[0]["size"] += 1
    elif failure == "hash":
        selected[0]["sha"] = "f" * 40
    elif failure == "mode":
        entries[0] = ("root/app.py", b"same", 0o100755)
    elif failure == "duplicate":
        entries.append(entries[0])
    elif failure == "traversal":
        entries[0] = ("root/../app.py", b"same", 0o100644)
    elif failure == "symlink":
        entries[0] = ("root/app.py", b"same", 0o120644)
    elif failure == "root":
        entries.append(("other/extra.py", b"extra", 0o100644))
    archive = upstream(tmp_path / "input.zip", entries)
    if failure == "encrypted":
        raw = bytearray(archive.read_bytes())
        index = raw.index(b"PK\x01\x02")
        raw[index + 8 : index + 10] = struct.pack("<H", 1)
        archive.write_bytes(raw)
    with pytest.raises(HTTPException) as error:
        canonical_repository_file(archive, tmp_path, selected, capture_request())
    assert error.value.status_code in (409, 422)


def test_explicit_exclusions_are_the_only_permitted_omission(tmp_path):
    archive = upstream(tmp_path / "input.zip", [("root/app.py", b"app", 0o100644), ("root/generated/large", b"excluded", 0o100644)])
    _, manifest, count = canonical_repository_file(archive, tmp_path, [blob("app.py", b"app")], capture_request(exclusions=["generated/"]))
    result = json.loads(manifest.read_bytes())
    assert count == 1 and result["exclusions"] == ["generated/"] and len(result["files"]) == 1


@pytest.mark.parametrize("change", ["files", "bytes", "directory", "false_directory_count"])
def test_over_limit_sources_fail_before_creating_output(tmp_path, change):
    archive = upstream(tmp_path / "input.zip", [("root/a", b"a", 0o100644), ("root/b", b"b", 0o100644)])
    request = capture_request(max_files=1) if change == "files" else capture_request(max_bytes=1) if change == "bytes" else capture_request()
    if change == "directory":
        raw = bytearray(archive.read_bytes())
        offset = raw.rindex(b"PK\x05\x06")
        raw[offset + 12 : offset + 16] = struct.pack("<L", 100 * 1024 * 1024)
        archive.write_bytes(raw)
    elif change == "false_directory_count":
        raw = bytearray(archive.read_bytes())
        offset = raw.rindex(b"PK\x05\x06")
        raw[offset + 8 : offset + 12] = struct.pack("<2H", 1, 1)
        archive.write_bytes(raw)
    with pytest.raises(HTTPException):
        canonical_repository_file(archive, tmp_path, [blob("a", b"a"), blob("b", b"b")], request)
    assert not (tmp_path / "content.zip").exists()


@pytest.mark.parametrize("file_count", [65535, 65536])
def test_large_metadata_limits_are_explicit_and_zip64_size_is_exact(tmp_path, file_count):
    metadata = {"sha": "b" * 40, "truncated": False, "tree": [blob("large", b"", size=MAX_CONTENT + 1)]}
    assert repository_capture_preflight(metadata, "b" * 40, [])["status"] == "exceeds_limits"
    assert (
        repository_capture_preflight(metadata, "b" * 40, [], max_bytes=REPOSITORY_MAX_BYTES, max_files=REPOSITORY_MAX_FILES)["status"]
        == "within_limits"
    )
    entries = [blob(f"file-{index}", b"") for index in range(file_count)]
    metadata["tree"] = entries
    predicted = repository_capture_preflight(metadata, "b" * 40, [], max_bytes=REPOSITORY_MAX_BYTES, max_files=REPOSITORY_MAX_FILES)["archive_bytes"]
    archive = tmp_path / "zip64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        for entry in entries:
            output.writestr(entry["path"], b"")
    assert archive.stat().st_size == predicted
    check_zip_directory(archive)


def artifact(content=b"abcdef"):
    return ArtifactRef(
        artifact_id="products/source-snapshots/test",
        version="immutable-version",
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="application/zip",
        bytes=len(content),
    )


def test_file_store_uses_streaming_upload_and_an_exact_immutable_version(tmp_path):
    path = tmp_path / "large.zip"
    content = b"z" * (MAX_CONTENT + 1)
    path.write_bytes(content)
    s3 = MagicMock()

    def upload(**kwargs):
        assert kwargs["IfNoneMatch"] == "*" and kwargs["ContentLength"] == len(content)
        assert hasattr(kwargs["Body"], "read")
        digest = hashlib.sha256()
        while chunk := kwargs["Body"].read(65536):
            digest.update(chunk)
        assert digest.hexdigest() == kwargs["Metadata"]["sha256"]
        assert base64.b64decode(kwargs["ChecksumSHA256"]) == digest.digest()
        return {"VersionId": "version-1"}

    s3.put_object.side_effect = upload
    captured = SnapshotObjects(s3, "private").put_file(path, "application/zip")
    assert captured.bytes == len(content) and captured.version == "version-1" and captured.sha256 == hashlib.sha256(content).hexdigest()
    with pytest.raises(HTTPException) as error:
        SnapshotObjects(s3, "private").put_file(path, "text/plain")
    assert error.value.status_code == 422 and s3.put_object.call_count == 1


@pytest.mark.parametrize("failure", [None, "version", "size", "range", "hash", "short", "long", "read_error"])
def test_ranged_storage_validates_version_bounds_and_closes_stream(failure):
    ref = artifact()
    stream = io.BytesIO(b"bc")
    result = {"Body": stream, "VersionId": ref.version, "ContentLength": 2, "ContentRange": "bytes 1-2/6", "Metadata": {"sha256": ref.sha256}}
    if failure == "version":
        result["VersionId"] = "other"
    elif failure == "size":
        result["ContentLength"] = 3
    elif failure == "range":
        result["ContentRange"] = "bytes 1-2/7"
    elif failure == "hash":
        result["Metadata"]["sha256"] = "f" * 64
    elif failure in ("short", "long"):
        stream = result["Body"] = io.BytesIO(b"b" if failure == "short" else b"bcd")
    elif failure == "read_error":
        stream = result["Body"] = MagicMock()
        stream.read.side_effect = OSError("synthetic read failure")
    s3 = MagicMock()
    s3.get_object.return_value = result
    store = SnapshotObjects(s3, "private")
    if failure:
        with pytest.raises(HTTPException) as error:
            store.read_range(ref, 1, 2)
        assert error.value.status_code == (503 if failure == "read_error" else 409)
    else:
        assert store.read_range(ref, 1, 2) == b"bc"
    if failure == "read_error":
        stream.close.assert_called_once()
    else:
        assert stream.closed
    assert s3.get_object.call_args.kwargs == {"Bucket": "private", "Key": ref.artifact_id, "VersionId": ref.version, "Range": "bytes=1-2"}


@pytest.mark.parametrize("bounds", [(-1, 0), (1, 0), (0, 6), (0, MAX_RANGE_BYTES), (True, 2)])
def test_invalid_storage_ranges_never_touch_object_store(bounds):
    s3 = MagicMock()
    with pytest.raises(HTTPException) as error:
        SnapshotObjects(s3, "private").read_range(artifact(), *bounds)
    assert error.value.status_code == 416
    s3.get_object.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", [False, True])
async def test_download_ranges_recheck_authority_before_releasing_data(monkeypatch, manifest):
    ref = artifact()
    receipt = SimpleNamespace(content=ref, file_manifest=ref)
    auth = AsyncMock(return_value=receipt)
    monkeypatch.setattr(source_snapshot_routes, "authorized_snapshot", auth)
    objects = SimpleNamespace(read_range=MagicMock(return_value=b"bc"))
    request = Request({"type": "http", "headers": [(b"range", b"bytes=1-2")]})
    response = await source_snapshot_routes.snapshot_bytes("product", "project", "snapshot", None, request, None, None, objects, manifest=manifest)
    assert response.status_code == 206 and response.body == b"bc" and auth.await_count == 2
    assert response.headers["content-range"] == "bytes 1-2/6"
    assert response.headers["x-artifact-sha256"] == ref.sha256
    assert response.headers["x-artifact-version"] == ref.version
    assert response.headers["x-artifact-size"] == "6" and response.headers["content-length"] == "2"
    assert response.headers["cache-control"] == "no-store"
    auth.side_effect = [receipt, SimpleNamespace(content=ref, file_manifest=None)]
    with pytest.raises(HTTPException) as error:
        await source_snapshot_routes.snapshot_bytes("product", "project", "snapshot", None, request, None, None, objects, manifest=manifest)
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "range_header", ["", "bytes=1-", "bytes=-2", "bytes=0-1,3-4", "bytes=3-2", "bytes=0-6", "bytes=0-1048576", "bytes=0-9999999999999"]
)
async def test_download_rejects_malformed_or_out_of_bounds_ranges_before_storage(monkeypatch, range_header):
    monkeypatch.setattr(source_snapshot_routes, "authorized_snapshot", AsyncMock(return_value=SimpleNamespace(content=artifact())))
    objects = SimpleNamespace(read_range=MagicMock())
    request = Request({"type": "http", "headers": [(b"range", range_header.encode())]})
    with pytest.raises(HTTPException) as error:
        await source_snapshot_routes.snapshot_bytes("product", "project", "snapshot", None, request, None, None, objects, manifest=False)
    assert error.value.status_code == 416
    objects.read_range.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "truncated", "changed_blob", "foreign_redirect", "over_archive_limit"])
async def test_capture_fetches_pinned_metadata_and_streams_only_the_verified_archive(tmp_path, monkeypatch, failure):
    request = capture_request()
    upstream_path = upstream(tmp_path / "input.zip", [("root/app.py", b"application", 0o100644)])
    raw_archive = upstream_path.read_bytes()
    requested = []

    def handle(http_request):
        requested.append(http_request)
        if http_request.url.host == "codeload.github.com":
            assert "authorization" not in http_request.headers
            return httpx.Response(200, content=raw_archive)
        assert http_request.headers["authorization"] == "Bearer synthetic-token"
        if "/git/commits/" in http_request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40, "tree": {"sha": "b" * 40}})
        if "/git/trees/" in http_request.url.path:
            item = blob("app.py", b"application")
            if failure == "changed_blob":
                item["sha"] = "f" * 40
            return httpx.Response(200, json={"sha": "b" * 40, "truncated": failure == "truncated", "tree": [item]})
        assert http_request.url.path == "/repos/example/project/zipball/" + "a" * 40
        return httpx.Response(
            302,
            headers={
                "location": "https://untrusted.example/archive"
                if failure == "foreign_redirect"
                else "https://codeload.github.com/example/project/legacy.zip/" + "a" * 40
            },
        )

    transport = httpx.MockTransport(handle)
    async_client = httpx.AsyncClient
    client = async_client(base_url="https://api.github.com", transport=transport)
    github = SimpleNamespace(
        _http_client=client,
        get_installation_repository=AsyncMock(return_value={"id": 123, "full_name": "example/project", "owner": {"id": 789, "login": "example"}}),
        get_installation_token=AsyncMock(return_value="synthetic-token"),
        aclose=client.aclose,
    )
    monkeypatch.setattr(source_capture_disk, "GitHubAppClient", lambda *_: github)
    monkeypatch.setattr(source_capture_disk.httpx, "AsyncClient", lambda **kwargs: async_client(transport=transport, **kwargs))
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("app", "key"))
    if failure == "over_archive_limit":
        monkeypatch.setattr(source_capture_disk, "REPOSITORY_MAX_BYTES", len(raw_archive) - 1)
    retained = {}
    s3 = MagicMock()

    def put(**kwargs):
        retained[kwargs["Key"]] = kwargs["Body"].read()
        return {"VersionId": "stored-version"}

    s3.put_object.side_effect = put
    binding = SimpleNamespace(
        kind="repository", provider="github", installation_id="installation-map", tenant_id="tenant", native_id="123", namespace="789"
    )
    mapping = SimpleNamespace(
        id="installation-map", org_id="tenant", provider="github", ownership_disputed=False, installation_id="456", provider_scope_id="789"
    )
    if failure:
        with pytest.raises(HTTPException):
            await capture_source(binding, mapping, request, SnapshotObjects(s3, "private"))
        assert not retained
    else:
        result = await capture_source(binding, mapping, request, SnapshotObjects(s3, "private"))
        assert result.file_count == 1 and result.content.version == "stored-version"
        with zipfile.ZipFile(io.BytesIO(retained[result.content.artifact_id])) as archive:
            assert archive.read("app.py") == b"application"
    assert client.is_closed
    assert all("/git/" in req.url.path or "/zipball/" in req.url.path or req.url.host == "codeload.github.com" for req in requested)

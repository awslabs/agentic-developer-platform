"""Real Product captures must be consumable by the L5 worker source reader."""

import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from l5_services.source_manifest import materialize_inputs

from src.products.source_capture import SnapshotObjects, canonical_repository, capture_source
from src.products.source_snapshots import SourceRef, SourceSnapshotRequest


def selected_document(path="architecture.md", content=b"# Architecture\nPrivate application storage.\n"):
    revision = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    binding = SimpleNamespace(
        kind="document",
        provider="github",
        installation_id="connection",
        tenant_id="tenant",
        project_id="project",
        native_id=f"123:{path}",
        desired={"repository_id": "123"},
    )
    request = SourceSnapshotRequest(
        delegation_id="delegation",
        delegation_revision=1,
        source_ref=SourceRef(
            tenant_id="tenant",
            project_id="project",
            source_connection_id="connection",
            record_type="document",
            record_id=binding.native_id,
            record_revision=revision,
        ),
        exclusions=[],
        max_bytes=8192,
        max_files=10,
        retain_until=datetime.now(UTC) + timedelta(hours=1),
    )
    return binding, SimpleNamespace(id="connection"), request, content


def object_store():
    retained = {}
    s3 = MagicMock()

    def put(**kwargs):
        retained[kwargs["Key"]] = kwargs["Body"]
        return {"VersionId": "immutable-version"}

    def get(**kwargs):
        assert kwargs["VersionId"] == "immutable-version"
        return {"Body": io.BytesIO(retained[kwargs["Key"]])}

    s3.put_object.side_effect = put
    s3.get_object.side_effect = get
    return SnapshotObjects(s3, "private-test-snapshots"), retained


@pytest.mark.parametrize("kind", ["document", "repository_commit"])
async def test_product_capture_materializes_through_actual_worker(monkeypatch, kind):
    binding, mapping, request, original = selected_document()
    if kind == "document":
        monkeypatch.setattr("src.products.source_capture.read_document_bytes", AsyncMock(return_value=(original, request.source_ref.record_revision)))
    else:
        binding.kind, binding.native_id = "repository", "123"
        request = request.model_copy(
            update={
                "source_ref": request.source_ref.model_copy(
                    update={
                        "record_type": kind,
                        "record_id": "123",
                        "record_revision": "a" * 40,
                    }
                )
            }
        )
        upstream = io.BytesIO()
        with zipfile.ZipFile(upstream, "w") as archive:
            archive.writestr("repo-commit/architecture.md", original)

        async def fetched_repository(_binding, _mapping, selected):
            return canonical_repository(upstream.getvalue(), selected)

        monkeypatch.setattr("src.products.source_capture.github_capture", fetched_repository)
    objects, retained = object_store()
    captured = await capture_source(binding, mapping, request, objects)
    snapshot = {"snapshot_id": "captured-snapshot", **captured.model_dump(mode="json")}
    reference = request.source_ref.model_dump()

    class SourceReader:
        async def read_source(self, *args):
            return snapshot

        async def read_source_manifest(self, *args):
            return objects.get(captured.file_manifest)

        async def stream_source(self, *args):
            yield objects.get(captured.content)

    class ArchiveStore:
        async def put_archive(self, run_id, content):
            return objects.put(content, "application/zip").model_dump()

        async def put_archive_file(self, run_id, path):
            with open(path, "rb") as content:
                return objects.put(content.read(), "application/zip").model_dump()

    run = SimpleNamespace(
        service_id="security",
        tenant_id="tenant",
        product_id="product",
        project_id="project",
        run_id="run",
        input={"capability": "threat_model" if kind == "document" else "code_review", "sources": [reference]},
        capture_limits={"max_files": request.max_files, "max_bytes": request.max_bytes},
        configuration_revision=1,
        binding_generation=1,
        admission={
            "scope": {"tenant_id": "tenant", "product_id": "product", "project_id": "project"},
            "source_snapshots": [snapshot],
            "exclusions": [],
            "delegation_id": "delegation",
            "delegation_version": 1,
            "configuration_hash": "b" * 64,
            "profile_ref": {},
            "adapter_revision": "1",
        },
    )
    manifest, contents = await materialize_inputs(
        run,
        SimpleNamespace(work_id="work", fence=1, owner="worker"),
        SourceReader(),
        archive_store=ArchiveStore(),
    )
    assert manifest["sources"] == [reference]
    assert manifest["files"][0]["source_ref"] == reference
    assert manifest["files"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["files"][0]["path"] == "architecture.md"
    if kind == "document":
        assert contents == [original]
        assert manifest["archive"]["media_type"] == "text/markdown"
        assert manifest["coverage_mode"] == "documents"
    else:
        assert contents == []
        with zipfile.ZipFile(io.BytesIO(retained[manifest["archive"]["artifact_id"]])) as archive:
            assert archive.read("architecture.md") == original
        assert manifest["coverage_mode"] == "full_snapshot"
    assert retained


@pytest.mark.parametrize("change", ["tenant_id", "project_id", "source_connection_id", "record_id", "record_type", "record_revision"])
async def test_document_capture_rejects_foreign_or_unpinned_selection(monkeypatch, change):
    binding, mapping, request, _ = selected_document()
    request = request.model_copy(update={"source_ref": request.source_ref.model_copy(update={change: "foreign"})})
    read = AsyncMock()
    monkeypatch.setattr("src.products.source_capture.read_document_bytes", read)
    objects, _ = object_store()
    with pytest.raises(HTTPException) as error:
        await capture_source(binding, mapping, request, objects)
    assert error.value.detail["code"] == "immutable_source_required"
    read.assert_not_awaited()
    objects.s3.put_object.assert_not_called()


@pytest.mark.parametrize("change", ["revision", "size", "binary", "pdf", "exclusion"])
async def test_document_capture_refuses_changed_or_unsupported_bytes_before_storage(monkeypatch, change):
    binding, mapping, request, content = selected_document()
    revision = request.source_ref.record_revision
    if change == "revision":
        revision = "c" * 40
    elif change == "size":
        request = request.model_copy(update={"max_bytes": len(content) - 1})
    elif change == "binary":
        content = b"\xff\xfe\x00"
    elif change == "pdf":
        binding, mapping, request, content = selected_document("document.pdf", b"not a PDF")
        revision = request.source_ref.record_revision
    else:
        request = request.model_copy(update={"exclusions": ["architecture.md"]})
    monkeypatch.setattr("src.products.source_capture.read_document_bytes", AsyncMock(return_value=(content, revision)))
    objects, _ = object_store()
    with pytest.raises(HTTPException) as error:
        await capture_source(binding, mapping, request, objects)
    assert error.value.status_code == (409 if change == "revision" else 422)
    objects.s3.put_object.assert_not_called()


@pytest.mark.parametrize(
    "path,content,media_type",
    [
        ("design.pdf", b"%PDF-1.7\nminimal provider fixture", "application/pdf"),
        ("infra/storage.tf", b'resource "aws_s3_bucket" "application" {}\n', "text/plain"),
    ],
)
async def test_document_capture_preserves_original_type_path_and_bytes(monkeypatch, path, content, media_type):
    binding, mapping, request, _ = selected_document(path, content)
    monkeypatch.setattr("src.products.source_capture.read_document_bytes", AsyncMock(return_value=(content, request.source_ref.record_revision)))
    objects, _ = object_store()
    captured = await capture_source(binding, mapping, request, objects)
    assert captured.content.media_type == media_type
    assert objects.get(captured.content) == content
    assert json.loads(objects.get(captured.file_manifest))["files"][0]["path"] == path


@pytest.mark.parametrize("failure", [None, "truncated", "changed_blob", "foreign_tree", "oversized", "symlink", "unsafe_exclusion"])
async def test_sparse_capture_reads_only_selected_immutable_blobs(monkeypatch, failure):
    import httpx

    from src.products.source_capture import github_capture

    binding, mapping, request, _ = selected_document()
    binding.kind, binding.native_id, binding.namespace = "repository", "123", "789"
    mapping.org_id, mapping.provider, mapping.ownership_disputed = "tenant", "github", False
    mapping.installation_id, mapping.provider_scope_id = "456", "789"
    request = request.model_copy(
        update={
            "exclusions": ["docs", "other-components/"],
            "source_ref": request.source_ref.model_copy(
                update={
                    "record_type": "repository_commit",
                    "record_id": "123",
                    "record_revision": "a" * 40,
                }
            ),
        }
    )
    if failure == "unsafe_exclusion":
        request = request.model_copy(update={"exclusions": ["../docs"]})
    original = b"#!/bin/sh\necho selected component\n"
    digest = hashlib.sha1(b"blob " + str(len(original)).encode() + b"\0" + original).hexdigest()
    paths = []

    def handler(sent):
        paths.append(sent.url.path)
        assert sent.headers["Authorization"] == "Bearer synthetic-installation-token"
        if "/commits/" in sent.url.path:
            return httpx.Response(200, json={"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}})
        if "/git/trees/" in sent.url.path:
            assert sent.url.params["recursive"] == "1"
            return httpx.Response(
                200,
                json={
                    "sha": "c" * 40 if failure == "foreign_tree" else "b" * 40,
                    "truncated": failure == "truncated",
                    "tree": [
                        {"path": "docs", "type": "tree", "sha": "d" * 40},
                        {"path": "docs/large.bin", "type": "blob", "size": 100000000, "sha": "e" * 40, "mode": "100644"},
                        {"path": "other-components/large.bin", "type": "blob", "size": 100000000, "sha": "f" * 40, "mode": "100644"},
                        {
                            "path": "component/run.sh",
                            "type": "blob",
                            "size": 9000 if failure == "oversized" else len(original),
                            "sha": digest,
                            "mode": "120000" if failure == "symlink" else "100755",
                        },
                    ],
                },
            )
        assert sent.url.path.endswith("/git/blobs/" + digest)
        content = b"x" * len(original) if failure == "changed_blob" else original
        return httpx.Response(200, json={"sha": digest, "encoding": "base64", "size": len(original), "content": base64.b64encode(content).decode()})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")

    class GitHub:
        _http_client = client

        async def get_installation_repository(self, installation, repository):
            assert (installation, repository) == (456, 123)
            return {"id": 123, "full_name": "org/project", "owner": {"id": 789, "login": "org"}}

        async def get_installation_token(self, installation):
            return "synthetic-installation-token"

        async def aclose(self):
            await client.aclose()

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("synthetic-app", "synthetic-key"))
    monkeypatch.setattr("src.products.source_capture.GitHubAppClient", lambda *args: GitHub())
    if failure:
        with pytest.raises(HTTPException):
            await github_capture(binding, mapping, request)
        assert not any("zipball" in path for path in paths)
        if failure != "changed_blob":
            assert not any("/blobs/" in path for path in paths)
    else:
        content, manifest, count = await github_capture(binding, mapping, request)
        assert count == 1
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert archive.namelist() == ["component/run.sh"]
            assert archive.read("component/run.sh") == original
            assert archive.getinfo("component/run.sh").external_attr >> 16 == 0o100755
        assert json.loads(manifest)["exclusions"] == request.exclusions
        assert len(paths) == 3
        assert not any("zipball" in path for path in paths)

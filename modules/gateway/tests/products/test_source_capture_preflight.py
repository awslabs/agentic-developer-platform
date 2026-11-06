"""Metadata diagnostics never read source blobs or imply native scan coverage."""

import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src import l5_source_preflight
from src.products.source_capture_metadata import MAX_CONTENT, repository_capture_preflight
from src.products.source_snapshots import SourceSnapshotRequest


def blob(path="app.py", size=10, **extra):
    return {"path": path, "size": size, "mode": "100644", "type": "blob", "sha": "c" * 40, **extra}


def tree(*files, **extra):
    return {"sha": "b" * 40, "truncated": False, "tree": list(files), **extra}


def test_preflight_exact_zip_overhead_and_explicit_exclusions():
    files = [blob("src/é.py", 19), blob("ARCHITECTURE.md", 12)]
    result = repository_capture_preflight(tree(*files, blob("other/large.bin", MAX_CONTENT + 1)), "b" * 40, ["other/"])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        for file in files:
            output.writestr(file["path"], b"x" * file["size"])
    assert result["archive_bytes"] == len(archive.getvalue())
    assert result["selected_files"] == 2 and result["selected_bytes"] == 31
    assert result["excluded_entries"] == 1 and result["coverage_scope"] == "selected_paths"
    assert result["exclusions"] == ["other/"] and result["status"] == "within_limits"


def test_preflight_reports_all_size_failures_and_bounds_oversized_paths():
    result = repository_capture_preflight(tree(*(blob(f"file-{i}.bin", MAX_CONTENT + i) for i in range(1001))), "b" * 40, [])
    assert result["status"] == "exceeds_limits" and result["selected_files"] == 1001
    assert result["reasons"] == ["source_file_count_exceeded", "source_content_bytes_exceeded", "source_archive_bytes_exceeded"]
    assert result["oversized_file_count"] == 1001 and result["oversized_files_truncated"]
    assert len(result["oversized_files"]) == 20
    assert result["oversized_files"][0] == {"path": "file-1000.bin", "bytes": MAX_CONTENT + 1000}
    assert result["coverage_scope"] == "whole_repository"


def test_archive_overhead_can_exceed_limit_when_content_fits():
    result = repository_capture_preflight(tree(blob(size=MAX_CONTENT)), "b" * 40, [])
    assert result["reasons"] == ["source_archive_bytes_exceeded"]
    assert result["oversized_file_count"] == 1
    assert repository_capture_preflight(tree(blob()), "b" * 40, ["app.py"])["reasons"] == ["empty_source_selection"]


def test_exclusion_bound_matches_the_existing_capture_request_contract():
    assert SourceSnapshotRequest.model_json_schema()["properties"]["exclusions"]["maxItems"] == 100
    with pytest.raises(ValueError, match="invalid_exclusions"):
        repository_capture_preflight(tree(blob()), "b" * 40, [f"excluded-{index}" for index in range(101)])


@pytest.mark.parametrize(
    "metadata,exclusions",
    [
        (tree(blob(), truncated=True), []),
        (tree(blob(), sha="d" * 40), []),
        (tree(blob(), blob()), []),
        (tree(blob("../secret.py")), []),
        (tree(blob(mode="120000")), []),
        (tree(blob(size=True)), []),
        (tree(blob()), ["src/*"]),
        (tree(blob()), ["../other"]),
    ],
)
def test_incomplete_or_unsafe_metadata_cannot_produce_a_partial_estimate(metadata, exclusions):
    with pytest.raises(ValueError):
        repository_capture_preflight(metadata, "b" * 40, exclusions)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "truncated", "foreign_repository", "revoked", "oversized_response", "malformed_commit"])
async def test_github_preflight_uses_only_bounded_git_metadata(monkeypatch, failure):
    requested = []

    def handle(request):
        requested.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.url.path.endswith("/git/commits/" + "a" * 40):
            return httpx.Response(200, json={"sha": "a" * 40, "tree": "invalid" if failure == "malformed_commit" else {"sha": "b" * 40}})
        assert request.url.path.endswith("/git/trees/" + "b" * 40)
        assert request.url.params["recursive"] == "1"
        if failure == "revoked":
            return httpx.Response(403)
        if failure == "oversized_response":
            return httpx.Response(200, content=b" " * 1025)
        return httpx.Response(200, json=tree(blob(size=l5_source_preflight.MAX_CONTENT + 1), truncated=failure == "truncated"))

    client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handle))

    class GitHub:
        _http_client = client

        async def get_installation_repository(self, installation_id, repository_id):
            assert (installation_id, repository_id) == (456, 123)
            return {"id": 123, "full_name": "example/project", "owner": {"id": 999 if failure == "foreign_repository" else 789, "login": "example"}}

        async def get_installation_token(self, installation_id):
            return "synthetic-token"

        async def aclose(self):
            await client.aclose()

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("app", "key"))
    monkeypatch.setattr(l5_source_preflight, "GitHubAppClient", lambda *_: GitHub())
    if failure == "oversized_response":
        monkeypatch.setattr(l5_source_preflight, "MAX_TREE_BYTES", 1024)
    args = (
        SimpleNamespace(native_id="123", namespace="789"),
        SimpleNamespace(installation_id="456", provider_scope_id="789"),
        {"record_revision": "a" * 40},
        [],
    )
    if failure:
        with pytest.raises((ValueError, HTTPException)):
            await l5_source_preflight.read_repository_preflight(*args)
    else:
        result = await l5_source_preflight.read_repository_preflight(*args)
        assert result["status"] == "exceeds_limits"
        assert result["oversized_files"] == [{"path": "app.py", "bytes": l5_source_preflight.MAX_CONTENT + 1}]
    assert all("/git/commits/" in path or "/git/trees/" in path for path in requested)
    assert len(requested) <= 2

"""Unit tests for the browse backend (door/browse_backend.py).

Verifies:
- Action alias resolution ("list" → "ls", "read" → read handler)
- Content-path URI routing (content/wikis → S3 direct listing)
- S3 prefix listing for content paths
- S3 object read for action="read"
- URI normalization edge cases

Issue #2406.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from door.browse_backend import (
    _ACTION_ALIASES,
    _CONTENT_ROOTS,
    _list_s3_prefix,
    _read_content,
    browse,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeS3Client:
    """Minimal S3 client mock for browse backend tests."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        """Initialize with a dict of key → content bytes."""
        self._objects: dict[str, bytes] = objects or {}
        self.exceptions = MagicMock()
        self.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

    def list_objects_v2(self, Bucket: str, Prefix: str, Delimiter: str = "") -> dict:
        """Simulate S3 list_objects_v2 response."""
        contents = []
        common_prefixes = set()

        for key in sorted(self._objects.keys()):
            if not key.startswith(Prefix):
                continue

            # Get the part after the prefix
            relative = key[len(Prefix) :]
            if not relative:
                continue

            if Delimiter and Delimiter in relative:
                # This is a "subdirectory" — add to common prefixes
                dir_part = relative.split(Delimiter)[0]
                common_prefixes.add(Prefix + dir_part + Delimiter)
            else:
                # This is a direct file under the prefix
                contents.append(
                    {
                        "Key": key,
                        "Size": len(self._objects[key]),
                        "LastModified": "2026-06-29T00:00:00Z",
                    }
                )

        result: dict[str, Any] = {}
        if contents:
            result["Contents"] = contents
        if common_prefixes:
            result["CommonPrefixes"] = [{"Prefix": p} for p in sorted(common_prefixes)]
        return result

    def get_object(self, Bucket: str, Key: str) -> dict:
        """Simulate S3 get_object response."""
        if Key not in self._objects:
            raise self.exceptions.NoSuchKey(f"NoSuchKey: {Key}")
        body = MagicMock()
        body.read.return_value = self._objects[Key]
        return {"Body": body}


@pytest.fixture
def wiki_s3_client() -> FakeS3Client:
    """S3 client with sample wiki content."""
    return FakeS3Client(
        {
            "content/wikis/HKUDS-Vibe-Trading-wiki.md": b"# Vibe Trading Wiki\n\nContent here.",
            "content/wikis/aws-e-adp-wiki.md": b"# ADP Wiki\n\nPlatform docs.",
            "content/code-indexes/aws-e-adp.json": b'{"symbols": []}',
            "content/code-indexes/mattpocock-skills.json": b'{"symbols": []}',
        }
    )


@pytest.fixture
def empty_s3_client() -> FakeS3Client:
    """S3 client with no objects."""
    return FakeS3Client({})


# ---------------------------------------------------------------------------
# Action alias tests
# ---------------------------------------------------------------------------


class TestActionAliases:
    """Verify action aliases map correctly."""

    def test_list_alias_exists(self):
        """'list' is an alias for 'ls'."""
        assert _ACTION_ALIASES["list"] == "ls"

    @pytest.mark.asyncio
    async def test_list_action_returns_results(self, wiki_s3_client):
        """action='list' works the same as 'ls'."""
        results = await browse(
            "list",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_ls_action_returns_results(self, wiki_s3_client):
        """action='ls' returns results for content paths."""
        results = await browse(
            "ls",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_unknown_action_returns_empty(self, wiki_s3_client):
        """Unknown actions return empty list."""
        results = await browse(
            "unknown_action",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert results == []


# ---------------------------------------------------------------------------
# Content-path routing tests
# ---------------------------------------------------------------------------


class TestContentPathRouting:
    """Verify URIs starting with content roots route to S3 directly."""

    def test_content_roots_defined(self):
        """Content roots include expected prefixes."""
        assert "content" in _CONTENT_ROOTS
        assert "code-indexes" in _CONTENT_ROOTS

    @pytest.mark.asyncio
    async def test_content_wikis_lists_files(self, wiki_s3_client):
        """'content/wikis' lists wiki files from S3."""
        results = await browse(
            "ls",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        names = [h.data["name"] for h in results]
        assert "HKUDS-Vibe-Trading-wiki.md" in names
        assert "aws-e-adp-wiki.md" in names

    @pytest.mark.asyncio
    async def test_content_code_indexes_lists_files(self, wiki_s3_client):
        """'content/code-indexes' lists index files from S3."""
        results = await browse(
            "ls",
            "content/code-indexes",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        names = [h.data["name"] for h in results]
        assert "aws-e-adp.json" in names
        assert "mattpocock-skills.json" in names

    @pytest.mark.asyncio
    async def test_content_path_with_leading_slash(self, wiki_s3_client):
        """Leading slash in URI is handled correctly."""
        results = await browse(
            "ls",
            "/content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        names = [h.data["name"] for h in results]
        assert "HKUDS-Vibe-Trading-wiki.md" in names

    @pytest.mark.asyncio
    async def test_content_path_with_trailing_slash(self, wiki_s3_client):
        """Trailing slash in URI is stripped."""
        results = await browse(
            "ls",
            "content/wikis/",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        names = [h.data["name"] for h in results]
        assert "HKUDS-Vibe-Trading-wiki.md" in names

    @pytest.mark.asyncio
    async def test_content_path_entries_have_correct_structure(self, wiki_s3_client):
        """Content-path entries have expected fields."""
        results = await browse(
            "ls",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) > 0
        entry = results[0].data
        assert "name" in entry
        assert "path" in entry
        assert "entry_type" in entry
        assert entry["entry_type"] == "file"

    @pytest.mark.asyncio
    async def test_content_path_hits_have_empty_repo_name(self, wiki_s3_client):
        """Content-path hits have repo_name='' (shared assets, not repo-scoped)."""
        results = await browse(
            "ls",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        for hit in results:
            assert hit.repo_name == ""

    @pytest.mark.asyncio
    async def test_content_path_no_s3_client_returns_empty(self):
        """Missing S3 client returns empty for content paths."""
        results = await browse(
            "ls",
            "content/wikis",
            s3_client=None,
            bucket="test-bucket",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_content_path_no_bucket_returns_empty(self, wiki_s3_client):
        """Missing bucket returns empty for content paths."""
        results = await browse(
            "ls",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="",
        )
        assert results == []


# ---------------------------------------------------------------------------
# Read action tests
# ---------------------------------------------------------------------------


class TestReadAction:
    """Verify action='read' fetches S3 object content."""

    @pytest.mark.asyncio
    async def test_read_wiki_file(self, wiki_s3_client):
        """Read a wiki file by its content path."""
        results = await browse(
            "read",
            "content/wikis/HKUDS-Vibe-Trading-wiki.md",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) == 1
        data = results[0].data
        assert data["name"] == "HKUDS-Vibe-Trading-wiki.md"
        assert data["path"] == "content/wikis/HKUDS-Vibe-Trading-wiki.md"
        assert "# Vibe Trading Wiki" in data["content"]
        assert data["entry_type"] == "file"
        assert data["size"] > 0

    @pytest.mark.asyncio
    async def test_read_with_leading_slash(self, wiki_s3_client):
        """Leading slash in read URI is stripped."""
        results = await browse(
            "read",
            "/content/wikis/HKUDS-Vibe-Trading-wiki.md",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) == 1
        assert "# Vibe Trading Wiki" in results[0].data["content"]

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, wiki_s3_client):
        """Reading a nonexistent file returns empty."""
        results = await browse(
            "read",
            "content/wikis/nonexistent.md",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_read_empty_uri(self, wiki_s3_client):
        """Empty URI for read returns empty."""
        results = await browse(
            "read",
            "",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_read_no_s3_client(self):
        """No S3 client returns empty for read."""
        results = await browse(
            "read",
            "content/wikis/test.md",
            s3_client=None,
            bucket="test-bucket",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_read_code_index_json(self, wiki_s3_client):
        """Read a code-index JSON file."""
        results = await browse(
            "read",
            "content/code-indexes/aws-e-adp.json",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) == 1
        assert '"symbols"' in results[0].data["content"]


# ---------------------------------------------------------------------------
# _list_s3_prefix tests
# ---------------------------------------------------------------------------


class TestListS3Prefix:
    """Direct tests for _list_s3_prefix helper."""

    @pytest.mark.asyncio
    async def test_lists_files_at_prefix(self, wiki_s3_client):
        """Lists all files under the given prefix."""
        results = await _list_s3_prefix(
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        names = [r.data["name"] for r in results]
        assert "HKUDS-Vibe-Trading-wiki.md" in names
        assert "aws-e-adp-wiki.md" in names

    @pytest.mark.asyncio
    async def test_handles_subdirectories(self):
        """Subdirectories appear as directory entries."""
        s3 = FakeS3Client(
            {
                "content/data/subdir/file.txt": b"hello",
                "content/data/root-file.txt": b"world",
            }
        )
        results = await _list_s3_prefix(
            "content/data",
            s3_client=s3,
            bucket="test-bucket",
        )
        names = {r.data["name"]: r.data["entry_type"] for r in results}
        assert names.get("subdir") == "directory"
        assert names.get("root-file.txt") == "file"

    @pytest.mark.asyncio
    async def test_no_bucket_returns_empty(self, wiki_s3_client):
        """Empty bucket param returns empty list."""
        results = await _list_s3_prefix(
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="",
        )
        assert results == []


# ---------------------------------------------------------------------------
# _read_content tests
# ---------------------------------------------------------------------------


class TestReadContent:
    """Direct tests for _read_content helper."""

    @pytest.mark.asyncio
    async def test_reads_utf8_content(self, wiki_s3_client):
        """UTF-8 content is decoded correctly."""
        results = await _read_content(
            "content/wikis/aws-e-adp-wiki.md",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) == 1
        assert results[0].data["content"] == "# ADP Wiki\n\nPlatform docs."

    @pytest.mark.asyncio
    async def test_binary_content_returns_hex(self):
        """Non-UTF-8 content is returned as hex string."""
        s3 = FakeS3Client({"content/bins/data.bin": b"\x00\x01\x02\xff"})
        results = await _read_content(
            "content/bins/data.bin",
            s3_client=s3,
            bucket="test-bucket",
        )
        assert len(results) == 1
        assert results[0].data["content"] == "000102ff"

    @pytest.mark.asyncio
    async def test_returns_correct_size(self, wiki_s3_client):
        """Size field reflects actual byte length."""
        results = await _read_content(
            "content/wikis/aws-e-adp-wiki.md",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        expected_size = len(b"# ADP Wiki\n\nPlatform docs.")
        assert results[0].data["size"] == expected_size


# ---------------------------------------------------------------------------
# URI normalization tests
# ---------------------------------------------------------------------------


class TestURINormalization:
    """Verify URI edge cases are handled correctly."""

    @pytest.mark.asyncio
    async def test_root_uri_lists_repos(self):
        """Root '/' triggers repo listing (even if empty)."""
        results = await browse(
            "ls",
            "/",
            db_pool=None,
            s3_client=None,
            bucket="",
        )
        # No db_pool and no S3 → empty but no error
        assert results == []

    @pytest.mark.asyncio
    async def test_multiple_slashes_normalized(self, wiki_s3_client):
        """Multiple slashes are collapsed via parts splitting."""
        results = await browse(
            "ls",
            "///content///wikis///",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        # After strip() + rstrip("/") + split, parts = ["content", "wikis"]
        # The first part "content" is in _CONTENT_ROOTS → S3 listing
        # But the prefix becomes "content/wikis" which lists correctly
        names = [h.data["name"] for h in results]
        assert "HKUDS-Vibe-Trading-wiki.md" in names

    @pytest.mark.asyncio
    async def test_list_alias_with_content_path(self, wiki_s3_client):
        """action='list' + content path URI works end-to-end (the exact bug scenario)."""
        results = await browse(
            "list",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        names = [h.data["name"] for h in results]
        assert "HKUDS-Vibe-Trading-wiki.md" in names

    @pytest.mark.asyncio
    async def test_read_alias_with_content_path(self, wiki_s3_client):
        """action='read' + content path URI works end-to-end (the exact bug scenario)."""
        results = await browse(
            "read",
            "content/wikis/HKUDS-Vibe-Trading-wiki.md",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
        )
        assert len(results) == 1
        assert "# Vibe Trading Wiki" in results[0].data["content"]

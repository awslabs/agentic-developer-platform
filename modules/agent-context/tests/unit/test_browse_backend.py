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

import json
import re
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from door.browse_backend import (
    _ACTION_ALIASES,
    _CONTENT_ROOTS,
    _list_s3_prefix,
    _read_content,
    browse,
)

ZOEKT_URL = "http://zoekt:6070"


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


# ---------------------------------------------------------------------------
# Zoekt mock helpers
# ---------------------------------------------------------------------------


def _zoekt_response(file_names: list[str]) -> str:
    """Build a Zoekt /api/search response body listing the given file names."""
    return json.dumps(
        {
            "Result": {
                "FileMatches": [
                    {"Repository": "HKUDS/Vibe-Trading", "FileName": fn} for fn in file_names
                ]
            }
        }
    )


def _zoekt_whole_file_response(repo: str, file_name: str, content: str) -> str:
    """Build a Zoekt response with whole-file content (Whole=true)."""
    return json.dumps(
        {
            "Result": {
                "FileMatches": [
                    {"Repository": repo, "FileName": file_name, "Content": content},
                ]
            }
        }
    )


# ---------------------------------------------------------------------------
# Repo-scoped browse tests (Issue #2485)
# ---------------------------------------------------------------------------


class TestRepoScopedListing:
    """Verify repo-scoped URIs list a repo's directory tree via Zoekt.

    The eval dataset browses repo directories (e.g. uri='agent/') with a
    'project' arg naming the repo — this must route the WHOLE uri as a path
    inside that repo, not treat 'agent' as a repo name.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_scoped_list_returns_dir_entries(self):
        """browse list uri='agent/' project=HKUDS/Vibe-Trading lists agent/ contents."""
        respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(
                200,
                text=_zoekt_response(
                    [
                        "agent/api_server.py",
                        "agent/backtest/engine.py",
                        "agent/cli/main.py",
                        "agent/src/tools/x.py",
                        "agent/tests/test_x.py",
                    ]
                ),
            )
        )
        results = await browse(
            "list",
            "agent/",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        names = {h.data["name"]: h.data["entry_type"] for h in results}
        assert names.get("api_server.py") == "file"
        assert names.get("backtest") == "directory"
        assert names.get("cli") == "directory"
        assert names.get("src") == "directory"
        assert names.get("tests") == "directory"
        assert len(results) >= 5

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_scoped_query_uses_full_uri_as_path(self):
        """The Zoekt query scopes to the repo AND anchors the full URI as a path."""
        route = respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(
                200, text=_zoekt_response(["agent/backtest/engines/base.py"])
            )
        )
        await browse(
            "list",
            "agent/backtest/engines/",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        sent = json.loads(route.calls[0].request.content)
        # Repo anchored exactly on the full org/repo name, path prefix applied.
        assert "r:^([^/]+/)?HKUDS/Vibe\\-Trading$" in sent["q"]
        assert "f:^agent/backtest/engines/" in sent["q"]

    def test_repo_filter_matches_domain_qualified_and_bare_names(self):
        """The r: regex must match live Zoekt shard names, which are
        domain-qualified ("github.com/org/repo"), as well as bare catalog
        slugs — but never a fork with a suffix."""
        from door.browse_backend import _zoekt_repo_filter

        pattern = re.compile(_zoekt_repo_filter("HKUDS/Vibe-Trading"))
        assert pattern.search("github.com/HKUDS/Vibe-Trading")
        assert pattern.search("HKUDS/Vibe-Trading")
        assert not pattern.search("github.com/HKUDS/Vibe-Trading-fork")
        assert not pattern.search("evil.com/prefix/HKUDS/Vibe-Trading-fork")

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_scoped_empty_uri_lists_repo_root(self):
        """Empty URI under a repo scope lists the repo top level."""
        respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(
                200,
                text=_zoekt_response(["agent/x.py", "frontend/y.tsx", "README.md"]),
            )
        )
        results = await browse(
            "list",
            "",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        names = {h.data["name"] for h in results}
        assert {"agent", "frontend", "README.md"} <= names

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_scoped_content_root_still_routes_to_s3(self, wiki_s3_client):
        """A content-root URI keeps S3 routing even when repo_scope is set."""
        results = await browse(
            "list",
            "content/wikis",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        names = [h.data["name"] for h in results]
        assert "HKUDS-Vibe-Trading-wiki.md" in names

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_scoped_nonexistent_path_returns_empty(self):
        """A nonexistent path under a repo scope returns empty (edge case)."""
        respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(200, text=_zoekt_response([]))
        )
        results = await browse(
            "list",
            "nonexistent/path/",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        assert results == []


class TestRepoScopedRead:
    """Verify action='read' with a repo scope reads file content via Zoekt."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_read_repo_file_returns_content(self):
        """read uri='agent/backtest/models.py' project=... returns file content."""
        respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(
                200,
                text=_zoekt_whole_file_response(
                    "HKUDS/Vibe-Trading",
                    "agent/backtest/models.py",
                    "class Position:\n    pass\n",
                ),
            )
        )
        results = await browse(
            "read",
            "agent/backtest/models.py",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        assert len(results) == 1
        data = results[0].data
        assert data["name"] == "models.py"
        assert data["path"] == "agent/backtest/models.py"
        assert "class Position" in data["content"]
        assert data["entry_type"] == "file"
        assert results[0].repo_name == "HKUDS/Vibe-Trading"

    @pytest.mark.asyncio
    @respx.mock
    async def test_read_repo_file_no_match_returns_empty(self):
        """read of a file Zoekt doesn't have returns empty."""
        respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(200, text=_zoekt_response([]))
        )
        results = await browse(
            "read",
            "agent/does_not_exist.py",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        assert results == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_read_content_root_still_uses_s3(self, wiki_s3_client):
        """A content-root read path uses S3 even with a repo scope set."""
        results = await browse(
            "read",
            "content/wikis/HKUDS-Vibe-Trading-wiki.md",
            s3_client=wiki_s3_client,
            bucket="test-bucket",
            zoekt_url=ZOEKT_URL,
            repo_scope="HKUDS/Vibe-Trading",
        )
        assert len(results) == 1
        assert "# Vibe Trading Wiki" in results[0].data["content"]


class TestUnscopedOrgRepoListing:
    """Verify unscoped 'org/repo' URIs list the repo (newest-comment repro)."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_org_repo_uri_lists_repo_root(self):
        """browse ls uri='HKUDS/Vibe-Trading' (no project) lists the repo top level."""
        route = respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(200, text=_zoekt_response(["agent/x.py", "README.md"]))
        )
        results = await browse(
            "ls",
            "HKUDS/Vibe-Trading",
            zoekt_url=ZOEKT_URL,
        )
        names = {h.data["name"] for h in results}
        assert {"agent", "README.md"} <= names
        # org/repo consumed as the repo name, not split into repo=HKUDS.
        sent = json.loads(route.calls[0].request.content)
        assert "r:^([^/]+/)?HKUDS/Vibe\\-Trading$" in sent["q"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_org_repo_uri_with_subpath(self):
        """browse ls uri='HKUDS/Vibe-Trading/agent' lists the agent/ subdir."""
        route = respx.post(f"{ZOEKT_URL}/api/search").mock(
            return_value=httpx.Response(200, text=_zoekt_response(["agent/api_server.py"]))
        )
        results = await browse(
            "ls",
            "HKUDS/Vibe-Trading/agent",
            zoekt_url=ZOEKT_URL,
        )
        names = {h.data["name"] for h in results}
        assert "api_server.py" in names
        sent = json.loads(route.calls[0].request.content)
        assert "r:^([^/]+/)?HKUDS/Vibe\\-Trading$" in sent["q"]
        assert "f:^agent/" in sent["q"]

"""Unit tests for browse catalog and capability manifest (Issues #2544, #2545, #2546).

Verifies:
- browse('/') returns repos from the catalog (C1 fix: no description column)
- Each repo entry includes a rich capability manifest from index_run_stages (C2)
- _build_capabilities_index correctly aggregates stage metrics
- _get_info returns per-repo manifest
- S3 fallback still works when no DB is available

Regression test: the original bug was SELECT repo_name, description FROM repositories
where `description` column did not exist, causing a silent exception and returning [].
These tests use a FakeDBPool that validates the SQL does NOT reference `description`.
"""

from __future__ import annotations

import pytest

from door.browse_backend import (
    _STAGE_TO_CAPABILITY,
    _build_capabilities_index,
    browse,
)


# ---------------------------------------------------------------------------
# Fixtures: FakeDBPool that validates SQL correctness
# ---------------------------------------------------------------------------


class FakeCursor:
    """A cursor that validates SQL queries against the real schema."""

    # Columns that ACTUALLY exist in the repositories table (from migration 001+)
    _VALID_REPO_COLUMNS = frozenset(
        {
            "id",
            "repo_name",
            "git_url",
            "owner",
            "allowed_principals",
            "last_indexed_sha",
            "indexed_at",
            "zoekt_status",
            "vectors_status",
            "structure_status",
            "sbom_status",
            "created_at",
            "updated_at",
            # From migration 002
            "wiki_status",
            # From migration 004
            "tenant_id",
            "owner_sub",
        }
    )

    def __init__(self, repo_rows: list[tuple], stage_rows: list[tuple]):
        self._repo_rows = repo_rows
        self._stage_rows = stage_rows
        self._result: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Execute SQL, validating it does NOT reference non-existent columns."""
        sql_lower = sql.lower()

        # REGRESSION CHECK: the old bug was referencing 'description' column
        if "from repositories" in sql_lower:
            assert "description" not in sql_lower, (
                "BUG REGRESSION: SQL references non-existent 'description' column "
                "in the repositories table. This was the root cause of #2544 — "
                "the column does not exist, causing psycopg2.errors.UndefinedColumn "
                "which is swallowed by the except block, returning [] to the caller."
            )

        # Route to appropriate mock data
        if "from repositories" in sql_lower:
            if params:
                # Single-repo lookup (WHERE repo_name = %s)
                target = params[0]
                self._result = [r for r in self._repo_rows if r[0] == target]
            else:
                self._result = self._repo_rows
        elif "from index_run_stages" in sql_lower:
            if params:
                # Per-repo filter — the SQL SELECT is (stage, status, metrics)
                # without the repo column, so strip it from returned rows
                target = params[0]
                self._result = [(r[1], r[2], r[3]) for r in self._stage_rows if r[0] == target]
            else:
                self._result = self._stage_rows

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConnection:
    """Fake psycopg2 connection wrapping a FakeCursor."""

    def __init__(self, repo_rows: list[tuple], stage_rows: list[tuple]):
        self._repo_rows = repo_rows
        self._stage_rows = stage_rows

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._repo_rows, self._stage_rows)


class FakeDBPool:
    """Fake connection pool for testing the catalog queries.

    Provides repo rows + index_run_stages rows as test data.
    """

    def __init__(
        self,
        repo_rows: list[tuple] | None = None,
        stage_rows: list[tuple] | None = None,
    ):
        """
        repo_rows: list of (repo_name, git_url, indexed_at) tuples
        stage_rows: list of (repo, stage, status, metrics) tuples
        """
        self._repo_rows = repo_rows or []
        self._stage_rows = stage_rows or []
        self._conn = FakeConnection(self._repo_rows, self._stage_rows)

    def getconn(self):
        return self._conn

    def putconn(self, conn):
        pass


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

SAMPLE_REPOS = [
    ("HKUDS/Vibe-Trading", "https://github.com/HKUDS/Vibe-Trading.git", "2026-06-29T10:00:00Z"),
    ("HKUDS/DeepTutor", "https://github.com/HKUDS/DeepTutor.git", "2026-06-28T08:00:00Z"),
    ("aws-e/adp", "https://github.com/aws-e/adp.git", "2026-06-30T12:00:00Z"),
]

SAMPLE_STAGES = [
    # Vibe-Trading: full indexing
    ("HKUDS/Vibe-Trading", "zoekt_index", "verified", {"shards": 1, "shard_bytes": 27661540}),
    ("HKUDS/Vibe-Trading", "cgc_structural", "verified", {"files": 549, "symbols": 5000}),
    ("HKUDS/Vibe-Trading", "scip_structural", "verified", {"nodes": 3512, "edges": 2923}),
    ("HKUDS/Vibe-Trading", "deepwiki", "verified", {"chars": 14616}),
    ("HKUDS/Vibe-Trading", "sbom_source", "verified", {"dependencies": 415}),
    # #2912: embed_vectors (source-code embeddings → S3 Vectors) feeds the
    # `vectors` capability. graphrag is skipped by design; embed_vectors verified
    # must be enough to flip vectors.ready true.
    ("HKUDS/Vibe-Trading", "embed_vectors", "verified", {"vectors": 1613, "files": 210}),
    ("HKUDS/Vibe-Trading", "graphrag", "skipped", None),
    # DeepTutor: partial
    ("HKUDS/DeepTutor", "zoekt_index", "verified", {"shards": 1, "shard_bytes": 15000000}),
    ("HKUDS/DeepTutor", "cgc_structural", "verified", {"files": 312, "symbols": 2800}),
    ("HKUDS/DeepTutor", "deepwiki", "verified", {"chars": 9200}),
    ("HKUDS/DeepTutor", "graphrag", "skipped", None),
    # adp: zoekt only
    ("aws-e/adp", "zoekt_index", "verified", {"shards": 2, "shard_bytes": 50000000}),
]


@pytest.fixture
def catalog_db_pool() -> FakeDBPool:
    """DB pool with sample repos and index stages."""
    return FakeDBPool(repo_rows=SAMPLE_REPOS, stage_rows=SAMPLE_STAGES)


@pytest.fixture
def empty_db_pool() -> FakeDBPool:
    """DB pool with no repos."""
    return FakeDBPool(repo_rows=[], stage_rows=[])


# ---------------------------------------------------------------------------
# C1: Catalog fix — browse('/') returns repos
# ---------------------------------------------------------------------------


class TestCatalogFix:
    """Verify browse('/') returns repos without referencing 'description' column."""

    @pytest.mark.asyncio
    async def test_browse_root_returns_repos(self, catalog_db_pool):
        """browse('/') returns all indexed repos from catalog."""
        results = await browse("ls", "/", db_pool=catalog_db_pool)
        assert len(results) == 3
        repo_names = [r.repo_name for r in results]
        assert "HKUDS/Vibe-Trading" in repo_names
        assert "HKUDS/DeepTutor" in repo_names
        assert "aws-e/adp" in repo_names

    @pytest.mark.asyncio
    async def test_browse_root_no_description_column(self, catalog_db_pool):
        """REGRESSION: SQL must NOT reference 'description' column (the #2544 bug)."""
        # If this test passes, the SQL is correct. The FakeCursor.execute()
        # method asserts that 'description' is NOT in any query on repositories.
        results = await browse("ls", "/", db_pool=catalog_db_pool)
        assert len(results) > 0  # Must succeed, not return []

    @pytest.mark.asyncio
    async def test_browse_root_entries_have_type(self, catalog_db_pool):
        """Each catalog entry has type=repository and entry_type=directory."""
        results = await browse("ls", "/", db_pool=catalog_db_pool)
        for hit in results:
            assert hit.data["type"] == "repository"
            assert hit.data["entry_type"] == "directory"

    @pytest.mark.asyncio
    async def test_browse_root_entries_have_git_url(self, catalog_db_pool):
        """Catalog entries include git_url and indexed_at."""
        results = await browse("ls", "/", db_pool=catalog_db_pool)
        vibe = next(r for r in results if r.repo_name == "HKUDS/Vibe-Trading")
        assert vibe.data["git_url"] == "https://github.com/HKUDS/Vibe-Trading.git"
        assert vibe.data["indexed_at"] is not None

    @pytest.mark.asyncio
    async def test_browse_root_empty_db_falls_to_s3(self, empty_db_pool):
        """Empty repositories table falls through to S3 fallback."""
        results = await browse("ls", "/", db_pool=empty_db_pool, s3_client=None, bucket="")
        # No S3 either → empty but no crash
        assert results == []

    @pytest.mark.asyncio
    async def test_browse_root_no_db_no_s3(self):
        """No DB and no S3 → empty, no crash."""
        results = await browse("ls", "/", db_pool=None, s3_client=None, bucket="")
        assert results == []

    @pytest.mark.asyncio
    async def test_browse_root_list_action_alias(self, catalog_db_pool):
        """action='list' (alias for 'ls') also returns catalog."""
        results = await browse("list", "/", db_pool=catalog_db_pool)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# C2: Rich capability manifest
# ---------------------------------------------------------------------------


class TestCapabilityManifest:
    """Verify per-repo capability manifest is rich and accurate."""

    @pytest.mark.asyncio
    async def test_manifest_present_in_catalog(self, catalog_db_pool):
        """Each catalog entry has a 'capabilities' dict."""
        results = await browse("ls", "/", db_pool=catalog_db_pool)
        for hit in results:
            assert "capabilities" in hit.data
            assert isinstance(hit.data["capabilities"], dict)

    @pytest.mark.asyncio
    async def test_vibe_trading_full_manifest(self, catalog_db_pool):
        """Vibe-Trading has code_search, call_graph, wiki, sbom, vectors."""
        results = await browse("ls", "/", db_pool=catalog_db_pool)
        vibe = next(r for r in results if r.repo_name == "HKUDS/Vibe-Trading")
        caps = vibe.data["capabilities"]

        # code_search: merged from zoekt_index + cgc_structural
        assert caps["code_search"]["ready"] is True
        assert caps["code_search"]["files"] == 549
        assert caps["code_search"]["symbols"] == 5000
        assert caps["code_search"]["shard_bytes"] == 27661540

        # call_graph from scip_structural
        assert caps["call_graph"]["ready"] is True
        assert caps["call_graph"]["nodes"] == 3512
        assert caps["call_graph"]["edges"] == 2923

        # wiki from deepwiki
        assert caps["wiki"]["ready"] is True
        assert caps["wiki"]["chars"] == 14616

        # sbom from sbom_source
        assert caps["sbom"]["ready"] is True
        assert caps["sbom"]["dependencies"] == 415

        # vectors: embed_vectors verified flips ready true even though graphrag
        # is skipped (both map to the `vectors` capability; ready is OR-merged).
        assert caps["vectors"]["ready"] is True
        assert caps["vectors"]["vectors"] == 1613

    @pytest.mark.asyncio
    async def test_partial_manifest(self, catalog_db_pool):
        """adp repo only has code_search (zoekt only), no other capabilities."""
        results = await browse("ls", "/", db_pool=catalog_db_pool)
        adp = next(r for r in results if r.repo_name == "aws-e/adp")
        caps = adp.data["capabilities"]

        assert caps["code_search"]["ready"] is True
        assert caps["code_search"]["shard_bytes"] == 50000000
        # No call_graph, wiki, sbom, vectors
        assert "call_graph" not in caps
        assert "wiki" not in caps
        assert "sbom" not in caps
        assert "vectors" not in caps

    @pytest.mark.asyncio
    async def test_info_returns_manifest(self, catalog_db_pool):
        """browse(action='info', uri='/HKUDS/Vibe-Trading') returns manifest."""
        results = await browse("info", "/HKUDS/Vibe-Trading", db_pool=catalog_db_pool)
        assert len(results) == 1
        data = results[0].data
        assert data["type"] == "repository"
        assert "capabilities" in data
        assert data["capabilities"]["code_search"]["ready"] is True

    @pytest.mark.asyncio
    async def test_info_nonexistent_repo(self, catalog_db_pool):
        """Info for a repo not in the DB returns empty."""
        results = await browse("info", "/nonexistent-repo", db_pool=catalog_db_pool)
        assert results == []


# ---------------------------------------------------------------------------
# _build_capabilities_index unit tests
# ---------------------------------------------------------------------------


class TestBuildCapabilitiesIndex:
    """Verify the capabilities index builder logic."""

    def test_empty_input(self):
        """Empty stage rows → empty index."""
        assert _build_capabilities_index([]) == {}

    def test_single_stage(self):
        """Single verified stage → one capability."""
        rows = [("repo-a", "zoekt_index", "verified", {"shards": 1, "shard_bytes": 100})]
        result = _build_capabilities_index(rows)
        assert result == {
            "repo-a": {"code_search": {"ready": True, "shards": 1, "shard_bytes": 100}}
        }

    def test_skipped_stage(self):
        """Skipped stage → ready=False."""
        rows = [("repo-a", "graphrag", "skipped", None)]
        result = _build_capabilities_index(rows)
        assert result == {"repo-a": {"vectors": {"ready": False}}}

    def test_embed_vectors_maps_to_vectors(self):
        """#2912: embed_vectors (verified) → vectors.ready=True with metrics."""
        rows = [("repo-a", "embed_vectors", "verified", {"vectors": 1613, "files": 210})]
        result = _build_capabilities_index(rows)
        assert result["repo-a"]["vectors"]["ready"] is True
        assert result["repo-a"]["vectors"]["vectors"] == 1613

    def test_vectors_or_merge_embed_over_skipped_graphrag(self):
        """#2912: embed_vectors verified flips vectors.ready even if graphrag skipped."""
        rows = [
            ("repo-a", "graphrag", "skipped", None),
            ("repo-a", "embed_vectors", "verified", {"vectors": 42}),
        ]
        result = _build_capabilities_index(rows)
        assert result["repo-a"]["vectors"]["ready"] is True

    def test_multiple_stages_merge(self):
        """Multiple stages contributing to same capability merge metrics."""
        rows = [
            ("repo-a", "zoekt_index", "verified", {"shard_bytes": 100}),
            ("repo-a", "cgc_structural", "verified", {"files": 50, "symbols": 200}),
        ]
        result = _build_capabilities_index(rows)
        cap = result["repo-a"]["code_search"]
        assert cap["ready"] is True
        assert cap["shard_bytes"] == 100
        assert cap["files"] == 50
        assert cap["symbols"] == 200

    def test_unknown_stage_ignored(self):
        """Stages not in _STAGE_TO_CAPABILITY are ignored."""
        rows = [("repo-a", "unknown_stage", "verified", {"foo": "bar"})]
        result = _build_capabilities_index(rows)
        assert result == {}

    def test_multiple_repos(self):
        """Capabilities are indexed per-repo."""
        rows = [
            ("repo-a", "zoekt_index", "verified", {"shard_bytes": 100}),
            ("repo-b", "deepwiki", "verified", {"chars": 5000}),
        ]
        result = _build_capabilities_index(rows)
        assert "repo-a" in result
        assert "repo-b" in result
        assert "code_search" in result["repo-a"]
        assert "wiki" in result["repo-b"]

    def test_none_metrics(self):
        """None metrics don't crash — just set ready status."""
        rows = [("repo-a", "deepwiki", "verified", None)]
        result = _build_capabilities_index(rows)
        assert result == {"repo-a": {"wiki": {"ready": True}}}


# ---------------------------------------------------------------------------
# Stage-to-capability mapping
# ---------------------------------------------------------------------------


class TestStageMapping:
    """Verify the stage→capability mapping covers expected stages."""

    def test_known_stages(self):
        """All expected stage names are mapped."""
        expected = {
            "zoekt_index",
            "cgc_structural",
            "scip_structural",
            "deepwiki",
            "sbom_source",
            "embed_vectors",
            "graphrag",
        }
        assert expected == set(_STAGE_TO_CAPABILITY.keys())

    def test_capability_keys(self):
        """All capability keys in the mapping are documented."""
        expected_caps = {"code_search", "call_graph", "wiki", "sbom", "vectors"}
        actual_caps = set(_STAGE_TO_CAPABILITY.values())
        assert actual_caps == expected_caps

"""Unit tests for the structural backend (door/structural_backend.py).

Tests the understand and impact verb implementations against the
code-index.json fixture format.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from door.structural_backend import (
    _find_callers,
    _matches_target,
    _parse_target,
    impact,
    load_code_index,
    understand,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def code_index_fixture() -> dict:
    """Load the code-index fixture."""
    fixture_path = FIXTURES_DIR / "code-index-fixture.json"
    return json.loads(fixture_path.read_text())


@pytest.fixture
def mock_s3_client(code_index_fixture):
    """Mock S3 client that returns the fixture data."""
    client = MagicMock()
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(code_index_fixture).encode()
    client.get_object.return_value = {"Body": body_mock}
    # Set up NoSuchKey exception
    client.exceptions = MagicMock()
    client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    return client


# ---------------------------------------------------------------------------
# _parse_target tests
# ---------------------------------------------------------------------------


class TestParseTarget:
    def test_symbol_reference(self):
        # Short-name format: first component is repo_id (eval corpus pattern)
        repo, target = _parse_target("myrepo::connect_db")
        assert repo == "myrepo"
        assert target == "connect_db"

    def test_symbol_reference_org_repo(self):
        # "org/repo::symbol" — both components form repo_id (Bug #1635 fix)
        repo, target = _parse_target("org/repo::connect_db")
        assert repo == "org/repo"
        assert target == "connect_db"

    def test_symbol_reference_org_repo_real_examples(self):
        # Real-world examples from the issue reproduction (Bug #1635)
        repo, target = _parse_target("colbymchenry/codegraph::length")
        assert repo == "colbymchenry/codegraph"
        assert target == "length"

        repo, target = _parse_target("addyosmani/agent-skills::fs")
        assert repo == "addyosmani/agent-skills"
        assert target == "fs"

    def test_symbol_reference_with_file_path(self):
        # "repo/path/file.py::symbol" — file has dot, so first component is repo
        repo, target = _parse_target("myrepo/src/db.py::connect_db")
        assert repo == "myrepo"
        assert target == "src/db.py::connect_db"

    def test_symbol_reference_org_repo_with_file_path(self):
        # "org/repo/src/db.py::symbol" — last component has dot (file), so falls
        # back to path-based heuristic (first component = repo). This is the
        # ambiguous case; resolve_repo_name handles the recovery at query time.
        # The critical fix (Bug #1635) is the org/repo::symbol case (no file).
        repo, target = _parse_target("org/repo/src/db.py::connect_db")
        assert repo == "org"
        assert target == "repo/src/db.py::connect_db"

    def test_file_path(self):
        # First component is repo_id, rest is the path (no :: present)
        repo, target = _parse_target("myrepo/src/db.py")
        assert repo == "myrepo"
        assert target == "src/db.py"

    def test_file_and_symbol(self):
        # "repo/file.py::symbol" — second component has dot = file
        repo, target = _parse_target("myrepo/src/db.py::connect_db")
        assert repo == "myrepo"
        assert target == "src/db.py::connect_db"

    def test_domain_prefix(self):
        # Domain-like first component uses 3 parts as repo_id
        repo, target = _parse_target("github.com/org/repo/src/db.py")
        assert repo == "github.com/org/repo"
        assert target == "src/db.py"

    def test_domain_prefix_with_symbol(self):
        # Domain-prefixed target with :: symbol
        repo, target = _parse_target("github.com/org/repo/src/db.py::connect_db")
        assert repo == "github.com/org/repo"
        assert target == "src/db.py::connect_db"

    def test_empty(self):
        repo, target = _parse_target("")
        assert repo == ""
        assert target == ""

    def test_single_component(self):
        # Single component = repo-level target (FIX for #1535: understand("codegraph") bug)
        repo, target = _parse_target("just-a-name")
        assert repo == "just-a-name"
        assert target == ""


# ---------------------------------------------------------------------------
# _matches_target tests
# ---------------------------------------------------------------------------


class TestMatchesTarget:
    def test_exact_symbol_match(self):
        assert _matches_target("connect_db", "connect_db", "src/db.py")

    def test_exact_file_match(self):
        assert _matches_target("src/db.py", "connect_db", "src/db.py")

    def test_partial_symbol_match(self):
        assert _matches_target("connect", "connect_db", "src/db.py")

    def test_file_and_symbol_match(self):
        assert _matches_target("src/db.py::connect_db", "connect_db", "src/db.py")

    def test_no_match(self):
        assert not _matches_target("nonexistent", "connect_db", "src/db.py")

    def test_empty_target_returns_false(self):
        assert not _matches_target("", "connect_db", "src/db.py")


# ---------------------------------------------------------------------------
# _find_callers tests
# ---------------------------------------------------------------------------


class TestFindCallers:
    def test_finds_callers(self, code_index_fixture):
        call_graph = code_index_fixture["call_graph"]
        callers = _find_callers("src/api.py::handle_request", call_graph)
        assert "src/db.py::connect_db" in callers

    def test_no_callers(self, code_index_fixture):
        call_graph = code_index_fixture["call_graph"]
        callers = _find_callers("src/db.py::connect_db", call_graph)
        assert callers == []

    def test_nonexistent_target(self, code_index_fixture):
        call_graph = code_index_fixture["call_graph"]
        callers = _find_callers("nonexistent::func", call_graph)
        assert callers == []


# ---------------------------------------------------------------------------
# load_code_index tests
# ---------------------------------------------------------------------------


class TestLoadCodeIndex:
    @pytest.mark.asyncio
    async def test_loads_fixture(self, mock_s3_client, code_index_fixture):
        result = await load_code_index(
            "org/fixture-repo",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert result["repo_id"] == "org/fixture-repo"
        assert len(result["definitions"]) == 3

    @pytest.mark.asyncio
    async def test_missing_key_returns_empty(self):
        client = MagicMock()
        client.exceptions = MagicMock()
        no_such_key = type("NoSuchKey", (Exception,), {})
        client.exceptions.NoSuchKey = no_such_key
        client.get_object.side_effect = no_such_key()
        result = await load_code_index("org/missing", s3_client=client, bucket="test", prefix="pfx")
        assert result == {}


# ---------------------------------------------------------------------------
# understand verb tests
# ---------------------------------------------------------------------------


class TestUnderstand:
    @pytest.mark.asyncio
    async def test_understand_symbol(self, mock_s3_client):
        # Short-name format: "repo::symbol"
        hits = await understand(
            "fixture-repo::connect_db",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert len(hits) > 0
        assert hits[0].data["symbol"] == "connect_db"
        assert hits[0].data["file"] == "src/db.py"
        assert hits[0].data["kind"] == "function"

    @pytest.mark.asyncio
    async def test_understand_file(self, mock_s3_client):
        # Short-name format: "repo/path"
        hits = await understand(
            "fixture-repo/src/db.py",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert len(hits) > 0
        # Should find the connect_db function in src/db.py
        symbols = [h.data["symbol"] for h in hits]
        assert "connect_db" in symbols

    @pytest.mark.asyncio
    async def test_understand_empty_target(self, mock_s3_client):
        hits = await understand(
            "",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert hits == []

    @pytest.mark.asyncio
    async def test_understand_repo_level_target(self, mock_s3_client):
        """FIX #1535: understand("repo-name") should return definitions, not empty."""
        hits = await understand(
            "fixture-repo",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        # With Neptune disabled, falls back to code-index; repo-level returns all defs
        assert len(hits) > 0
        # All hits should have source = "code-index-fallback"
        for hit in hits:
            assert hit.data.get("source") == "code-index-fallback"
            assert hit.data.get("repo_id") == "fixture-repo"


# ---------------------------------------------------------------------------
# impact verb tests
# ---------------------------------------------------------------------------


class TestImpact:
    @pytest.mark.asyncio
    async def test_impact_symbol_with_callers(self, mock_s3_client):
        hits = await impact(
            "fixture-repo::handle_request",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        # handle_request is called by connect_db
        assert len(hits) > 0
        callers = [h.data.get("symbol", "") for h in hits]
        assert "connect_db" in callers

    @pytest.mark.asyncio
    async def test_impact_leaf_symbol(self, mock_s3_client):
        # connect_db has no callers in the fixture
        hits = await impact(
            "fixture-repo::connect_db",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert hits == []

    @pytest.mark.asyncio
    async def test_impact_empty_target(self, mock_s3_client):
        hits = await impact(
            "",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert hits == []


# ---------------------------------------------------------------------------
# Neptune target-parsing integration tests (Bug #1635)
# ---------------------------------------------------------------------------


class TestImpactNeptuneTargetParsing:
    """Verify that org/repo::symbol targets reach Neptune with correct arguments.

    Bug #1635: _parse_target mis-splits org/repo::symbol, passing the repo name
    into query_target as "repo::symbol", which _impact_neptune_inner then splits
    into file_part="repo", symbol_name="symbol" — causing a 0-result Neptune
    query and silent S3 fallback.
    """

    @pytest.mark.asyncio
    async def test_org_repo_symbol_calls_neptune_with_empty_file(self):
        """impact("colbymchenry/codegraph::length") must call query_impact with file=""."""
        from unittest.mock import patch

        from door.structural_backend import _impact_via_neptune

        caller_records = [
            {
                "caller_repo": "colbymchenry/codegraph",
                "caller_file": "src/graph.ts",
                "caller_name": "buildGraph",
                "caller_kind": "function",
                "distance": 1,
            }
        ]

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="colbymchenry/codegraph"),
            patch("door.neptune_client.query_impact", return_value=caller_records) as mock_qi,
        ):
            results = await _impact_via_neptune("colbymchenry/codegraph", "length")

        # Must call query_impact with file="" (bare symbol), NOT file="codegraph"
        mock_qi.assert_called_once_with("colbymchenry/codegraph", "", "length")
        assert results is not None
        assert len(results) == 1
        assert results[0].data["source"] == "neptune"

    @pytest.mark.asyncio
    async def test_short_repo_symbol_calls_neptune_with_empty_file(self):
        """impact("codegraph::length") resolves repo and queries with file=""."""
        from unittest.mock import patch

        from door.structural_backend import _impact_via_neptune

        caller_records = [
            {
                "caller_repo": "colbymchenry/codegraph",
                "caller_file": "src/graph.ts",
                "caller_name": "buildGraph",
                "caller_kind": "function",
                "distance": 1,
            }
        ]

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch(
                "door.neptune_client.resolve_repo_name",
                return_value="colbymchenry/codegraph",
            ) as mock_resolve,
            patch("door.neptune_client.query_impact", return_value=caller_records) as mock_qi,
        ):
            results = await _impact_via_neptune("codegraph", "length")

        # resolve_repo_name should be called with the short name
        mock_resolve.assert_called_once_with("codegraph")
        # Must call query_impact with file="" (bare symbol)
        mock_qi.assert_called_once_with("colbymchenry/codegraph", "", "length")
        assert results is not None
        assert len(results) == 1
        assert results[0].data["source"] == "neptune"

    @pytest.mark.asyncio
    async def test_org_repo_file_symbol_passes_file_correctly(self):
        """impact with query_target="src/api.py::handler" must pass file="src/api.py"."""
        from unittest.mock import patch

        from door.structural_backend import _impact_via_neptune

        caller_records = [
            {
                "caller_repo": "org/repo",
                "caller_file": "src/routes.py",
                "caller_name": "router",
                "caller_kind": "function",
                "distance": 1,
            }
        ]

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="org/repo"),
            patch("door.neptune_client.query_impact", return_value=caller_records) as mock_qi,
        ):
            results = await _impact_via_neptune("org/repo", "src/api.py::handler")

        # Must call query_impact with the actual file path
        mock_qi.assert_called_once_with("org/repo", "src/api.py", "handler")
        assert results is not None
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_no_silent_fallback_for_known_symbol(self):
        """End-to-end: impact("colbymchenry/codegraph::length") returns neptune source."""
        from unittest.mock import patch

        caller_records = [
            {
                "caller_repo": "colbymchenry/codegraph",
                "caller_file": "src/graph.ts",
                "caller_name": "buildGraph",
                "caller_kind": "function",
                "distance": 1,
            },
            {
                "caller_repo": "colbymchenry/codegraph",
                "caller_file": "src/mcp/tools.ts",
                "caller_name": "handleImpact",
                "caller_kind": "function",
                "distance": 2,
            },
        ]

        mock_s3 = MagicMock()

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="colbymchenry/codegraph"),
            patch("door.neptune_client.query_impact", return_value=caller_records),
        ):
            hits = await impact(
                "colbymchenry/codegraph::length",
                s3_client=mock_s3,
                bucket="test-bucket",
                prefix="content/code-indexes",
            )

        # Must NOT fall back to S3 — Neptune has the data
        assert len(hits) == 2
        for hit in hits:
            assert hit.data["source"] == "neptune"
        # S3 should never have been touched
        mock_s3.get_object.assert_not_called()


class TestUnderstandNeptuneTargetParsing:
    """Verify that org/repo::symbol targets reach Neptune correctly for understand."""

    @pytest.mark.asyncio
    async def test_org_repo_symbol_calls_neptune_with_empty_file(self):
        """understand("addyosmani/agent-skills::fs") must query with file=""."""
        from unittest.mock import patch

        from door.structural_backend import _understand_via_neptune

        understand_records = [
            {
                "symbol_name": "fs",
                "symbol_kind": "module",
                "symbol_file": "commands/fs.toml",
                "signature": "",
                "callees": [],
                "callers": [],
                "parents": [],
                "owners": [],
            }
        ]

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="addyosmani/agent-skills"),
            patch(
                "door.neptune_client.query_understand", return_value=understand_records
            ) as mock_qu,
        ):
            results = await _understand_via_neptune("addyosmani/agent-skills", "fs", "overview")

        # Must call query_understand with file="" (bare symbol), NOT file="agent-skills"
        mock_qu.assert_called_once_with("addyosmani/agent-skills", "", "fs")
        assert results is not None
        assert len(results) == 1
        assert results[0].data["source"] == "neptune"

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

    def test_symbol_reference_with_path(self):
        # "org/repo::symbol" — first component is repo, rest includes ::
        repo, target = _parse_target("org/repo::connect_db")
        assert repo == "org"
        assert target == "repo::connect_db"

    def test_file_path(self):
        # First component is repo_id, rest is the path
        repo, target = _parse_target("myrepo/src/db.py")
        assert repo == "myrepo"
        assert target == "src/db.py"

    def test_file_and_symbol(self):
        repo, target = _parse_target("myrepo/src/db.py::connect_db")
        assert repo == "myrepo"
        assert target == "src/db.py::connect_db"

    def test_domain_prefix(self):
        # Domain-like first component uses 3 parts as repo_id
        repo, target = _parse_target("github.com/org/repo/src/db.py")
        assert repo == "github.com/org/repo"
        assert target == "src/db.py"

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

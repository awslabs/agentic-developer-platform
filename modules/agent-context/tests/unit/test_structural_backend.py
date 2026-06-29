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
    _normalize_symbol,
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

    def test_org_repo_no_symbol_path_branch(self):
        """Bug #2405: 'HKUDS/Vibe-Trading' (no ::) must be treated as org/repo, not repo/path."""
        repo, target = _parse_target("HKUDS/Vibe-Trading")
        assert repo == "HKUDS/Vibe-Trading"
        assert target == ""

    def test_org_repo_no_symbol_path_branch_lowercase(self):
        """Bug #2405: org/repo detection works regardless of casing."""
        repo, target = _parse_target("addyosmani/agent-skills")
        assert repo == "addyosmani/agent-skills"
        assert target == ""

    def test_org_repo_with_file_path(self):
        """3+ components with dotted last: first component is repo (eval compat)."""
        repo, target = _parse_target("CopilotKit/packages/react-core/src/hooks.ts")
        assert repo == "CopilotKit"
        assert target == "packages/react-core/src/hooks.ts"

    def test_org_repo_symbol_bug_2405(self):
        """Bug #2405: 'HKUDS/Vibe-Trading::Artifact' must parse correctly."""
        repo, target = _parse_target("HKUDS/Vibe-Trading::Artifact")
        assert repo == "HKUDS/Vibe-Trading"
        assert target == "Artifact"


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

    def test_exact_class_name_match(self):
        """Bug #2405: Artifact class matches by exact symbol name."""
        assert _matches_target("Artifact", "Artifact", "agent/api_server.py")

    def test_case_insensitive_partial(self):
        """Partial match is case-insensitive."""
        assert _matches_target("artifact", "Artifact", "agent/api_server.py")


# ---------------------------------------------------------------------------
# _normalize_symbol tests
# ---------------------------------------------------------------------------


class TestNormalizeSymbol:
    def test_standard_fields(self):
        """Symbol field takes priority over name."""
        result = _normalize_symbol({"symbol": "Foo", "kind": "class", "file": "a.py", "line": 1})
        assert result == {
            "symbol": "Foo",
            "kind": "class",
            "file": "a.py",
            "line": 1,
            "signature": "",
        }

    def test_name_type_fallback(self):
        """Falls back to name/type fields from code-index JSON."""
        result = _normalize_symbol({"name": "Bar", "type": "function", "file": "b.py", "line": 5})
        assert result == {
            "symbol": "Bar",
            "kind": "function",
            "file": "b.py",
            "line": 5,
            "signature": "",
        }

    def test_whitespace_stripped(self):
        """Bug #2405: whitespace in symbol names is stripped."""
        result = _normalize_symbol(
            {
                "name": "  Artifact ",
                "type": " class",
                "file": " agent/api_server.py ",
                "line": 61,
                "signature": " def foo() ",
            }
        )
        assert result["symbol"] == "Artifact"
        assert result["kind"] == "class"
        assert result["file"] == "agent/api_server.py"
        assert result["signature"] == "def foo()"


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

    @pytest.mark.asyncio
    async def test_understand_org_repo_overview(self, mock_s3_client):
        """Bug #2405: understand('org/fixture-repo') returns overview (repo-level)."""
        hits = await understand(
            "org/fixture-repo",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        # org/fixture-repo has no dot in last component → treated as org/repo (repo-level)
        assert len(hits) > 0
        for hit in hits:
            assert hit.data.get("source") == "code-index-fallback"

    @pytest.mark.asyncio
    async def test_understand_org_repo_symbol(self, mock_s3_client):
        """Bug #2405: understand('org/fixture-repo::connect_db') finds the symbol."""
        hits = await understand(
            "org/fixture-repo::connect_db",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert len(hits) > 0
        assert hits[0].data["symbol"] == "connect_db"
        assert hits[0].data["file"] == "src/db.py"

    @pytest.mark.asyncio
    async def test_understand_org_repo_symbol_not_found(self, mock_s3_client):
        """Bug #2405: non-existent symbol returns empty (not crash)."""
        hits = await understand(
            "org/fixture-repo::nonexistent_thing",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert hits == []


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

    @pytest.mark.asyncio
    async def test_impact_org_repo_symbol(self, mock_s3_client):
        """Bug #2405: impact('org/fixture-repo::handle_request') finds callers."""
        hits = await impact(
            "org/fixture-repo::handle_request",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="content/code-indexes",
        )
        assert len(hits) > 0
        callers = [h.data.get("symbol", "") for h in hits]
        assert "connect_db" in callers


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

    @pytest.mark.asyncio
    async def test_bare_symbol_end_to_end_understand(self):
        """Bug #1698: understand("aws-e/adp::event_type") must return neptune results.

        Full end-to-end: _parse_target strips :: → bare symbol "event_type" →
        _understand_neptune_inner bare-symbol branch → query_understand(repo, "", symbol).
        """
        from unittest.mock import patch

        understand_records = [
            {
                "symbol_name": "event_type",
                "symbol_kind": "function",
                "symbol_file": "src/models/events.py",
                "signature": "def event_type(self) -> str",
                "callees": [{"name": "validate", "file": "src/validators.py"}],
                "callers": [{"name": "process_event", "file": "src/handlers.py"}],
                "parents": [],
                "owners": [],
            }
        ]

        mock_s3 = MagicMock()

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="aws-e/adp"),
            patch(
                "door.neptune_client.query_understand", return_value=understand_records
            ) as mock_qu,
        ):
            hits = await understand(
                "aws-e/adp::event_type",
                s3_client=mock_s3,
                bucket="test-bucket",
                prefix="content/code-indexes",
            )

        # Must call query_understand with file="" (bare symbol after :: stripped by _parse_target)
        mock_qu.assert_called_once_with("aws-e/adp", "", "event_type")
        # Must return non-empty results from Neptune, NOT fall back to S3
        assert len(hits) == 1
        assert hits[0].data["source"] == "neptune"
        assert hits[0].data["symbol"] == "event_type"
        assert hits[0].data["file"] == "src/models/events.py"
        # S3 should never be touched
        mock_s3.get_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_bare_symbol_resolve_fallback_understand(self):
        """Bug #1698: bare symbol uses resolve_symbol when query_understand returns empty."""
        from unittest.mock import patch

        from door.structural_backend import _understand_via_neptune

        resolved_records = [
            {
                "symbol_name": "event_type",
                "symbol_kind": "function",
                "symbol_file": "src/models/events.py",
                "signature": "def event_type(self) -> str",
                "callees": [],
                "callers": [],
                "parents": [],
                "owners": [],
            }
        ]

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="aws-e/adp"),
            patch("door.neptune_client.query_understand", side_effect=[[], resolved_records]),
            patch(
                "door.neptune_client.resolve_symbol",
                return_value=[{"file": "src/models/events.py", "name": "event_type"}],
            ) as mock_resolve,
        ):
            results = await _understand_via_neptune("aws-e/adp", "event_type", "overview")

        # When query_understand with file="" returns nothing, resolve_symbol is tried
        mock_resolve.assert_called_once_with("aws-e/adp", "event_type")
        assert results is not None
        assert len(results) == 1
        assert results[0].data["source"] == "neptune"

    @pytest.mark.asyncio
    async def test_bare_symbol_end_to_end_impact(self):
        """Bug #1698: impact("aws-e/adp::event_type") must return neptune results.

        Full end-to-end: _parse_target strips :: → bare symbol "event_type" →
        _impact_neptune_inner bare-symbol branch → query_impact(repo, "", symbol).
        """
        from unittest.mock import patch

        caller_records = [
            {
                "caller_repo": "aws-e/adp",
                "caller_file": "src/handlers.py",
                "caller_name": "process_event",
                "caller_kind": "function",
                "distance": 1,
            },
            {
                "caller_repo": "aws-e/adp",
                "caller_file": "src/api/routes.py",
                "caller_name": "handle_webhook",
                "caller_kind": "function",
                "distance": 2,
            },
        ]

        mock_s3 = MagicMock()

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="aws-e/adp"),
            patch("door.neptune_client.query_impact", return_value=caller_records) as mock_qi,
        ):
            hits = await impact(
                "aws-e/adp::event_type",
                s3_client=mock_s3,
                bucket="test-bucket",
                prefix="content/code-indexes",
            )

        # Must call query_impact with file="" (bare symbol)
        mock_qi.assert_called_once_with("aws-e/adp", "", "event_type")
        # Must return non-empty results from Neptune
        assert len(hits) == 2
        for hit in hits:
            assert hit.data["source"] == "neptune"
        assert hits[0].data["symbol"] == "process_event"
        assert hits[1].data["symbol"] == "handle_webhook"
        # S3 should never be touched
        mock_s3.get_object.assert_not_called()

"""Unit tests for Bug #1611 fix — Neptune collect({map}) crash.

These tests verify:
1. query_understand() uses WITH-chaining + collect(node) Cypher (no inline map
   literals in aggregate), and projects node properties to dicts in Python.
2. query_understand() raises NeptuneQueryError on failure (not silent return []).
3. query_impact() raises NeptuneQueryError on failure (not silent return []).
4. _understand_via_neptune catches NeptuneQueryError and returns None (S3 fallback).
5. _impact_via_neptune catches NeptuneQueryError and returns None (S3 fallback).
6. _nodes_to_dicts correctly projects node objects to dicts and filters None.
7. The Cypher in query_understand does NOT contain 'collect(DISTINCT {' pattern.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _nodes_to_dicts tests
# ---------------------------------------------------------------------------


class TestNodesToDicts:
    """Tests for the node-to-dict projection helper."""

    def test_projects_node_properties(self):
        """Nodes with .get() should be projected to dicts with specified keys."""
        from door.neptune_client import _nodes_to_dicts

        # Simulate neo4j Node objects (they support .get())
        node1 = MagicMock()
        node1.get = lambda k: {"name": "foo", "file": "a.py", "kind": "function"}.get(k)
        node2 = MagicMock()
        node2.get = lambda k: {"name": "bar", "file": "b.py", "kind": "class"}.get(k)

        result = _nodes_to_dicts([node1, node2], ["name", "file", "kind"])

        assert len(result) == 2
        assert result[0] == {"name": "foo", "file": "a.py", "kind": "function"}
        assert result[1] == {"name": "bar", "file": "b.py", "kind": "class"}

    def test_filters_none_nodes(self):
        """None entries (from unmatched OPTIONAL MATCH) should be filtered out."""
        from door.neptune_client import _nodes_to_dicts

        node1 = MagicMock()
        node1.get = lambda k: {"name": "foo", "file": "a.py"}.get(k)

        result = _nodes_to_dicts([node1, None, None], ["name", "file"])

        assert len(result) == 1
        assert result[0] == {"name": "foo", "file": "a.py"}

    def test_empty_list(self):
        """Empty input returns empty output."""
        from door.neptune_client import _nodes_to_dicts

        assert _nodes_to_dicts([], ["name", "file"]) == []

    def test_subset_of_keys(self):
        """Only requested keys are projected."""
        from door.neptune_client import _nodes_to_dicts

        node = MagicMock()
        node.get = lambda k: {"name": "x", "file": "y.py", "kind": "method", "line": 42}.get(k)

        result = _nodes_to_dicts([node], ["name", "file"])

        assert result == [{"name": "x", "file": "y.py"}]
        assert "kind" not in result[0]
        assert "line" not in result[0]


# ---------------------------------------------------------------------------
# query_understand — Cypher shape tests (Bug #1611)
# ---------------------------------------------------------------------------


class TestQueryUnderstandCypherShape:
    """Verify query_understand uses Neptune-compatible Cypher (no inline map in collect)."""

    def _setup_mock_driver(self):
        """Create a mock driver that returns empty results."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_cypher_does_not_use_inline_map_in_collect(self):
        """The Cypher MUST NOT contain collect(DISTINCT {... inline map ...})."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "src/main.py", "connect")

        cypher = mock_session.run.call_args[0][0]
        # The old broken pattern
        assert "collect(DISTINCT {" not in cypher
        assert "collect(DISTINCT{" not in cypher

    def test_cypher_uses_with_chaining(self):
        """The Cypher should use WITH to separate OPTIONAL MATCH + collect patterns."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "src/main.py", "connect")

        cypher = mock_session.run.call_args[0][0]
        # Must contain WITH clauses for intermediate collect results
        assert "WITH s, collect(DISTINCT callee) AS callee_nodes" in cypher
        assert "WITH s, callee_nodes, collect(DISTINCT caller) AS caller_nodes" in cypher

    def test_cypher_collects_node_references(self):
        """The Cypher should collect node variables, not map literals."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "", "connect")  # empty file variant

        cypher = mock_session.run.call_args[0][0]
        assert "collect(DISTINCT callee)" in cypher
        assert "collect(DISTINCT caller)" in cypher
        assert "collect(DISTINCT parent)" in cypher
        assert "collect(DISTINCT owner)" in cypher

    def test_returns_projected_dicts_with_expected_keys(self):
        """Results should have callees/callers/parents/owners as list of dicts."""
        mock_driver = MagicMock()
        mock_session = MagicMock()

        # Simulate Neptune returning collected Node objects
        callee_node = MagicMock()
        callee_node.get = lambda k: {"name": "helper", "file": "util.py", "kind": "function"}.get(k)
        caller_node = MagicMock()
        caller_node.get = lambda k: {"name": "main", "file": "app.py", "kind": "function"}.get(k)
        parent_node = MagicMock()
        parent_node.get = lambda k: {"name": "Base", "file": "base.py"}.get(k)
        owner_node = MagicMock()
        owner_node.get = lambda k: {"name": "Service", "file": "svc.py"}.get(k)

        mock_record = MagicMock()
        mock_record.__iter__ = lambda s: iter(
            [
                "symbol_name",
                "symbol_kind",
                "symbol_file",
                "signature",
                "callee_nodes",
                "caller_nodes",
                "parent_nodes",
                "owner_nodes",
            ]
        )
        mock_record.__getitem__ = lambda s, k: {
            "symbol_name": "connect",
            "symbol_kind": "function",
            "symbol_file": "src/db.py",
            "signature": "def connect()",
            "callee_nodes": [callee_node],
            "caller_nodes": [caller_node],
            "parent_nodes": [parent_node],
            "owner_nodes": [owner_node],
        }[k]

        # Make dict(record) work by implementing keys/items
        record_data = {
            "symbol_name": "connect",
            "symbol_kind": "function",
            "symbol_file": "src/db.py",
            "signature": "def connect()",
            "callee_nodes": [callee_node],
            "caller_nodes": [caller_node],
            "parent_nodes": [parent_node],
            "owner_nodes": [owner_node],
        }

        class FakeRecord:
            def keys(self):
                return record_data.keys()

            def __getitem__(self, key):
                return record_data[key]

            def __iter__(self):
                return iter(record_data)

            def __len__(self):
                return len(record_data)

        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([FakeRecord()])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            result = query_understand("org/repo", "src/db.py", "connect")

        assert len(result) == 1
        rec = result[0]
        assert rec["symbol_name"] == "connect"
        assert rec["symbol_kind"] == "function"
        assert rec["callees"] == [{"name": "helper", "file": "util.py", "kind": "function"}]
        assert rec["callers"] == [{"name": "main", "file": "app.py", "kind": "function"}]
        assert rec["parents"] == [{"name": "Base", "file": "base.py"}]
        assert rec["owners"] == [{"name": "Service", "file": "svc.py"}]
        # The raw node keys should NOT be in the result
        assert "callee_nodes" not in rec
        assert "caller_nodes" not in rec


# ---------------------------------------------------------------------------
# NeptuneQueryError — error handling tests (Bug #1611)
# ---------------------------------------------------------------------------


class TestNeptuneQueryErrorHandling:
    """Verify queries raise NeptuneQueryError instead of silently returning []."""

    def test_query_understand_raises_on_error(self):
        """query_understand must raise NeptuneQueryError, not return []."""
        from door.neptune_client import NeptuneQueryError, query_understand

        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("Operation terminated (internal error)")
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            with pytest.raises(NeptuneQueryError):
                query_understand("org/repo", "src/db.py", "connect")

    def test_query_impact_raises_on_error(self):
        """query_impact must raise NeptuneQueryError, not return []."""
        from door.neptune_client import NeptuneQueryError, query_impact

        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("Neptune timeout")
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            with pytest.raises(NeptuneQueryError):
                query_impact("org/repo", "src/api.py", "handle_request")


# ---------------------------------------------------------------------------
# Structural backend — NeptuneQueryError fallback tests (Bug #1611)
# ---------------------------------------------------------------------------


class TestNeptuneQueryErrorFallback:
    """Verify structural_backend catches NeptuneQueryError and falls back to S3."""

    @pytest.mark.asyncio
    async def test_understand_falls_back_on_query_error(self):
        """_understand_via_neptune returns None (triggers S3) on NeptuneQueryError."""
        from door.neptune_client import NeptuneQueryError
        from door.structural_backend import _understand_via_neptune

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="org/repo"),
            patch(
                "door.neptune_client.query_understand",
                side_effect=NeptuneQueryError("crash"),
            ),
        ):
            result = await _understand_via_neptune("org/repo", "connect", "detailed")

        # Should return None (trigger S3 fallback), not crash
        assert result is None

    @pytest.mark.asyncio
    async def test_impact_falls_back_on_query_error(self):
        """_impact_via_neptune returns None (triggers S3) on NeptuneQueryError."""
        from door.neptune_client import NeptuneQueryError
        from door.structural_backend import _impact_via_neptune

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="org/repo"),
            patch(
                "door.neptune_client.query_impact",
                side_effect=NeptuneQueryError("crash"),
            ),
        ):
            result = await _impact_via_neptune("org/repo", "main")

        # Should return None (trigger S3 fallback), not crash
        assert result is None

    @pytest.mark.asyncio
    async def test_understand_still_works_on_success(self):
        """_understand_via_neptune still returns results when query succeeds."""
        from door.structural_backend import _understand_via_neptune

        understand_result = [
            {
                "symbol_name": "connect",
                "symbol_kind": "function",
                "symbol_file": "src/db.py",
                "signature": "def connect()",
                "callees": [{"name": "open", "file": "net.py", "kind": "function"}],
                "callers": [],
                "parents": [],
                "owners": [],
            }
        ]

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="org/repo"),
            patch("door.neptune_client.query_understand", return_value=understand_result),
        ):
            result = await _understand_via_neptune("org/repo", "connect", "detailed")

        # Should return results, not None
        assert result is not None
        assert len(result) == 1
        assert result[0].data["symbol"] == "connect"
        assert result[0].data["source"] == "neptune"

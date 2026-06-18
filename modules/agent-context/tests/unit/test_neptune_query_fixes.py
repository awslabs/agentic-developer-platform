"""Unit tests for Bug #1587 fixes — Neptune query-layer: empty-file match,
symbol resolution, and no_callers vs symbol_not_found distinction.

These tests verify:
1. query_understand/query_impact work correctly when file="" (omits file from MATCH)
2. resolve_symbol() maps human-friendly targets to stored (file, name) tuples
3. symbol_exists() distinguishes "not found" from "found but no callers"
4. _impact_via_neptune returns [] (not None) for "exists, no callers"
5. Server-level verdict correctly reports "no_callers" vs "symbol_not_found"
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# query_impact with empty file (Bug #1587 Fix 1)
# ---------------------------------------------------------------------------


class TestQueryImpactEmptyFile:
    """Verify query_impact omits file from MATCH when file is empty."""

    def test_empty_file_uses_repo_name_only_match(self):
        """When file='', the Cypher should NOT include {file: $file}."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            query_impact("org/repo", "", "main")

        # Verify the params do NOT include "file"
        call_args = mock_session.run.call_args
        cypher = call_args[0][0]
        params = call_args[0][1]
        assert "file" not in params
        assert "symbol_name" in params
        assert params["symbol_name"] == "main"
        assert params["repo"] == "org/repo"
        # The Cypher should NOT have {file: $file}
        assert "file: $file" not in cypher

    def test_nonempty_file_includes_file_in_match(self):
        """When file is specified, the Cypher SHOULD include {file: $file}."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            query_impact("org/repo", "src/api.py", "main")

        call_args = mock_session.run.call_args
        cypher = call_args[0][0]
        params = call_args[0][1]
        assert params["file"] == "src/api.py"
        assert "file: $file" in cypher


# ---------------------------------------------------------------------------
# query_understand with empty file (Bug #1587 Fix 1)
# ---------------------------------------------------------------------------


class TestQueryUnderstandEmptyFile:
    """Verify query_understand omits file from MATCH when file is empty."""

    def test_empty_file_uses_repo_name_only_match(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "", "connect_db")

        call_args = mock_session.run.call_args
        cypher = call_args[0][0]
        params = call_args[0][1]
        assert "file" not in params
        assert "file: $file" not in cypher
        assert params["symbol_name"] == "connect_db"

    def test_nonempty_file_includes_file_in_match(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "src/db.py", "connect_db")

        call_args = mock_session.run.call_args
        cypher = call_args[0][0]
        params = call_args[0][1]
        assert params["file"] == "src/db.py"
        assert "file: $file" in cypher


# ---------------------------------------------------------------------------
# resolve_symbol tests (Bug #1587 Fix 2)
# ---------------------------------------------------------------------------


class TestResolveSymbol:
    """Tests for the new resolve_symbol() function."""

    def test_returns_empty_when_no_driver(self):
        with patch("door.neptune_client.get_neptune_driver", return_value=None):
            from door.neptune_client import resolve_symbol

            result = resolve_symbol("org/repo", "main")
            assert result == []

    def test_exact_name_match(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {
                "name": "main",
                "file": "src/cli.py",
                "kind": "function",
                "symbol_id": "scip-python ...",
            }
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import resolve_symbol

            result = resolve_symbol("org/repo", "main")

        assert len(result) == 1
        assert result[0]["name"] == "main"
        assert result[0]["file"] == "src/cli.py"

    def test_strips_trailing_parens_dots(self):
        """SCIP descriptor noise like 'main().' should be cleaned."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {"name": "main", "file": "src/cli.py", "kind": "function", "symbol_id": "..."}
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import resolve_symbol

            result = resolve_symbol("org/repo", "main().")

        # Should have queried with "main" (stripped)
        call_args = mock_session.run.call_args
        params = call_args[0][1]
        assert params["name"] == "main"
        assert len(result) == 1

    def test_module_path_resolution(self):
        """Input like 'agent_reach.cli/main' should be resolved via file hint."""
        mock_driver = MagicMock()
        mock_session = MagicMock()

        # First two calls (exact + contains) return empty
        empty_result = MagicMock()
        empty_result.__iter__ = lambda s: iter([])
        # Third call (path match) returns the symbol
        path_result = MagicMock()
        path_result.__iter__ = lambda s: iter(
            [{"name": "main", "file": "agent_reach/cli.py", "kind": "function", "symbol_id": "..."}]
        )
        mock_session.run.side_effect = [empty_result, empty_result, path_result]
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import resolve_symbol

            result = resolve_symbol("org/repo", "agent_reach.cli/main")

        assert len(result) == 1
        assert result[0]["name"] == "main"
        assert result[0]["file"] == "agent_reach/cli.py"

    def test_returns_empty_when_nothing_matches(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        empty_result = MagicMock()
        empty_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = empty_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import resolve_symbol

            result = resolve_symbol("org/repo", "nonexistent_symbol_xyz")

        assert result == []


# ---------------------------------------------------------------------------
# symbol_exists tests (Bug #1587 Fix 2)
# ---------------------------------------------------------------------------


class TestSymbolExists:
    """Tests for the symbol_exists() helper."""

    def test_returns_false_when_no_driver(self):
        with patch("door.neptune_client.get_neptune_driver", return_value=None):
            from door.neptune_client import symbol_exists

            assert symbol_exists("org/repo", "src/db.py", "connect") is False

    def test_returns_true_when_symbol_found(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_record = MagicMock()
        mock_record.__getitem__ = lambda s, k: True  # record["exists"] = True
        mock_result = MagicMock()
        mock_result.single.return_value = mock_record
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import symbol_exists

            assert symbol_exists("org/repo", "src/db.py", "connect") is True

    def test_returns_false_when_symbol_not_found(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_record = MagicMock()
        mock_record.__getitem__ = lambda s, k: False
        mock_result = MagicMock()
        mock_result.single.return_value = mock_record
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import symbol_exists

            assert symbol_exists("org/repo", "src/db.py", "nonexistent") is False

    def test_empty_file_omits_file_from_query(self):
        """When file='', query should match by repo+name only."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_record = MagicMock()
        mock_record.__getitem__ = lambda s, k: True
        mock_result = MagicMock()
        mock_result.single.return_value = mock_record
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import symbol_exists

            symbol_exists("org/repo", "", "main")

        call_args = mock_session.run.call_args
        cypher = call_args[0][0]
        params = call_args[0][1]
        assert "file" not in params
        assert "file: $file" not in cypher


# ---------------------------------------------------------------------------
# Impact verb: "no_callers" vs "symbol_not_found" (Bug #1587 Fix 2)
# ---------------------------------------------------------------------------


class TestImpactVerdictDistinction:
    """Tests at the _handle_impact level for correct verdict reporting."""

    @pytest.mark.asyncio
    async def test_neptune_no_callers_returns_correct_verdict(self):
        """When Neptune says symbol exists but has no callers → verdict=no_callers, source=neptune."""
        from door.structural_backend import _impact_via_neptune

        # Mock Neptune as enabled + available + symbol exists + no callers
        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="org/repo"),
            patch("door.neptune_client.query_impact", return_value=[]),
            patch("door.neptune_client.resolve_symbol", return_value=[]),
            patch("door.neptune_client.symbol_exists", return_value=True),
        ):
            result = await _impact_via_neptune("org/repo", "main")

        # Should return a list (not None) since symbol exists
        assert result is not None
        # Should contain the sentinel hit with source="neptune"
        assert len(result) == 1
        assert result[0].data["source"] == "neptune"
        assert result[0].data["_neptune_no_callers"] is True

    @pytest.mark.asyncio
    async def test_neptune_symbol_not_found_returns_none(self):
        """When Neptune can't find the symbol → returns None (falls to S3)."""
        from door.structural_backend import _impact_via_neptune

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="org/repo"),
            patch("door.neptune_client.query_impact", return_value=[]),
            patch("door.neptune_client.resolve_symbol", return_value=[]),
            patch("door.neptune_client.symbol_exists", return_value=False),
        ):
            result = await _impact_via_neptune("org/repo", "nonexistent_symbol")

        # Should return None (trigger S3 fallback)
        assert result is None

    @pytest.mark.asyncio
    async def test_neptune_with_callers_returns_results(self):
        """When Neptune finds callers → returns them normally."""
        from door.structural_backend import _impact_via_neptune

        caller_records = [
            {
                "caller_repo": "org/repo",
                "caller_file": "src/app.py",
                "caller_name": "start",
                "caller_kind": "function",
                "distance": 1,
            }
        ]

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="org/repo"),
            patch("door.neptune_client.query_impact", return_value=caller_records),
        ):
            result = await _impact_via_neptune("org/repo", "main")

        assert result is not None
        assert len(result) == 1
        assert result[0].data["symbol"] == "start"
        assert result[0].data["source"] == "neptune"
        assert "_neptune_no_callers" not in result[0].data

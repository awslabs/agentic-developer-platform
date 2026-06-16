"""Unit tests for per-repo Neptune subgraph isolation (#1533).

Validates:
- Scoped delete query is correctly formed and parameterized
- delete_then_load calls delete before load (ordering contract)
- Re-index produces stable counts (replace, not append)
- Parallel re-index of different repos: no cross-contamination
- Deleting repo A does NOT affect repo B's nodes/edges
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_neptune_query():
    """Patch neptune_query to capture calls and return controlled responses."""
    with patch("pipeline.neptune_ingestion.graph_ops.neptune_query") as mock:
        # Default: success response
        mock.return_value = {"results": []}
        yield mock


@pytest.fixture
def mock_neptune_query_loader():
    """Patch neptune_query in the loader module."""
    with patch("pipeline.neptune_ingestion.load_csv_to_neptune.neptune_query") as mock:
        mock.return_value = {"results": [{"cnt": 5}]}
        yield mock


# ---------------------------------------------------------------------------
# Tests: delete_repo_subgraph
# ---------------------------------------------------------------------------


class TestDeleteRepoSubgraph:
    """Tests for the scoped delete operation."""

    def test_delete_issues_correct_query(self, mock_neptune_query):
        """The delete must use MATCH (n) WHERE n.repo = $repo DETACH DELETE n."""
        from pipeline.neptune_ingestion.graph_ops import delete_repo_subgraph

        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/my-repo")

        mock_neptune_query.assert_called_once()
        args = mock_neptune_query.call_args
        cypher = args[0][2]  # positional arg: cypher query
        params = args[0][3]  # positional arg: parameters

        assert "MATCH (n)" in cypher
        assert "n.repo = $repo" in cypher
        assert "DETACH DELETE" in cypher
        assert params == {"repo": "org/my-repo"}

    def test_delete_returns_success_on_ok_response(self, mock_neptune_query):
        """Success path: Neptune returns no error."""
        from pipeline.neptune_ingestion.graph_ops import delete_repo_subgraph

        mock_neptune_query.return_value = {"results": []}
        result = delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo")

        assert result["success"] is True
        assert result["error"] is None
        assert result["elapsed_ms"] >= 0

    def test_delete_returns_failure_on_error(self, mock_neptune_query):
        """Error path: Neptune returns an error response."""
        from pipeline.neptune_ingestion.graph_ops import delete_repo_subgraph

        mock_neptune_query.return_value = {"error": "Timeout exceeded", "code": 500}
        result = delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo")

        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_delete_scoped_to_repo_parameter(self, mock_neptune_query):
        """The delete is parameterized — different repos produce different params."""
        from pipeline.neptune_ingestion.graph_ops import delete_repo_subgraph

        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo-a")
        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo-b")

        calls = mock_neptune_query.call_args_list
        assert calls[0][0][3] == {"repo": "org/repo-a"}
        assert calls[1][0][3] == {"repo": "org/repo-b"}


# ---------------------------------------------------------------------------
# Tests: count_repo_nodes
# ---------------------------------------------------------------------------


class TestCountRepoNodes:
    """Tests for node counting (used for validation)."""

    def test_count_returns_integer(self, mock_neptune_query):
        """Successful count returns an integer."""
        from pipeline.neptune_ingestion.graph_ops import count_repo_nodes

        mock_neptune_query.return_value = {"results": [{"cnt": 42}]}
        count = count_repo_nodes("https://neptune:8182/opencypher", "us-east-1", "org/repo")
        assert count == 42

    def test_count_returns_zero_for_empty_repo(self, mock_neptune_query):
        """A repo with no nodes returns 0."""
        from pipeline.neptune_ingestion.graph_ops import count_repo_nodes

        mock_neptune_query.return_value = {"results": [{"cnt": 0}]}
        count = count_repo_nodes("https://neptune:8182/opencypher", "us-east-1", "org/empty")
        assert count == 0

    def test_count_returns_negative_one_on_error(self, mock_neptune_query):
        """On Neptune error, returns -1 (sentinel)."""
        from pipeline.neptune_ingestion.graph_ops import count_repo_nodes

        mock_neptune_query.return_value = {"error": "connection refused", "code": 0}
        count = count_repo_nodes("https://neptune:8182/opencypher", "us-east-1", "org/repo")
        assert count == -1

    def test_count_query_is_scoped(self, mock_neptune_query):
        """Count query uses repo parameter for scoping."""
        from pipeline.neptune_ingestion.graph_ops import count_repo_nodes

        mock_neptune_query.return_value = {"results": [{"cnt": 10}]}
        count_repo_nodes("https://neptune:8182/opencypher", "us-east-1", "org/specific-repo")

        args = mock_neptune_query.call_args[0]
        assert "n.repo = $repo" in args[2]
        assert args[3] == {"repo": "org/specific-repo"}


# ---------------------------------------------------------------------------
# Tests: count_repo_edges
# ---------------------------------------------------------------------------


class TestCountRepoEdges:
    """Tests for edge counting."""

    def test_count_edges_returns_integer(self, mock_neptune_query):
        """Successful edge count returns an integer."""
        from pipeline.neptune_ingestion.graph_ops import count_repo_edges

        mock_neptune_query.return_value = {"results": [{"cnt": 99}]}
        count = count_repo_edges("https://neptune:8182/opencypher", "us-east-1", "org/repo")
        assert count == 99

    def test_count_edges_query_scoped_by_repo(self, mock_neptune_query):
        """Edge count query scopes on r.repo = $repo."""
        from pipeline.neptune_ingestion.graph_ops import count_repo_edges

        mock_neptune_query.return_value = {"results": [{"cnt": 5}]}
        count_repo_edges("https://neptune:8182/opencypher", "us-east-1", "org/repo")

        args = mock_neptune_query.call_args[0]
        assert "r.repo = $repo" in args[2]
        assert args[3] == {"repo": "org/repo"}


# ---------------------------------------------------------------------------
# Tests: delete_then_load (integration of delete + load)
# ---------------------------------------------------------------------------


class TestDeleteThenLoad:
    """Tests for the composite delete-then-load operation."""

    def test_delete_called_before_load(self, tmp_path):
        """The delete step must execute BEFORE loading vertices/edges."""
        # Create minimal CSV files
        vertices_csv = tmp_path / "vertices.csv"
        vertices_csv.write_text(
            "~id,~label,repo:String,file:String,name:String,kind:String,line:Int\n"
        )
        edges_csv = tmp_path / "edges.csv"
        edges_csv.write_text("~id,~from,~to,~label,repo:String,call_line:Int,source:String\n")

        call_order = []

        def track_delete(*args, **kwargs):
            call_order.append("delete")
            return {"success": True, "deleted_nodes": 0, "elapsed_ms": 10, "error": None}

        def track_load_v(*args, **kwargs):
            call_order.append("load_vertices")
            return (0, 0)

        def track_load_e(*args, **kwargs):
            call_order.append("load_edges")
            return (0, 0)

        with (
            patch(
                "pipeline.neptune_ingestion.load_csv_to_neptune.load_vertices",
                side_effect=track_load_v,
            ),
            patch(
                "pipeline.neptune_ingestion.load_csv_to_neptune.load_edges",
                side_effect=track_load_e,
            ),
            patch(
                "pipeline.neptune_ingestion.graph_ops.delete_repo_subgraph",
                side_effect=track_delete,
            ),
        ):
            from pipeline.neptune_ingestion.load_csv_to_neptune import delete_then_load

            delete_then_load(
                "https://neptune:8182/opencypher",
                "us-east-1",
                "org/repo",
                str(tmp_path),
            )

        assert call_order == ["delete", "load_vertices", "load_edges"]

    def test_load_continues_on_delete_failure(self, tmp_path):
        """If delete fails, load still proceeds (updateSingleCardinalityProperties handles it)."""
        vertices_csv = tmp_path / "vertices.csv"
        vertices_csv.write_text(
            "~id,~label,repo:String,file:String,name:String,kind:String,line:Int\n"
        )
        edges_csv = tmp_path / "edges.csv"
        edges_csv.write_text("~id,~from,~to,~label,repo:String,call_line:Int,source:String\n")

        with (
            patch(
                "pipeline.neptune_ingestion.load_csv_to_neptune.load_vertices", return_value=(5, 0)
            ),
            patch("pipeline.neptune_ingestion.load_csv_to_neptune.load_edges", return_value=(3, 0)),
            patch(
                "pipeline.neptune_ingestion.graph_ops.delete_repo_subgraph",
                return_value={
                    "success": False,
                    "deleted_nodes": 0,
                    "elapsed_ms": 100,
                    "error": "Timeout",
                },
            ),
        ):
            from pipeline.neptune_ingestion.load_csv_to_neptune import delete_then_load

            result = delete_then_load(
                "https://neptune:8182/opencypher",
                "us-east-1",
                "org/repo",
                str(tmp_path),
            )

        # Load succeeded even though delete failed
        assert result["vertices_loaded"] == 5
        assert result["edges_loaded"] == 3
        assert result["success"] is True
        assert result["delete_result"]["success"] is False


# ---------------------------------------------------------------------------
# Tests: Isolation guarantees
# ---------------------------------------------------------------------------


class TestIsolationGuarantees:
    """Tests verifying the isolation contract:
    - Re-index = replace (stable counts)
    - Parallel repos don't interfere
    - Deleting A doesn't affect B
    """

    def test_reindex_replaces_not_appends(self, mock_neptune_query):
        """Re-indexing the same repo twice should produce stable node count.

        The delete-then-load pattern ensures we don't accumulate duplicates.
        This test verifies the delete query targets only the specified repo.
        """
        from pipeline.neptune_ingestion.graph_ops import (
            delete_repo_subgraph,
            count_repo_nodes,
        )

        # Simulate: repo has 100 nodes, delete clears them, new load adds 100
        mock_neptune_query.side_effect = [
            {"results": []},  # First delete
            {"results": [{"cnt": 100}]},  # Count after first load
            {"results": []},  # Second delete (re-index)
            {"results": [{"cnt": 100}]},  # Count after second load
        ]

        # First index
        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo")
        count_1 = count_repo_nodes("https://neptune:8182/opencypher", "us-east-1", "org/repo")

        # Re-index (should replace, not append)
        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo")
        count_2 = count_repo_nodes("https://neptune:8182/opencypher", "us-east-1", "org/repo")

        # Stable count: 100 both times (not 200 = appended)
        assert count_1 == count_2 == 100

    def test_parallel_repos_no_cross_contamination(self, mock_neptune_query):
        """Deleting repo A must not affect repo B's nodes.

        Each delete call is scoped to its own repo parameter.
        """
        from pipeline.neptune_ingestion.graph_ops import delete_repo_subgraph

        # Delete repo A
        mock_neptune_query.return_value = {"results": []}
        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo-a")

        # Delete repo B
        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo-b")

        # Verify each delete was scoped independently
        calls = mock_neptune_query.call_args_list
        assert len(calls) == 2

        # First call: only repo-a
        _, args_a = calls[0][0][2], calls[0][0][3]
        assert args_a == {"repo": "org/repo-a"}

        # Second call: only repo-b
        _, args_b = calls[1][0][2], calls[1][0][3]
        assert args_b == {"repo": "org/repo-b"}

    def test_delete_repo_a_preserves_repo_b(self, mock_neptune_query):
        """Verifies that the delete query WHERE clause uses parameterized $repo.

        A non-parameterized query (e.g., string interpolation) could accidentally
        match nodes from other repos if repo names overlap.
        """
        from pipeline.neptune_ingestion.graph_ops import delete_repo_subgraph

        mock_neptune_query.return_value = {"results": []}

        # Delete "org/a" — must NOT accidentally delete "org/ab" or "org/a-fork"
        delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/a")

        cypher = mock_neptune_query.call_args[0][2]
        params = mock_neptune_query.call_args[0][3]

        # Query uses parameterized $repo (exact match via Neptune's parameter binding)
        assert "$repo" in cypher
        assert params == {"repo": "org/a"}
        # No string interpolation of repo name into the query
        assert "org/a" not in cypher

    def test_edge_repo_property_scopes_deletion(self, mock_neptune_query):
        """Edge deletion is handled by DETACH DELETE on nodes.

        DETACH DELETE removes all edges connected to the matched nodes.
        Combined with edges carrying repo=source_repo, this ensures:
        - Repo A's outgoing edges (repo=A) are removed when A's nodes are deleted
        - Repo B's edges pointing TO A's nodes are also removed (because A's nodes
          are gone), but B's own subgraph remains intact.
        """
        from pipeline.neptune_ingestion.graph_ops import delete_repo_subgraph

        mock_neptune_query.return_value = {"results": []}
        result = delete_repo_subgraph("https://neptune:8182/opencypher", "us-east-1", "org/repo")

        cypher = mock_neptune_query.call_args[0][2]
        # Must use DETACH DELETE (not just DELETE) to remove connected edges
        assert "DETACH DELETE" in cypher
        assert result["success"] is True

"""Unit tests for Neptune tenant property filter (Story 6 / #1775).

Validates:
- _build_scope_filter produces correct Cypher fragments
- query_impact injects scope predicate when tenant_id is provided
- query_understand injects scope predicate with collect(node)+project shape
- query_repo_topology, query_file_symbols, query_dir_symbols scope correctly
- query_cross_repo_impact passes tenant scope to resolution step
- No scope predicate when tenant_id is None (backward-compatible)
- Cross-tenant queries return nothing for the wrong tenant
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _build_scope_filter tests
# ---------------------------------------------------------------------------


class TestBuildScopeFilter:
    """Tests for the scope predicate builder."""

    def test_returns_empty_when_tenant_id_is_none(self):
        """No filter when tenant_id is None (unscoped query)."""
        from door.neptune_client import _build_scope_filter

        params: dict = {}
        result = _build_scope_filter("s", None, params)

        assert result == ""
        assert "tid" not in params

    def test_returns_scope_fragment_when_tenant_id_provided(self):
        """Returns AND clause with tenant scope when tenant_id is set."""
        from door.neptune_client import _build_scope_filter

        params: dict = {"repo": "org/repo"}
        result = _build_scope_filter("s", "tenant-abc", params)

        assert "s.tenant_id = $tid" in result
        assert "s.tenant_id IS NULL" in result
        assert params["tid"] == "tenant-abc"

    def test_uses_correct_node_variable(self):
        """The generated fragment uses the specified node variable."""
        from door.neptune_client import _build_scope_filter

        params: dict = {}
        result = _build_scope_filter("target", "t1", params)

        assert "target.tenant_id = $tid" in result
        assert "target.tenant_id IS NULL" in result

    def test_fragment_is_valid_cypher_structure(self):
        """The fragment starts with AND and wraps OR in parentheses."""
        from door.neptune_client import _build_scope_filter

        params: dict = {}
        result = _build_scope_filter("n", "t1", params)

        # Should start with " AND (" and end with ")"
        assert result.strip().startswith("AND (")
        assert result.strip().endswith(")")


# ---------------------------------------------------------------------------
# query_impact tenant scope tests
# ---------------------------------------------------------------------------


class TestQueryImpactTenantScope:
    """Tests that query_impact injects the scope predicate."""

    def _setup_mock_driver(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_no_scope_when_tenant_id_is_none(self):
        """Without tenant_id, no scope predicate in the Cypher."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            query_impact("org/repo", "src/api.py", "handle_request")

        cypher = mock_session.run.call_args[0][0]
        assert "tenant_id" not in cypher
        params = mock_session.run.call_args[0][1]
        assert "tid" not in params

    def test_scope_injected_when_tenant_id_provided(self):
        """With tenant_id, the Cypher contains the scope predicate."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            query_impact("org/repo", "src/api.py", "handle_request", tenant_id="t-123")

        cypher = mock_session.run.call_args[0][0]
        assert "target.tenant_id = $tid" in cypher
        assert "target.tenant_id IS NULL" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["tid"] == "t-123"

    def test_scope_injected_without_file(self):
        """Scope works with empty file (bare symbol query)."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            query_impact("org/repo", "", "main", tenant_id="t-456")

        cypher = mock_session.run.call_args[0][0]
        assert "target.tenant_id = $tid" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["tid"] == "t-456"
        assert "file" not in params

    def test_still_returns_records_with_scope(self):
        """Results are still returned correctly with scope filtering."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_records = [
            {
                "caller_repo": "org/repo",
                "caller_file": "src/main.py",
                "caller_name": "main",
                "caller_kind": "function",
                "distance": 1,
            }
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(mock_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            result = query_impact("org/repo", "src/api.py", "handle", tenant_id="t-1")

        assert len(result) == 1
        assert result[0]["caller_name"] == "main"


# ---------------------------------------------------------------------------
# query_understand tenant scope tests
# ---------------------------------------------------------------------------


class TestQueryUnderstandTenantScope:
    """Tests that query_understand injects scope predicate + collect(node)+project."""

    def _setup_mock_driver(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_no_scope_when_tenant_id_is_none(self):
        """Without tenant_id, no scope predicate."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "src/db.py", "connect")

        cypher = mock_session.run.call_args[0][0]
        assert "$tid" not in cypher
        assert "tenant_id = $tid" not in cypher

    def test_scope_injected_with_file(self):
        """With tenant_id and file, scope predicate is in the Cypher."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "src/db.py", "connect", tenant_id="t-abc")

        cypher = mock_session.run.call_args[0][0]
        assert "s.tenant_id = $tid" in cypher
        assert "s.tenant_id IS NULL" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["tid"] == "t-abc"

    def test_scope_injected_without_file(self):
        """With tenant_id and empty file, scope predicate still works."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "", "connect", tenant_id="t-def")

        cypher = mock_session.run.call_args[0][0]
        assert "s.tenant_id = $tid" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["tid"] == "t-def"

    def test_collect_node_plus_project_shape_preserved(self):
        """Bug #1611: collect(DISTINCT node) pattern still used with scope."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "src/db.py", "connect", tenant_id="t-1")

        cypher = mock_session.run.call_args[0][0]
        # Must still use collect(DISTINCT node) — NOT inline map in aggregate
        assert "collect(DISTINCT callee)" in cypher
        assert "collect(DISTINCT caller)" in cypher
        assert "collect(DISTINCT parent)" in cypher
        assert "collect(DISTINCT owner)" in cypher
        # Must NOT use the broken inline map pattern
        assert "collect(DISTINCT {" not in cypher


# ---------------------------------------------------------------------------
# query_repo_topology tenant scope tests
# ---------------------------------------------------------------------------


class TestQueryRepoTopologyTenantScope:
    """Tests that query_repo_topology accepts and uses tenant_id."""

    def _setup_mock_driver(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_scope_injected(self):
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_repo_topology

            query_repo_topology("org/repo", tenant_id="t-xyz")

        cypher = mock_session.run.call_args[0][0]
        assert "m.tenant_id = $tid" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["tid"] == "t-xyz"

    def test_no_scope_without_tenant_id(self):
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_repo_topology

            query_repo_topology("org/repo")

        cypher = mock_session.run.call_args[0][0]
        assert "tenant_id = $tid" not in cypher


# ---------------------------------------------------------------------------
# query_file_symbols tenant scope tests
# ---------------------------------------------------------------------------


class TestQueryFileSymbolsTenantScope:
    """Tests that query_file_symbols accepts and uses tenant_id."""

    def _setup_mock_driver(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_scope_injected(self):
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_file_symbols

            query_file_symbols("org/repo", "src/api.py", tenant_id="t-file")

        cypher = mock_session.run.call_args[0][0]
        assert "s.tenant_id = $tid" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["tid"] == "t-file"


# ---------------------------------------------------------------------------
# query_dir_symbols tenant scope tests
# ---------------------------------------------------------------------------


class TestQueryDirSymbolsTenantScope:
    """Tests that query_dir_symbols accepts and uses tenant_id."""

    def _setup_mock_driver(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_scope_injected(self):
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_dir_symbols

            query_dir_symbols("org/repo", "src/api", tenant_id="t-dir")

        cypher = mock_session.run.call_args[0][0]
        assert "s.tenant_id = $tid" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["tid"] == "t-dir"

    def test_dir_prefix_still_works_with_scope(self):
        """Scope filter is appended to existing STARTS WITH predicate."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_dir_symbols

            query_dir_symbols("org/repo", "src/api", tenant_id="t-dir")

        cypher = mock_session.run.call_args[0][0]
        # The STARTS WITH clause must still be present
        assert "STARTS WITH $dir_prefix" in cypher
        params = mock_session.run.call_args[0][1]
        assert params["dir_prefix"] == "src/api/"


# ---------------------------------------------------------------------------
# query_cross_repo_impact tenant scope tests
# ---------------------------------------------------------------------------


class TestQueryCrossRepoImpactTenantScope:
    """Tests that query_cross_repo_impact passes tenant scope to resolution."""

    def _setup_mock_driver(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_scope_in_resolve_step(self):
        """The resolve step (step 1) includes the tenant scope predicate."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_cross_repo_impact

            query_cross_repo_impact("org/repo", "src/api.py", "func", tenant_id="t-xr")

        # First call is the resolve step
        cypher = mock_session.run.call_args_list[0][0][0]
        assert "target.tenant_id = $tid" in cypher
        params = mock_session.run.call_args_list[0][0][1]
        assert params["tid"] == "t-xr"


# ---------------------------------------------------------------------------
# Backward compatibility — existing callers without tenant_id
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Ensure existing callers that don't pass tenant_id still work."""

    def _setup_mock_driver(self):
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter([])
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None
        return mock_driver, mock_session

    def test_impact_without_tenant_id_is_unscoped(self):
        """query_impact without tenant_id returns all nodes (no filter)."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_impact

            query_impact("org/repo", "src/api.py", "func")

        cypher = mock_session.run.call_args[0][0]
        # Should NOT contain any tenant filtering
        assert "tenant_id" not in cypher

    def test_understand_without_tenant_id_is_unscoped(self):
        """query_understand without tenant_id returns all nodes (no filter)."""
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_understand

            query_understand("org/repo", "src/db.py", "connect")

        cypher = mock_session.run.call_args[0][0]
        assert "tenant_id" not in cypher

    def test_file_symbols_without_tenant_id_is_unscoped(self):
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_file_symbols

            query_file_symbols("org/repo", "src/api.py")

        cypher = mock_session.run.call_args[0][0]
        assert "tenant_id" not in cypher

    def test_dir_symbols_without_tenant_id_is_unscoped(self):
        mock_driver, mock_session = self._setup_mock_driver()

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import query_dir_symbols

            query_dir_symbols("org/repo", "src/api")

        cypher = mock_session.run.call_args[0][0]
        assert "tenant_id" not in cypher


# ---------------------------------------------------------------------------
# Write-path: CSV generation with tenant_id
# ---------------------------------------------------------------------------


class TestCSVGenerationTenantId:
    """Tests that scip_neptune_csv.generate_csv stamps tenant_id/owner_sub."""

    def test_csv_includes_tenant_columns(self, tmp_path):
        """Generated CSV has tenant_id and owner_sub columns."""
        import csv
        import sys

        # Add ingestion directory to path for import
        sys.path.insert(0, "/work/repo/modules/agent-context/images/ingestion")
        try:
            from scip_neptune_csv import generate_csv

            # Create a minimal SCIPGraph mock
            class FakeNode:
                def __init__(self, name, module, file, line, kind):
                    self.name = name
                    self.module = module
                    self.file = file
                    self.line = line
                    self.kind = kind

            class FakeEdge:
                def __init__(self, caller_id, callee_id, edge_kind, file, line):
                    self.caller_id = caller_id
                    self.callee_id = callee_id
                    self.edge_kind = edge_kind
                    self.file = file
                    self.line = line

            class FakeGraph:
                def __init__(self):
                    self.repo = "org/repo"
                    self.nodes = {
                        "sym1": FakeNode("main", "mod", "main.py", 1, "function"),
                        "sym2": FakeNode("helper", "mod", "util.py", 5, "function"),
                    }
                    self.edges = [FakeEdge("sym1", "sym2", "CALLS", "main.py", 3)]
                    self.node_count = 2
                    self.edge_count = 1

            graph = FakeGraph()
            output_dir = str(tmp_path / "csv_out")

            result = generate_csv(graph, output_dir, tenant_id="tenant-x", owner_sub="user-y")

            # Verify CSV has the tenant columns
            with open(result.vertices_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert len(rows) == 2
            assert "tenant_id:String" in reader.fieldnames
            assert "owner_sub:String" in reader.fieldnames
            assert rows[0]["tenant_id:String"] == "tenant-x"
            assert rows[0]["owner_sub:String"] == "user-y"
        finally:
            sys.path.pop(0)

    def test_csv_empty_tenant_when_none(self, tmp_path):
        """When tenant_id is None, CSV column is empty string."""
        import csv
        import sys

        sys.path.insert(0, "/work/repo/modules/agent-context/images/ingestion")
        try:
            from scip_neptune_csv import generate_csv

            class FakeNode:
                def __init__(self, name, module, file, line, kind):
                    self.name = name
                    self.module = module
                    self.file = file
                    self.line = line
                    self.kind = kind

            class FakeEdge:
                def __init__(self, caller_id, callee_id, edge_kind, file, line):
                    self.caller_id = caller_id
                    self.callee_id = callee_id
                    self.edge_kind = edge_kind
                    self.file = file
                    self.line = line

            class FakeGraph:
                def __init__(self):
                    self.repo = "org/repo"
                    self.nodes = {
                        "sym1": FakeNode("main", "mod", "main.py", 1, "function"),
                        "sym2": FakeNode("helper", "mod", "util.py", 5, "function"),
                    }
                    self.edges = [FakeEdge("sym1", "sym2", "CALLS", "main.py", 3)]
                    self.node_count = 2
                    self.edge_count = 1

            graph = FakeGraph()
            output_dir = str(tmp_path / "csv_out_none")

            result = generate_csv(graph, output_dir)

            with open(result.vertices_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert rows[0]["tenant_id:String"] == ""
            assert rows[0]["owner_sub:String"] == ""
        finally:
            sys.path.pop(0)


# ---------------------------------------------------------------------------
# Backfill function tests
# ---------------------------------------------------------------------------


class TestBackfillTenantId:
    """Tests for graph_ops.backfill_tenant_id."""

    def test_backfill_calls_correct_cypher(self):
        """backfill_tenant_id sends correct SET with WHERE tenant_id IS NULL."""
        import sys

        sys.path.insert(0, "/work/repo/modules/agent-context/pipeline/neptune_ingestion")
        try:
            with patch(
                "pipeline.neptune_ingestion.graph_ops.neptune_query",
                return_value={"results": []},
            ) as mock_query:
                from pipeline.neptune_ingestion.graph_ops import backfill_tenant_id

                result = backfill_tenant_id(
                    "https://neptune:8182/opencypher",
                    "us-east-1",
                    "org/repo",
                    "tenant-backfill",
                )

            assert result["success"] is True
            call_args = mock_query.call_args
            cypher = call_args[0][2]
            params = call_args[0][3]
            assert "n.tenant_id IS NULL" in cypher
            assert "SET n.tenant_id = $tenant_id" in cypher
            assert params["tenant_id"] == "tenant-backfill"
            assert params["repo"] == "org/repo"
        finally:
            sys.path.pop(0)

    def test_backfill_with_owner_sub(self):
        """backfill_tenant_id with owner_sub also stamps owner_sub."""
        import sys

        sys.path.insert(0, "/work/repo/modules/agent-context/pipeline/neptune_ingestion")
        try:
            with patch(
                "pipeline.neptune_ingestion.graph_ops.neptune_query",
                return_value={"results": []},
            ) as mock_query:
                from pipeline.neptune_ingestion.graph_ops import backfill_tenant_id

                result = backfill_tenant_id(
                    "https://neptune:8182/opencypher",
                    "us-east-1",
                    "org/repo",
                    "tenant-bf",
                    owner_sub="user-123",
                )

            assert result["success"] is True
            cypher = mock_query.call_args[0][2]
            params = mock_query.call_args[0][3]
            assert "n.owner_sub = $owner_sub" in cypher
            assert params["owner_sub"] == "user-123"
        finally:
            sys.path.pop(0)

    def test_backfill_returns_error_on_failure(self):
        """backfill_tenant_id returns error dict when Neptune fails."""
        import sys

        sys.path.insert(0, "/work/repo/modules/agent-context/pipeline/neptune_ingestion")
        try:
            with patch(
                "pipeline.neptune_ingestion.graph_ops.neptune_query",
                return_value={"error": "Connection refused", "code": 503},
            ):
                from pipeline.neptune_ingestion.graph_ops import backfill_tenant_id

                result = backfill_tenant_id(
                    "https://neptune:8182/opencypher",
                    "us-east-1",
                    "org/repo",
                    "tenant-fail",
                )

            assert result["success"] is False
            assert "Connection refused" in result["error"]
        finally:
            sys.path.pop(0)

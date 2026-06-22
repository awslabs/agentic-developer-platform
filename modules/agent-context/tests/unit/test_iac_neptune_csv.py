"""Unit tests for IaC Neptune CSV generation.

Tests the CSV output format, ~id encoding, edge deduplication,
and compatibility with scip_neptune_loader.py.
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

# Add the ingestion image directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

from iac_neptune_csv import (
    _make_infra_edge_id,
    _make_infra_node_id,
    _sanitize_csv_value,
    generate_csv,
    generate_summary,
    get_infra_delete_queries,
)
from iac_terraform_parser import IaCEdge, IaCGraph, IaCNode


# ---------------------------------------------------------------------------
# ~id encoding tests
# ---------------------------------------------------------------------------


class TestIdEncoding:
    """Test Neptune ~id generation."""

    def test_node_id_has_iac_prefix(self):
        """Node IDs must start with 'iac:' to avoid code graph collision."""
        node_id = _make_infra_node_id("aws_iam_role.agent_runner", "aws-e/adp")
        assert node_id.startswith("iac:")

    def test_node_id_deterministic(self):
        """Same input produces same ID."""
        id1 = _make_infra_node_id("aws_s3_bucket.data", "org/repo")
        id2 = _make_infra_node_id("aws_s3_bucket.data", "org/repo")
        assert id1 == id2

    def test_node_id_different_for_different_addresses(self):
        """Different addresses produce different IDs."""
        id1 = _make_infra_node_id("aws_iam_role.admin", "org/repo")
        id2 = _make_infra_node_id("aws_iam_role.user", "org/repo")
        assert id1 != id2

    def test_node_id_different_for_different_repos(self):
        """Same address in different repos produces different IDs."""
        id1 = _make_infra_node_id("aws_iam_role.admin", "org/repo-a")
        id2 = _make_infra_node_id("aws_iam_role.admin", "org/repo-b")
        assert id1 != id2

    def test_node_id_format(self):
        """Node ID follows iac:{repo_safe}|{hash} format."""
        node_id = _make_infra_node_id("aws_iam_role.admin", "aws-e/adp")
        parts = node_id.split("|")
        assert len(parts) == 2
        assert parts[0] == "iac:aws-e-adp"
        assert len(parts[1]) == 16  # SHA-256 truncated to 16 hex chars

    def test_edge_id_has_iac_prefix(self):
        """Edge IDs must start with 'iac:e|'."""
        edge_id = _make_infra_edge_id("iac:repo|abc", "iac:repo|def", "DEPENDS_ON")
        assert edge_id.startswith("iac:e|")

    def test_edge_id_contains_label(self):
        """Edge ID includes the edge label."""
        edge_id = _make_infra_edge_id("from", "to", "DEPENDS_ON")
        assert "DEPENDS_ON" in edge_id

    def test_edge_id_deterministic(self):
        """Same inputs produce same edge ID."""
        id1 = _make_infra_edge_id("from", "to", "DEPENDS_ON")
        id2 = _make_infra_edge_id("from", "to", "DEPENDS_ON")
        assert id1 == id2


# ---------------------------------------------------------------------------
# CSV sanitization tests
# ---------------------------------------------------------------------------


class TestSanitization:
    """Test CSV value sanitization."""

    def test_clean_string_unchanged(self):
        assert _sanitize_csv_value("hello world") == "hello world"

    def test_null_bytes_removed(self):
        assert _sanitize_csv_value("hello\x00world") == "helloworld"

    def test_empty_string(self):
        assert _sanitize_csv_value("") == ""

    def test_control_chars_removed(self):
        assert _sanitize_csv_value("line\x01one") == "lineone"


# ---------------------------------------------------------------------------
# CSV generation tests
# ---------------------------------------------------------------------------


def _make_test_graph() -> IaCGraph:
    """Create a minimal test graph."""
    graph = IaCGraph(repo="test-org/test-repo")
    graph.nodes["aws_iam_role.admin"] = IaCNode(
        node_id="aws_iam_role.admin",
        label="InfraResource",
        resource_type="aws_iam_role",
        name="admin",
        provider="aws",
        file="main.tf",
        line=1,
        repo="test-org/test-repo",
        module_path="",
        source="",
        version_constraint="",
    )
    graph.nodes["aws_iam_role_policy_attachment.admin_ecr"] = IaCNode(
        node_id="aws_iam_role_policy_attachment.admin_ecr",
        label="InfraResource",
        resource_type="aws_iam_role_policy_attachment",
        name="admin_ecr",
        provider="aws",
        file="main.tf",
        line=10,
        repo="test-org/test-repo",
        module_path="",
        source="",
        version_constraint="",
    )
    graph.nodes["module.networking"] = IaCNode(
        node_id="module.networking",
        label="InfraModule",
        resource_type="",
        name="networking",
        provider="",
        file="main.tf",
        line=20,
        repo="test-org/test-repo",
        module_path="",
        source="./modules/networking",
        version_constraint="",
    )
    graph.edges.append(
        IaCEdge(
            from_id="aws_iam_role_policy_attachment.admin_ecr",
            to_id="aws_iam_role.admin",
            edge_label="DEPENDS_ON",
            file="main.tf",
            line=12,
        )
    )
    return graph


class TestCSVGeneration:
    """Test Neptune CSV file generation."""

    def test_generates_vertex_and_edge_files(self):
        """CSV generation creates vertices.csv and edges.csv."""
        graph = _make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_csv(graph, tmpdir)
            assert os.path.exists(result.vertices_path)
            assert os.path.exists(result.edges_path)

    def test_vertex_count_matches(self):
        """Vertex count in output matches graph node count."""
        graph = _make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_csv(graph, tmpdir)
            assert result.vertex_count == 3

    def test_edge_count_matches(self):
        """Edge count in output matches graph edge count."""
        graph = _make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_csv(graph, tmpdir)
            assert result.edge_count == 1
            assert result.depends_on_count == 1

    def test_vertex_csv_format(self):
        """Vertex CSV has correct headers and ~id prefix."""
        graph = _make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_csv(graph, tmpdir)
            with open(result.vertices_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            # Check headers
            assert "~id" in reader.fieldnames
            assert "~label" in reader.fieldnames
            assert "address:String" in reader.fieldnames
            assert "repo:String" in reader.fieldnames

            # Check all IDs have iac: prefix
            for row in rows:
                assert row["~id"].startswith("iac:")

            # Check labels are infra-specific
            labels = {row["~label"] for row in rows}
            assert "InfraResource" in labels
            assert "InfraModule" in labels
            assert "Symbol" not in labels  # Must NOT use code graph label

    def test_edge_csv_format(self):
        """Edge CSV has correct headers and ~id prefix."""
        graph = _make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_csv(graph, tmpdir)
            with open(result.edges_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            # Check headers
            assert "~id" in reader.fieldnames
            assert "~from" in reader.fieldnames
            assert "~to" in reader.fieldnames
            assert "~label" in reader.fieldnames

            # Check edge IDs have iac:e| prefix
            for row in rows:
                assert row["~id"].startswith("iac:e|")
                assert row["~from"].startswith("iac:")
                assert row["~to"].startswith("iac:")

            # Check edge label
            assert rows[0]["~label"] == "DEPENDS_ON"

    def test_no_code_graph_collision(self):
        """Infra ~ids do not collide with code graph ~id format."""
        # Code graph format: {repo_safe}|{moniker_hash}
        # Infra graph format: iac:{repo_safe}|{address_hash}
        infra_id = _make_infra_node_id("aws_iam_role.admin", "org/repo")
        # A hypothetical code graph ID for the same repo
        code_id = "org-repo|abcdef1234567890"
        assert infra_id != code_id
        assert infra_id.startswith("iac:")
        assert not code_id.startswith("iac:")


# ---------------------------------------------------------------------------
# Scoped delete query tests
# ---------------------------------------------------------------------------


class TestScopedDelete:
    """Test the label-specific delete queries for idempotent re-ingestion."""

    def test_delete_queries_target_infra_labels(self):
        """Delete queries must target InfraResource, InfraModule, InfraProvider only."""
        queries = get_infra_delete_queries("aws-e/adp")
        assert len(queries) == 3
        assert any("InfraResource" in q for q, _params in queries)
        assert any("InfraModule" in q for q, _params in queries)
        assert any("InfraProvider" in q for q, _params in queries)
        # Must NOT delete Symbol nodes (code graph)
        assert not any("Symbol" in q for q, _params in queries)

    def test_delete_queries_scoped_to_repo(self):
        """Delete queries use parameterized $repo (not interpolated) for safety."""
        queries = get_infra_delete_queries("aws-e/adp")
        for q, params in queries:
            # Query uses $repo parameter, not string interpolation
            assert "$repo" in q
            assert params == {"repo": "aws-e/adp"}

    def test_delete_queries_use_detach_delete(self):
        """Queries must DETACH DELETE to remove edges too."""
        queries = get_infra_delete_queries("aws-e/adp")
        for q, _params in queries:
            assert "DETACH DELETE" in q

    def test_delete_queries_no_injection(self):
        """Repo names with special chars don't appear in the query string."""
        queries = get_infra_delete_queries("org/repo'; DROP ALL")
        for q, params in queries:
            # The dangerous string must be in params, not in the query itself
            assert "'; DROP ALL" not in q
            assert params["repo"] == "org/repo'; DROP ALL"


# ---------------------------------------------------------------------------
# Summary generation tests
# ---------------------------------------------------------------------------


class TestSummaryGeneration:
    """Test JSON summary output."""

    def test_summary_contains_counts(self):
        graph = _make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_output = generate_csv(graph, tmpdir)
            summary_path = os.path.join(tmpdir, "summary.json")
            summary = generate_summary(graph, csv_output, summary_path)
            assert summary["repo"] == "test-org/test-repo"
            assert summary["node_count"] == 3
            assert summary["edge_count"] == 1
            assert os.path.exists(summary_path)

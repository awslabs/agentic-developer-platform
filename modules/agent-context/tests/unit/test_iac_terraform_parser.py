"""Unit tests for Terraform IaC parser.

Tests the core parsing logic: resource extraction, dependency resolution
(explicit depends_on + implicit interpolation references), module/provider
extraction, and the fail-loud rule.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add the ingestion image directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

from iac_terraform_parser import (
    _extract_string_values,
    _infer_provider_from_type,
    _parse_depends_on,
    _resolve_implicit_refs,
    discover_tf_files,
    parse_terraform,
)


# ---------------------------------------------------------------------------
# Helper: create temp .tf files for testing
# ---------------------------------------------------------------------------


def _write_tf_file(tmpdir: str, rel_path: str, content: str) -> str:
    """Write a .tf file and return its absolute path."""
    abs_path = os.path.join(tmpdir, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(content)
    return abs_path


# ---------------------------------------------------------------------------
# Provider inference tests
# ---------------------------------------------------------------------------


class TestProviderInference:
    """Test provider name inference from resource type."""

    def test_aws_resource(self):
        assert _infer_provider_from_type("aws_iam_role") == "aws"

    def test_google_resource(self):
        assert _infer_provider_from_type("google_compute_instance") == "google"

    def test_azurerm_resource(self):
        assert _infer_provider_from_type("azurerm_resource_group") == "azurerm"

    def test_kubernetes_resource(self):
        assert _infer_provider_from_type("kubernetes_deployment") == "kubernetes"

    def test_single_word_type(self):
        assert _infer_provider_from_type("unknown") == "unknown"


# ---------------------------------------------------------------------------
# String extraction tests
# ---------------------------------------------------------------------------


class TestStringExtraction:
    """Test recursive string value extraction from nested structures."""

    def test_simple_string(self):
        assert _extract_string_values("hello") == ["hello"]

    def test_nested_dict(self):
        obj = {"a": "value1", "b": {"c": "value2"}}
        result = _extract_string_values(obj)
        assert "value1" in result
        assert "value2" in result

    def test_list_of_strings(self):
        obj = ["a", "b", "c"]
        assert _extract_string_values(obj) == ["a", "b", "c"]

    def test_mixed_types(self):
        obj = {"key": [1, "str_val", {"nested": "deep"}]}
        result = _extract_string_values(obj)
        assert "str_val" in result
        assert "deep" in result

    def test_empty_structures(self):
        assert _extract_string_values({}) == []
        assert _extract_string_values([]) == []


# ---------------------------------------------------------------------------
# depends_on parsing tests
# ---------------------------------------------------------------------------


class TestDependsOnParsing:
    """Test explicit depends_on reference extraction."""

    def test_simple_depends_on(self):
        body = {"depends_on": ["aws_iam_role.admin", "module.networking"]}
        result = _parse_depends_on(body)
        assert "aws_iam_role.admin" in result
        assert "module.networking" in result

    def test_no_depends_on(self):
        body = {"name": "test"}
        assert _parse_depends_on(body) == []

    def test_empty_depends_on(self):
        body = {"depends_on": []}
        assert _parse_depends_on(body) == []

    def test_nested_list_depends_on(self):
        """python-hcl2 sometimes returns nested lists."""
        body = {"depends_on": [["aws_s3_bucket.data"]]}
        result = _parse_depends_on(body)
        assert "aws_s3_bucket.data" in result


# ---------------------------------------------------------------------------
# Implicit reference resolution tests
# ---------------------------------------------------------------------------


class TestImplicitRefs:
    """Test implicit interpolation reference resolution."""

    def test_resource_reference(self):
        body = {"role": "aws_iam_role.agent_runner.name"}
        known_resources = {"aws_iam_role.agent_runner"}
        refs = _resolve_implicit_refs(body, known_resources, set(), set())
        assert ("aws_iam_role.agent_runner", "resource") in refs

    def test_module_reference(self):
        body = {"vpc_id": "module.networking.vpc_id"}
        known_modules = {"module.networking"}
        refs = _resolve_implicit_refs(body, set(), known_modules, set())
        assert ("module.networking", "module") in refs

    def test_data_source_reference(self):
        body = {"account_id": "data.aws_caller_identity.current.account_id"}
        known_data = {"data.aws_caller_identity.current"}
        refs = _resolve_implicit_refs(body, set(), set(), known_data)
        assert ("data.aws_caller_identity.current", "data") in refs

    def test_unknown_reference_ignored(self):
        body = {"value": "aws_iam_role.nonexistent.arn"}
        refs = _resolve_implicit_refs(body, set(), set(), set())
        assert len(refs) == 0

    def test_interpolation_syntax(self):
        body = {"name": "${aws_iam_role.agent_runner.arn}"}
        known_resources = {"aws_iam_role.agent_runner"}
        refs = _resolve_implicit_refs(body, known_resources, set(), set())
        assert ("aws_iam_role.agent_runner", "resource") in refs

    def test_deduplication(self):
        """Multiple references to same target should deduplicate."""
        body = {
            "a": "aws_iam_role.agent_runner.arn",
            "b": "aws_iam_role.agent_runner.name",
        }
        known_resources = {"aws_iam_role.agent_runner"}
        refs = _resolve_implicit_refs(body, known_resources, set(), set())
        assert len(refs) == 1


# ---------------------------------------------------------------------------
# File discovery tests
# ---------------------------------------------------------------------------


class TestFileDiscovery:
    """Test .tf file discovery."""

    def test_discovers_tf_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(tmpdir, "main.tf", "# empty")
            _write_tf_file(tmpdir, "modules/vpc/main.tf", "# empty")
            result = discover_tf_files(tmpdir)
            assert "main.tf" in result
            assert "modules/vpc/main.tf" in result

    def test_skips_dot_terraform(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(tmpdir, "main.tf", "# real")
            _write_tf_file(tmpdir, ".terraform/providers/main.tf", "# cached")
            result = discover_tf_files(tmpdir)
            assert "main.tf" in result
            assert len(result) == 1

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_tf_files(tmpdir)
            assert result == []


# ---------------------------------------------------------------------------
# Full parser integration tests
# ---------------------------------------------------------------------------


class TestParseTerraform:
    """Integration tests for the full parse_terraform function."""

    def test_basic_resource_parsing(self):
        """Parse a simple resource and verify node creation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
resource "aws_iam_role" "agent_runner" {
  name = "agent-runner"
  assume_role_policy = jsonencode({})
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            assert graph.node_count >= 1
            assert "aws_iam_role.agent_runner" in graph.nodes
            node = graph.nodes["aws_iam_role.agent_runner"]
            assert node.label == "InfraResource"
            assert node.resource_type == "aws_iam_role"
            assert node.name == "agent_runner"
            assert node.provider == "aws"
            assert node.repo == "test-org/test-repo"

    def test_implicit_dependency_edges(self):
        """Implicit references create DEPENDS_ON edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
resource "aws_iam_role" "agent_runner" {
  name = "agent-runner"
}

resource "aws_iam_role_policy_attachment" "agent_ecr" {
  role = aws_iam_role.agent_runner.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            # Should have DEPENDS_ON edge from policy_attachment → role
            depends_on_edges = [e for e in graph.edges if e.edge_label == "DEPENDS_ON"]
            role_deps = [
                e
                for e in depends_on_edges
                if e.from_id == "aws_iam_role_policy_attachment.agent_ecr"
                and e.to_id == "aws_iam_role.agent_runner"
            ]
            assert len(role_deps) == 1

    def test_explicit_depends_on(self):
        """Explicit depends_on creates DEPENDS_ON edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
resource "aws_iam_role" "base" {
  name = "base-role"
}

resource "aws_iam_role" "dependent" {
  name = "dependent-role"
  depends_on = [aws_iam_role.base]
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            depends_on_edges = [e for e in graph.edges if e.edge_label == "DEPENDS_ON"]
            explicit_deps = [
                e
                for e in depends_on_edges
                if e.from_id == "aws_iam_role.dependent" and e.to_id == "aws_iam_role.base"
            ]
            assert len(explicit_deps) == 1

    def test_module_parsing(self):
        """Module blocks create InfraModule nodes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
module "networking" {
  source = "./modules/networking"
  vpc_cidr = "10.0.0.0/16"
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            assert "module.networking" in graph.nodes
            node = graph.nodes["module.networking"]
            assert node.label == "InfraModule"
            assert node.name == "networking"
            assert node.source == "./modules/networking"

    def test_module_cross_reference(self):
        """Module output references create DEPENDS_ON edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
module "networking" {
  source = "./modules/networking"
}

module "eks" {
  source = "./modules/eks"
  vpc_id = module.networking.vpc_id
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            depends_on_edges = [e for e in graph.edges if e.edge_label == "DEPENDS_ON"]
            mod_deps = [
                e
                for e in depends_on_edges
                if e.from_id == "module.eks" and e.to_id == "module.networking"
            ]
            assert len(mod_deps) == 1

    def test_provider_extraction(self):
        """Required providers create InfraProvider nodes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            assert "provider.aws" in graph.nodes
            node = graph.nodes["provider.aws"]
            assert node.label == "InfraProvider"
            assert node.source == "hashicorp/aws"
            assert node.version_constraint == "~> 5.0"

    def test_data_source_parsing(self):
        """Data sources are parsed as InfraResource nodes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
data "aws_caller_identity" "current" {}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            assert "data.aws_caller_identity.current" in graph.nodes
            node = graph.nodes["data.aws_caller_identity.current"]
            assert node.label == "InfraResource"
            assert node.resource_type == "data.aws_caller_identity"

    def test_no_tf_files_returns_empty_graph(self):
        """Empty directory returns empty graph (no error)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            assert graph.node_count == 0
            assert graph.edge_count == 0

    def test_fail_loud_on_zero_nodes_with_tf_files(self):
        """If .tf files exist but produce 0 nodes, raise ValueError (fail-loud rule)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a .tf file with only comments (no parseable resources)
            _write_tf_file(tmpdir, "main.tf", "# This file has no resources\n")
            # The fail-loud rule raises ValueError when .tf files exist
            # but parsing produces 0 nodes — this prevents the "silent empty"
            # bug class where agents read "no dependencies" as "safe to delete"
            with pytest.raises(ValueError, match="produced 0 nodes"):
                parse_terraform(tmpdir, "test-org/test-repo")

    def test_no_self_reference_edges(self):
        """A resource should not have a DEPENDS_ON edge to itself."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
resource "aws_iam_role" "self_ref" {
  name = aws_iam_role.self_ref.id
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            self_edges = [e for e in graph.edges if e.from_id == e.to_id]
            assert len(self_edges) == 0

    def test_uses_provider_edge(self):
        """Resources with matching provider get USES_PROVIDER edge."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

resource "aws_s3_bucket" "data" {
  bucket = "my-bucket"
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            provider_edges = [e for e in graph.edges if e.edge_label == "USES_PROVIDER"]
            bucket_to_aws = [
                e
                for e in provider_edges
                if e.from_id == "aws_s3_bucket.data" and e.to_id == "provider.aws"
            ]
            assert len(bucket_to_aws) == 1

    def test_multi_file_parsing(self):
        """Parser handles multiple .tf files in same directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_tf_file(
                tmpdir,
                "main.tf",
                """
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}
""",
            )
            _write_tf_file(
                tmpdir,
                "iam.tf",
                """
resource "aws_iam_role" "admin" {
  name = "admin"
}
""",
            )
            graph = parse_terraform(tmpdir, "test-org/test-repo")
            assert "aws_vpc.main" in graph.nodes
            assert "aws_iam_role.admin" in graph.nodes

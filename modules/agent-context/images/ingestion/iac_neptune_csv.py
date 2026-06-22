"""Neptune CSV generation from IaC dependency graph.

Converts an IaCGraph (from iac_terraform_parser.py) into Neptune-compatible CSV
files for loading via openCypher UNWIND batch (reusing scip_neptune_loader.py).

CSV format: openCypher (Neptune Bulk Loader compatible)
  - Nodes: ~id, ~label, address:String, resource_type:String, name:String,
            provider:String, file:String, line:Int, repo:String, module_path:String,
            source:String, version_constraint:String
  - Edges: ~id, ~from, ~to, ~label, file:String, line:Int, repo:String

Node ~id encoding: iac:{repo_safe}|{address_hash} (design doc §2.2)
Edge ~id encoding: iac:e|{from_hash}|{edge_label}|{to_hash}

Design authority: docs/agent-context/design-notes/1647-iac-dependency-graph-design.md
Parent EPIC: #1647 (IAC-1)
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from dataclasses import dataclass

from iac_terraform_parser import IaCGraph

log = logging.getLogger("iac_neptune_csv")


# ---------------------------------------------------------------------------
# ~id generation (per design doc §4.4)
# ---------------------------------------------------------------------------


def _make_infra_node_id(address: str, repo: str) -> str:
    """Create a deterministic Neptune ~id for an infra node.

    Format: iac:{repo_safe}|{address_hash}

    The iac: prefix ensures zero collision with code graph IDs
    (which use {repo_safe}|{moniker_hash} without prefix).
    """
    repo_safe = repo.replace("/", "-")
    address_hash = hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]
    return f"iac:{repo_safe}|{address_hash}"


def _make_infra_edge_id(from_id: str, to_id: str, edge_label: str) -> str:
    """Create a deterministic Neptune edge ~id.

    Format: iac:e|{from_hash}|{edge_label}|{to_hash}
    """
    from_hash = hashlib.sha256(from_id.encode("utf-8")).hexdigest()[:10]
    to_hash = hashlib.sha256(to_id.encode("utf-8")).hexdigest()[:10]
    return f"iac:e|{from_hash}|{edge_label}|{to_hash}"


# ---------------------------------------------------------------------------
# CSV output dataclass (compatible with scip_neptune_loader.py's CSVOutput)
# ---------------------------------------------------------------------------


@dataclass
class IaCCSVOutput:
    """Result of IaC CSV generation.

    Intentionally mirrors scip_neptune_csv.CSVOutput so scip_neptune_loader.py
    can consume it directly via duck typing (vertices_path + edges_path).
    """

    vertices_path: str
    edges_path: str
    vertex_count: int
    edge_count: int
    depends_on_count: int
    declared_in_count: int
    uses_module_count: int
    uses_provider_count: int
    output_dir: str


# ---------------------------------------------------------------------------
# CSV generation
# ---------------------------------------------------------------------------


def _sanitize_csv_value(value: str) -> str:
    """Sanitize a string value for CSV output.

    Removes control characters that could break CSV parsing or Neptune loading.
    """
    if not value:
        return ""
    return "".join(c for c in value if c.isprintable() and c != "\x00")


def generate_csv(graph: IaCGraph, output_dir: str) -> IaCCSVOutput:
    """Generate Neptune openCypher CSV files from an IaCGraph.

    Args:
        graph: The IaC dependency graph to convert
        output_dir: Directory to write CSV files to

    Returns:
        IaCCSVOutput with paths and counts
    """
    os.makedirs(output_dir, exist_ok=True)

    # --- Write vertices.csv ---
    vertices_path = os.path.join(output_dir, "vertices.csv")
    vertex_fieldnames = [
        "~id",
        "~label",
        "address:String",
        "resource_type:String",
        "name:String",
        "provider:String",
        "file:String",
        "line:Int",
        "repo:String",
        "module_path:String",
        "source:String",
        "version_constraint:String",
    ]

    vertex_count = 0
    with open(vertices_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=vertex_fieldnames)
        writer.writeheader()

        for address, node in graph.nodes.items():
            writer.writerow(
                {
                    "~id": _make_infra_node_id(address, graph.repo),
                    "~label": node.label,
                    "address:String": _sanitize_csv_value(address),
                    "resource_type:String": _sanitize_csv_value(node.resource_type),
                    "name:String": _sanitize_csv_value(node.name),
                    "provider:String": _sanitize_csv_value(node.provider),
                    "file:String": _sanitize_csv_value(node.file),
                    "line:Int": node.line,
                    "repo:String": graph.repo,
                    "module_path:String": _sanitize_csv_value(node.module_path),
                    "source:String": _sanitize_csv_value(node.source),
                    "version_constraint:String": _sanitize_csv_value(node.version_constraint),
                }
            )
            vertex_count += 1

    # --- Write edges.csv ---
    edges_path = os.path.join(output_dir, "edges.csv")
    edge_fieldnames = [
        "~id",
        "~from",
        "~to",
        "~label",
        "file:String",
        "line:Int",
        "repo:String",
    ]

    depends_on_count = 0
    declared_in_count = 0
    uses_module_count = 0
    uses_provider_count = 0

    with open(edges_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=edge_fieldnames)
        writer.writeheader()

        for edge in graph.edges:
            from_node_id = _make_infra_node_id(edge.from_id, graph.repo)
            to_node_id = _make_infra_node_id(edge.to_id, graph.repo)
            edge_id = _make_infra_edge_id(from_node_id, to_node_id, edge.edge_label)

            writer.writerow(
                {
                    "~id": edge_id,
                    "~from": from_node_id,
                    "~to": to_node_id,
                    "~label": edge.edge_label,
                    "file:String": _sanitize_csv_value(edge.file),
                    "line:Int": edge.line,
                    "repo:String": graph.repo,
                }
            )

            if edge.edge_label == "DEPENDS_ON":
                depends_on_count += 1
            elif edge.edge_label == "DECLARED_IN":
                declared_in_count += 1
            elif edge.edge_label == "USES_MODULE":
                uses_module_count += 1
            elif edge.edge_label == "USES_PROVIDER":
                uses_provider_count += 1

    log.info(
        "IaC Neptune CSV generated: %d vertices, %d edges "
        "(%d DEPENDS_ON, %d DECLARED_IN, %d USES_MODULE, %d USES_PROVIDER) -> %s",
        vertex_count,
        len(graph.edges),
        depends_on_count,
        declared_in_count,
        uses_module_count,
        uses_provider_count,
        output_dir,
    )

    return IaCCSVOutput(
        vertices_path=vertices_path,
        edges_path=edges_path,
        vertex_count=vertex_count,
        edge_count=len(graph.edges),
        depends_on_count=depends_on_count,
        declared_in_count=declared_in_count,
        uses_module_count=uses_module_count,
        uses_provider_count=uses_provider_count,
        output_dir=output_dir,
    )


def generate_summary(graph: IaCGraph, csv_output: IaCCSVOutput, report_path: str) -> dict:
    """Generate an extraction summary JSON file.

    Returns the summary dict (also written to report_path).
    """
    summary = {
        "repo": graph.repo,
        "node_count": csv_output.vertex_count,
        "edge_count": csv_output.edge_count,
        "depends_on_count": csv_output.depends_on_count,
        "declared_in_count": csv_output.declared_in_count,
        "uses_module_count": csv_output.uses_module_count,
        "uses_provider_count": csv_output.uses_provider_count,
        "vertices_file": csv_output.vertices_path,
        "edges_file": csv_output.edges_path,
        "output_dir": csv_output.output_dir,
    }

    with open(report_path, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


# ---------------------------------------------------------------------------
# Scoped delete queries (per design doc §2.4)
# ---------------------------------------------------------------------------


def get_infra_delete_queries(repo: str) -> list[tuple[str, dict]]:
    """Return parameterized openCypher queries to delete all infra nodes for a repo.

    Must be run BEFORE loading new infra CSV (idempotent re-ingestion).
    Mirrors the SCIP pipeline's clear_repo_graph but is label-specific
    to avoid deleting code graph (:Symbol) nodes.

    Returns list of (cypher_template, parameters) tuples. Uses parameterized
    queries ($repo) to prevent Cypher injection — same pattern as
    scip_neptune_loader.clear_repo_graph().
    """
    return [
        (
            "MATCH (n:InfraResource {repo: $repo}) DETACH DELETE n RETURN count(n) AS deleted",
            {"repo": repo},
        ),
        (
            "MATCH (n:InfraModule {repo: $repo}) DETACH DELETE n RETURN count(n) AS deleted",
            {"repo": repo},
        ),
        (
            "MATCH (n:InfraProvider {repo: $repo}) DETACH DELETE n RETURN count(n) AS deleted",
            {"repo": repo},
        ),
    ]

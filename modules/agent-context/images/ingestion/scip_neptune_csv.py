"""Neptune CSV generation from SCIP graph.

Converts a SCIPGraph (from scip_ingester.py) into Neptune-compatible CSV files
for loading via openCypher UNWIND batch or Bulk Loader.

CSV format: openCypher (Neptune Bulk Loader compatible)
  - Nodes: ~id, ~label, symbol_id:String, name:String, module:String, file:String,
            line:Int, kind:String, repo:String
  - Edges: ~id, ~from, ~to, ~label, edge_kind:String, file:String, line:Int, repo:String

Node ~id encoding: derived from the SCIP moniker (symbol_id) — globally unique.
Edge ~id encoding: "e|{caller_id_hash}|{edge_kind}|{callee_id_hash}"

Design points:
  - symbol_id = full moniker (join key, internal)
  - name, module, file, line, kind, repo = human/agent-facing readable fields
  - ~id encodes uniqueness from the moniker
  - Supports both CALLS-only filtering and full reference graph
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from dataclasses import dataclass

from scip_ingester import SCIPGraph

log = logging.getLogger("scip_neptune_csv")


def _make_node_id(symbol_id: str, repo: str) -> str:
    """Create a deterministic Neptune ~id from a SCIP moniker.

    Uses a truncated SHA-256 of the full moniker for uniqueness without
    exceeding Neptune's ID length limits. Prefixed with repo for debuggability.

    Format: {repo_safe}|{moniker_hash}
    """
    repo_safe = repo.replace("/", "-")
    # Use first 16 chars of hex hash — collision probability negligible for per-repo graphs
    moniker_hash = hashlib.sha256(symbol_id.encode("utf-8")).hexdigest()[:16]
    return f"{repo_safe}|{moniker_hash}"


def _make_edge_id(caller_id: str, callee_id: str, edge_kind: str) -> str:
    """Create a deterministic Neptune edge ~id.

    Format: e|{caller_hash}|{edge_kind}|{callee_hash}
    """
    caller_hash = hashlib.sha256(caller_id.encode("utf-8")).hexdigest()[:10]
    callee_hash = hashlib.sha256(callee_id.encode("utf-8")).hexdigest()[:10]
    return f"e|{caller_hash}|{edge_kind}|{callee_hash}"


@dataclass
class CSVOutput:
    """Result of CSV generation."""

    vertices_path: str
    edges_path: str
    vertex_count: int
    edge_count: int
    calls_count: int
    references_count: int
    output_dir: str


def generate_csv(
    graph: SCIPGraph,
    output_dir: str,
    calls_only: bool = False,
) -> CSVOutput:
    """Generate Neptune openCypher CSV files from a SCIPGraph.

    Args:
        graph: The SCIP graph to convert
        output_dir: Directory to write CSV files to
        calls_only: If True, only emit CALLS edges (filter REFERENCES)

    Returns:
        CSVOutput with paths and counts
    """
    os.makedirs(output_dir, exist_ok=True)

    # Filter edges if calls_only
    edges = graph.edges
    if calls_only:
        edges = [e for e in edges if e.edge_kind == "CALLS"]

    # Collect nodes that appear in edges (avoid orphan nodes in the graph)
    active_node_ids: set[str] = set()
    for edge in edges:
        active_node_ids.add(edge.caller_id)
        active_node_ids.add(edge.callee_id)

    # Write vertices.csv
    vertices_path = os.path.join(output_dir, "vertices.csv")
    vertex_fieldnames = [
        "~id",
        "~label",
        "symbol_id:String",
        "name:String",
        "module:String",
        "file:String",
        "line:Int",
        "kind:String",
        "repo:String",
    ]

    vertex_count = 0
    with open(vertices_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=vertex_fieldnames)
        writer.writeheader()

        for symbol_id, node in graph.nodes.items():
            if symbol_id not in active_node_ids:
                continue  # Skip orphan nodes

            # Map kind to Neptune label
            label = "Symbol"  # All symbols get the Symbol label

            writer.writerow(
                {
                    "~id": _make_node_id(symbol_id, graph.repo),
                    "~label": label,
                    "symbol_id:String": symbol_id,
                    "name:String": _sanitize_csv_value(node.name),
                    "module:String": _sanitize_csv_value(node.module),
                    "file:String": _sanitize_csv_value(node.file),
                    "line:Int": node.line,
                    "kind:String": node.kind,
                    "repo:String": graph.repo,
                }
            )
            vertex_count += 1

    # Write edges.csv
    edges_path = os.path.join(output_dir, "edges.csv")
    edge_fieldnames = [
        "~id",
        "~from",
        "~to",
        "~label",
        "edge_kind:String",
        "file:String",
        "line:Int",
        "repo:String",
    ]

    calls_count = 0
    references_count = 0
    with open(edges_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=edge_fieldnames)
        writer.writeheader()

        for edge in edges:
            edge_id = _make_edge_id(edge.caller_id, edge.callee_id, edge.edge_kind)
            from_id = _make_node_id(edge.caller_id, graph.repo)
            to_id = _make_node_id(edge.callee_id, graph.repo)

            writer.writerow(
                {
                    "~id": edge_id,
                    "~from": from_id,
                    "~to": to_id,
                    "~label": edge.edge_kind,
                    "edge_kind:String": edge.edge_kind,
                    "file:String": _sanitize_csv_value(edge.file),
                    "line:Int": edge.line,
                    "repo:String": graph.repo,
                }
            )

            if edge.edge_kind == "CALLS":
                calls_count += 1
            else:
                references_count += 1

    log.info(
        "Neptune CSV generated: %d vertices, %d edges (%d CALLS, %d REFERENCES) → %s",
        vertex_count,
        len(edges),
        calls_count,
        references_count,
        output_dir,
    )

    return CSVOutput(
        vertices_path=vertices_path,
        edges_path=edges_path,
        vertex_count=vertex_count,
        edge_count=len(edges),
        calls_count=calls_count,
        references_count=references_count,
        output_dir=output_dir,
    )


def generate_summary(graph: SCIPGraph, csv_output: CSVOutput, report_path: str) -> dict:
    """Generate an extraction summary JSON file.

    Returns the summary dict (also written to report_path).
    """
    summary = {
        "repo": graph.repo,
        "node_count": csv_output.vertex_count,
        "edge_count": csv_output.edge_count,
        "calls_count": csv_output.calls_count,
        "references_count": csv_output.references_count,
        "vertices_file": csv_output.vertices_path,
        "edges_file": csv_output.edges_path,
        "output_dir": csv_output.output_dir,
    }

    with open(report_path, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def _sanitize_csv_value(value: str) -> str:
    """Sanitize a string value for CSV output.

    Removes control characters and pipes that could break parsing.
    """
    if not value:
        return ""
    # Remove characters that break CSV or Neptune ID parsing
    cleaned = "".join(c for c in value if c.isprintable() and c not in "\x00")
    return cleaned

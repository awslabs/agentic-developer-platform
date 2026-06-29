#!/usr/bin/env python3
"""
FalkorDB → Neptune CSV Extractor

Extracts Function/Class nodes and CALLS edges from cgc's embedded FalkorDB,
remaps internal IDs to Neptune-compatible ~id format, and produces:
  - vertices.csv (Neptune Bulk Loader vertex format)
  - edges.csv (Neptune Bulk Loader edge format)

Proven in SPIKE-3 (Issue #1541): 521 nodes, 596 edges from Agent-Reach repo,
with zero collisions and full count conservation through to Neptune.

Usage:
    python extract_falkordb_to_csv.py --repo-name MyRepo --repo-prefix /path/to/repo/ --output-dir ./output/

Prerequisites:
    - cgc (codegraphcontext) installed and in PATH
    - cgc index already run on the target repo (FalkorDB populated)
"""

import argparse
import csv
import json
import os
import subprocess
import sys


def cgc_query(cgc_bin: str, cypher: str) -> list[dict]:
    """Run cgc query and parse the JSON array from stdout.

    Handles cgc's status-line prefix and control characters in string fields.
    """
    result = subprocess.run([cgc_bin, "query", cypher], capture_output=True, text=True, timeout=120)  # nosemgrep: dangerous-subprocess-use-audit
    stdout = result.stdout
    # Find start of JSON array (after status lines)
    start = stdout.find("\n[")
    if start != -1:
        start += 1
    else:
        start = stdout.find("[")
    if start == -1:
        print("ERROR: No JSON array found in query output", file=sys.stderr)
        return []

    json_str = stdout[start:].strip()
    try:
        return json.loads(json_str, strict=False)
    except json.JSONDecodeError as e:
        print(f"JSON parse error at pos {e.pos}: {e.msg}", file=sys.stderr)
        return []


def make_vertex_id(
    repo_name: str, repo_prefix: str, path: str, name: str, kind: str, line_number: int
) -> str:
    """Create deterministic Neptune ~id: repo|file|name|kind.

    For cross-repo uniqueness, the composite key includes repo name.
    Line number is used for disambiguation when repo|file|name|kind collides.
    """
    rel_path = path.replace(repo_prefix, "") if path else "unknown"
    # Sanitize: remove chars that break CSV or Neptune ID parsing
    clean_name = "".join(c for c in (name or "unnamed") if c.isprintable() and c not in '|,"')
    clean_name = clean_name.strip() or "unnamed"
    return f"{repo_name}|{rel_path}|{clean_name}|{kind}"


def extract(repo_name: str, repo_prefix: str, output_dir: str, cgc_bin: str = "cgc") -> dict:
    """Run the full extraction pipeline.

    Returns a summary dict with counts and gate status.
    """
    os.makedirs(output_dir, exist_ok=True)

    # --- PASS 1: Extract nodes ---
    print("Pass 1a: Extracting Function nodes...")
    functions = cgc_query(
        cgc_bin,
        "MATCH (n:Function) RETURN id(n) AS internal_id, n.name AS name, "
        "n.path AS path, n.line_number AS line_number",
    )
    print(f"  Functions: {len(functions)}")

    print("Pass 1b: Extracting Class nodes...")
    classes = cgc_query(
        cgc_bin,
        "MATCH (n:Class) RETURN id(n) AS internal_id, n.name AS name, "
        "n.path AS path, n.line_number AS line_number",
    )
    print(f"  Classes: {len(classes)}")

    # Build ID map and vertices
    id_map: dict[int, str] = {}
    vertices: list[dict] = []
    id_collisions = 0
    seen_ids: set[str] = set()

    def process_node(node: dict, kind: str):
        nonlocal id_collisions
        internal_id = node.get("internal_id")
        name = node.get("name") or "unnamed"
        path = node.get("path") or "unknown"
        line = node.get("line_number") or 0

        nid = make_vertex_id(repo_name, repo_prefix, path, name, kind, line)

        if nid in seen_ids:
            id_collisions += 1
            nid_with_line = f"{nid}:{line}"
            if nid_with_line in seen_ids:
                nid_with_line = f"{nid}:{internal_id}"
            nid = nid_with_line

        seen_ids.add(nid)
        id_map[internal_id] = nid

        rel_path = path.replace(repo_prefix, "") if path else "unknown"
        clean_name = "".join(c for c in (name or "") if c.isprintable())

        vertices.append(
            {
                "~id": nid,
                "~label": kind,
                "repo:String": repo_name,
                "file:String": rel_path,
                "name:String": clean_name,
                "kind:String": kind,
                "line:Int": int(line) if line else 0,
            }
        )

    for node in functions:
        process_node(node, "Function")
    for node in classes:
        process_node(node, "Class")

    # Write vertices.csv
    vertex_file = os.path.join(output_dir, "vertices.csv")
    with open(vertex_file, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "~id",
                "~label",
                "repo:String",
                "file:String",
                "name:String",
                "kind:String",
                "line:Int",
            ],
        )
        writer.writeheader()
        writer.writerows(vertices)
    print(f"  Written: {vertex_file} ({len(vertices)} rows)")

    # --- PASS 2: Extract CALLS edges ---
    print("\nPass 2: Extracting CALLS edges...")
    edges_raw = cgc_query(
        cgc_bin,
        "MATCH (a)-[r:CALLS]->(b) RETURN id(a) AS src_id, id(b) AS dst_id, "
        "a.name AS src_name, b.name AS dst_name, r.line_number AS call_line, "
        "r.source AS source",
    )
    print(f"  Raw CALLS edges: {len(edges_raw)}")

    edges: list[dict] = []
    dangling_dropped = 0

    for e in edges_raw:
        src_nid = id_map.get(e.get("src_id"))
        dst_nid = id_map.get(e.get("dst_id"))

        if src_nid is None or dst_nid is None:
            dangling_dropped += 1
            continue

        edges.append(
            {
                "~id": f"CALLS-{len(edges) + 1}",
                "~from": src_nid,
                "~to": dst_nid,
                "~label": "CALLS",
                "repo:String": repo_name,
                "call_line:Int": int(e.get("call_line") or 0),
                "source:String": e.get("source") or "unknown",
            }
        )

    # Write edges.csv
    edge_file = os.path.join(output_dir, "edges.csv")
    with open(edge_file, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "~id",
                "~from",
                "~to",
                "~label",
                "repo:String",
                "call_line:Int",
                "source:String",
            ],
        )
        writer.writeheader()
        writer.writerows(edges)
    print(f"  Written: {edge_file} ({len(edges)} rows)")
    print(f"  Dangling dropped: {dangling_dropped}")

    # Validation
    vertex_ids = [v["~id"] for v in vertices]
    unique_set = set(vertex_ids)
    vertex_id_set = set(vertex_ids)
    dangling_ep = sum(
        1 for e in edges if e["~from"] not in vertex_id_set or e["~to"] not in vertex_id_set
    )

    summary = {
        "repo": repo_name,
        "vertices_written": len(vertices),
        "edges_written": len(edges),
        "raw_edges": len(edges_raw),
        "dangling_dropped": dangling_dropped,
        "id_collisions_resolved": id_collisions,
        "final_ids_unique": len(vertex_ids) == len(unique_set),
        "dangling_endpoints_in_edges": dangling_ep,
        "vertex_file": vertex_file,
        "edge_file": edge_file,
    }

    summary_file = os.path.join(output_dir, "extraction_summary.json")
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def main():
    parser = argparse.ArgumentParser(description="Extract FalkorDB graph to Neptune CSV format")
    parser.add_argument("--repo-name", required=True, help="Repository name (used in ~id keys)")
    parser.add_argument(
        "--repo-prefix", required=True, help="Local path prefix to strip from file paths"
    )
    parser.add_argument("--output-dir", default="./neptune_csv", help="Output directory for CSVs")
    parser.add_argument("--cgc-bin", default="cgc", help="Path to cgc binary")
    args = parser.parse_args()

    # Ensure prefix ends with /
    prefix = args.repo_prefix if args.repo_prefix.endswith("/") else args.repo_prefix + "/"

    summary = extract(args.repo_name, prefix, args.output_dir, args.cgc_bin)

    print(f"\n{'=' * 60}")
    print("Extraction complete:")
    print(f"  Vertices: {summary['vertices_written']}")
    print(f"  Edges: {summary['edges_written']} (dropped {summary['dangling_dropped']} dangling)")
    print(f"  IDs unique: {summary['final_ids_unique']}")
    print(f"  Dangling endpoints: {summary['dangling_endpoints_in_edges']}")

    ok = summary["final_ids_unique"] and summary["dangling_endpoints_in_edges"] == 0
    print(f"\n  Status: {'✅ PASS' if ok else '❌ FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

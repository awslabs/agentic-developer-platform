#!/usr/bin/env python3
"""
Neptune CSV Loader via openCypher

Loads vertices.csv and edges.csv (produced by extract_falkordb_to_csv.py) into
Amazon Neptune using batched UNWIND MERGE statements over the openCypher HTTP API.

This is the fallback loading path when the Neptune Bulk Loader IAM role is not
configured. For production at scale (>10K edges), the Bulk Loader should be used.

Proven in SPIKE-3 (Issue #1541): 521 nodes + 596 edges loaded in ~19s, zero errors.

Usage:
    python load_csv_to_neptune.py --input-dir ./neptune_csv/ --neptune-endpoint <endpoint> --region us-east-1

Prerequisites:
    - AWS credentials with neptune-db:connect + neptune-db:WriteDataViaQuery
    - botocore installed (for SigV4 auth)
    - Neptune IAM auth enabled on the cluster
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.parse

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session


def neptune_query(
    neptune_url: str, region: str, cypher: str, parameters: dict | None = None
) -> dict:
    """Execute an openCypher query with IAM SigV4 authentication."""
    session = Session()
    credentials = session.get_credentials().get_frozen_credentials()

    form_data = {"query": cypher}
    if parameters:
        form_data["parameters"] = json.dumps(parameters)

    body = urllib.parse.urlencode(form_data).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    request = AWSRequest(method="POST", url=neptune_url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", region).add_auth(request)

    req = urllib.request.Request(
        neptune_url, data=body, headers=dict(request.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return {"error": error_body, "code": e.code}
    except Exception as ex:
        return {"error": str(ex), "code": 0}


def load_vertices(
    neptune_url: str, region: str, input_dir: str, batch_size: int = 50
) -> tuple[int, int]:
    """Load vertices into Neptune using batched UNWIND MERGE."""
    vertex_file = os.path.join(input_dir, "vertices.csv")
    vertices = []
    with open(vertex_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            vertices.append(row)

    total = len(vertices)
    print(f"Loading {total} vertices into Neptune...")

    functions = [v for v in vertices if v["~label"] == "Function"]
    classes = [v for v in vertices if v["~label"] == "Class"]

    loaded = 0
    errors = 0
    start_time = time.time()

    for label, nodes in [("Function", functions), ("Class", classes)]:
        print(f"  Loading {len(nodes)} {label} nodes...")
        for i in range(0, len(nodes), batch_size):
            batch = nodes[i : i + batch_size]
            params = [
                {
                    "id": n["~id"],
                    "repo": n["repo:String"],
                    "file": n["file:String"],
                    "name": n["name:String"],
                    "kind": n["kind:String"],
                    "line": int(n["line:Int"]),
                }
                for n in batch
            ]

            cypher = f"""
            UNWIND $nodes AS node
            MERGE (n:{label} {{`~id`: node.id}})
            SET n.repo = node.repo, n.file = node.file, n.name = node.name,
                n.kind = node.kind, n.line = node.line
            RETURN count(n) AS cnt
            """
            result = neptune_query(neptune_url, region, cypher, {"nodes": params})
            if "error" in result:
                errors += len(batch)
                print(f"    Batch error: {str(result['error'])[:150]}", file=sys.stderr)
            else:
                cnt = result.get("results", [{}])[0].get("cnt", 0)
                loaded += cnt

    elapsed = time.time() - start_time
    print(f"  Vertex loading: {loaded}/{total} loaded, {errors} errors ({elapsed:.1f}s)")
    return loaded, errors


def load_edges(
    neptune_url: str, region: str, input_dir: str, batch_size: int = 50
) -> tuple[int, int]:
    """Load edges into Neptune using batched UNWIND MERGE."""
    edge_file = os.path.join(input_dir, "edges.csv")
    edges = []
    with open(edge_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            edges.append(row)

    total = len(edges)
    print(f"\nLoading {total} edges into Neptune...")

    loaded = 0
    errors = 0
    start_time = time.time()

    for i in range(0, total, batch_size):
        batch = edges[i : i + batch_size]
        params = [
            {
                "id": e["~id"],
                "from_id": e["~from"],
                "to_id": e["~to"],
                "repo": e["repo:String"],
                "call_line": int(e["call_line:Int"]),
                "source": e["source:String"],
            }
            for e in batch
        ]

        cypher = """
        UNWIND $edges AS edge
        MATCH (a {`~id`: edge.from_id})
        MATCH (b {`~id`: edge.to_id})
        MERGE (a)-[r:CALLS {`~id`: edge.id}]->(b)
        SET r.repo = edge.repo, r.call_line = edge.call_line, r.source = edge.source
        RETURN count(r) AS cnt
        """
        result = neptune_query(neptune_url, region, cypher, {"edges": params})
        if "error" in result:
            errors += len(batch)
            print(f"    Batch error: {str(result['error'])[:150]}", file=sys.stderr)
        else:
            cnt = result.get("results", [{}])[0].get("cnt", 0)
            loaded += cnt

        if (i // batch_size) % 5 == 0 and i > 0:
            elapsed = time.time() - start_time
            print(f"    Progress: {i + len(batch)}/{total} edges, {loaded} loaded ({elapsed:.1f}s)")

    elapsed = time.time() - start_time
    print(f"  Edge loading: {loaded}/{total} loaded, {errors} errors ({elapsed:.1f}s)")
    return loaded, errors


def main():
    parser = argparse.ArgumentParser(description="Load Neptune CSVs via openCypher UNWIND")
    parser.add_argument(
        "--input-dir", required=True, help="Directory containing vertices.csv and edges.csv"
    )
    parser.add_argument(
        "--neptune-endpoint", required=True, help="Neptune cluster endpoint (host:port)"
    )
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--batch-size", type=int, default=50, help="Nodes/edges per UNWIND batch")
    args = parser.parse_args()

    neptune_url = f"https://{args.neptune_endpoint}/opencypher"

    # Test connectivity
    print(f"Connecting to Neptune: {args.neptune_endpoint}")
    result = neptune_query(neptune_url, args.region, "RETURN 1 AS alive")
    if "error" in result:
        print(f"FATAL: Cannot connect to Neptune: {result}", file=sys.stderr)
        return 1
    print("Connected ✅\n")

    v_loaded, v_errors = load_vertices(neptune_url, args.region, args.input_dir, args.batch_size)
    e_loaded, e_errors = load_edges(neptune_url, args.region, args.input_dir, args.batch_size)

    total_errors = v_errors + e_errors
    print(f"\n{'=' * 60}")
    print(f"Load complete: {v_loaded} vertices + {e_loaded} edges, {total_errors} errors")
    print(f"Status: {'✅ PASS' if total_errors == 0 else '❌ FAIL'}")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

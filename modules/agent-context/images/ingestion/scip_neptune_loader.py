"""Neptune loader for SCIP-generated CSV files.

Loads vertices.csv and edges.csv into Neptune using batched openCypher UNWIND
MERGE statements. Also supports uploading to S3 for later Bulk Loader use.

Two loading paths:
  1. openCypher UNWIND batch (this module) — immediate, works without Bulk Loader IAM
  2. S3 → Bulk Loader API (when neptune-s3-loader IAM role is attached per #1531)

The UNWIND batch path is the default until Bulk Loader IAM is wired.
Proven in SPIKE-3: 521 nodes + 596 edges in ~19s, zero errors.
"""

from __future__ import annotations

import csv
import json
import logging
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession

from scip_neptune_csv import CSVOutput

log = logging.getLogger("scip_neptune_loader")


def _neptune_query(
    neptune_url: str, region: str, cypher: str, parameters: dict | None = None
) -> dict:
    """Execute an openCypher query with IAM SigV4 authentication."""
    session = BotocoreSession()
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


def clear_repo_graph(neptune_url: str, region: str, repo: str) -> bool:
    """Delete all existing nodes/edges for a repo before re-indexing.

    Neptune re-index strategy: scoped delete then bulk-load (per design doc D16).
    """
    cypher = "MATCH (n) WHERE n.repo = $repo DETACH DELETE n RETURN count(n) AS deleted"
    result = _neptune_query(neptune_url, region, cypher, {"repo": repo})
    if "error" in result:
        log.warning("Failed to clear graph for %s: %s", repo, result["error"][:200])
        return False
    deleted = result.get("results", [{}])[0].get("deleted", 0)
    log.info("Cleared %d nodes for repo %s", deleted, repo)
    return True


def load_to_neptune(
    csv_output: CSVOutput,
    neptune_endpoint: str,
    region: str,
    batch_size: int = 50,
    clear_existing: bool = True,
) -> dict:
    """Load CSV files into Neptune via openCypher UNWIND batch.

    Args:
        csv_output: Output from scip_neptune_csv.generate_csv()
        neptune_endpoint: Neptune cluster endpoint (host:port)
        region: AWS region
        batch_size: Nodes/edges per UNWIND batch
        clear_existing: Whether to delete existing repo graph first

    Returns:
        Dict with load results: vertices_loaded, edges_loaded, errors
    """
    neptune_url = f"https://{neptune_endpoint}/opencypher"

    # Test connectivity
    result = _neptune_query(neptune_url, region, "RETURN 1 AS alive")
    if "error" in result:
        log.error("Cannot connect to Neptune at %s: %s", neptune_endpoint, result)
        return {"error": "connection_failed", "detail": str(result)}

    # Read repo from CSV to determine which graph to clear
    repo = ""
    with open(csv_output.vertices_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            repo = row.get("repo:String", "")
            break

    # Clear existing graph for this repo
    if clear_existing and repo:
        clear_repo_graph(neptune_url, region, repo)

    # Load vertices
    vertices_loaded, v_errors = _load_vertices(
        neptune_url, region, csv_output.vertices_path, batch_size
    )

    # Load edges
    edges_loaded, e_errors = _load_edges(neptune_url, region, csv_output.edges_path, batch_size)

    total_errors = v_errors + e_errors
    result = {
        "vertices_loaded": vertices_loaded,
        "edges_loaded": edges_loaded,
        "total_errors": total_errors,
        "success": total_errors == 0,
    }

    if total_errors == 0:
        log.info(
            "Neptune load complete: %d vertices + %d edges (0 errors)",
            vertices_loaded,
            edges_loaded,
        )
    else:
        log.warning(
            "Neptune load completed with errors: %d vertices + %d edges, %d errors",
            vertices_loaded,
            edges_loaded,
            total_errors,
        )

    return result


def _load_vertices(
    neptune_url: str, region: str, vertices_path: str, batch_size: int
) -> tuple[int, int]:
    """Load vertices into Neptune using batched UNWIND MERGE."""
    vertices = []
    with open(vertices_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            vertices.append(row)

    total = len(vertices)
    log.info("Loading %d vertices into Neptune...", total)

    loaded = 0
    errors = 0

    for i in range(0, total, batch_size):
        batch = vertices[i : i + batch_size]
        params = [
            {
                "id": v["~id"],
                "symbol_id": v["symbol_id:String"],
                "name": v["name:String"],
                "module": v["module:String"],
                "file": v["file:String"],
                "line": int(v["line:Int"]),
                "kind": v["kind:String"],
                "repo": v["repo:String"],
            }
            for v in batch
        ]

        cypher = """
        UNWIND $nodes AS node
        MERGE (n:Symbol {`~id`: node.id})
        SET n.symbol_id = node.symbol_id, n.name = node.name,
            n.module = node.module, n.file = node.file,
            n.line = node.line, n.kind = node.kind, n.repo = node.repo
        RETURN count(n) AS cnt
        """
        result = _neptune_query(neptune_url, region, cypher, {"nodes": params})
        if "error" in result:
            errors += len(batch)
            log.warning("Vertex batch error: %s", str(result["error"])[:150])
        else:
            cnt = result.get("results", [{}])[0].get("cnt", 0)
            loaded += cnt

    log.info("Vertex loading: %d/%d loaded, %d errors", loaded, total, errors)
    return loaded, errors


def _load_edges(neptune_url: str, region: str, edges_path: str, batch_size: int) -> tuple[int, int]:
    """Load edges into Neptune using batched UNWIND MERGE."""
    edges = []
    with open(edges_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            edges.append(row)

    total = len(edges)
    log.info("Loading %d edges into Neptune...", total)

    loaded = 0
    errors = 0

    # Group by edge label for type-specific MERGE
    calls_edges = [e for e in edges if e["~label"] == "CALLS"]
    ref_edges = [e for e in edges if e["~label"] == "REFERENCES"]

    for label, edge_batch in [("CALLS", calls_edges), ("REFERENCES", ref_edges)]:
        for i in range(0, len(edge_batch), batch_size):
            batch = edge_batch[i : i + batch_size]
            params = [
                {
                    "id": e["~id"],
                    "from_id": e["~from"],
                    "to_id": e["~to"],
                    "file": e["file:String"],
                    "line": int(e["line:Int"]),
                    "repo": e["repo:String"],
                }
                for e in batch
            ]

            cypher = f"""
            UNWIND $edges AS edge
            MATCH (a {{`~id`: edge.from_id}})
            MATCH (b {{`~id`: edge.to_id}})
            MERGE (a)-[r:{label} {{`~id`: edge.id}}]->(b)
            SET r.file = edge.file, r.line = edge.line, r.repo = edge.repo
            RETURN count(r) AS cnt
            """
            result = _neptune_query(neptune_url, region, cypher, {"edges": params})
            if "error" in result:
                errors += len(batch)
                log.warning("Edge batch error (%s): %s", label, str(result["error"])[:150])
            else:
                cnt = result.get("results", [{}])[0].get("cnt", 0)
                loaded += cnt

    log.info("Edge loading: %d/%d loaded, %d errors", loaded, total, errors)
    return loaded, errors


def upload_csv_to_s3(
    csv_output: CSVOutput,
    s3_bucket: str,
    repo: str,
    region: str,
) -> dict:
    """Upload Neptune CSV files to S3 for Bulk Loader use.

    S3 staging path: s3://{bucket}/neptune-bulk-load/{repo_safe}/{timestamp}/

    Returns dict with S3 paths.
    """
    from datetime import datetime, timezone

    repo_safe = repo.replace("/", "-")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s3_prefix = f"neptune-bulk-load/{repo_safe}/{timestamp}"

    s3_client = boto3.client("s3", region_name=region)

    results = {}
    for local_path, name in [
        (csv_output.vertices_path, "vertices.csv"),
        (csv_output.edges_path, "edges.csv"),
    ]:
        s3_key = f"{s3_prefix}/{name}"
        try:
            s3_client.upload_file(local_path, s3_bucket, s3_key)
            results[name] = f"s3://{s3_bucket}/{s3_key}"
            log.info("Uploaded %s to s3://%s/%s", name, s3_bucket, s3_key)
        except Exception as e:
            log.error("S3 upload failed for %s: %s", name, e)
            results[name] = f"error: {e}"

    results["s3_prefix"] = f"s3://{s3_bucket}/{s3_prefix}/"
    return results

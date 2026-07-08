"""Neptune loader for SCIP-generated CSV files.

Loads vertices.csv and edges.csv into Neptune. Two loading paths:
  1. Neptune Bulk Loader API (default when NEPTUNE_BULK_LOAD_ROLE_ARN is set) —
     POST /loader with S3 source, format=csv (Gremlin ~id/~label headers).
     9–24s per file, zero errors at 500k+ records (#3233).
  2. openCypher UNWIND batch (fallback when role ARN unset) — immediate, no IAM
     needed beyond pod-level neptune-db:WriteDataViaQuery.

Also supports uploading to S3 for Bulk Loader use (upload_csv_to_s3).
"""

from __future__ import annotations

import csv
import json
import logging
import random
import time
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession

from scip_neptune_csv import CSVOutput

log = logging.getLogger("scip_neptune_loader")

# Retry configuration for transient Neptune errors (#3173)
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0

# Errors that indicate a transient failure (DB restart, throttle, OOM)
_RETRYABLE_ERROR_PATTERNS = (
    "Connection refused",
    "timed out",
    "Remote end closed",
    "MemoryLimitExceededException",
    "ConcurrentModificationException",
    "ThrottlingException",
)
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


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


def _is_retryable(result: dict) -> bool:
    """Determine if a Neptune query result represents a retryable error."""
    if "error" not in result:
        return False
    error_str = str(result.get("error", ""))
    code = result.get("code", 0)
    # HTTP status codes that are retryable
    if code in _RETRYABLE_HTTP_CODES:
        return True
    # Error message patterns that are retryable
    for pattern in _RETRYABLE_ERROR_PATTERNS:
        if pattern in error_str:
            return True
    return False


def _neptune_query_with_retry(
    neptune_url: str, region: str, cypher: str, parameters: dict | None = None
) -> dict:
    """Execute a Neptune query with bounded retry and exponential backoff+jitter.

    Retries up to MAX_RETRIES times for transient errors (connection refused,
    timeouts, 5xx, 429, MemoryLimitExceededException). Non-retryable errors
    (4xx query errors) return immediately.
    """
    for attempt in range(MAX_RETRIES + 1):
        result = _neptune_query(neptune_url, region, cypher, parameters)
        if "error" not in result:
            return result
        if not _is_retryable(result):
            return result
        if attempt == MAX_RETRIES:
            # Exhausted retries — return the last error
            return result
        # Exponential backoff with jitter: base * 2^attempt + random jitter
        backoff = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 1)
        log.warning(
            "Retryable Neptune error (attempt %d/%d), backoff %.1fs: %s",
            attempt + 1,
            MAX_RETRIES,
            backoff,
            str(result["error"])[:100],
        )
        time.sleep(backoff)
    return result  # Should not reach here, but satisfies type checker


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
    batch_size: int = 200,
    clear_existing: bool = True,
) -> dict:
    """Load CSV files into Neptune via openCypher UNWIND batch.

    Args:
        csv_output: Output from scip_neptune_csv.generate_csv()
        neptune_endpoint: Neptune cluster endpoint (host:port)
        region: AWS region
        batch_size: Nodes/edges per UNWIND batch (default 200 — reduced from 400 to
            lower per-query memory on serverless Neptune, #3173)
        clear_existing: Whether to delete existing repo graph first

    Returns:
        Dict with load results: vertices_loaded, edges_loaded, errors, error_rate
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
    total_attempted = (vertices_loaded + v_errors) + (edges_loaded + e_errors)
    error_rate = total_errors / total_attempted if total_attempted > 0 else 0.0
    result = {
        "vertices_loaded": vertices_loaded,
        "edges_loaded": edges_loaded,
        "total_errors": total_errors,
        "error_rate": error_rate,
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
                "tenant_id": v.get("tenant_id:String") or None,
                "owner_sub": v.get("owner_sub:String") or None,
            }
            for v in batch
        ]

        cypher = """
        UNWIND $nodes AS node
        MERGE (n:Symbol {`~id`: node.id})
        SET n.symbol_id = node.symbol_id, n.name = node.name,
            n.module = node.module, n.file = node.file,
            n.line = node.line, n.kind = node.kind, n.repo = node.repo,
            n.tenant_id = node.tenant_id, n.owner_sub = node.owner_sub
        RETURN count(n) AS cnt
        """
        result = _neptune_query_with_retry(neptune_url, region, cypher, {"nodes": params})
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
            result = _neptune_query_with_retry(neptune_url, region, cypher, {"edges": params})
            if "error" in result:
                errors += len(batch)
                log.warning("Edge batch error (%s): %s", label, str(result["error"])[:150])
            else:
                cnt = result.get("results", [{}])[0].get("cnt", 0)
                loaded += cnt

    log.info("Edge loading: %d/%d loaded, %d errors", loaded, total, errors)
    return loaded, errors


# ─── Neptune Bulk Loader API ─────────────────────────────────────────────────
# Uses the Neptune Bulk Loader (POST /loader) to load CSV files from S3.
# Requires an IAM role attached to the Neptune cluster with S3 read access.
# Format MUST be "csv" (Gremlin-style ~id/~label headers), NOT "opencypher".
# Proven in 2026-07-07 incident recovery: 222k+293k vertices, 523k+630k edges
# in 9–24 seconds each, zero errors (#3233).

BULK_LOADER_POLL_INTERVAL_SECONDS = 5
BULK_LOADER_TIMEOUT_SECONDS = 600  # 10 minutes


def _neptune_http_request(
    url: str, region: str, method: str = "GET", body: bytes | None = None
) -> dict:
    """Make a SigV4-signed HTTP request to Neptune (non-openCypher endpoints)."""
    session = BotocoreSession()
    credentials = session.get_credentials().get_frozen_credentials()

    headers = {}
    if body:
        headers["Content-Type"] = "application/json"

    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", region).add_auth(request)

    req = urllib.request.Request(url, data=body, headers=dict(request.headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return {"error": error_body, "code": e.code}
    except Exception as ex:
        return {"error": str(ex), "code": 0}


def _start_bulk_load(
    neptune_endpoint: str,
    region: str,
    s3_source: str,
    iam_role_arn: str,
) -> dict:
    """Start a Neptune Bulk Loader job.

    Args:
        neptune_endpoint: Neptune host:port
        s3_source: S3 URI of the CSV file to load
        iam_role_arn: IAM role ARN for Neptune to assume for S3 read
        region: AWS region

    Returns:
        Response dict with loadId on success, or error details.
    """
    url = f"https://{neptune_endpoint}/loader"
    payload = {
        "source": s3_source,
        "format": "csv",
        "iamRoleArn": iam_role_arn,
        "region": region,
        "failOnError": "FALSE",
        "parallelism": "MEDIUM",
        "queueRequest": "TRUE",
    }
    body = json.dumps(payload).encode("utf-8")
    return _neptune_http_request(url, region, method="POST", body=body)


def _poll_bulk_load(
    neptune_endpoint: str,
    region: str,
    load_id: str,
) -> dict:
    """Poll a Neptune Bulk Loader job until completion or timeout.

    Returns:
        Dict with status (LOAD_COMPLETED or LOAD_FAILED) and details.
    """
    url = f"https://{neptune_endpoint}/loader/{load_id}"
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > BULK_LOADER_TIMEOUT_SECONDS:
            return {
                "status": "TIMEOUT",
                "error": f"Bulk load {load_id} timed out after {BULK_LOADER_TIMEOUT_SECONDS}s",
                "load_id": load_id,
            }

        result = _neptune_http_request(url, region, method="GET")
        if "error" in result:
            # Transient HTTP error during poll — retry
            log.warning("Poll error for load %s: %s", load_id, str(result["error"])[:100])
            time.sleep(BULK_LOADER_POLL_INTERVAL_SECONDS)
            continue

        # Neptune returns: {"status": "200 OK", "payload": {"loadStatus": {...}}}
        payload = result.get("payload", {})
        overall_status = payload.get("overallStatus", {})
        status = overall_status.get("status", "")

        if status == "LOAD_COMPLETED":
            return {
                "status": "LOAD_COMPLETED",
                "load_id": load_id,
                "total_records": overall_status.get("totalRecords", 0),
                "total_time_spent": overall_status.get("totalTimeSpent", 0),
                "errors": overall_status.get("errors", {}).get("errorCount", 0),
            }
        elif status == "LOAD_FAILED":
            feed_url = f"{url}?details=true&errors=true"
            error_detail = _neptune_http_request(feed_url, region, method="GET")
            return {
                "status": "LOAD_FAILED",
                "load_id": load_id,
                "error": f"Bulk load failed: {overall_status}",
                "error_detail": error_detail,
            }
        elif status in ("LOAD_NOT_STARTED", "LOAD_IN_PROGRESS"):
            time.sleep(BULK_LOADER_POLL_INTERVAL_SECONDS)
        else:
            # Unknown status — keep polling
            log.warning("Unknown bulk load status for %s: %s", load_id, status)
            time.sleep(BULK_LOADER_POLL_INTERVAL_SECONDS)


def load_via_bulk_loader(
    s3_prefix: str,
    neptune_endpoint: str,
    region: str,
    iam_role_arn: str,
    repo: str,
    clear_existing: bool = True,
) -> dict:
    """Load CSV files into Neptune via the Bulk Loader API.

    Loads vertices then edges sequentially (edges reference vertex ~id values).

    Args:
        s3_prefix: S3 URI prefix containing vertices.csv and edges.csv
        neptune_endpoint: Neptune cluster endpoint (host:port)
        region: AWS region
        iam_role_arn: IAM role ARN attached to Neptune cluster for S3 read
        repo: Repository name (org/repo) for graph clearing
        clear_existing: Whether to delete existing repo graph first

    Returns:
        Dict with load results: method, vertices/edges status, errors.
    """
    neptune_url = f"https://{neptune_endpoint}/opencypher"

    # Test connectivity
    result = _neptune_query(neptune_url, region, "RETURN 1 AS alive")
    if "error" in result:
        log.error("Cannot connect to Neptune at %s: %s", neptune_endpoint, result)
        return {"error": "connection_failed", "detail": str(result), "method": "bulk_loader"}

    # Clear existing graph for this repo
    if clear_existing and repo:
        clear_repo_graph(neptune_url, region, repo)

    # Load vertices first (edges reference them)
    vertices_s3 = f"{s3_prefix}vertices.csv"
    log.info("Starting bulk load: vertices from %s", vertices_s3)
    v_start = _start_bulk_load(neptune_endpoint, region, vertices_s3, iam_role_arn)

    if "error" in v_start:
        log.error("Failed to start vertex bulk load: %s", v_start)
        return {
            "error": "vertex_load_start_failed",
            "detail": str(v_start),
            "method": "bulk_loader",
            "success": False,
        }

    v_load_id = v_start.get("payload", {}).get("loadId", "")
    if not v_load_id:
        log.error("No loadId in vertex bulk load response: %s", v_start)
        return {
            "error": "no_load_id",
            "detail": str(v_start),
            "method": "bulk_loader",
            "success": False,
        }

    log.info("Vertex bulk load started: loadId=%s", v_load_id)
    v_result = _poll_bulk_load(neptune_endpoint, region, v_load_id)

    if v_result["status"] != "LOAD_COMPLETED":
        log.error("Vertex bulk load failed: %s", v_result)
        return {
            "error": "vertex_load_failed",
            "detail": v_result,
            "method": "bulk_loader",
            "success": False,
            "vertices_status": v_result["status"],
        }

    log.info(
        "Vertex bulk load complete: %d records in %ds",
        v_result.get("total_records", 0),
        v_result.get("total_time_spent", 0),
    )

    # Load edges (vertices must exist first)
    edges_s3 = f"{s3_prefix}edges.csv"
    log.info("Starting bulk load: edges from %s", edges_s3)
    e_start = _start_bulk_load(neptune_endpoint, region, edges_s3, iam_role_arn)

    if "error" in e_start:
        log.error("Failed to start edge bulk load: %s", e_start)
        return {
            "error": "edge_load_start_failed",
            "detail": str(e_start),
            "method": "bulk_loader",
            "success": False,
            "vertices_loaded": v_result.get("total_records", 0),
        }

    e_load_id = e_start.get("payload", {}).get("loadId", "")
    if not e_load_id:
        log.error("No loadId in edge bulk load response: %s", e_start)
        return {
            "error": "no_load_id",
            "detail": str(e_start),
            "method": "bulk_loader",
            "success": False,
            "vertices_loaded": v_result.get("total_records", 0),
        }

    log.info("Edge bulk load started: loadId=%s", e_load_id)
    e_result = _poll_bulk_load(neptune_endpoint, region, e_load_id)

    if e_result["status"] != "LOAD_COMPLETED":
        log.error("Edge bulk load failed: %s", e_result)
        return {
            "error": "edge_load_failed",
            "detail": e_result,
            "method": "bulk_loader",
            "success": False,
            "vertices_loaded": v_result.get("total_records", 0),
            "edges_status": e_result["status"],
        }

    log.info(
        "Edge bulk load complete: %d records in %ds",
        e_result.get("total_records", 0),
        e_result.get("total_time_spent", 0),
    )

    total_errors = v_result.get("errors", 0) + e_result.get("errors", 0)
    return {
        "method": "bulk_loader",
        "success": total_errors == 0,
        "vertices_loaded": v_result.get("total_records", 0),
        "edges_loaded": e_result.get("total_records", 0),
        "total_errors": total_errors,
        "error_rate": 0.0,
        "vertices_load_id": v_load_id,
        "edges_load_id": e_load_id,
        "vertices_time_s": v_result.get("total_time_spent", 0),
        "edges_time_s": e_result.get("total_time_spent", 0),
    }


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

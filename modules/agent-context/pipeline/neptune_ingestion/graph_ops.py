"""Neptune graph operations for per-repo subgraph isolation.

Implements scoped delete-then-load: before bulk-loading a repo's CSV,
delete its existing subgraph (all nodes + edges with matching `repo` property).

Design doc: docs/agent-context/neptune-deep-graph-design.md (D18)
EPIC: #1529, Story: #1533
"""

from __future__ import annotations

import logging
import time

from pipeline.neptune_ingestion.load_csv_to_neptune import neptune_query

log = logging.getLogger(__name__)


def delete_repo_subgraph(neptune_url: str, region: str, repo: str) -> dict:
    """Delete all nodes and edges belonging to a repo.

    Executes: MATCH (n) WHERE n.repo = $repo DETACH DELETE n

    DETACH DELETE removes both the matched nodes AND all edges connected to them.
    Since edges carry repo = source node's repo, this correctly removes:
    - All nodes with repo = R
    - All edges originating from repo R (outgoing)
    - Any edges pointing TO repo R's nodes (incoming from other repos)

    Cross-repo edges from OTHER repos pointing at R's nodes are removed here,
    but that's fine because cross-repo resolution is query-time via symbol_id
    (not materialized edges). See design doc D12.

    Args:
        neptune_url: Neptune openCypher endpoint URL
        region: AWS region for SigV4 signing
        repo: Repository identifier (e.g., "aws-e/adp")

    Returns:
        Dict with keys: success (bool), deleted_nodes (int), elapsed_ms (int), error (str|None)
    """
    start = time.time()

    # Neptune openCypher supports parameterized queries
    cypher = "MATCH (n) WHERE n.repo = $repo DETACH DELETE n"
    result = neptune_query(neptune_url, region, cypher, {"repo": repo})

    elapsed_ms = int((time.time() - start) * 1000)

    if "error" in result:
        log.warning(
            "delete_repo_subgraph failed for %s: %s (HTTP %s, %dms)",
            repo,
            str(result["error"])[:200],
            result.get("code", "?"),
            elapsed_ms,
        )
        return {
            "success": False,
            "deleted_nodes": 0,
            "elapsed_ms": elapsed_ms,
            "error": str(result["error"])[:500],
        }

    log.info("delete_repo_subgraph completed for %s (%dms)", repo, elapsed_ms)
    return {
        "success": True,
        "deleted_nodes": -1,  # Neptune DETACH DELETE doesn't return count
        "elapsed_ms": elapsed_ms,
        "error": None,
    }


def count_repo_nodes(neptune_url: str, region: str, repo: str) -> int:
    """Count all nodes belonging to a repo.

    Useful for validation: after delete, count should be 0;
    after load, count should match CSV vertex count.

    Args:
        neptune_url: Neptune openCypher endpoint URL
        region: AWS region for SigV4 signing
        repo: Repository identifier

    Returns:
        Node count, or -1 on error.
    """
    cypher = "MATCH (n) WHERE n.repo = $repo RETURN count(n) AS cnt"
    result = neptune_query(neptune_url, region, cypher, {"repo": repo})

    if "error" in result:
        log.warning("count_repo_nodes failed for %s: %s", repo, str(result["error"])[:200])
        return -1

    # Neptune openCypher returns results in a nested structure
    try:
        results = result.get("results", [])
        if results:
            return int(results[0].get("cnt", 0))
    except (IndexError, KeyError, TypeError, ValueError):
        pass

    return 0


def backfill_tenant_id(
    neptune_url: str,
    region: str,
    repo: str,
    tenant_id: str,
    owner_sub: str | None = None,
) -> dict:
    """Backfill tenant_id (and optionally owner_sub) on existing nodes for a repo.

    Stamps all nodes with repo = $repo that currently have no tenant_id.
    This is idempotent — nodes that already carry tenant_id are left unchanged.

    Used during migration to stamp existing graph data with tenant scope
    so that the query-side scope filter can take effect without breaking
    shared-corpus semantics (Story 6 / #1775).

    Args:
        neptune_url: Neptune openCypher endpoint URL
        region: AWS region for SigV4 signing
        repo: Repository identifier (e.g., "aws-e/adp")
        tenant_id: Tenant identifier to stamp on unscoped nodes
        owner_sub: Optional owner subject identifier

    Returns:
        Dict with keys: success (bool), updated_nodes (int), elapsed_ms (int), error (str|None)
    """
    start = time.time()

    # Only stamp nodes that don't already have a tenant_id
    cypher = "MATCH (n) WHERE n.repo = $repo AND n.tenant_id IS NULL SET n.tenant_id = $tenant_id"
    params: dict[str, str] = {"repo": repo, "tenant_id": tenant_id}

    if owner_sub:
        cypher = (
            "MATCH (n) WHERE n.repo = $repo AND n.tenant_id IS NULL "
            "SET n.tenant_id = $tenant_id, n.owner_sub = $owner_sub"
        )
        params["owner_sub"] = owner_sub

    result = neptune_query(neptune_url, region, cypher, params)

    elapsed_ms = int((time.time() - start) * 1000)

    if "error" in result:
        log.warning(
            "backfill_tenant_id failed for %s: %s (HTTP %s, %dms)",
            repo,
            str(result["error"])[:200],
            result.get("code", "?"),
            elapsed_ms,
        )
        return {
            "success": False,
            "updated_nodes": 0,
            "elapsed_ms": elapsed_ms,
            "error": str(result["error"])[:500],
        }

    log.info(
        "backfill_tenant_id completed for %s tenant=%s (%dms)",
        repo,
        tenant_id,
        elapsed_ms,
    )
    return {
        "success": True,
        "updated_nodes": -1,  # Neptune SET doesn't return affected count
        "elapsed_ms": elapsed_ms,
        "error": None,
    }


def count_repo_edges(neptune_url: str, region: str, repo: str) -> int:
    """Count all edges belonging to a repo (edges where repo property = repo).

    Args:
        neptune_url: Neptune openCypher endpoint URL
        region: AWS region for SigV4 signing
        repo: Repository identifier

    Returns:
        Edge count, or -1 on error.
    """
    cypher = "MATCH ()-[r]->() WHERE r.repo = $repo RETURN count(r) AS cnt"
    result = neptune_query(neptune_url, region, cypher, {"repo": repo})

    if "error" in result:
        log.warning("count_repo_edges failed for %s: %s", repo, str(result["error"])[:200])
        return -1

    try:
        results = result.get("results", [])
        if results:
            return int(results[0].get("cnt", 0))
    except (IndexError, KeyError, TypeError, ValueError):
        pass

    return 0

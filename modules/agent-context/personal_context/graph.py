"""Neptune graph client for personal-context relationships.

Persists relationships detected by synthesis (#3.1) as Neptune edges and
provides owner-filtered traversal for graph-aware recall. Gated behind
``personal_context_graph_enabled`` — when disabled (default), all public
functions are no-ops or return empty results.

Graph model (Neptune property graph):
- Vertices: one per learning/synthesis/pattern, id = entry ULID.
  Mandatory properties: owner_sub, tenant_id, type, persona, visibility.
- Edges: derived_from, contradicts, supports, exemplifies, cross_persona.
- Every traversal filters on owner_sub == caller OR
  (visibility == 'shared' AND tenant_id == caller_tenant).

Authentication: IAM database auth via SigV4-signed requests (IRSA, no
stored credential).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .identity import CallerIdentity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PERSONAL_CONTEXT_GRAPH_ENABLED = (
    os.environ.get("PERSONAL_CONTEXT_GRAPH_ENABLED", "false").lower() == "true"
)
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT = int(os.environ.get("NEPTUNE_PORT", "8182"))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Valid edge types from schema-pack.yml
VALID_EDGE_TYPES = frozenset(
    {"derived_from", "contradicts", "supports", "exemplifies", "cross_persona"}
)


# ---------------------------------------------------------------------------
# Neptune HTTP Client (IAM SigV4 auth)
# ---------------------------------------------------------------------------


def _get_neptune_url() -> str:
    """Build the Neptune Gremlin HTTP endpoint URL."""
    return f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"


def _sign_request(method: str, url: str, body: str | None = None) -> dict[str, str]:
    """Sign a Neptune request with IAM SigV4.

    Falls back to plain headers if botocore is unavailable or signing fails.
    """
    headers = {"Content-Type": "application/json"}
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.session import Session as BotocoreSession

        session = BotocoreSession()
        creds = session.get_credentials()
        if creds:
            creds = creds.get_frozen_credentials()
            request = AWSRequest(method=method, url=url, headers=headers, data=body)
            SigV4Auth(creds, "neptune-db", AWS_REGION).add_auth(request)
            return dict(request.headers)
    except ImportError:
        logger.debug("botocore not available - sending unsigned Neptune request")
    except Exception as e:
        logger.warning("SigV4 signing failed for Neptune: %s", e)
    return headers


def _execute_gremlin(query: str) -> dict[str, Any] | None:
    """Execute a Gremlin query against Neptune via HTTP API.

    Returns the response JSON or None on failure. Never raises — all errors
    are logged and swallowed (graceful fallback).
    """
    import httpx

    url = _get_neptune_url()
    body = json.dumps({"gremlin": query})
    headers = _sign_request("POST", url, body)

    try:
        resp = httpx.post(url, content=body, headers=headers, timeout=30.0, verify=False)
        if resp.status_code >= 400:
            logger.warning(
                "Neptune query failed: HTTP %d - %s",
                resp.status_code,
                resp.text[:200],
            )
            return None
        return resp.json()
    except Exception as e:
        logger.warning("Neptune request failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_graph_enabled() -> bool:
    """Check whether the personal-context graph feature is enabled."""
    return PERSONAL_CONTEXT_GRAPH_ENABLED and bool(NEPTUNE_ENDPOINT)


def upsert_vertex(
    entry_id: str,
    owner_sub: str,
    tenant_id: str,
    entry_type: str,
    persona: str,
    visibility: str,
) -> bool:
    """Upsert a vertex in Neptune for a personal-context entry.

    Every vertex MUST carry owner_sub and tenant_id (isolation invariant).

    Parameters
    ----------
    entry_id:
        ULID of the entry (used as the vertex id property).
    owner_sub:
        Cognito sub (UUID) — mandatory for isolation.
    tenant_id:
        Tenant/org ID — mandatory for shared-visibility traversals.
    entry_type:
        One of: learning, synthesis, pattern.
    persona:
        One of: operations, developer, architect, reviewer.
    visibility:
        One of: private, shared.

    Returns
    -------
    True if the vertex was written successfully, False otherwise.
    """
    if not is_graph_enabled():
        return False

    if not owner_sub or not tenant_id:
        logger.error("upsert_vertex called without owner_sub/tenant_id — refusing")
        return False

    # Escape single quotes in values for Gremlin query safety
    def esc(val: str) -> str:
        return val.replace("'", "\\'")

    query = (
        f"g.V().has('entry_id', '{esc(entry_id)}').fold()"
        f".coalesce(unfold(), addV('personal_context').property('entry_id', '{esc(entry_id)}'))"
        f".property('owner_sub', '{esc(owner_sub)}')"
        f".property('tenant_id', '{esc(tenant_id)}')"
        f".property('type', '{esc(entry_type)}')"
        f".property('persona', '{esc(persona)}')"
        f".property('visibility', '{esc(visibility)}')"
    )

    result = _execute_gremlin(query)
    return result is not None


def add_edge(
    from_entry_id: str,
    to_entry_id: str,
    edge_type: str,
    properties: dict[str, str] | None = None,
) -> bool:
    """Add an edge between two personal-context vertices.

    Parameters
    ----------
    from_entry_id:
        ULID of the source vertex.
    to_entry_id:
        ULID of the target vertex.
    edge_type:
        One of: derived_from, contradicts, supports, exemplifies, cross_persona.
    properties:
        Optional edge properties (e.g. transfer_context for cross_persona).

    Returns
    -------
    True if the edge was written successfully, False otherwise.
    """
    if not is_graph_enabled():
        return False

    if edge_type not in VALID_EDGE_TYPES:
        logger.error("Invalid edge type: %r (must be one of %s)", edge_type, VALID_EDGE_TYPES)
        return False

    def esc(val: str) -> str:
        return val.replace("'", "\\'")

    # Build optional property steps for the edge
    prop_steps = ""
    if properties:
        for k, v in properties.items():
            prop_steps += f".property('{esc(k)}', '{esc(str(v))}')"

    query = (
        f"g.V().has('entry_id', '{esc(from_entry_id)}').as('a')"
        f".V().has('entry_id', '{esc(to_entry_id)}').as('b')"
        f".select('a').coalesce("
        f"  outE('{esc(edge_type)}').where(inV().has('entry_id', '{esc(to_entry_id)}')),"
        f"  addE('{esc(edge_type)}').to('b'){prop_steps}"
        f")"
    )

    result = _execute_gremlin(query)
    return result is not None


def get_neighbors(
    entry_id: str,
    identity: CallerIdentity,
    max_hops: int = 1,
) -> list[dict[str, Any]]:
    """Get the 1-hop graph neighborhood of an entry, filtered by owner isolation.

    Only returns vertices the caller is allowed to see:
    - owner_sub == caller's owner_sub, OR
    - visibility == 'shared' AND tenant_id == caller's tenant_id

    Parameters
    ----------
    entry_id:
        ULID of the center vertex.
    identity:
        Caller identity for isolation filtering.
    max_hops:
        Number of hops to traverse (default 1, max 2).

    Returns
    -------
    List of neighbor dicts with entry_id, type, edge_type, direction.
    Returns empty list if graph is disabled or Neptune is unreachable.
    """
    if not is_graph_enabled():
        return []

    max_hops = min(max_hops, 2)  # Cap at 2 hops for safety

    def esc(val: str) -> str:
        return val.replace("'", "\\'")

    owner_sub = esc(identity.owner_sub)
    tenant_id = esc(identity.tenant_id)

    # Query both incoming and outgoing edges, filter by isolation invariant
    query = (
        f"g.V().has('entry_id', '{esc(entry_id)}')"
        f".bothE().as('e')"
        f".otherV()"
        f".or("
        f"  has('owner_sub', '{owner_sub}'),"
        f"  and(has('visibility', 'shared'), has('tenant_id', '{tenant_id}'))"
        f")"
        f".project('entry_id', 'type', 'persona', 'edge_type', 'direction')"
        f".by(values('entry_id'))"
        f".by(values('type'))"
        f".by(values('persona'))"
        f".by(select('e').label())"
        f".by("
        f"  select('e').choose("
        f"    outV().has('entry_id', '{esc(entry_id)}'),"
        f"    constant('outgoing'),"
        f"    constant('incoming')"
        f"  )"
        f")"
    )

    result = _execute_gremlin(query)
    if result is None:
        return []

    # Parse Neptune response format
    try:
        data = result.get("result", {}).get("data", {}).get("@value", [])
        neighbors = []
        for item in data:
            if isinstance(item, dict):
                # Handle both Neptune response formats
                neighbor = {
                    "entry_id": _extract_value(item.get("entry_id")),
                    "type": _extract_value(item.get("type")),
                    "persona": _extract_value(item.get("persona")),
                    "edge_type": _extract_value(item.get("edge_type")),
                    "direction": _extract_value(item.get("direction")),
                }
                neighbors.append(neighbor)
        return neighbors
    except Exception as e:
        logger.warning("Failed to parse Neptune neighbor response: %s", e)
        return []


def _extract_value(val: Any) -> Any:
    """Extract a scalar value from Neptune's GraphSON response format.

    Neptune may return values wrapped in @type/@value dicts or as plain scalars.
    """
    if isinstance(val, dict) and "@value" in val:
        return val["@value"]
    return val


def remove_vertex(entry_id: str) -> bool:
    """Remove a vertex and all its edges from the graph.

    Used when an entry is deleted. Graceful — no-op when graph is disabled.
    """
    if not is_graph_enabled():
        return False

    def esc(val: str) -> str:
        return val.replace("'", "\\'")

    query = f"g.V().has('entry_id', '{esc(entry_id)}').drop()"
    result = _execute_gremlin(query)
    return result is not None

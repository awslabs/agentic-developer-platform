"""Neptune graph client for personal-context relationships.

Persists relationships detected by synthesis (#3.1) as Neptune edges and
provides owner-filtered traversal for graph-aware recall. Gated behind
``personal_context_graph_enabled`` — when disabled (default), all public
functions are no-ops or return empty results.

Graph model (Neptune property graph):
- Vertices: one per learning/synthesis/pattern, id = entry ULID.
  Mandatory properties: owner_sub, tenant_id, type, persona, visibility.
- Edges: derived_from, contradicts, supports, exemplifies, cross_persona.
- Every traversal filters on owner_sub == caller within the same tenant OR
  (visibility == 'shared' AND tenant_id == caller_tenant).

Authentication: IAM database auth via SigV4-signed requests (IRSA, no
stored credential).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlencode

from .identity import CallerIdentity, is_valid_tenant_id, is_valid_uuid

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

# Neptune TLS verification — Amazon CA bundle (Issue #2224)
# Override with NEPTUNE_CA_BUNDLE_PATH env var for local dev (set to "" to disable).
NEPTUNE_CA_BUNDLE: str | bool = (
    os.environ.get("NEPTUNE_CA_BUNDLE_PATH", "/etc/ssl/certs/rds-global-bundle.pem") or False
)

# Valid edge types from schema-pack.yml
VALID_EDGE_TYPES = frozenset(
    {"derived_from", "contradicts", "supports", "exemplifies", "cross_persona"}
)


# ---------------------------------------------------------------------------
# Neptune HTTP Client (IAM SigV4 auth)
# ---------------------------------------------------------------------------


def _get_neptune_url() -> str:
    """Build the Neptune openCypher HTTP endpoint URL."""
    return f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/openCypher"


def _sign_request(method: str, url: str, body: str | None = None) -> dict[str, str]:
    """Sign a Neptune request with IAM SigV4.

    Falls back to plain headers if botocore is unavailable or signing fails.
    """
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
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


def _execute_cypher(query: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    """Send fixed openCypher text and separate values using Neptune's HTTP API.

    Neptune Gremlin text requests do not support parameter bindings. openCypher
    operates on the same property graph and supports JSON-encoded parameters.
    """
    import httpx

    url = _get_neptune_url()
    body = urlencode({"query": query, "parameters": json.dumps(parameters)})
    headers = _sign_request("POST", url, body)
    try:
        resp = httpx.post(
            url, content=body, headers=headers, timeout=30.0, verify=NEPTUNE_CA_BUNDLE
        )
        if resp.status_code >= 400:
            logger.warning("Neptune query failed: HTTP %d", resp.status_code)
            return None
        return resp.json()
    except Exception as e:
        logger.warning("Neptune request failed: %s", type(e).__name__)
        return None


# Only relationship types are selected from a closed, source-defined enumeration.
# All other values, including property keys/values, are parameters, never query text.
_UPSERT = """
MERGE (v:personal_context {entry_id: $entry_id, owner_sub: $owner_sub, tenant_id: $tenant_id})
SET v.type = $entry_type, v.persona = $persona, v.visibility = $visibility
RETURN v.entry_id AS entry_id
"""
_EDGE_TEMPLATE = """
MATCH (a:personal_context {entry_id: $from_entry_id, owner_sub: $owner_sub, tenant_id: $tenant_id}),
      (b:personal_context {entry_id: $to_entry_id, tenant_id: $tenant_id})
WHERE b.owner_sub = $owner_sub OR b.visibility = 'shared'
MERGE (a)-[e:__EDGE_TYPE__]->(b)
SET e += $properties
RETURN type(e) AS edge_type
"""
_EDGE_QUERIES = {kind: _EDGE_TEMPLATE.replace("__EDGE_TYPE__", kind) for kind in VALID_EDGE_TYPES}
_NEIGHBORS = """
MATCH (a:personal_context {entry_id: $entry_id, tenant_id: $tenant_id})-[e]-(b:personal_context)
WHERE (a.owner_sub = $owner_sub OR a.visibility = 'shared')
  AND b.tenant_id = $tenant_id
  AND (b.owner_sub = $owner_sub OR b.visibility = 'shared')
RETURN b.entry_id AS entry_id, b.type AS type, b.persona AS persona,
       type(e) AS edge_type,
       CASE WHEN startNode(e) = a THEN 'outgoing' ELSE 'incoming' END AS direction
"""
_REMOVE = """
MATCH (v:personal_context {entry_id: $entry_id, owner_sub: $owner_sub, tenant_id: $tenant_id})
DETACH DELETE v
"""


def _valid_identity(identity: CallerIdentity | None) -> bool:
    return bool(
        identity and is_valid_uuid(identity.owner_sub) and is_valid_tenant_id(identity.tenant_id)
    )


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

    if not is_valid_uuid(owner_sub) or not is_valid_tenant_id(tenant_id):
        logger.error("upsert_vertex called with invalid owner/tenant identity — refusing")
        return False
    result = _execute_cypher(
        _UPSERT,
        {
            "entry_id": entry_id,
            "owner_sub": owner_sub,
            "tenant_id": tenant_id,
            "entry_type": entry_type,
            "persona": persona,
            "visibility": visibility,
        },
    )
    return bool(result and result.get("results"))


def add_edge(
    from_entry_id: str,
    to_entry_id: str,
    edge_type: str,
    properties: dict[str, str] | None = None,
    *,
    identity: CallerIdentity | None = None,
) -> bool:
    """Add an edge from a caller-owned vertex to a readable same-tenant vertex.

    A validated identity is required; callers without one fail closed.

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

    if not _valid_identity(identity):
        return False
    result = _execute_cypher(
        _EDGE_QUERIES[edge_type],
        {
            "from_entry_id": from_entry_id,
            "to_entry_id": to_entry_id,
            "owner_sub": identity.owner_sub,
            "tenant_id": identity.tenant_id,
            "properties": {k: str(v) for k, v in (properties or {}).items()},
        },
    )
    return bool(result and result.get("results"))


def get_neighbors(
    entry_id: str,
    identity: CallerIdentity,
    max_hops: int = 1,
) -> list[dict[str, Any]]:
    """Get the 1-hop graph neighborhood of an entry, filtered by owner isolation.

    Only returns vertices the caller is allowed to see:
    - tenant_id == caller's tenant_id, AND
    - owner_sub == caller's owner_sub OR visibility == 'shared'

    Parameters
    ----------
    entry_id:
        ULID of the center vertex.
    identity:
        Caller identity for isolation filtering. Both starting and returned vertices
        must be in this tenant and either caller-owned or shared.
    max_hops:
        Retained for compatibility; traversal remains one hop.

    Returns
    -------
    List of neighbor dicts with entry_id, type, edge_type, direction.
    Returns empty list if graph is disabled or Neptune is unreachable.
    """
    if not is_graph_enabled():
        return []

    if not _valid_identity(identity):
        return []
    # Preserve the existing one-hop contract; max_hops never enabled a second hop.
    result = _execute_cypher(
        _NEIGHBORS,
        {
            "entry_id": entry_id,
            "owner_sub": identity.owner_sub,
            "tenant_id": identity.tenant_id,
        },
    )
    if result is None:
        return []
    data = result.get("results", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def remove_vertex(entry_id: str, *, identity: CallerIdentity | None = None) -> bool:
    """Remove only the caller's vertex and its edges; absent identity fails closed."""
    if not is_graph_enabled() or not _valid_identity(identity):
        return False
    result = _execute_cypher(
        _REMOVE,
        {
            "entry_id": entry_id,
            "owner_sub": identity.owner_sub,
            "tenant_id": identity.tenant_id,
        },
    )
    return result is not None

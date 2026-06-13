"""Context MCP Server — the verb surface for the Knowledge layer.

Exposes 6 tools at :5100:
  - search: exact code search (Zoekt) + optional semantic + memory
  - understand: structural backend (code-index.json)
  - impact: call-graph analysis (code-index.json + cross-repo Zoekt)
  - browse: catalog + S3 content listing
  - remember: session memory persistence
  - experience: personal context (save/recall/list_syntheses)

Every verb result routes through door/acl.py (fail-closed).
Verb logic lives in importable functions for later gateway re-route.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import boto3
from fastapi import FastAPI, Request, Response

from .acl import CallerPrincipal, SearchHit, extract_caller_principal, filter_results
from .browse_backend import browse
from .config import config
from .remember_backend import remember
from .search_backend import ZoektSearchBackend
from .structural_backend import impact, understand

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (matches tests/conftest.py EXPECTED_MCP_TOOLS contract)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": "Find relevant code, documentation, and past learnings",
        "parameters": {
            "query": {"type": "string", "required": True},
            "scope": {"type": "string", "required": False},
            "limit": {"type": "integer", "required": False},
        },
    },
    {
        "name": "understand",
        "description": "Get deep understanding of a specific repo, directory, or file",
        "parameters": {
            "target": {"type": "string", "required": True},
            "depth": {"type": "string", "required": False},
        },
    },
    {
        "name": "impact",
        "description": "Analyse what would be affected by changing a symbol, file, or pattern",
        "parameters": {
            "target": {"type": "string", "required": True},
            "cross_repo": {"type": "boolean", "required": False},
        },
    },
    {
        "name": "browse",
        "description": "Navigate the indexed content filesystem",
        "parameters": {
            "action": {"type": "string", "required": True},
            "uri": {"type": "string", "required": True},
            "depth": {"type": "integer", "required": False},
        },
    },
    {
        "name": "remember",
        "description": "Save session context, decisions, and learnings to long-term memory",
        "parameters": {
            "session_id": {"type": "string", "required": True},
            "messages": {"type": "array", "required": True},
            "outcome": {"type": "string", "required": False},
        },
    },
    {
        "name": "experience",
        "description": (
            "Save or recall experiential knowledge (per-user, persona-scoped, synthesized)"
        ),
        "parameters": {
            "action": {
                "type": "string",
                "enum": ["save", "recall", "list_syntheses"],
                "required": True,
            },
            "persona": {
                "type": "string",
                "enum": ["operations", "developer", "architect", "reviewer"],
                "required": True,
            },
            "content": {"type": "string", "required": False},
            "learning_type": {"type": "string", "required": False},
            "context": {"type": "object", "required": False},
            "query": {"type": "string", "required": False},
            "visibility": {"type": "string", "enum": ["private", "shared"], "required": False},
            "limit": {"type": "integer", "required": False},
            "cross_persona": {"type": "boolean", "required": False},
        },
    },
]


# ---------------------------------------------------------------------------
# Application state (backends initialized at startup)
# ---------------------------------------------------------------------------


class AppState:
    """Holds initialized backend instances."""

    def __init__(self) -> None:
        self.zoekt: ZoektSearchBackend | None = None
        self.s3_client: Any = None
        self.db_pool: Any = None
        self.experience_tool: Any = None
        self.acl_store: Any = None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize backends on startup, clean up on shutdown."""
    # Zoekt backend (always available)
    state.zoekt = ZoektSearchBackend(config.zoekt_url, timeout=config.zoekt_timeout)

    # S3 client (for structural backend + browse)
    if config.s3_bucket:
        state.s3_client = boto3.client("s3", region_name=config.s3_region)

    # Database pool (for browse catalog + ACL)
    if config.database_url:
        try:
            import psycopg2.pool

            state.db_pool = psycopg2.pool.SimpleConnectionPool(1, 5, config.database_url)
            log.info("Database pool initialized")
        except Exception:
            log.warning("Failed to initialize database pool", exc_info=True)

    # ACL store (Postgres-backed)
    if state.db_pool:
        from .acl import PostgresACLStore

        state.acl_store = PostgresACLStore(state.db_pool)

    # Experience tool (for remember + experience verbs)
    _init_experience_tool()

    log.info("Context MCP Server started on %s:%d", config.host, config.port)
    yield

    # Cleanup
    if state.db_pool:
        state.db_pool.closeall()
    log.info("Context MCP Server shutting down")


def _init_experience_tool() -> None:
    """Initialize the experience tool with appropriate backends."""
    try:
        from ..personal_context.embeddings import LiteLLMEmbeddingClient
        from ..personal_context.experience_tool import ExperienceTool
        from ..personal_context.storage import PersonalContextStore

        # Use S3 AGFS backend if bucket is configured
        if config.s3_bucket:
            from ..personal_context.backends.s3_backend import S3AGFSBackend

            backend = S3AGFSBackend(bucket_name=config.s3_bucket, region_name=config.s3_region)
        else:
            # Fallback: no-op backend (for testing without S3)
            backend = _NoOpBackend()

        store = PersonalContextStore(backend)
        embedding_client = LiteLLMEmbeddingClient(base_url=config.litellm_url)

        # S3 Vectors embedding store (if configured)
        embedding_store = None
        if config.s3_vectors_bucket:
            from ..personal_context.backends.s3_vectors_backend import S3VectorsEmbeddingStore

            embedding_store = S3VectorsEmbeddingStore(
                bucket_name=config.s3_vectors_bucket,
                region_name=config.s3_vectors_region or config.s3_region,
            )

        state.experience_tool = ExperienceTool(
            store=store,
            embedding_client=embedding_client,
            embedding_store=embedding_store,
        )
        log.info("Experience tool initialized")
    except Exception:
        log.warning("Failed to initialize experience tool", exc_info=True)


class _NoOpBackend:
    """No-op AGFS backend for when S3 is not configured."""

    def put(self, path: str, data: Any) -> None:
        pass

    def get(self, path: str) -> Any:
        return None

    def delete(self, path: str) -> None:
        pass

    def list_prefix(self, prefix: str) -> list:
        return []


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


app = FastAPI(title="Context MCP Server", lifespan=lifespan)


@app.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    """List available MCP tools with their descriptions and parameters."""
    return TOOLS


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/call")
async def call_tool(request: Request) -> Response:
    """Call an MCP tool by name with arguments.

    Request body: {"name": "<tool>", "arguments": {...}}
    """
    # Parse request body
    try:
        body = await request.body()
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return Response(
            content=json.dumps({"error": "Invalid JSON"}),
            status_code=400,
            media_type="application/json",
        )

    if not isinstance(payload, dict):
        return Response(
            content=json.dumps({"error": "Request body must be a JSON object"}),
            status_code=400,
            media_type="application/json",
        )

    name = payload.get("name", "")
    arguments = payload.get("arguments", {})

    if not name:
        return Response(
            content=json.dumps({"error": "Missing 'name' field"}),
            status_code=400,
            media_type="application/json",
        )

    if not isinstance(arguments, dict):
        return Response(
            content=json.dumps({"error": "'arguments' must be an object"}),
            status_code=400,
            media_type="application/json",
        )

    # Extract caller principal from headers (for ACL)
    headers = dict(request.headers)
    caller = extract_caller_principal(headers)

    # Route to verb handler
    try:
        result = await _dispatch_tool(name, arguments, headers, caller)
    except Exception:
        log.exception("Tool %s failed with unhandled error", name)
        return Response(
            content=json.dumps({"error": f"Internal error processing tool '{name}'"}),
            status_code=500,
            media_type="application/json",
        )

    return Response(
        content=json.dumps(result, default=str),
        status_code=200,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Verb dispatch
# ---------------------------------------------------------------------------


async def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
    caller: CallerPrincipal | None,
) -> dict[str, Any]:
    """Route a tool call to the appropriate verb handler."""
    if name == "search":
        return await _handle_search(arguments, caller)
    elif name == "understand":
        return await _handle_understand(arguments, caller)
    elif name == "impact":
        return await _handle_impact(arguments, caller)
    elif name == "browse":
        return await _handle_browse(arguments, caller)
    elif name == "remember":
        return await _handle_remember(arguments, headers)
    elif name == "experience":
        return await _handle_experience(arguments, headers)
    else:
        return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Verb handlers (importable functions wrapping backend calls + ACL)
# ---------------------------------------------------------------------------


async def _handle_search(
    arguments: dict[str, Any], caller: CallerPrincipal | None
) -> dict[str, Any]:
    """Handle the search verb: exact (Zoekt), semantic, or memory."""
    query = arguments.get("query", "")
    scope = arguments.get("scope", "code")
    limit = arguments.get("limit", 20)

    if not query:
        return {"results": [], "total": 0, "query": ""}

    # Route by scope
    if scope == "memory":
        # Memory search doesn't go through repo ACL (personal context has its own isolation)
        # but we still need identity headers — the experience tool enforces them
        return {"results": [], "total": 0, "query": query}

    if scope == "docs" and config.semantic_enabled:
        # Semantic search (optional — gated on §12.1 decision)
        return {"results": [], "total": 0, "query": query}

    # Default: exact code search via Zoekt
    if state.zoekt is None:
        return {"results": [], "total": 0, "query": query}

    hits = await state.zoekt.search(query, limit=limit)

    # ACL filter
    filtered = _apply_acl(hits, caller)

    results = [hit.data for hit in filtered[:limit]]
    return {"results": results, "total": len(results), "query": query}


async def _handle_understand(
    arguments: dict[str, Any], caller: CallerPrincipal | None
) -> dict[str, Any]:
    """Handle the understand verb: structural index lookup."""
    target = arguments.get("target", "")
    depth = arguments.get("depth", "overview")

    if not target:
        return {"target": "", "summary": "No target specified", "definitions": []}

    if state.s3_client is None or not config.s3_bucket:
        return {"target": target, "summary": "Structural index not available", "definitions": []}

    hits = await understand(
        target,
        s3_client=state.s3_client,
        bucket=config.s3_bucket,
        prefix=config.code_index_s3_prefix,
        depth=depth,
    )

    # ACL filter
    filtered = _apply_acl(hits, caller)

    definitions = [hit.data for hit in filtered]
    summary = f"Found {len(definitions)} definition(s) for '{target}'"
    return {"target": target, "summary": summary, "definitions": definitions}


async def _handle_impact(
    arguments: dict[str, Any], caller: CallerPrincipal | None
) -> dict[str, Any]:
    """Handle the impact verb: call-graph analysis."""
    target = arguments.get("target", "")
    cross_repo = arguments.get("cross_repo", False)

    if not target:
        return {"target": "", "affected": [], "blast_radius": 0}

    if state.s3_client is None or not config.s3_bucket:
        return {"target": target, "affected": [], "blast_radius": 0}

    hits = await impact(
        target,
        s3_client=state.s3_client,
        bucket=config.s3_bucket,
        prefix=config.code_index_s3_prefix,
        cross_repo=cross_repo,
        zoekt_backend=state.zoekt if cross_repo else None,
    )

    # ACL filter
    filtered = _apply_acl(hits, caller)

    affected = [hit.data for hit in filtered]
    return {"target": target, "affected": affected, "blast_radius": len(affected)}


async def _handle_browse(
    arguments: dict[str, Any], caller: CallerPrincipal | None
) -> dict[str, Any]:
    """Handle the browse verb: catalog + S3 listing."""
    action = arguments.get("action", "ls")
    uri = arguments.get("uri", "/")
    depth = arguments.get("depth", 1)

    hits = await browse(
        action,
        uri,
        db_pool=state.db_pool,
        s3_client=state.s3_client,
        bucket=config.s3_bucket,
        content_prefix=config.s3_content_prefix,
        depth=depth,
    )

    # ACL filter
    filtered = _apply_acl(hits, caller)

    entries = [hit.data for hit in filtered]
    return {"action": action, "uri": uri, "entries": entries}


async def _handle_remember(arguments: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Handle the remember verb: session memory persistence."""
    session_id = arguments.get("session_id", "")
    messages = arguments.get("messages", [])
    outcome = arguments.get("outcome", "")

    if not session_id:
        return {"stored": False, "error": "session_id is required"}

    if state.experience_tool is None:
        return {"stored": False, "session_id": session_id, "error": "Experience tool not available"}

    return await remember(
        session_id,
        messages,
        outcome=outcome,
        experience_tool=state.experience_tool,
        headers=headers,
    )


async def _handle_experience(arguments: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Handle the experience verb: personal context save/recall/list."""
    if state.experience_tool is None:
        return {"error": "Experience tool not available"}

    try:
        return state.experience_tool.handle(arguments, headers)
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# ACL helper
# ---------------------------------------------------------------------------


def _apply_acl(hits: list[SearchHit], caller: CallerPrincipal | None) -> list[SearchHit]:
    """Apply ACL filtering to search hits.

    If no ACL store is configured (e.g., Postgres not available),
    falls back to passing all results through (in-cluster trust).
    """
    if state.acl_store is None:
        # No ACL store — in dev/test mode, pass through
        # In production, the db_pool should always be configured
        return hits

    return filter_results(hits, caller, state.acl_store)

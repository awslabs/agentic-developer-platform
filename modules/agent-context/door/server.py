"""Context MCP Server — the verb surface for the Knowledge layer.

Exposes 7 tools at :5100:
  - search: exact code search (Zoekt) + optional semantic + memory
  - understand: structural backend (code-index.json)
  - impact: call-graph analysis (code-index.json + cross-repo Zoekt)
  - browse: catalog + S3 content listing
  - remember: session memory persistence
  - experience: personal context (save/recall/list_syntheses)
  - secure: vulnerability identification, remediation planning, verification

Every verb result routes through door/acl.py (fail-closed).
Verb logic lives in importable functions for later gateway re-route.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import boto3
from fastapi import FastAPI, Request, Response

from .acl import CallerPrincipal, SearchHit, extract_caller_principal, filter_results
from .browse_backend import browse
from .config import config
from .metrics import record_query, setup_door_metrics
from .project_filter import (
    ProjectFilterError,
    ProjectScope,
    apply_project_filter,
    resolve_project_repos,
)
from .remember_backend import recall_memory, remember
from .search_backend import ZoektSearchBackend
from .structural_backend import impact, understand
from .tracing import get_tracer, setup_tracing, shutdown_tracing

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
            "project": {"type": "string", "required": False},
        },
    },
    {
        "name": "understand",
        "description": "Get deep understanding of a specific repo, directory, or file",
        "parameters": {
            "target": {"type": "string", "required": True},
            "depth": {"type": "string", "required": False},
            "project": {"type": "string", "required": False},
        },
    },
    {
        "name": "impact",
        "description": (
            "Before editing or deleting a symbol, call impact(cross_repo=true) for the "
            "COMPLETE caller set across all repos. Returns verdict-first ranked results "
            "bounded at 100, grouped by repo. Prefer this over grep for blast-radius analysis."
        ),
        "parameters": {
            "target": {"type": "string", "required": True},
            "cross_repo": {"type": "boolean", "required": False},
            "project": {"type": "string", "required": False},
        },
    },
    {
        "name": "browse",
        "description": (
            "Navigate the indexed content filesystem. "
            "Start with browse(action='ls', uri='/') to discover all indexed repos "
            "and their capabilities (code_search, call_graph, wiki, sbom, vectors "
            "with metrics). Use the capability manifest to plan which verbs to call."
        ),
        "parameters": {
            "action": {"type": "string", "required": True},
            "uri": {"type": "string", "required": True},
            "depth": {"type": "integer", "required": False},
            "project": {"type": "string", "required": False},
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
    {
        "name": "secure",
        "description": (
            "Identify, locate, and plan remediation for vulnerabilities. "
            "Given a CVE, repo, or package, returns reachability-scored findings "
            "with remediation guidance. The security entry point."
        ),
        "parameters": {
            "cve": {"type": "string", "required": False},
            "repo": {"type": "string", "required": False},
            "package": {"type": "string", "required": False},
            "action": {
                "type": "string",
                "enum": ["identify", "plan", "verify"],
                "required": False,
            },
            "severity_min": {
                "type": "string",
                "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                "required": False,
            },
            "reachable_only": {"type": "boolean", "required": False},
            "project": {"type": "string", "required": False},
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
        self.neptune_driver: Any = None
        self.semantic_code_store: Any = None
        self.semantic_http_client: Any = None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize backends on startup, clean up on shutdown."""
    # Metrics (fail-open: never blocks startup)
    setup_door_metrics()

    # Tracing setup (must be early, before middleware registration)
    setup_tracing(app)

    # Zoekt backend (always available)
    state.zoekt = ZoektSearchBackend(config.zoekt_url, timeout=config.zoekt_timeout)

    # S3 client (for structural backend + browse)
    if config.s3_bucket:
        state.s3_client = boto3.client("s3", region_name=config.s3_region)

    # Database pool (for browse catalog + ACL)
    # Uses IAM auth tokens in production (DB_USE_IAM_AUTH=true) — mints a
    # fresh token per new connection since RDS IAM tokens expire ~15 min.
    # Falls back to static DATABASE_URL for local/CI.
    try:
        from .db import create_db_pool

        state.db_pool = create_db_pool(config)
        if state.db_pool:
            log.info("Database pool initialized (iam=%s)", config.db_use_iam_auth)
    except Exception:
        log.warning("Failed to initialize database pool", exc_info=True)

    # ACL store (Postgres-backed)
    if state.db_pool:
        from .acl import PostgresACLStore

        state.acl_store = PostgresACLStore(
            state.db_pool, tenant_scope_enabled=config.tenant_scope_enabled
        )

    # Neptune driver (for structural queries — impact/understand)
    if config.neptune_enabled:
        try:
            from .neptune_client import get_neptune_driver

            state.neptune_driver = get_neptune_driver()
            if state.neptune_driver:
                log.info("Neptune driver initialized (endpoint: %s)", config.neptune_endpoint)
            else:
                log.warning("Neptune enabled but driver not created (no endpoint?)")
        except Exception:
            log.warning("Failed to initialize Neptune driver", exc_info=True)

    # Experience tool (for remember + experience verbs)
    _init_experience_tool()

    # S3 Vectors code store + async HTTP client for semantic search (#1774)
    if config.semantic_enabled and config.s3_vectors_bucket:
        try:
            import httpx

            from personal_context.backends.s3_vectors_backend import S3VectorsCodeStore

            state.semantic_code_store = S3VectorsCodeStore(
                bucket_name=config.s3_vectors_bucket,
                region_name=config.s3_vectors_region or config.s3_region,
            )
            state.semantic_http_client = httpx.AsyncClient(timeout=10.0)
            log.info("Semantic code store initialized (bucket: %s)", config.s3_vectors_bucket)
        except Exception:
            log.warning("Failed to initialize semantic code store", exc_info=True)

    # Start the MCP session manager's task group AFTER backends are initialized.
    # The MCP tool shims reference module-level `state`, so backends must be live
    # before the session manager accepts requests. The sub-app's own lifespan is
    # NOT run by the parent (Starlette mount semantics), so we compose it here.
    # Import inside function body to avoid circular import (mcp_app imports from
    # this module; this module imports mcp_app at the bottom).
    from .mcp_app import mcp_server  # noqa: E402

    async with mcp_server.session_manager.run():
        log.info("Context MCP Server started on %s:%d", config.host, config.port)
        yield

    # Cleanup
    shutdown_tracing()
    if state.semantic_http_client:
        await state.semantic_http_client.aclose()
    if state.db_pool:
        state.db_pool.closeall()
    log.info("Context MCP Server shutting down")


def _init_experience_tool() -> None:
    """Initialize the experience tool with appropriate backends."""
    try:
        from personal_context.embeddings import LiteLLMEmbeddingClient
        from personal_context.experience_tool import ExperienceTool
        from personal_context.storage import PersonalContextStore

        # Use S3 AGFS backend if bucket is configured
        if config.s3_bucket:
            from personal_context.backends.s3_backend import S3AGFSBackend

            backend = S3AGFSBackend(bucket_name=config.s3_bucket, region_name=config.s3_region)
        else:
            # Fallback: no-op backend (for testing without S3)
            backend = _NoOpBackend()

        store = PersonalContextStore(backend)
        embedding_client = LiteLLMEmbeddingClient(proxy_url=config.litellm_url)

        # S3 Vectors embedding store (if configured)
        embedding_store = None
        if config.s3_vectors_bucket:
            from personal_context.backends.s3_vectors_backend import S3VectorsEmbeddingStore

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


# ---------------------------------------------------------------------------
# Identity-enrichment middleware (stamps caller identity onto current OTel span)
# ---------------------------------------------------------------------------

_door_tracer = get_tracer("knowledge-layer.door")


@app.middleware("http")
async def enrich_span_with_identity(request: Request, call_next):
    """Stamp caller identity headers onto the active OTel span for trace filtering."""
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute("caller.owner_sub", request.headers.get("x-owner-sub", ""))
            span.set_attribute("caller.tenant_id", request.headers.get("x-tenant-id", ""))
            span.set_attribute("caller.github_login", request.headers.get("x-github-login", ""))
    except Exception:
        pass  # Fail-open: tracing unavailable doesn't block requests
    response = await call_next(request)
    return response


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
    # Uses shared helper for parity with MCP path (Issue #1602, I1)
    from .mcp_app import _extract_headers

    headers = _extract_headers(request)
    caller = extract_caller_principal(headers)

    # Route to verb handler (with metrics)
    tenant_id = headers.get("x-tenant-id", "")
    query_start = time.monotonic()
    try:
        result = await _dispatch_tool(name, arguments, headers, caller)
    except Exception:
        log.exception("Tool %s failed with unhandled error", name)
        duration_ms = (time.monotonic() - query_start) * 1000
        try:
            record_query(tenant_id=tenant_id, verb=name, duration_ms=duration_ms, error=True)
        except Exception:
            pass  # fail-open
        return Response(
            content=json.dumps({"error": f"Internal error processing tool '{name}'"}),
            status_code=500,
            media_type="application/json",
        )

    duration_ms = (time.monotonic() - query_start) * 1000
    try:
        record_query(tenant_id=tenant_id, verb=name, duration_ms=duration_ms, error=False)
    except Exception:
        pass  # fail-open

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
    # Resolve project scope for retrieval verbs (search/understand/impact/browse)
    project_scope = _resolve_project_scope(arguments, caller)
    if isinstance(project_scope, dict):
        # Error dict — return immediately
        return project_scope

    if name == "search":
        return await _handle_search(arguments, caller, project_scope, headers=headers)
    elif name == "understand":
        return await _handle_understand(arguments, caller, project_scope)
    elif name == "impact":
        return await _handle_impact(arguments, caller, project_scope)
    elif name == "browse":
        return await _handle_browse(arguments, caller, project_scope)
    elif name == "remember":
        return await _handle_remember(arguments, headers)
    elif name == "experience":
        return await _handle_experience(arguments, headers)
    elif name == "secure":
        return await _handle_secure(arguments, caller, project_scope, headers=headers)
    else:
        return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Project scope resolution helper
# ---------------------------------------------------------------------------


def _resolve_project_scope(
    arguments: dict[str, Any],
    caller: CallerPrincipal | None,
) -> ProjectScope | None | dict[str, Any]:
    """Resolve the optional 'project' argument to a ProjectScope.

    Returns:
        ProjectScope — if project was specified and resolved successfully.
        None — if no project specified (passthrough) or feature disabled.
        dict — error response if project resolution failed.
    """
    project_arg = arguments.get("project", "")
    if not project_arg or not config.project_filter_enabled:
        return None

    if caller is None or not caller.owner_sub:
        # Cannot resolve project without identity — but ACL will fail-close anyway
        return None

    if state.db_pool is None:
        log.warning("Project filter requested but no database pool available")
        return None

    try:
        return resolve_project_repos(
            project_id=project_arg,
            caller_owner_sub=caller.owner_sub,
            db_pool=state.db_pool,
            caller_tenant_id=caller.tenant_id,
        )
    except ProjectFilterError as e:
        return {"error": e.args[0], "code": e.code}
    except Exception:
        log.warning("Project resolution failed unexpectedly", exc_info=True)
        return {"error": "Failed to resolve project", "code": "project_resolution_error"}


# ---------------------------------------------------------------------------
# Verb handlers (importable functions wrapping backend calls + ACL)
# ---------------------------------------------------------------------------


async def _handle_search(
    arguments: dict[str, Any],
    caller: CallerPrincipal | None,
    project_scope: ProjectScope | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Handle the search verb: exact (Zoekt), semantic, or memory.

    Deduplicates results to one entry per file to maximize diversity
    (Zoekt returns multiple line-matches per file which consume result slots).
    Re-ranks results to boost files whose path matches the query (filename relevance).
    """
    query = arguments.get("query", "")
    scope = arguments.get("scope", "code")
    limit = arguments.get("limit", 20)

    if not query:
        return {"results": [], "total": 0, "query": ""}

    # Route by scope
    if scope == "memory":
        # Memory search routes through recall_memory (personal context isolation).
        # Requires identity headers — the experience tool enforces them.
        if state.experience_tool is None or headers is None:
            return {"results": [], "total": 0, "query": query}
        results = await recall_memory(
            query,
            experience_tool=state.experience_tool,
            headers=headers,
            limit=limit,
        )
        return {"results": results, "total": len(results), "query": query}

    if scope == "docs" and config.semantic_enabled:
        # Semantic search via S3 Vectors — scoped per caller (#1774)
        return await _handle_semantic_search(query, limit, caller)

    # Default: exact code search via Zoekt
    if state.zoekt is None:
        return {"results": [], "total": 0, "query": query}

    # Request more results than needed to compensate for file-level dedup + re-ranking
    raw_limit = limit * 10
    hits = await state.zoekt.search(query, limit=raw_limit)

    # ACL filter (Step 2: #1721 isolation)
    filtered = _apply_acl(hits, caller)

    # Project filter (Step 3: narrow to project repos)
    filtered = apply_project_filter(filtered, project_scope)

    # Deduplicate to one result per file (collect all unique files)
    seen_files: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for hit in filtered:
        file_key = hit.data.get("file", "")
        if file_key and file_key not in seen_files:
            seen_files.add(file_key)
            deduped.append(hit.data)

    # Re-rank: boost files whose path matches the query (filename relevance)
    deduped.sort(key=lambda d: -_file_relevance_score(d.get("file", ""), query))

    return {"results": deduped[:limit], "total": min(len(deduped), limit), "query": query}


async def _handle_semantic_search(
    query: str,
    limit: int,
    caller: CallerPrincipal | None,
) -> dict[str, Any]:
    """Scoped semantic search via S3 Vectors (Story 5, #1774).

    Unions results from shared + caller's tenant + caller's personal indexes.
    Requires semantic_enabled=True, s3_vectors_bucket configured, and a resolved caller.
    Uses singleton code_store and async HTTP client initialized at startup.
    """
    if caller is None or not caller.is_resolved:
        return {"results": [], "total": 0, "query": query}

    if state.semantic_code_store is None or state.semantic_http_client is None:
        return {"results": [], "total": 0, "query": query}

    try:
        # Generate query embedding via LiteLLM (async — does not block event loop)
        embed_response = await state.semantic_http_client.post(
            f"{config.litellm_url}/embeddings",
            json={"input": query, "model": "bedrock/amazon.titan-embed-text-v2:0"},
        )
        embed_response.raise_for_status()
        query_vector = embed_response.json()["data"][0]["embedding"]

        # Determine caller's org_id from tenant_id (best effort)
        org_id = caller.tenant_id or ""

        # Scoped query: union shared + tenant + personal indexes
        results = state.semantic_code_store.query_scoped(
            query_vector=query_vector,
            org_id=org_id,
            tenant_id=caller.tenant_id or None,
            owner_sub=caller.owner_sub or None,
            top_k=limit,
        )

        # Transform S3 Vectors results to search result format
        formatted = []
        for r in results:
            metadata = r.get("metadata", {})
            formatted.append(
                {
                    "key": r.get("key", ""),
                    "distance": r.get("distance", 1.0),
                    "repo": metadata.get("repo", ""),
                    "source_type": metadata.get("source_type", ""),
                    "section_heading": metadata.get("section_heading", ""),
                    "chunk_text": metadata.get("chunk_text", ""),
                }
            )

        return {"results": formatted[:limit], "total": len(formatted), "query": query}
    except Exception:
        log.warning("Semantic search failed", exc_info=True)
        return {"results": [], "total": 0, "query": query}


async def _handle_understand(
    arguments: dict[str, Any],
    caller: CallerPrincipal | None,
    project_scope: ProjectScope | None = None,
) -> dict[str, Any]:
    """Handle the understand verb: structural index lookup."""
    target = arguments.get("target", "")
    depth = arguments.get("depth", "overview")

    if not target:
        return {"target": "", "summary": "No target specified", "definitions": []}

    if state.s3_client is None or not config.s3_bucket:
        return {"target": target, "summary": "Structural index not available", "definitions": []}

    # Debug: try to load the code-index directly to diagnose S3 issues
    from .structural_backend import _parse_target, load_code_index

    repo_id, query_target = _parse_target(target)
    debug_info: dict[str, Any] = {
        "repo_id": repo_id,
        "query_target": query_target,
        "bucket": config.s3_bucket,
        "prefix": config.code_index_s3_prefix,
    }
    try:
        raw_index = await load_code_index(
            repo_id,
            s3_client=state.s3_client,
            bucket=config.s3_bucket,
            prefix=config.code_index_s3_prefix,
        )
        debug_info["index_keys"] = list(raw_index.keys()) if raw_index else []
        symbols = raw_index.get("symbols", []) or raw_index.get("definitions", [])
        debug_info["symbols_count"] = len(symbols)
        debug_info["call_graph_count"] = len(raw_index.get("call_graph", {}))
        if symbols:
            debug_info["first_symbol"] = symbols[0] if symbols else None
    except Exception as e:
        debug_info["load_error"] = str(e)

    hits = await understand(
        target,
        s3_client=state.s3_client,
        bucket=config.s3_bucket,
        prefix=config.code_index_s3_prefix,
        depth=depth,
        zoekt_backend=state.zoekt,
    )

    # ACL filter (Step 2: #1721 isolation)
    filtered = _apply_acl(hits, caller)

    # Project filter (Step 3: narrow to project repos)
    filtered = apply_project_filter(filtered, project_scope)

    definitions = [hit.data for hit in filtered]
    summary = f"Found {len(definitions)} definition(s) for '{target}'"
    result: dict[str, Any] = {"target": target, "summary": summary, "definitions": definitions}
    # Include debug info when no results found (helps diagnose S3 issues)
    if not definitions:
        result["_debug"] = debug_info
    return result


async def _handle_impact(
    arguments: dict[str, Any],
    caller: CallerPrincipal | None,
    project_scope: ProjectScope | None = None,
) -> dict[str, Any]:
    """Handle the impact verb: call-graph analysis.

    Returns verdict-first response: verdict, per-repo attribution, then details.
    Bounded at 100 results, ranked by distance (closest first).
    """
    target = arguments.get("target", "")
    cross_repo = arguments.get("cross_repo", False)

    if not target:
        return {"verdict": "no_target", "target": "", "affected": [], "blast_radius": 0}

    if state.s3_client is None or not config.s3_bucket:
        return {
            "verdict": "unavailable",
            "target": target,
            "affected": [],
            "blast_radius": 0,
        }

    hits = await impact(
        target,
        s3_client=state.s3_client,
        bucket=config.s3_bucket,
        prefix=config.code_index_s3_prefix,
        cross_repo=cross_repo,
        zoekt_backend=state.zoekt,
    )

    # ACL filter (Step 2: #1721 isolation)
    filtered = _apply_acl(hits, caller)

    # Project filter (Step 3: narrow to project repos)
    filtered = apply_project_filter(filtered, project_scope)

    all_data = [hit.data for hit in filtered]

    # Separate real results from Neptune "no callers" sentinel (Bug #1587 Fix 2).
    # The sentinel carries _neptune_no_callers=True and is used only for source
    # attribution — it's not a real caller and should not be shown to the user.
    affected = [d for d in all_data if not d.get("_neptune_no_callers")]
    has_neptune_sentinel = any(d.get("_neptune_no_callers") for d in all_data)

    # Per-repo attribution: group results by repo
    repos_affected: dict[str, int] = {}
    for item in affected:
        repo = item.get("repo_id", "unknown")
        repos_affected[repo] = repos_affected.get(repo, 0) + 1

    # Determine source (neptune vs code-index-fallback).
    # Bug #1587 Fix 2: use the sentinel to correctly attribute source="neptune"
    # even when no real callers were found (symbol exists, zero callers).
    sources = {item.get("source", "unknown") for item in affected}
    if sources:
        source = "neptune" if "neptune" in sources else "code-index-fallback"
    elif has_neptune_sentinel:
        source = "neptune"
    else:
        source = "none"

    # Verdict: how severe is the blast radius.
    # Bug #1587 Fix 2: distinguish "no_callers" (symbol found, zero callers)
    # from "symbol_not_found" (lookup miss — couldn't resolve target).
    blast_radius = len(affected)
    if blast_radius == 0:
        if source == "none":
            # Couldn't find the symbol in any backend
            verdict = "symbol_not_found"
        else:
            # Backend found the symbol but it has zero callers (true negative)
            verdict = "no_callers"
    elif len(repos_affected) > 1:
        verdict = "cross_repo_impact"
    elif blast_radius > 20:
        verdict = "high_impact"
    else:
        verdict = "contained"

    return {
        "verdict": verdict,
        "target": target,
        "blast_radius": blast_radius,
        "repos_affected": repos_affected,
        "source": source,
        "affected": affected,
    }


async def _handle_browse(
    arguments: dict[str, Any],
    caller: CallerPrincipal | None,
    project_scope: ProjectScope | None = None,
) -> dict[str, Any]:
    """Handle the browse verb: catalog + S3 + Zoekt file listing.

    The optional ``project`` argument doubles as a **repo scope**: when present,
    a non content-root URI is treated as a repo-relative path within that repo
    (``browse list uri=agent/ project=HKUDS/Vibe-Trading``) and served from
    Zoekt. This is the repo directory-tree browse contract. The raw ``project``
    string is used directly as the repo name — the eval dataset passes the
    "org/repo" repo name in the ``project`` field.
    """
    action = arguments.get("action", "ls")
    uri = arguments.get("uri", "/")
    depth = arguments.get("depth", 1)
    repo_scope = arguments.get("project") or None

    hits = await browse(
        action,
        uri,
        db_pool=state.db_pool,
        s3_client=state.s3_client,
        bucket=config.s3_bucket,
        content_prefix=config.s3_content_prefix,
        depth=depth,
        zoekt_url=config.zoekt_url,
        repo_scope=repo_scope,
    )

    # ACL filter (Step 2: #1721 isolation)
    filtered = _apply_acl(hits, caller)

    # Project filter (Step 3: narrow to project repos).
    # When a repo_scope is active the URI is already scoped to that single repo
    # via Zoekt, so skip the project-set intersection (which requires the
    # PROJECT_FILTER_ENABLED catalog and would otherwise drop the repo-scoped
    # hits). The unscoped catalog/content paths keep the intersection.
    if repo_scope is None:
        filtered = apply_project_filter(filtered, project_scope)

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


async def _handle_secure(
    arguments: dict[str, Any],
    caller: CallerPrincipal | None,
    project_scope: ProjectScope | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Handle the secure verb: vulnerability identification, planning, verification.

    Input validation (fail-fast):
    - At least one of cve, repo, package required
    - action=plan requires both cve AND repo
    - action=verify requires cve
    - Unknown action returns validation error

    ACL: fail-closed — unauthenticated caller returns empty findings.
    """
    cve = arguments.get("cve", "")
    repo = arguments.get("repo", "")
    package = arguments.get("package", "")
    action = arguments.get("action", "identify")
    severity_min = arguments.get("severity_min", "")
    reachable_only = arguments.get("reachable_only", False)

    # Input validation: at least one filter required
    if not cve and not repo and not package:
        return {
            "error": "At least one of 'cve', 'repo', or 'package' is required",
            "code": "validation_error",
        }

    # Validate action value
    valid_actions = ("identify", "plan", "verify")
    if action not in valid_actions:
        return {
            "error": f"Unknown action '{action}'. Must be one of: {', '.join(valid_actions)}",
            "code": "validation_error",
        }

    # Action-specific validation
    if action == "plan" and (not cve or not repo):
        return {
            "error": "action='plan' requires both 'cve' and 'repo'",
            "code": "validation_error",
        }

    if action == "verify" and not cve:
        return {
            "error": "action='verify' requires 'cve'",
            "code": "validation_error",
        }

    # ACL fail-closed: no identity → empty results
    if caller is None or not caller.is_resolved:
        return {
            "action": action,
            "findings": [],
            "findings_count": 0,
            "error": "unauthorized",
        }

    # Resolve tenant_id for Neptune scope filtering
    tenant_id = caller.tenant_id if caller else None

    # Dispatch to backend
    from .secure_backend import handle_identify, handle_plan, handle_verify

    if action == "identify":
        return await handle_identify(
            cve=cve,
            repo=repo,
            package=package,
            severity_min=severity_min,
            reachable_only=reachable_only,
            db_pool=state.db_pool,
            acl_store=state.acl_store,
            caller=caller,
            zoekt_backend=state.zoekt,
            neptune_driver=state.neptune_driver,
            s3_client=state.s3_client,
            s3_bucket=config.s3_bucket or "",
            tenant_id=tenant_id,
        )
    elif action == "plan":
        return await handle_plan(
            cve=cve,
            repo=repo,
            db_pool=state.db_pool,
            acl_store=state.acl_store,
            caller=caller,
            zoekt_backend=state.zoekt,
            neptune_driver=state.neptune_driver,
            s3_client=state.s3_client,
            s3_bucket=config.s3_bucket or "",
            tenant_id=tenant_id,
        )
    elif action == "verify":
        return await handle_verify(
            cve=cve,
            repo=repo or "",
            db_pool=state.db_pool,
        )

    return {"error": f"Unhandled action: {action}", "code": "internal_error"}


# ---------------------------------------------------------------------------
# ACL helper
# ---------------------------------------------------------------------------


def _apply_acl(hits: list[SearchHit], caller: CallerPrincipal | None) -> list[SearchHit]:
    """Apply ACL filtering to search hits.

    FAIL-CLOSED: If caller is None (no identity headers), always returns [].
    When no ACL store is configured (Postgres unavailable in dev), uses an
    AllowIndexedRepos store that permits access to all indexed repos but still
    enforces the identity-header requirement.

    Content-path hits (repo_name == "") represent shared platform content
    (wikis, indexes) that are not repo-scoped. They pass through ACL once
    the caller is authenticated — the identity gate at the top is sufficient.
    """
    # FAIL-CLOSED: no identity headers → empty results regardless of ACL store
    if caller is None:
        log.debug("_apply_acl: no caller principal, returning empty (fail-closed)")
        return []
    if not caller.is_resolved:
        log.debug("_apply_acl: caller unresolved, returning empty (fail-closed)")
        return []

    if state.acl_store is None:
        # No Postgres ACL store — use AllowIndexedRepos (dev-mode only).
        # This allows any authenticated caller to see all indexed repos,
        # while still enforcing the fail-closed rule for unauthenticated requests.
        return hits

    # Separate content-path hits (no repo scope) from repo-scoped hits.
    # Content-path hits are shared assets visible to any authenticated caller.
    content_hits = [h for h in hits if not h.repo_name]
    repo_hits = [h for h in hits if h.repo_name]

    filtered_repo = filter_results(repo_hits, caller, state.acl_store)
    return content_hits + filtered_repo


# ---------------------------------------------------------------------------
# Search ranking helper
# ---------------------------------------------------------------------------


def _file_relevance_score(file_path: str, query: str) -> int:
    """Score a file's relevance to the query based on filename proximity.

    Higher score = more relevant. Used to re-rank search results so that
    implementation files (whose names match the query) rank above files that
    merely reference the query term in their content.

    Scoring tiers:
      100 — exact filename match (query IS the filename)
       80 — query appears literally in the file path
       70 — query appears in the filename portion
       60 — normalized match (e.g., "ContentRouter" matches "content_router.py")
       50 — path component prefix match (e.g., "humanize" → "human/" dir)
        0 — no filename relevance signal (default)
    """
    query_lower = query.lower()
    file_lower = file_path.lower()

    # Normalize: strip separators for fuzzy matching
    query_normalized = re.sub(r"[-_./]", "", query_lower)
    file_normalized = re.sub(r"[-_./]", "", file_lower)

    # Get just the filename
    filename = file_path.split("/")[-1].lower()
    filename_stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    # Tier 100: exact filename or stem match
    if query_lower == filename or query_lower == filename_stem:
        return 100

    # Tier 80: query appears literally in file path
    if query_lower in file_lower:
        return 80

    # Tier 70: query in filename
    if query_lower in filename:
        return 70

    # Tier 60: normalized match (ContentRouter → contentrouter in contentrouter.py)
    if query_normalized in file_normalized:
        return 60

    # Tier 50: path component prefix match (humanize → human)
    # Require overlap to be at least 60% of query length to avoid false boosts
    path_parts = re.split(r"[-_./]", file_lower)
    for pp in path_parts:
        if pp and len(pp) >= 4:
            if query_lower.startswith(pp) and len(pp) / len(query_lower) >= 0.6:
                return 50
            if pp.startswith(query_lower) and len(query_lower) >= 4:
                return 50

    return 0


# ---------------------------------------------------------------------------
# Mount native MCP (Streamable HTTP) sub-app — Issue #1602
# ---------------------------------------------------------------------------
# Placed at module bottom to avoid circular import: mcp_app imports TOOLS and
# _dispatch_tool from this module, so this module must be fully defined first.

from .mcp_app import get_mcp_app  # noqa: E402

app.mount("/mcp", get_mcp_app())

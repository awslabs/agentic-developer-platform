"""Native MCP server (Streamable HTTP) for the Door.

Mounts on the existing FastAPI app at ``/mcp``, exposing the same 6 verbs
(search, understand, impact, browse, remember, experience) as native MCP tools
over JSON-RPC 2.0.

Design decisions (Issue #1602, approved design):
- ``stateless_http=True`` is an ACL-correctness requirement: guarantees each
  JSON-RPC message is its own HTTP request carrying its own identity headers.
- Tool handlers are thin shims over ``_dispatch_tool()``; no logic duplication.
- ``_tools_to_json_schema()`` converts the bespoke ``TOOLS`` format to standard
  JSON Schema for validation (TOOLS remains the single source of truth).
- ``_extract_headers()`` is shared between REST and MCP paths for parity.

Endpoints after mounting:
- ``/mcp`` = native MCP (JSON-RPC 2.0) for MCP clients
- ``/call`` = legacy REST for recall-at-task-start.ts, experience-save-hook.ts
- Both route to the same underlying ``_dispatch_tool()`` + ``_handle_*`` functions.

References:
- Issue: #1602 (parent: #1586)
- Supersedes: #1592 in-process-wrapper approach (now a URL mount)
- ACL gate: door/acl.py (extract_caller_principal, filter_results)
- Identity: personal_context/identity.py (extract_identity)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import FastMCP
from mcp.server.fastmcp import Context
from starlette.requests import Request

from .acl import extract_caller_principal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared header extraction helper (used by BOTH REST and MCP paths)
# ---------------------------------------------------------------------------


def _extract_headers(request: Request) -> dict[str, str]:
    """Extract HTTP headers as a plain dict from a Starlette Request.

    Bridges the Starlette ``Headers`` multidict to the ``dict[str, str]``
    expected by ``extract_caller_principal()`` and ``extract_identity()``.
    Both REST and MCP paths must use this to ensure ACL parity.
    """
    return dict(request.headers)


# ---------------------------------------------------------------------------
# Schema conversion: bespoke TOOLS format -> JSON Schema (C1)
# ---------------------------------------------------------------------------


def _tools_to_json_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert one bespoke TOOLS entry to standard JSON Schema ``inputSchema``.

    The existing ``TOOLS`` constant uses per-parameter dicts like::

        {"type": "string", "required": True, "enum": [...]}

    MCP's ``tools/list`` requires standard JSON Schema::

        {"type": "object", "properties": {...}, "required": [...]}

    This function performs that conversion. ``TOOLS`` remains the single source
    of truth; this converter is used for MCP registration and test validation.
    """
    parameters = tool.get("parameters", {})
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param_def in parameters.items():
        # Build the JSON Schema property
        prop: dict[str, Any] = {}
        if "type" in param_def:
            prop["type"] = param_def["type"]
        if "enum" in param_def:
            prop["enum"] = param_def["enum"]
        properties[param_name] = prop

        # Collect required fields
        if param_def.get("required", False):
            required.append(param_name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema


# ---------------------------------------------------------------------------
# Lazy access to server internals (avoids circular import)
# ---------------------------------------------------------------------------


def _get_dispatch_tool():
    """Get _dispatch_tool from server module (lazy import to break cycle).

    mcp_app is imported at the bottom of server.py (after all definitions).
    Tool handlers call this at invocation time, not import time, so the
    import always succeeds.
    """
    from .server import _dispatch_tool

    return _dispatch_tool


# ---------------------------------------------------------------------------
# MCP server construction
# ---------------------------------------------------------------------------

# Create the FastMCP server instance with stateless_http=True.
# stateless_http=True is an ACL-correctness requirement (not just scaling):
# it guarantees each JSON-RPC message is its own HTTP request, so identity
# headers (x-github-login, x-github-teams, x-owner-sub, x-tenant-id) are
# always per-request. Without this, session reuse could break header semantics.
mcp_server = FastMCP(
    name="context-mcp",
    instructions=(
        "Knowledge Layer code verbs: search, understand, impact, browse, "
        "remember, experience. All results are ACL-filtered per caller identity."
    ),
    stateless_http=True,
    # The sub-app is mounted at /mcp on the parent FastAPI app, so the internal
    # route is "/" (resolves to POST /mcp externally).
    streamable_http_path="/",
)


# ---------------------------------------------------------------------------
# Tool handlers (thin shims delegating to _dispatch_tool)
# ---------------------------------------------------------------------------

# Tool descriptions in the docstrings below match the TOOLS constant.
# The MCP SDK uses these docstrings for the tool descriptions in tools/list.
# Tests verify parity with the TOOLS constant via _tools_to_json_schema.


@mcp_server.tool(name="search")
async def mcp_search(
    query: str,
    scope: str = "code",
    limit: int = 20,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Find relevant code, documentation, and past learnings."""
    headers = _get_headers_from_context(ctx)
    caller = extract_caller_principal(headers)
    arguments = {"query": query, "scope": scope, "limit": limit}
    dispatch = _get_dispatch_tool()
    result = await dispatch("search", arguments, headers, caller)
    return json.dumps(result, default=str)


@mcp_server.tool(name="understand")
async def mcp_understand(
    target: str,
    depth: str = "overview",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Get deep understanding of a specific repo, directory, or file."""
    headers = _get_headers_from_context(ctx)
    caller = extract_caller_principal(headers)
    arguments = {"target": target, "depth": depth}
    dispatch = _get_dispatch_tool()
    result = await dispatch("understand", arguments, headers, caller)
    return json.dumps(result, default=str)


@mcp_server.tool(name="impact")
async def mcp_impact(
    target: str,
    cross_repo: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Analyse blast radius before editing or deleting a symbol."""
    headers = _get_headers_from_context(ctx)
    caller = extract_caller_principal(headers)
    arguments = {"target": target, "cross_repo": cross_repo}
    dispatch = _get_dispatch_tool()
    result = await dispatch("impact", arguments, headers, caller)
    return json.dumps(result, default=str)


@mcp_server.tool(name="browse")
async def mcp_browse(
    action: str,
    uri: str,
    depth: int = 1,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Navigate the indexed content filesystem."""
    headers = _get_headers_from_context(ctx)
    caller = extract_caller_principal(headers)
    arguments = {"action": action, "uri": uri, "depth": depth}
    dispatch = _get_dispatch_tool()
    result = await dispatch("browse", arguments, headers, caller)
    return json.dumps(result, default=str)


@mcp_server.tool(name="remember")
async def mcp_remember(
    session_id: str,
    messages: list,
    outcome: str = "",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Save session context, decisions, and learnings to long-term memory."""
    headers = _get_headers_from_context(ctx)
    arguments = {"session_id": session_id, "messages": messages, "outcome": outcome}
    caller = extract_caller_principal(headers)
    dispatch = _get_dispatch_tool()
    result = await dispatch("remember", arguments, headers, caller)
    return json.dumps(result, default=str)


@mcp_server.tool(name="experience")
async def mcp_experience(
    action: str,
    persona: str,
    content: str = "",
    learning_type: str = "",
    context: dict | None = None,
    query: str = "",
    visibility: str = "",
    limit: int = 0,
    cross_persona: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Save or recall experiential knowledge (per-user, persona-scoped)."""
    headers = _get_headers_from_context(ctx)
    # Build arguments dict, only including non-default values
    arguments: dict[str, Any] = {"action": action, "persona": persona}
    if content:
        arguments["content"] = content
    if learning_type:
        arguments["learning_type"] = learning_type
    if context is not None:
        arguments["context"] = context
    if query:
        arguments["query"] = query
    if visibility:
        arguments["visibility"] = visibility
    if limit:
        arguments["limit"] = limit
    if cross_persona:
        arguments["cross_persona"] = cross_persona
    caller = extract_caller_principal(headers)
    dispatch = _get_dispatch_tool()
    result = await dispatch("experience", arguments, headers, caller)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Header extraction from MCP context
# ---------------------------------------------------------------------------


def _get_headers_from_context(ctx: Context | None) -> dict[str, str]:
    """Extract HTTP headers from the MCP Context's underlying request.

    In stateless_http mode, each JSON-RPC message is a fresh HTTP request,
    so the Request object reliably carries the headers for that specific call.
    This is what makes per-request ACL correct under MCP transport.

    Falls back to empty dict if context is unavailable (e.g., in tests without
    a real HTTP transport), which triggers fail-closed ACL behavior.
    """
    if ctx is None:
        return {}
    try:
        request = ctx.request_context.request
        if request is not None:
            return _extract_headers(request)
    except (ValueError, AttributeError):
        # Context not available outside of a request, or no request attached
        pass
    return {}


# ---------------------------------------------------------------------------
# ASGI app for mounting
# ---------------------------------------------------------------------------


def get_mcp_app():
    """Return the Starlette ASGI app for the MCP Streamable HTTP endpoint.

    Mount this on the FastAPI app: ``app.mount("/mcp", get_mcp_app())``
    The session manager lifespan is managed by the Starlette sub-app independently.
    The MCP tool shims reference the module-level ``state`` object (via
    ``_dispatch_tool``), which is initialized by the parent FastAPI app's lifespan
    (startup order: parent first, then sub-apps).
    """
    return mcp_server.streamable_http_app()

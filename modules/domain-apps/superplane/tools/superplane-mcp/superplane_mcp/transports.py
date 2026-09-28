"""The two transports, as explicit thin shims over `dispatch_tool()`.

Mirrors the door's arrangement (`door/mcp_app.py:10`: "Tool handlers are thin
shims over `_dispatch_tool()`; no logic duplication") and exists to make R9
acc. 5 checkable: if every transport is a shim that only reshapes the envelope,
then authorization cannot differ between them, because none of them makes an
authorization decision at all.

"Thin" is a hard constraint here, not a style note. Each function below extracts
headers, calls `dispatch_tool`, and formats the return value. There is no
validation, no scoping, no capability check and no error interpretation in this
module — every one of those lives behind the single dispatch. A future transport
should be addable in the same handful of lines, and `tests/test_tool_separation.py`
asserts that all transports resolve to the same decision for the same input.
"""

from __future__ import annotations

import json
from typing import Any

from .contract import CapacityContract
from .server import TOOLS, dispatch_tool


def normalize_headers(raw: Any) -> dict[str, str]:
    """Lower-case a header mapping from any transport's native representation.

    Accepts a plain dict or an iterable of (name, value) pairs — ASGI scopes
    carry the latter, often as bytes. Normalizing in one shared place is what
    keeps a caller's identity from resolving differently per transport, which is
    the failure mode R9 acc. 5 targets.
    """
    if raw is None:
        return {}
    items: Any
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = raw
    out: dict[str, str] = {}
    for key, value in items:
        if isinstance(key, bytes):
            key = key.decode("latin-1")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        out[str(key).lower()] = str(value)
    return out


# ---------------------------------------------------------------------------
# Transport 1: REST (`POST /call`) — the door's legacy-compatible shape.
# ---------------------------------------------------------------------------


def rest_call(
    body: dict[str, Any],
    headers: Any,
    contract: CapacityContract | None = None,
) -> dict[str, Any]:
    """Handle a REST tool call. Shim only: reshape, dispatch, return."""
    return dispatch_tool(
        body.get("name", ""),
        body.get("arguments") or {},
        normalize_headers(headers),
        contract=contract,
    )


# ---------------------------------------------------------------------------
# Transport 2: MCP (JSON-RPC 2.0 `tools/call`).
# ---------------------------------------------------------------------------


def mcp_list_tools() -> list[dict[str, Any]]:
    """Advertise the tool set as JSON Schema, derived from `TOOLS`.

    Generated from the same `TOOLS` constant the REST path uses so the two
    transports cannot advertise different tool sets — a divergence that would let
    a spending tool appear on one transport while being invisible on the other.
    """
    listed: list[dict[str, Any]] = []
    for tool in TOOLS:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param, spec in tool["parameters"].items():
            properties[param] = {"type": spec["type"]}
            if spec.get("required"):
                required.append(param)
        listed.append(
            {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        )
    return listed


def mcp_call_tool(
    name: str,
    arguments: dict[str, Any] | None,
    headers: Any,
    contract: CapacityContract | None = None,
) -> dict[str, Any]:
    """Handle an MCP `tools/call`. Shim only: reshape, dispatch, wrap.

    Returns MCP content blocks. The payload is the *same* dict the REST
    transport returns, JSON-encoded — deliberately not re-derived, so the two
    transports cannot disagree about what a tool returned.
    """
    result = dispatch_tool(
        name, arguments or {}, normalize_headers(headers), contract=contract
    )
    return {
        "content": [{"type": "text", "text": json.dumps(result, default=str)}],
        # Surfaced so an MCP client can branch on failure without parsing prose.
        "isError": "error" in result,
    }

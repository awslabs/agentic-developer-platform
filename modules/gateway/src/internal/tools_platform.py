"""First-party, read-only platform tools in the tenant tool catalogue (#8113).

The adp-tools-platform flow (#7245) shipped one tools layer: a catalogue, explicit
tenant/team entitlements (``tool_entitlements``), a per-user protected policy
(``TOOLS_POLICY#<owner>``: admin-written ``allowed_tools``, owner-written
``consented_tools``) and one authorization decision per call. Until now that
catalogue was read exclusively from the deployed AgentCore gateway, so the chat
assistant had no tool that could read ADP itself.

This module registers the platform's own read tools as catalogue entries of the
same shape (``<namespace>.<operation>`` permission names, ``<namespace>___<operation>``
MCP names, JSON input schemas) so they flow through the SAME entitlement, consent
and access-report code paths as every other tool. Two properties distinguish them,
and both are declared here rather than inferred anywhere else:

* ``first_party``: they execute inside the gateway under the owner's own verified
  chat identity (``/v1/chat/data/tools/call``), never through the tools façade or
  AgentCore. They are therefore ``tool_chat_data_plane_only`` on the façade
  authorization path.
* ``read_only`` with no provider cost and no credential beyond the owner's identity:
  once a tenant entitles one, every tenant member is implicitly *allowed* it and only
  the owner's own consent remains (``implicitly_allowed``). The admin per-user
  ``allowed_tools`` writer keeps governing third-party tools unchanged.
"""

from __future__ import annotations

PLATFORM_CONNECTOR = "platform"

_WINDOW = {
    "since": {"type": "string", "format": "date-time", "description": "Inclusive ISO-8601 start of the window (defaults to the recent past)."},
    "until": {"type": "string", "format": "date-time", "description": "Exclusive ISO-8601 end of the window (defaults to now)."},
}

PLATFORM_TOOLS: dict[str, dict] = {
    "platform.my_flows": {
        "description": (
            "The delivery flows (epics, waves, stories, gates) in the owner's tenant with derived status, "
            "attention counts and last update. Read-only; reports source and freshness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Flows to return, most recently updated first."},
                "status": {
                    "type": "string",
                    "enum": ["attention_needed", "awaiting_you", "running", "queued", "complete"],
                    "description": "Only flows in this derived status.",
                },
                "include_archived": {"type": "boolean", "description": "Include archived flows (default false)."},
            },
            "additionalProperties": False,
        },
    },
    "platform.my_agent_runs": {
        "description": (
            "The owner's recent ADP agent invocations and tasks (persona, status, repository, issue, links) in a time window. "
            "A completed run is not proof an issue is closed. Read-only; reports source, window and freshness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {**_WINDOW, "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Runs to return, newest first."}},
            "additionalProperties": False,
        },
    },
    "platform.my_work_items": {
        "description": (
            "GitHub issues and pull requests the owner authored, is assigned to or is otherwise involved in, across the repositories "
            "the tenant's GitHub App installation can see (state, repository, title, updated time, link). Read-only; reports coverage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Item state filter (default open)."},
                "kind": {"type": "string", "enum": ["issue", "pull_request", "all"], "description": "Issues, pull requests or both (default all)."},
                "since": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only items updated at or after this instant (default 30 days ago).",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Items to return, most recently updated first."},
            },
            "additionalProperties": False,
        },
    },
    "platform.my_budget": {
        "description": (
            "The owner's own budget position for a calendar period: binding cap, settled spend, headroom and whether spend is still "
            "being settled. Read-only; never another person's figures."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period_type": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "Calendar period (default monthly)."}
            },
            "additionalProperties": False,
        },
    },
}

PLATFORM_TOOL_NAMES = frozenset(PLATFORM_TOOLS)


def mcp_name(permission: str) -> str:
    """``platform.my_flows`` -> ``platform___my_flows`` (the model-facing/MCP form)."""
    return permission.replace(".", "___", 1)


def permission_name(tool: str) -> str:
    """``platform___my_flows`` -> ``platform.my_flows`` (the entitlement/consent form)."""
    return tool.replace("___", ".", 1)


def is_platform_tool(name: str) -> bool:
    """True for either spelling of a registered first-party tool."""
    return name in PLATFORM_TOOL_NAMES or permission_name(name) in PLATFORM_TOOL_NAMES


def implicitly_allowed(name: str) -> bool:
    """First-party read-only tools need no admin per-user ``allowed_tools`` grant.

    Tenant entitlement (admin) and the owner's own consent still both apply.
    """
    return is_platform_tool(name)


def platform_catalogue() -> dict:
    """Catalogue entries in the exact shape ``gateway_catalogue()`` produces, plus the two flags."""
    tools = [
        {
            "name": name,
            "description": spec["description"],
            "connector": PLATFORM_CONNECTOR,
            "credential_kinds": [],
            "inputSchema": spec["inputSchema"],
            "first_party": True,
            "read_only": True,
        }
        for name, spec in sorted(PLATFORM_TOOLS.items())
    ]
    return {"tools": tools, "connectors": [{"name": PLATFORM_CONNECTOR, "credential_kinds": [], "dynamic": False, "first_party": True}]}


__all__ = [
    "PLATFORM_CONNECTOR",
    "PLATFORM_TOOLS",
    "PLATFORM_TOOL_NAMES",
    "implicitly_allowed",
    "is_platform_tool",
    "mcp_name",
    "permission_name",
    "platform_catalogue",
]

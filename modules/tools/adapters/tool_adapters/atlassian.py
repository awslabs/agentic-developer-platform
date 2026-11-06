"""Discover and call only reviewed, schema-pinned Atlassian operations."""

import hashlib
import http.client
import re
import time

from tool_adapters.atlassian_observe import (
    MAX_CATALOGUE_BYTES,
    MAX_CATALOGUE_PAGES,
    MAX_CATALOGUE_TOOLS,
    MAX_DEFINITION_BYTES,
    PROTOCOL_VERSION,
    MCPTransport,
    ProviderResponseError,
    canonical,
)
from tool_adapters.schema_contract import same_input_contract

READ_TOOLS = {
    "search_jira": ("searchJiraIssuesUsingJql", "search:jira:agent-interface"),
    "get_jira_issue": ("getJiraIssue", "read:jira:agent-interface"),
    "search_confluence": ("searchConfluence", "search:confluence:agent-interface"),
    "get_confluence_page": ("getConfluenceContent", "read:confluence:agent-interface"),
}
WRITE_TOOLS = {
    "create_jira_issue": ("createJiraIssue", "write:jira:agent-interface"),
    "update_jira_issue": ("editJiraIssue", "write:jira:agent-interface"),
    "comment_jira_issue": ("addOrEditJiraIssueComment", "write:jira:agent-interface"),
    "transition_jira_issue": ("transitionJiraIssue", "write:jira:agent-interface"),
}


class ReadMCPTransport(MCPTransport):
    def __init__(self, *, deadline):
        super().__init__(
            deadline=deadline, catalogue_bytes=MAX_CATALOGUE_BYTES + 128 * 1024, result_bytes=MAX_CATALOGUE_BYTES
        )

    def send(self, method, params, token):
        original_deadline = self.deadline
        if method == "tools/call":
            self.deadline = min(original_deadline, time.monotonic() + 5)
        try:
            try:
                return self._attempt(method, params, token)
            except ProviderResponseError as error:
                if (
                    method != "tools/call"
                    or error.code not in {"rate_limited", "provider_unavailable"}
                    or (error.code == "rate_limited" and error.retry_after is None)
                    or time.monotonic() + (error.retry_after or 0) >= self.deadline
                ):
                    raise
                if error.retry_after:
                    time.sleep(error.retry_after)
                return self._attempt(method, params, token)
        finally:
            self.deadline = original_deadline

    def _attempt(self, method, params, token):
        try:
            return super().send(method, params, token)
        except (OSError, http.client.HTTPException):
            raise ProviderResponseError("provider_unavailable") from None


class WriteMCPTransport(MCPTransport):
    def __init__(self, *, deadline):
        super().__init__(
            deadline=deadline, catalogue_bytes=MAX_CATALOGUE_BYTES + 128 * 1024, result_bytes=MAX_CATALOGUE_BYTES
        )

    def send(self, method, params, token):
        try:
            return super().send(method, params, token)
        except (OSError, http.client.HTTPException):
            raise ProviderResponseError("provider_unavailable") from None


def discover_reads(token, schema_digests, *, deadline, transport_factory=ReadMCPTransport, reviewed_schemas=None):
    """Verify all four contracts, retaining pinned schemas after description edits."""
    return _discover(
        token,
        schema_digests,
        READ_TOOLS,
        deadline=deadline,
        transport_factory=transport_factory,
        reviewed_schemas=reviewed_schemas,
    )


def discover_writes(token, schema_digests, *, deadline, transport_factory=WriteMCPTransport):
    """Refuse the entire write catalogue unless all four schemas match reviewed pins."""
    return _discover(token, schema_digests, WRITE_TOOLS, deadline=deadline, transport_factory=transport_factory)


def _discover(token, schema_digests, allowed_tools, *, deadline, transport_factory, reviewed_schemas=None):
    deadline = min(deadline, time.monotonic() + 20)
    if (
        not isinstance(schema_digests, dict)
        or set(schema_digests) != set(allowed_tools)
        or any(
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in schema_digests.values()
        )
    ):
        raise ValueError("unverified provider schema")
    transport = transport_factory(deadline=deadline)
    initial = transport.send(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "adp-atlassian-reads", "version": "1"},
        },
        token,
    )
    if not isinstance(initial, dict) or initial.get("result", {}).get("protocolVersion") != PROTOCOL_VERSION:
        raise ValueError("provider protocol mismatch")
    transport.send("notifications/initialized", {}, token)
    tools = {}
    seen_cursors = set()
    cursor = None
    total_bytes = 0
    for _ in range(MAX_CATALOGUE_PAGES):
        reply = transport.send("tools/list", {"cursor": cursor} if cursor else {}, token)
        page = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(page, dict) or type(page.get("tools")) is not list or "error" in reply:
            raise ValueError("catalogue_incomplete")
        for tool in page["tools"]:
            if type(tool) is not dict or not isinstance(tool.get("name"), str) or tool["name"] in tools:
                raise ValueError("catalogue_incomplete")
            definition_bytes = len(canonical(tool))
            total_bytes += definition_bytes
            if (
                definition_bytes > MAX_DEFINITION_BYTES
                or total_bytes > MAX_CATALOGUE_BYTES
                or len(tools) >= MAX_CATALOGUE_TOOLS
            ):
                raise ValueError("catalogue_incomplete")
            tools[tool["name"]] = tool
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512 or cursor in seen_cursors:
            raise ValueError("catalogue_incomplete")
        seen_cursors.add(cursor)
    else:
        raise ValueError("catalogue_incomplete")
    result = []
    for alias, (upstream, _) in allowed_tools.items():
        tool = tools.get(upstream)
        if tool is None:
            raise ValueError("catalogue_incomplete")
        schema = tool.get("inputSchema") if isinstance(tool, dict) else None
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or not isinstance(schema.get("properties"), dict)
        ):
            raise ValueError("unverified provider schema")
        if hashlib.sha256(canonical(schema)).hexdigest() != schema_digests[alias]:
            reviewed = reviewed_schemas.get(alias) if isinstance(reviewed_schemas, dict) else None
            if (
                reviewed is None
                or hashlib.sha256(canonical(reviewed)).hexdigest() != schema_digests[alias]
                or not same_input_contract(schema, reviewed)
            ):
                raise ValueError("unverified provider schema")
            schema = reviewed
        result.append({"name": alias, "description": tool.get("description") or alias, "inputSchema": schema})
    if time.monotonic() >= deadline:
        raise ValueError("provider deadline exceeded")
    return result


def read_tool(
    token, alias, arguments, schema_digests, *, deadline, transport_factory=ReadMCPTransport, reviewed_schemas=None
):
    """Call a pinned read using the same authenticated MCP session as discovery."""
    deadline = min(deadline, time.monotonic() + 20)
    if alias not in READ_TOOLS or type(arguments) is not dict:
        raise ValueError("unsupported read operation")
    transport = transport_factory(deadline=deadline)
    catalogue = discover_reads(
        token,
        schema_digests,
        deadline=deadline,
        transport_factory=lambda **_: transport,
        reviewed_schemas=reviewed_schemas,
    )
    schema = next(tool["inputSchema"] for tool in catalogue if tool["name"] == alias)
    if any(key not in schema["properties"] for key in arguments) or any(
        key not in arguments for key in schema.get("required", [])
    ):
        raise ValueError("invalid read arguments")
    reply = transport.send("tools/call", {"name": READ_TOOLS[alias][0], "arguments": arguments}, token)
    if (
        not isinstance(reply, dict)
        or "error" in reply
        or not isinstance(reply.get("result"), dict)
        or reply["result"].get("isError")
        or (
            not isinstance(reply["result"].get("structuredContent"), dict)
            and not isinstance(reply["result"].get("content"), list)
        )
    ):
        raise ProviderResponseError("provider_unavailable")
    if time.monotonic() >= deadline:
        raise ValueError("provider deadline exceeded")
    return reply["result"]


def write_tool(token, alias, arguments, schema_digests, *, deadline, transport_factory=WriteMCPTransport):
    """Send one approved provider mutation, without read-style automatic retry.

    The caller must durably claim dispatch before entering this function. An
    ambiguous exception here must be recorded as unknown, never retried.
    """
    deadline = min(deadline, time.monotonic() + 20)
    if alias not in WRITE_TOOLS or type(arguments) is not dict:
        raise ValueError("unsupported write operation")
    transport = transport_factory(deadline=deadline)
    catalogue = discover_writes(token, schema_digests, deadline=deadline, transport_factory=lambda **_: transport)
    schema = next(tool["inputSchema"] for tool in catalogue if tool["name"] == alias)
    if any(key not in schema["properties"] for key in arguments) or any(
        key not in arguments for key in schema.get("required", [])
    ):
        raise ValueError("invalid write arguments")
    if alias == "comment_jira_issue" and _has_key(arguments, {"commentid", "id", "edit", "mode", "operation"}):
        raise ValueError("comment edits are not supported")
    if alias == "transition_jira_issue" and _has_key(arguments, {"sprint", "sprintid", "backlog", "rank"}):
        raise ValueError("transition side effects are not supported")
    reply = transport.send("tools/call", {"name": WRITE_TOOLS[alias][0], "arguments": arguments}, token)
    if (
        not isinstance(reply, dict)
        or "error" in reply
        or not isinstance(reply.get("result"), dict)
        or reply["result"].get("isError")
        or (
            not isinstance(reply["result"].get("structuredContent"), dict)
            and not isinstance(reply["result"].get("content"), list)
        )
    ):
        raise ProviderResponseError("provider_unavailable")
    if time.monotonic() >= deadline:
        raise ValueError("provider deadline exceeded")
    return reply["result"]


def _has_key(value, forbidden):
    if isinstance(value, dict):
        return any(key.lower() in forbidden or _has_key(item, forbidden) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_key(item, forbidden) for item in value)
    return False

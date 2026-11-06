"""Identity-authorized, bounded MCP tool discovery for the standalone probe."""

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time

from tests.e2e.tools.atlassian_contract import load_contract, require_observed_schema
from tests.e2e.tools.atlassian_oauth_probe import ALLOWED_SCOPES, IDENTITY_SCOPES, ProbeBlocked


PROTOCOL_VERSION = "2025-03-26"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class MCPTransport:
    """Pinned HTTPS destination with DNS-address and response-size bounds."""

    def __init__(self, contract):
        self.contract = contract
        self.session_id = None
        self.request_id = 0
        self.deadline = time.monotonic() + contract["retry"]["total_upstream_seconds"]

    def send(self, method, params, token):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeBlocked("Upstream discovery deadline exceeded")
        if method == "notifications/initialized":
            payload = {"jsonrpc": "2.0", "method": method}
        else:
            self.request_id += 1
            payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        host = "mcp.atlassian.com"
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
                raise ProbeBlocked("Provider host resolved to a non-public address")
            address = addresses[0][4][0]
            connection = http.client.HTTPSConnection(host, timeout=min(2, remaining))
            try:
                raw_socket = socket.create_connection((address, 443), timeout=min(2, remaining))
                connection.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
                connection.sock.settimeout(min(5, self.deadline - time.monotonic()))
                headers = {
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": PROTOCOL_VERSION,
                }
                if self.session_id:
                    headers["MCP-Session-Id"] = self.session_id
                connection.request("POST", "/v2/mcp?tools=all", body=canonical(payload), headers=headers)
                response = connection.getresponse()
                if response.status not in (200, 202):
                    raise ProbeBlocked("MCP request rejected or redirected")
                session_id = response.getheader("MCP-Session-Id")
                if session_id:
                    if len(session_id) > 256 or not session_id.isascii() or not session_id.isprintable():
                        raise ProbeBlocked("Invalid MCP session identifier")
                    self.session_id = session_id
                limit = self.contract["retry"]["catalogue_max_total_bytes"]
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise ProbeBlocked("MCP response exceeds catalogue limit")
                if method == "notifications/initialized":
                    return None
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                if content_type == "text/event-stream":
                    events = [line[5:].strip() for line in body.decode().splitlines() if line.startswith("data:")]
                    responses = [json.loads(event) for event in events if event != "[DONE]"]
                    matches = [item for item in responses if isinstance(item, dict) and item.get("id") == self.request_id]
                    if len(matches) != 1:
                        raise ProbeBlocked("MCP event response is incomplete")
                    return matches[0]
                if content_type != "application/json":
                    raise ProbeBlocked("Unsupported MCP response type")
                result = json.loads(body)
                if not isinstance(result, dict) or result.get("id") != self.request_id:
                    raise ProbeBlocked("MCP response does not match the request")
                return result
            finally:
                connection.close()
        except ProbeBlocked:
            raise
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProbeBlocked("MCP transport or response failed") from None


def discover(ledger, owner, selected_site, granted_scopes, accessible_sites, pinned_schema_hashes=None, transport_factory=MCPTransport, *, tenant=None):
    """Return only candidate hashes; a fixture match never proves a live grant."""
    contract = load_contract()
    if not selected_site or selected_site not in accessible_sites:
        raise ProbeBlocked("The selected site is not in the provider-authorized site set")
    if not set(granted_scopes) <= ALLOWED_SCOPES or not set(IDENTITY_SCOPES) <= set(granted_scopes):
        raise ProbeBlocked("MCP identity/site and managed-refresh scopes must be observed; legacy scopes are not a grant")
    transport = transport_factory(contract)

    def collect(token):
        response = transport.send("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "adp-atlassian-proof", "version": "1"}}, token)
        if not isinstance(response, dict) or response.get("error") or response.get("result", {}).get("protocolVersion") != PROTOCOL_VERSION:
            raise ProbeBlocked("MCP protocol negotiation failed")
        transport.send("notifications/initialized", {}, token)
        tools = {}
        cursor = None
        seen_cursors = set()
        total_bytes = 0
        for _ in range(contract["retry"]["catalogue_max_pages"]):
            response = transport.send("tools/list", {"cursor": cursor} if cursor else {}, token)
            if not isinstance(response, dict) or response.get("error") or not isinstance(response.get("result"), dict):
                raise ProbeBlocked("MCP catalogue page failed")
            page = response["result"]
            if not isinstance(page.get("tools"), list):
                raise ProbeBlocked("MCP catalogue page is incomplete")
            for tool in page["tools"]:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), dict):
                    raise ProbeBlocked("MCP tool name or input schema is missing")
                encoded = canonical(tool)
                if len(encoded) > contract["retry"]["catalogue_max_definition_bytes"]:
                    raise ProbeBlocked("MCP tool definition exceeds the bound")
                total_bytes += len(encoded)
                if total_bytes > contract["retry"]["catalogue_max_total_bytes"] or len(tools) >= contract["retry"]["catalogue_max_tools"]:
                    raise ProbeBlocked("MCP catalogue exceeds the bound")
                if tool["name"] in tools:
                    raise ProbeBlocked("MCP catalogue contains duplicate tool names")
                tools[tool["name"]] = tool
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise ProbeBlocked("MCP catalogue cursor is invalid or looping")
            seen_cursors.add(cursor)
        else:
            raise ProbeBlocked("MCP catalogue exceeds the page bound")
        schemas = {}
        for operation in contract["operations"]:
            method = operation["method"]
            if method not in tools:
                raise ProbeBlocked("Required named MCP operation is missing")
            if operation["scope"] not in granted_scopes:
                raise ProbeBlocked("Required operation scope is not in the observed grant")
            schema = tools[method]["inputSchema"]
            if schema.get("type") != "object":
                raise ProbeBlocked("Required MCP input schema is not an object")
            digest = hashlib.sha256(canonical(schema)).hexdigest()
            schemas[method] = digest
            if pinned_schema_hashes is not None:
                expected = require_observed_schema(operation, pinned_schema_hashes.get(method))
                if digest != expected["observed_schema_sha256"]:
                    raise ProbeBlocked("Observed MCP schema differs from the pinned contract")
        return {
            "status": "candidate_match" if pinned_schema_hashes is not None else "blocked_unpinned_schemas",
            "revision": contract["revision"],
            "catalogue_sha256": hashlib.sha256(canonical(tools)).hexdigest(),
            "schema_sha256": schemas,
            "tool_count": len(tools),
            "site_sha256": hashlib.sha256(selected_site.encode()).hexdigest(),
        }

    try:
        return ledger.observe(owner, collect, tenant=tenant)
    except ProbeBlocked:
        raise
    except Exception:
        raise ProbeBlocked("Identity-authorized MCP discovery failed") from None

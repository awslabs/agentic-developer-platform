"""Bounded Atlassian MCP identity/site observations, never user tool execution."""

import asyncio
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time

from adp_tools.adapter_contracts import ObservedSite
from fastapi import HTTPException

PROTOCOL_VERSION = "2025-03-26"
RESOURCE_PATH = "/v2/mcp?tools=all"
SUBJECT_TOOL = "atlassianUserInfo"
SITES_TOOL = "getAccessibleAtlassianResources"
MAX_CATALOGUE_PAGES = 32
MAX_CATALOGUE_TOOLS = 1024
MAX_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_DEFINITION_BYTES = 256 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class ProviderResponseError(ValueError):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("ambiguous provider observation")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("invalid provider observation")


def _decode_json(data):
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _observation_value(result, normalize):
    """Read MCP structured data or its standard JSON text representation."""
    if type(result) is not dict or result.get("isError", False) is not False:
        raise ValueError("provider observation unavailable")
    values = []
    if "structuredContent" in result:
        if type(result["structuredContent"]) is not dict:
            raise ValueError("provider observation unavailable")
        values.append(normalize(result["structuredContent"]))
    content = result.get("content", [])
    if type(content) is not list:
        raise ValueError("provider observation unavailable")
    if content:
        if len(content) != 1 or type(content[0]) is not dict or content[0].get("type") != "text":
            raise ValueError("ambiguous provider observation")
        text = content[0].get("text")
        if type(text) is not str or len(text.encode()) > 65536:
            raise ValueError("provider observation unavailable")
        value = _decode_json(text)
        values.append(normalize(value))
    if not values or any(canonical(value) != canonical(values[0]) for value in values[1:]):
        raise ValueError("ambiguous provider observation")
    return values[0]


def _subject(value):
    # Atlassian ROVO-999 documents account_id plus optional OAuth profile data.
    # MCP also returns {"data": {"accountId": ...}}. Compare every supplied ID
    # across the envelope instead of choosing one when the provider disagrees.
    if type(value) is not dict or "error" in value:
        raise ValueError("provider subject changed")
    identifiers = [value[key] for key in ("account_id", "accountId") if key in value]
    if "data" in value:
        data = value["data"]
        if type(data) is not dict or "error" in data or not {"account_id", "accountId"} & data.keys():
            raise ValueError("provider subject changed")
        identifiers.extend(data[key] for key in ("account_id", "accountId") if key in data)
    if (
        not identifiers
        or any(type(item) is not str or not 0 < len(item) <= 512 for item in identifiers)
        or any(item != identifiers[0] for item in identifiers)
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in identifiers[0])
    ):
        raise ValueError("provider subject changed")
    return identifiers[0]


def _sites(value):
    if type(value) is dict and set(value) == {"data"}:
        value = value["data"]
    if type(value) is dict and set(value) == {"resources"}:
        value = value["resources"]
    if type(value) is not list or not 0 < len(value) <= 1000:
        raise ValueError("provider sites changed")
    sites = []
    for site in value:
        if type(site) is dict and "cloudId" in site:
            if "error" in site or "id" in site or "scopes" in site:
                raise ValueError("provider sites changed")
            products = site.get("products")
            if type(products) is not list or not 0 < len(products) <= 64:
                raise ValueError("provider sites changed")
            access = {}
            for product in products:
                if (
                    type(product) is not dict
                    or set(product) != {"id", "access"}
                    or type(product["id"]) is not str
                    or not 0 < len(product["id"]) <= 128
                    or product["id"] in access
                    or product["access"] not in ("read-only", "read-write")
                ):
                    raise ValueError("provider sites changed")
                access[product["id"]] = product["access"]
            observed = ObservedSite(site_id=site["cloudId"], scopes=())
            sites.append({"id": observed.site_id, "products": access})
            continue
        if (
            type(site) is not dict
            or "error" in site
            or not {"id", "scopes"} <= site.keys()
            or type(site["scopes"]) is not list
        ):
            raise ValueError("provider sites changed")
        observed = ObservedSite(site_id=site["id"], scopes=tuple(site["scopes"]))
        if len(set(observed.scopes)) != len(observed.scopes):
            raise ValueError("provider sites changed")
        # Site display metadata and URLs are not authority and are not retained.
        sites.append({"id": observed.site_id, "scopes": sorted(observed.scopes)})
    if len({site["id"] for site in sites}) != len(sites):
        raise ValueError("provider sites changed")
    return sorted(sites, key=lambda site: site["id"])


def fetch_site_scopes(token, *, deadline):
    """Read exact OAuth grants; MCP product access labels are not scope evidence."""
    host = "api.atlassian.com"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("provider deadline exceeded")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("provider resolution refused")
    connection = http.client.HTTPSConnection(host, timeout=min(2, remaining))
    try:
        raw = socket.create_connection((addresses[0][4][0], 443), timeout=min(2, remaining))
        connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("provider deadline exceeded")
        connection.sock.settimeout(min(5, remaining))
        connection.request(
            "GET",
            "/oauth/token/accessible-resources",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        )
        response = connection.getresponse()
        if (
            response.status != 200
            or response.getheader("Content-Type", "").split(";", 1)[0].lower() != "application/json"
        ):
            raise ValueError("provider scope evidence unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("provider deadline exceeded")
        connection.sock.settimeout(min(5, remaining))
        body = response.read(65537)
        if len(body) > 65536 or time.monotonic() >= deadline:
            raise ValueError("provider scope evidence unavailable")
        return _decode_json(body)
    finally:
        connection.close()


def _verified_site_scopes(sites, resources):
    grants = {site["id"]: site for site in _sites(resources)}
    verified = []
    for site in sites:
        if "products" not in site:
            verified.append(site)
            continue
        grant = grants.get(site["id"])
        if grant is None or "scopes" not in grant:
            raise ValueError("provider scope evidence unavailable")
        scopes = []
        for scope in grant["scopes"]:
            parts = scope.split(":")
            if len(parts) != 3 or parts[2] != "agent-interface":
                continue
            access = site["products"].get(parts[1])
            if access and (parts[0] in ("read", "search") or parts[0] == "write" and access == "read-write"):
                scopes.append(scope)
        verified.append({"id": site["id"], "scopes": scopes})
    return verified


class MCPTransport:
    def __init__(self, *, deadline, catalogue_bytes=65536, result_bytes=65536):
        self.deadline = deadline
        self.catalogue_bytes = catalogue_bytes
        self.result_bytes = result_bytes
        self.session_id = None
        self.request_id = 0

    def send(self, method, params, token):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("provider deadline exceeded")
        if method == "notifications/initialized":
            payload = {"jsonrpc": "2.0", "method": method}
        else:
            self.request_id += 1
            payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        host = "mcp.atlassian.com"
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
            raise ValueError("provider resolution refused")
        address = addresses[0][4][0]
        connection = http.client.HTTPSConnection(host, timeout=min(2, remaining))
        try:
            raw = socket.create_connection((address, 443), timeout=min(2, remaining))
            connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("provider deadline exceeded")
            connection.sock.settimeout(min(5, remaining))
            headers = {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            }
            if self.session_id:
                headers["MCP-Session-Id"] = self.session_id
            connection.request("POST", RESOURCE_PATH, body=canonical(payload), headers=headers)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("provider deadline exceeded")
            connection.sock.settimeout(min(5, remaining))
            response = connection.getresponse()
            if response.status not in (200, 202):
                code = {
                    401: "provider_unauthorized",
                    403: "provider_forbidden",
                    404: "not_found",
                    429: "rate_limited",
                }.get(response.status, "provider_unavailable")
                retry_header = response.getheader("Retry-After", "")
                retry_after = int(retry_header) if retry_header.isdecimal() and len(retry_header) <= 2 else None
                raise ProviderResponseError(code, retry_after)
            session_id = response.getheader("MCP-Session-Id")
            if session_id:
                if len(session_id) > 256 or not session_id.isascii() or not session_id.isprintable():
                    raise ValueError("invalid provider session")
                self.session_id = session_id
            max_bytes = (
                self.catalogue_bytes
                if method == "tools/list"
                else self.result_bytes
                if method == "tools/call"
                else 65536
            )
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("provider deadline exceeded")
            connection.sock.settimeout(min(5, remaining))
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                code = "catalogue_incomplete" if method == "tools/list" else "provider_response_too_large"
                raise ProviderResponseError(code)
            if time.monotonic() >= self.deadline:
                raise ValueError("provider deadline exceeded")
            if method == "notifications/initialized":
                return None
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
            if content_type == "text/event-stream":
                events = [
                    _decode_json(line[5:].strip())
                    for line in body.decode().splitlines()
                    if line.startswith("data:") and line[5:].strip() != "[DONE]"
                ]
                matches = [event for event in events if isinstance(event, dict) and event.get("id") == self.request_id]
                if len(matches) != 1:
                    raise ValueError("ambiguous provider response")
                return matches[0]
            if content_type != "application/json":
                raise ValueError("unsupported provider response")
            result = _decode_json(body)
            if type(result) is not dict or result.get("id") != self.request_id:
                raise ValueError("provider reply does not match")
            return result
        finally:
            connection.close()


class ObservationMCPTransport(MCPTransport):
    def __init__(self, *, deadline):
        super().__init__(deadline=deadline, catalogue_bytes=MAX_CATALOGUE_BYTES + 128 * 1024)


def collect_observation(token, settings, *, transport_factory=ObservationMCPTransport, scope_fetcher=fetch_site_scopes):
    deadline = time.monotonic() + 8
    transport = transport_factory(deadline=deadline)
    initial = transport.send(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "adp-atlassian-observer", "version": "1"},
        },
        token,
    )
    if initial.get("result", {}).get("protocolVersion") != PROTOCOL_VERSION:
        raise ValueError("provider protocol mismatch")
    transport.send("notifications/initialized", {}, token)
    tools, cursor, seen, size = {}, None, set(), 0
    for _ in range(MAX_CATALOGUE_PAGES):
        page = transport.send("tools/list", {"cursor": cursor} if cursor else {}, token).get("result")
        if type(page) is not dict or type(page.get("tools")) is not list:
            raise ValueError("provider catalogue missing")
        for tool in page["tools"]:
            if type(tool) is not dict or not isinstance(tool.get("name"), str) or tool["name"] in tools:
                raise ValueError("provider catalogue changed")
            definition_size = len(canonical(tool))
            size += definition_size
            if (
                definition_size > MAX_DEFINITION_BYTES
                or size > MAX_CATALOGUE_BYTES
                or len(tools) >= MAX_CATALOGUE_TOOLS
            ):
                raise ValueError("provider catalogue incomplete")
            tools[tool["name"]] = tool
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or cursor in seen or len(cursor) > 512:
            raise ValueError("provider catalogue cursor changed")
        seen.add(cursor)
    else:
        raise ValueError("provider catalogue incomplete")
    for name, digest in ((SUBJECT_TOOL, settings.subject_schema_sha256), (SITES_TOOL, settings.sites_schema_sha256)):
        if name not in tools or hashlib.sha256(canonical(tools[name].get("inputSchema"))).hexdigest() != digest:
            raise ValueError("unverified provider schema")
    responses = []
    for name, normalize in ((SUBJECT_TOOL, _subject), (SITES_TOOL, _sites)):
        result = transport.send("tools/call", {"name": name, "arguments": {}}, token).get("result")
        responses.append(_observation_value(result, normalize))
    subject, site_report = responses
    if any("products" in site for site in site_report):
        site_report = _verified_site_scopes(site_report, scope_fetcher(token, deadline=deadline))
    if time.monotonic() >= deadline:
        raise ValueError("provider deadline exceeded")
    sites = tuple(ObservedSite(site_id=site["id"], scopes=tuple(site["scopes"])) for site in site_report)
    scopes = tuple(sorted({"read:me", "read:account"} | {scope for site in sites for scope in site.scopes}))
    return subject, sites, scopes


async def observe_provider(token, settings):
    try:
        return await asyncio.to_thread(collect_observation, token, settings)
    except Exception:
        raise HTTPException(503, detail={"error": "adapter_unavailable"}) from None

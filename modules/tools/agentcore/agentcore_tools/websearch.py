"""Bounded AgentCore Gateway Web Search connector transport."""

import json
import os
import re
import time
from urllib.parse import urlsplit

import boto3
import requests
from adp_tools.search_input import SearchInput
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from fastapi import HTTPException
def gateway_call(
    endpoint, region, method, params, *, session=None, credentials=None, timeout=12
):
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(
            r"[a-z0-9-]+\.gateway\.bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com",
            parsed.hostname,
        )
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/mcp"
        or parsed.port not in (None, 443)
        or not parsed.hostname.endswith("." + region + ".amazonaws.com")
    ):
        raise HTTPException(503, "Web Search gateway configuration unavailable")
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, **params}, separators=(",", ":")
    ).encode()
    creds = credentials or boto3.Session().get_credentials()
    if creds is None:
        raise HTTPException(503, "Web Search gateway credentials unavailable")
    request = AWSRequest(
        method="POST",
        url=endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    SigV4Auth(creds.get_frozen_credentials(), "bedrock-agentcore", region).add_auth(
        request
    )
    transport = session or requests.Session()
    transport.trust_env = False
    response = None
    try:
        response = transport.post(
            endpoint,
            data=body,
            headers=dict(request.headers),
            timeout=(min(2, timeout), timeout),
            stream=True,
            allow_redirects=False,
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            if (not retry_after.isascii() or not retry_after.isdecimal() or
                    len(retry_after) > 5 or not 0 < int(retry_after) <= 86400):
                retry_after = "1"
            raise HTTPException(429, "Web Search provider throttled", headers={"Retry-After": retry_after})
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=8192):
            size += len(chunk)
            if size > 262144:
                raise ValueError("Oversized gateway response")
            chunks.append(chunk)
        content = b"".join(chunks)
        if (
            response.headers.get("Content-Type", "").split(";", 1)[0]
            == "text/event-stream"
        ):
            events = []
            for event in content.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
                data = [
                    line[5:].lstrip()
                    for line in event.splitlines()
                    if line.startswith("data:")
                ]
                if data:
                    events.append(json.loads("\n".join(data)))
            if len(events) != 1:
                raise ValueError("Unexpected MCP event stream")
            packet = events[0]
        else:
            packet = json.loads(content)
        if (
            not isinstance(packet, dict)
            or packet.get("jsonrpc") != "2.0"
            or packet.get("id") != 1
            or "error" in packet
        ):
            raise ValueError("Invalid MCP response")
        return packet["result"]
    finally:
        if response is not None:
            response.close()
        if session is None:
            transport.close()


def search(payload, *, call=gateway_call, environ=None, before_search=None):
    config = os.environ if environ is None else environ
    query = SearchInput.model_validate(payload)
    endpoint = config.get("ADP_WEBSEARCH_GATEWAY_URL", "")
    region = config.get("ADP_WEBSEARCH_REGION", "")
    target = config.get("ADP_WEBSEARCH_TARGET", "")
    if (
        not re.fullmatch(r"[a-z0-9-]+", region)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", target)
        or config.get("ADP_WEBSEARCH_CONNECTOR_VERSION") != "1.2.0"
    ):
        raise HTTPException(503, "Web Search connector configuration unavailable")
    deadline = time.monotonic() + 18

    def invoke(method, params):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPException(503, "Web Search discovery deadline exceeded")
        if call is gateway_call:
            return call(endpoint, region, method, params, timeout=min(8, remaining))
        return call(endpoint, region, method, params)

    matches, cursor, seen = [], None, set()
    for _ in range(5):
        listing = invoke("tools/list", {"params": {"cursor": cursor}} if cursor else {})
        tools = listing.get("tools") if isinstance(listing, dict) else None
        if not isinstance(tools, list):
            raise HTTPException(503, "Web Search tool listing unavailable")
        matches.extend(
            item
            for item in tools
            if isinstance(item, dict) and item.get("name") == target + "___WebSearch"
        )
        if matches:
            break
        cursor = listing.get("nextCursor")
        if not cursor:
            break
        if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
            raise HTTPException(503, "Web Search tool listing unavailable")
        seen.add(cursor)
    if len(matches) != 1 or not isinstance(matches[0].get("inputSchema"), dict):
        raise HTTPException(503, "Pinned Web Search connector schema unavailable")
    properties = matches[0]["inputSchema"].get("properties", {})
    filters = properties.get("filters", {}).get("properties", {})
    if (
        properties.get("query", {}).get("type") != "string"
        or properties.get("maxResults", {}).get("type") != "integer"
        or properties.get("filters", {}).get("type") != "object"
        or not {"domainFilter", "publishedDateFilter"}.issubset(filters)
    ):
        raise HTTPException(503, "Pinned Web Search connector schema unavailable")
    arguments = query.model_dump(
        exclude_none=True, exclude_defaults=True, by_alias=True
    )
    if before_search is not None:
        before_search()
    result = invoke(
        "tools/call",
        {"params": {"name": matches[0]["name"], "arguments": arguments}},
    )
    if (
        not isinstance(result, dict)
        or result.get("isError", False) is not False
        or not isinstance(result.get("content"), list)
    ):
        raise ValueError("Web Search provider failure")
    if len(result["content"]) != 1 or result["content"][0].get("type") != "text":
        raise ValueError("Unexpected Web Search content")
    found = json.loads(result["content"][0]["text"])
    if not isinstance(found, dict) or not isinstance(found.get("results"), list):
        raise TypeError("Unexpected Web Search results")
    results = []
    truncated = len(found["results"]) > query.maxResults
    for item in found["results"][: query.maxResults]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise TypeError("Unexpected Web Search result")
        url = item.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("Search result has no source URL")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid source URL")
        if len(url) > 2048:
            raise ValueError("Source URL exceeds bound")
        if len(item["text"]) > 1200 or len(str(item.get("title") or "")) > 300:
            truncated = True
        candidate = {
            "text": item["text"][:1200],
            "url": url,
            "title": str(item.get("title") or "")[:300],
            "publishedDate": str(item.get("publishedDate") or "")[:40],
        }
        bounded_results = results + [candidate]
        if len(json.dumps({"results": bounded_results, "citations":
                           [result["url"] for result in bounded_results]}, ensure_ascii=True).encode()) > 20000:
            truncated = True
            break
        results.append(candidate)
    if found["results"] and not results:
        raise ValueError("Web Search result exceeds bound")
    return {
        "status": "completed" if results else "empty",
        "results": results,
        "citations": [item["url"] for item in results],
        "results_truncated": truncated,
        "query_count": 1,
        "estimated_search_usd": 0.007,
        "pricing_source": "https://aws.amazon.com/bedrock/agentcore/pricing/",
    }

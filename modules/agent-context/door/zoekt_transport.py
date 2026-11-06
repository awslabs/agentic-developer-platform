"""Authenticated, bounded requests to the pinned Zoekt JSON API.

The API consumes Q/RepoIDs/Opts; top-level num is ignored. The private
auth proxy enforces these same ceilings before forwarding to the provider.
These per-request limits are not a complete Door or billing reservation.
"""

from __future__ import annotations

import asyncio
import json
import math
import os

import httpx

MAX_WALL_SECONDS = 10
MAX_DOCUMENTS = 500
# Match the pinned provider's existing default match ceilings explicitly.
MAX_SHARD_MATCHES = 100_000
MAX_TOTAL_MATCHES = 1_000_000
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ZoektUnavailable(Exception):
    """Search could not be completed; do not report an empty result."""


def search_payload(query, *, limit, whole=False, timeout=MAX_WALL_SECONDS):
    if (
        not isinstance(query, str)
        or not query.strip()
        or type(limit) is not int
        or limit <= 0
        or type(whole) is not bool
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ZoektUnavailable()
    payload = {
        "q": query,
        "opts": {
            # Go time.Duration is an integer number of nanoseconds in JSON.
            "MaxWallTime": max(1, int(min(timeout, MAX_WALL_SECONDS) * 1_000_000_000)),
            "MaxDocDisplayCount": min(limit, MAX_DOCUMENTS),
            "ShardMaxMatchCount": MAX_SHARD_MATCHES,
            "TotalMaxMatchCount": MAX_TOTAL_MATCHES,
            "Whole": whole,
        },
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > MAX_REQUEST_BYTES:
        raise ZoektUnavailable()
    return body


async def search_json(
    base_url, query, *, limit, whole=False, timeout=MAX_WALL_SECONDS, api_key=None
):
    key = os.environ.get("ZOEKT_API_KEY", "") if api_key is None else api_key
    if not isinstance(key, str) or len(key) < 32 or key.upper().startswith("PLACEHOLDER"):
        raise ZoektUnavailable()
    body = search_payload(query, limit=limit, whole=whole, timeout=timeout)
    try:
        async with asyncio.timeout(min(timeout, MAX_WALL_SECONDS)), httpx.AsyncClient(
            timeout=min(timeout, MAX_WALL_SECONDS), follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                "POST",
                base_url.rstrip("/") + "/api/search",
                content=body,
                headers={
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "").lower() not in ("", "identity"):
                    raise ZoektUnavailable()
                parts, size = [], 0
                async for chunk in response.aiter_bytes(chunk_size=65_536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ZoektUnavailable()
                    parts.append(chunk)
                result = json.loads(b"".join(parts))
                if not isinstance(result, dict) or not isinstance(
                    result.get("Result", result.get("result")), dict
                ):
                    raise ZoektUnavailable()
                return result
    except (httpx.HTTPError, ValueError, OSError, TimeoutError):
        raise ZoektUnavailable() from None

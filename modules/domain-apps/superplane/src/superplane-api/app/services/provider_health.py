"""Fresh health of the installed provider transport, independent of paid work.

Only the proxy's health-only credential is loaded here. The manager receives the
observed result and timestamp; no provider or execution credential leaves API.
"""

import asyncio
import json
import re
from datetime import UTC, datetime

import httpx

from app.config import settings


async def installed_skypilot_health() -> bool:
    origin = settings.superplane_provider_health_url
    token = settings.superplane_provider_health_token
    if (
        not re.fullmatch(
            r"http://skypilot-api\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"\.svc\.cluster\.local:46580",
            origin,
        )
        or not 48 <= len(token) <= 8192
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        return False
    try:
        # One read for the entire registry response. There is no successful
        # value cache: an unavailable provider immediately produces false.
        async with (
            asyncio.timeout(4),
            httpx.AsyncClient(
                timeout=3, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(
                "GET",
                origin + "/api/health",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "Cache-Control": "no-cache, no-store",
                },
            ) as response,
        ):
            if response.status_code != 200:
                return False
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 4096:
                    return False
            document = json.loads(body)
            return isinstance(document, dict) and document.get("status") == "healthy"
    except (TimeoutError, httpx.HTTPError, ValueError):
        return False


async def project_provider_health(targets, *, org_id):
    """Project only for this installation's canonical dedicated workspaces."""
    if org_id != settings.superplane_provider_health_org_id:
        return
    configured = set(settings.superplane_provider_health_workspace_ids)
    selected = [
        target
        for target in targets
        if target["workspace_id"] in configured
        and not target.get("provisional")
        and not target.get("shared_membership")
        and target.get("workspace_status") in {"Ready", "active"}
        and target.get("cluster_status") in {"Ready", "Active"}
        and target.get("cluster_id")
    ]
    if not selected:
        return
    healthy = await installed_skypilot_health()
    observation = {
        "skypilot_healthy": healthy,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    for target in selected:
        # Replace any older assignment result, including on failure. The
        # consumer accepts any fresh true observation, so appending could mask
        # the current failed read with an earlier successful assignment check.
        target["provider_observations"] = [dict(observation)]

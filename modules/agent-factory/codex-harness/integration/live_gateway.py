"""Opt-in live inference through a copied ADP login; never refresh live stores."""
from __future__ import annotations

import base64
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


async def invoke_live_gateway(request, *, model):
    source = Path(os.environ["ADP_CODEX_LIVE_CONFIG_SOURCE"]).resolve()
    target = Path(os.environ["BG_CONFIG_DIR"]).resolve()
    if source == target or not str(target).startswith("/tmp/adp-isolated-tests-"):
        raise RuntimeError("Live qualification requires isolated config stores")
    for name in ("config.json", "tokens.json"):
        if not (target / name).exists():
            shutil.copyfile(source / name, target / name)
            (target / name).chmod(0o600)
    config = json.loads((target / "config.json").read_text())
    token = json.loads((target / "tokens.json").read_text())["access_token"]
    segment = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    if claims["exp"] <= time.time() + 30:
        raise RuntimeError("Live token expires too soon; no automatic refresh")
    base = config["gateway_url"].rstrip("/")
    endpoint = urlsplit(base)
    if endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise RuntimeError("Live gateway endpoint invalid")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), follow_redirects=False, trust_env=False) as client:
        response = await client.post(base + "/openai/v1/responses", headers={"Authorization": "Bearer " + token},
            json={**request, "model": model, "stream": False, "store": False, "include": ["reasoning.encrypted_content"]})
    if response.status_code != 200:
        # Do not print upstream content, headers, credentials or model inputs.
        raise RuntimeError(f"Live gateway request refused (HTTP {response.status_code})")
    if len(response.content) > 65536:
        raise RuntimeError("Live response exceeded admitted frame bound")
    document = response.json()
    provider_id = response.headers.get("x-amzn-requestid") or response.headers.get("x-request-id") or document.get("id")
    return document, {"provider_request_id": provider_id, "duration_seconds": round(time.monotonic()-started, 3), "usage": document.get("usage")}

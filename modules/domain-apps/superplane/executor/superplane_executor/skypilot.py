"""SkyPilot 0.12.0 REST transport; no SDK pickle decoding and no launch retries."""

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from harness_jobs.identity import OperationRefused

from .authority import read_token


class SkyPilot:
    def __init__(self, origin, token_file, client=None):
        url = urlsplit(origin)
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or not url.hostname
            or not (
                url.scheme == "https"
                or (
                    url.scheme == "http" and url.hostname.endswith(".svc.cluster.local")
                )
            )
        ):
            raise ValueError("explicit private SkyPilot origin required")
        self.origin = origin.rstrip("/")
        self.token_file = Path(token_file)
        self.client = client or httpx.AsyncClient(
            timeout=60, follow_redirects=False, trust_env=False
        )

    async def request(self, method, path, **kwargs):
        try:
            response = await self.client.request(
                method,
                self.origin + path,
                headers={"Authorization": "Bearer " + read_token(self.token_file)},
                **kwargs,
            )
            if response.status_code != 200 or len(response.content) > 2**20:
                raise ValueError("provider refused or uncertain")
            return response
        except (ValueError, httpx.HTTPError):
            raise OperationRefused("SkyPilot response unavailable or refused") from None

    async def identity(self):
        return (await self.request("GET", "/internal/provider-identity")).json()

    async def submit(self, path, body):
        if path not in ("/launch", "/down", "/status"):
            raise OperationRefused("unsupported SkyPilot action")
        response = await self.request("POST", path, json=body)
        # Pinned upstream server.py middleware publishes this header. The response
        # body is JSON null; it is not the older {'request_id': ...} shape.
        request_id = response.headers.get("X-Skypilot-Request-ID", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", request_id):
            raise OperationRefused("SkyPilot request handle unavailable")
        return request_id

    async def status(self, request_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", request_id):
            raise OperationRefused("invalid SkyPilot request handle")
        results = (
            await self.request(
                "GET",
                "/api/status",
                params={"request_ids": request_id, "all_status": "true"},
            )
        ).json()
        # Prefix queries may match several requests. Require the exact full ID.
        if (
            not isinstance(results, list)
            or len(results) != 1
            or results[0].get("request_id") != request_id
        ):
            raise OperationRefused("SkyPilot request identity unavailable")
        status = results[0].get("status")
        if status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}:
            raise OperationRefused("SkyPilot request status unavailable")
        return status

    async def complete(self, request_id, authorize):
        # /api/get blocks behind the proxy timeout. Poll the pinned status API
        # using the durable handle, rechecking revocation before every read.
        async with asyncio.timeout(800):
            while True:
                await authorize()
                status = await self.status(request_id)
                if status not in {"PENDING", "RUNNING"}:
                    return status == "SUCCEEDED"
                await asyncio.sleep(5)

    async def aclose(self):
        await self.client.aclose()

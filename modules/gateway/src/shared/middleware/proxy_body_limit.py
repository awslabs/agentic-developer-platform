"""Bound proxy payloads before JSON parsing, including chunked requests."""

import os

from starlette.responses import JSONResponse

from src.shared.enforced_paths import ENFORCED_PATHS


class ProxyBodyLimitMiddleware:
    def __init__(self, app, max_bytes=None):
        self.app = app
        self.max_bytes = max_bytes or max(1, min(32 * 1024 * 1024, int(os.getenv("BG_PROXY_MAX_BODY_BYTES", str(25 * 1024 * 1024)))))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(ENFORCED_PATHS):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                return await response(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)

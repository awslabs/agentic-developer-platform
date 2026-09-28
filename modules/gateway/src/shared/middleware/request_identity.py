"""Server-owned accounting identity, established before admission middleware."""

import re
from datetime import UTC, datetime
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_CORRELATION = re.compile(rb"[A-Za-z0-9._:/-]{1,128}\Z")


class RequestIdentityMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        # Never trust a header (or recycled scope state) as a monetary identity.
        request_id = str(uuid4())
        state["request_id"] = request_id
        state["request_started_at"] = datetime.now(UTC)
        state["client_request_id"] = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"x-request-id" and _CORRELATION.fullmatch(value):
                state["client_request_id"] = value.decode("ascii")
                break

        async def identified_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode())]}
            await send(message)

        await self.app(scope, receive, identified_send)

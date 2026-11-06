"""Dedicated TLS listener for credential-free chat sandboxes.

This listener retains the ordinary gateway authentication and authorization
stack. Its transport allowlist is an additional restriction, never an identity.
It is opt-in and is not started by the ordinary gateway entrypoint.
"""

import os
import re
from pathlib import Path

from starlette.responses import Response

HOST = "chat-sandbox-gateway.adp-gateway.svc"
PORT = 8443
CERTIFICATE = "/var/run/adp-chat-tls/tls.crt"
PRIVATE_KEY = "/var/run/adp-chat-tls/tls.key"
POST_PATHS = frozenset(
    f"/v1/chat/{suffix}"
    for suffix in (
        "model/keys",
        "model/decision",
        "model/invoke",
        "model/crew-events",
        "turn/next",
        "data/turn/result",
        "data/bootstrap",
        "data/tools",
        "data/tools/list",  # first-party platform tools (#8113): listing offered to the turn
        "data/tools/call",  # ... and server-side execution under the turn capability
        "data/qualification/pending",
        "data/qualification/claim",
        "data/qualification/active",
        "data/qualification/result",
        "data/qualification/fixtures",
        "data/session/next",
        "data/session/state",
        "data/session/admit",
        "data/installation/status",
        "data/installation/failure",
        "data/activity/work",
        "data/history/read",
        "data/history/messages",
        "data/history/summary",
        "data/history/turn",
        "data/history/append",
        "data/history/summary/append",
        "data/history/compact",
        "data/memory/read",
        "data/memory/search",
        "data/memory/write",
        "data/draft/read",
        "data/draft/write",
        "data/session/acl/read",
        "data/session/acl/write",
        "data/artifact/list",
        "data/artifact/create",
    )
)
ARTIFACT_PATH = re.compile(r"/v1/chat/data/artifact/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
CHECKPOINT_PATH = re.compile(
    r"/internal/v1/agent/crew/checkpoints/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"/(upload-grant|complete|restore-grant|restore-release)\Z"
)


class SandboxTransport:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        hosts = [value for key, value in scope.get("headers", []) if key.lower() == b"host"]
        canonical = (
            scope.get("scheme") == "https"
            and scope.get("root_path", "") == ""
            and scope.get("raw_path") == path.encode("ascii", errors="replace")
            and hosts == [f"{HOST}:{PORT}".encode()]
        )
        allowed = (scope.get("method") == "POST" and (path in POST_PATHS or CHECKPOINT_PATH.fullmatch(path))) or (
            scope.get("method") == "GET" and (path == "/health" or ARTIFACT_PATH.fullmatch(path))
        )
        if not canonical or not allowed:
            return await Response(status_code=404, headers={"Cache-Control": "no-store"})(scope, receive, send)
        # Pass the ORIGINAL request to the normal gateway. Never manufacture a
        # caller, forward trusted identity headers, or bypass its feature gates.
        return await self.app(scope, receive, send)


def create_app():
    if os.environ.get("ADP_CHAT_TLS_ENABLED") != "true":
        raise RuntimeError("Dedicated chat TLS listener is disabled")
    from src.app import create_app as gateway_app

    return SandboxTransport(gateway_app())


def main():
    if os.environ.get("ADP_CHAT_TLS_ENABLED") != "true":
        raise RuntimeError("Dedicated chat TLS listener is disabled")
    if not all(Path(path).is_file() for path in (CERTIFICATE, PRIVATE_KEY)):
        raise RuntimeError("Chat TLS certificate and key are required")
    import uvicorn

    uvicorn.run(
        "src.agentauth.chat_tls:create_app",
        factory=True,
        host="0.0.0.0",
        port=PORT,
        ssl_certfile=CERTIFICATE,
        ssl_keyfile=PRIVATE_KEY,
        proxy_headers=False,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()

"""Loopback-only gateway relay that drops exactly one committed Product PATCH response."""

from __future__ import annotations

import contextlib
import http.server
import socket
import threading
import urllib.error
import urllib.request

import common


# The installed CLI normalizes whatever base it is configured with so the base
# it actually sends against ALWAYS ends in this mount: `adp_common.gateway_url()`
# returns the configured base unchanged when it already ends in `/api` and
# appends `/api` otherwise. The reviewed gateway base this relay fronts carries
# the prefix already, but the loopback base yielded below cannot, so the CLI
# appends it and every request arrives here spelled `/api/v1/products/...`.
#
# Both halves of the relay have to honour that, and neither did. Matching the
# bare mounted app path never matched the committed PATCH, so the response that
# had to be dropped was delivered instead and PR03-C02 failed with "Committed
# Product response was not dropped and reported as unknown". Appending the wire
# path to a base that already ends in `/api` addressed
# `<base>/api/api/v1/products/...`, which nothing serves, so the forwarded
# request was answered 404 as well.
#
# The mount is therefore stripped from the upstream base and re-honoured in the
# match. That keeps this a transparent relay for everything the CLI sends,
# including the `/me/cli-capabilities` lookup it makes before any mutation.
CLI_MOUNT = "/api"


@contextlib.contextmanager
def drop_product_response(gateway, path, key):
    common.require(
        gateway.startswith("https://"), "Fault relay requires the pinned HTTPS gateway"
    )
    upstream = gateway.rstrip("/")
    if upstream.endswith(CLI_MOUNT):
        upstream = upstream[: -len(CLI_MOUNT)]
    dropped_path = CLI_MOUNT + path
    state = {"forwarded": False, "dropped": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            pass

        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def do_PATCH(self):
            self.forward()

        def forward(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else None
            request = urllib.request.Request(
                upstream + self.path,
                data=body,
                headers={
                    header: value
                    for header, value in self.headers.items()
                    if header.lower() not in {"host", "content-length", "connection"}
                },
                method=self.command,
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=60) as response:
                    status, payload, content_type = (
                        response.status,
                        response.read(),
                        response.headers.get("Content-Type", "application/json"),
                    )
            except urllib.error.HTTPError as error:
                status, payload, content_type = (
                    error.code,
                    error.read(),
                    error.headers.get("Content-Type", "application/json"),
                )
            state["forwarded"] = True
            if (
                self.command == "PATCH"
                and self.path == dropped_path
                and self.headers.get("Idempotency-Key") == key
                and status == 200
                and not state["dropped"]
            ):
                state["dropped"] = True
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

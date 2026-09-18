#!/usr/bin/env python3
"""Localhost-only token-injecting forward proxy for the Bedrock Gateway (Issue #4154).

Why this exists
---------------
Codex CLI (and any client without an ``apiKeyHelper``-style hook) reads its
credential from an env var **once at launch** and never asks again. A Cognito
access token lives 60 minutes, so a session that outlives it starts failing
with 401s until the human restarts Codex with a fresh token. Claude Code does
not have this problem: it re-invokes ``bg-cognito-auth.sh token`` on a TTL.

This proxy is the missing "ask again" hook. Codex points at
``http://127.0.0.1:<port>``; every request that arrives here is forwarded to the
real gateway with a freshly-obtained ``Authorization: Bearer <token>`` header.

Same architecture as the hosted-agent sigv4-proxy sidecar
(``modules/agent-factory/agent-worker-image/entrypoint.py``, ``codex-config.toml``
-> ``127.0.0.1:9090``): local listener, per-request auth injection, streaming
passthrough. The auth material differs (Cognito JWT here, SigV4 there), so no
code is shared — only the shape.

Design constraints (from Issue #4154)
-------------------------------------
* **Loopback only.** ``BIND_HOST`` is a hardcoded literal. There is no flag to
  widen it: a proxy that injects the user's credential must never be reachable
  from the LAN.
* **Exactly one refresh implementation.** The token is obtained by shelling out
  to ``bg-cognito-auth.sh token``, which already owns the reuse/refresh decision
  (renew ~5 min before the 60-min expiry, off the 30-day refresh token). The
  Cognito logic is deliberately NOT duplicated here.
* **Unbuffered streaming.** Codex sends ``stream=true``; a proxy that buffers
  the response hangs it. Bodies are relayed with ``read1()`` so each upstream
  socket read is written straight through.
* **No secrets in logs.** One line per request (method, path, status) on stderr.
  Never the token, never headers, never bodies.

stdlib only, single file: macOS and Linux both ship a usable python3, so the
helper keeps its zero-install property.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Loopback only, never configurable. See module docstring.
BIND_HOST = "127.0.0.1"

DEFAULT_PORT = 9191

# Port 0 asks the OS for any free port (Issue #5413). Three deployments running
# at once cannot all own 9191, and picking candidate ports ourselves would be a
# race: another process can take the port between our check and our bind. The OS
# assigns and reserves in one step, so the port a caller reads back from the
# published identity is a port that is already bound.
ANY_PORT = 0

# The local, unauthenticated identity route. A launcher that finds something
# listening needs to know WHICH deployment's proxy it is before sending a
# request, because reusing another deployment's proxy would send this
# deployment's traffic to the other one's gateway. Answered from loopback only
# (like every other route here) and deliberately secret-free: an id, a name and
# the upstream URL the user chose — never a token, and never a header or body
# from a relayed request.
IDENTITY_PATH = "/_adp/proxy"

# Generous: an SSE completion can stream for many minutes.
UPSTREAM_TIMEOUT_SECONDS = 600

# Relay granularity. Small enough that SSE events are not held back waiting for
# a full buffer, large enough that bulk bodies do not thrash syscalls.
RELAY_CHUNK_BYTES = 65536

# Per-hop headers must not be forwarded in either direction (RFC 9110 7.6.1).
# ``transfer-encoding`` matters most: we relay a decoded body, so passing the
# upstream framing header through would make the client misparse it.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Client-supplied credentials are dropped, not merged: whatever placeholder
# Codex was configured with (``env_key`` is mandatory in its config) must never
# reach the gateway and must never win over the token we inject.
CLIENT_AUTH_HEADERS = frozenset({"authorization", "x-api-key", "api-key"})

# ``host`` is set by the upstream connection; ``content-length`` is recomputed
# from the body we actually read.
DROPPED_REQUEST_HEADERS = HOP_BY_HOP_HEADERS | CLIENT_AUTH_HEADERS | {"host", "content-length"}


class TokenError(RuntimeError):
    """The auth helper could not produce a usable access token."""


class TokenSource:
    """Obtains a current access token by calling the bash helper's ``token``.

    Calls are serialized: concurrent Codex requests arriving while the token is
    inside its pre-expiry window would otherwise race two ``REFRESH_TOKEN_AUTH``
    calls and two writers on ``~/.bedrock-gateway/tokens.json``.

    No caching. The helper is already the cache — it re-reads the token store
    and refreshes only when needed — and a second expiry judgement here would
    be a second place for a stale-token bug to live.
    """

    def __init__(self, helper_path: str) -> None:
        self._helper_path = helper_path
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            try:
                result = subprocess.run(
                    ["bash", self._helper_path, "token"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                raise TokenError("auth helper timed out") from exc
            except OSError as exc:
                raise TokenError(f"could not run auth helper: {exc}") from exc

        if result.returncode != 0:
            # The helper prints the token on stdout and diagnostics on stderr,
            # so surfacing stderr cannot leak the credential — and the operator
            # needs it to tell "refresh token expired" from "gateway down".
            raise TokenError(_first_line(result.stderr) or "auth helper failed")

        token = result.stdout.strip()
        if not token:
            raise TokenError("auth helper returned no token")
        return token


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class GatewayProxyHandler(BaseHTTPRequestHandler):
    """Forwards each request upstream with a freshly-injected bearer token."""

    # HTTP/1.1 so keep-alive works for ordinary JSON responses. Streaming
    # responses (no upstream Content-Length) are framed by connection close
    # instead — see _relay_response.
    protocol_version = "HTTP/1.1"
    server_version = "bg-gateway-proxy"
    sys_version = ""

    # Injected by serve().
    token_source: TokenSource
    upstream_scheme: str
    upstream_host: str
    upstream_port: int | None
    upstream_base_path: str
    # Which deployment this proxy serves, for IDENTITY_PATH. Empty on a legacy
    # single-deployment run, where there is nothing to disambiguate.
    deployment_id: str = ""
    deployment_name: str = ""
    gateway_url: str = ""

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == IDENTITY_PATH:
            self._send_identity()
            return
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    # -- logging ---------------------------------------------------------
    #
    # BaseHTTPRequestHandler's default logging echoes the request line only.
    # We route everything through log_message so there is exactly one place
    # that can ever write to stderr, and it is never handed a header or a body.

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write(f"[proxy] {format % args}\n")
        sys.stderr.flush()

    def log_request(self, code: object = "-", size: object = "-") -> None:
        # Suppressed: _proxy emits the single authoritative line per request so
        # the status we log is the one actually relayed.
        pass

    # -- proxying --------------------------------------------------------

    def _proxy(self) -> None:
        try:
            token = self.token_source.token()
        except TokenError as exc:
            # 502 with an explicit proxy_ error code: a request that fails here
            # never reached the gateway, and the user must be able to tell the
            # difference without reading the gateway's logs.
            self.log_message("%s %s -> 502 (token error: %s)", self.command, self._log_path(), exc)
            self._send_error_body(502, "proxy_token_error", f"{exc}. Try: bg-cognito-auth.sh status")
            return

        try:
            body = self._read_request_body()
        except ValueError as exc:
            self.log_message("%s %s -> 400 (%s)", self.command, self._log_path(), exc)
            self._send_error_body(400, "proxy_bad_request", str(exc))
            return

        body = self._normalize_model(body)

        connection = self._open_upstream()
        try:
            connection.request(
                self.command,
                self._upstream_path(),
                body=body,
                headers=self._upstream_headers(token, body),
            )
            response = connection.getresponse()
            self.log_message("%s %s -> %s", self.command, self._log_path(), response.status)
            self._relay_response(response)
        except (OSError, http.client.HTTPException) as exc:
            self.log_message("%s %s -> 502 (upstream error: %s)", self.command, self._log_path(), exc)
            self._send_error_body(502, "proxy_upstream_error", f"could not reach the gateway: {exc}")
        finally:
            connection.close()

    def _open_upstream(self) -> http.client.HTTPConnection:
        if self.upstream_scheme == "https":
            return http.client.HTTPSConnection(self.upstream_host, self.upstream_port, timeout=UPSTREAM_TIMEOUT_SECONDS)
        return http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=UPSTREAM_TIMEOUT_SECONDS)

    def _read_request_body(self) -> bytes:
        """Read the request body, honouring either framing the client may use.

        Chunked request bodies are decoded rather than ignored: silently
        forwarding an empty body would turn a valid request into a confusing
        gateway-side validation error.
        """
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            return self._read_chunked_body()

        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        return self.rfile.read(length)

    def _read_chunked_body(self) -> bytes:
        chunks = []
        while True:
            size_line = self.rfile.readline().split(b";", 1)[0].strip()
            try:
                size = int(size_line, 16)
            except ValueError as exc:
                raise ValueError("malformed chunked request body") from exc
            if size == 0:
                # Consume the trailer section up to the terminating blank line.
                while self.rfile.readline().strip():
                    pass
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()  # trailing CRLF
        return b"".join(chunks)

    def _normalize_model(self, body: bytes) -> bytes:
        """Prefix bare model names on the OpenAI route with ``openai.``.

        Codex's in-app model picker writes short slugs (``gpt-5.6-sol``) into
        config.toml, but the gateway's OpenAI passthrough only serves models
        under their prefixed ids (``openai.gpt-5.6-sol``). Rewriting here lets
        in-app model switching work without hand-editing config.toml.
        Anything that is not JSON with a string ``model`` passes through
        untouched.
        """
        if not body or not self.path.startswith("/openai/"):
            return body
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or not model or model.startswith("openai."):
            return body
        payload["model"] = f"openai.{model}"
        self.log_message("model %r -> %r", model, payload["model"])
        return json.dumps(payload).encode("utf-8")

    def _upstream_headers(self, token: str, body: bytes) -> dict[str, str]:
        headers = {name: value for name, value in self.headers.items() if name.lower() not in DROPPED_REQUEST_HEADERS}
        headers["Authorization"] = f"Bearer {token}"
        if body:
            headers["Content-Length"] = str(len(body))
        return headers

    def _upstream_path(self) -> str:
        # gateway_url may carry a base path (e.g. ".../api"), which CloudFront
        # needs in front of the client's path.
        return f"{self.upstream_base_path}{self.path}"

    def _log_path(self) -> str:
        # Query strings are dropped from logs: they are the one part of a URL
        # that occasionally carries credentials.
        return self.path.split("?", 1)[0]

    def _relay_response(self, response: http.client.HTTPResponse) -> None:
        self.send_response_only(response.status, response.reason)

        streaming = response.getheader("Content-Length") is None
        for name, value in response.getheaders():
            if name.lower() in HOP_BY_HOP_HEADERS:
                continue
            self.send_header(name, value)
        if streaming:
            # No Content-Length and we do not re-chunk, so the body is framed by
            # connection close. That is what keeps SSE flowing without the proxy
            # having to know where the stream ends.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        if self.command == "HEAD":
            return

        while True:
            # read1, not read: read() would block filling the whole buffer and
            # stall every SSE event behind it.
            chunk = response.read1(RELAY_CHUNK_BYTES)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    def _send_identity(self) -> None:
        """Answer "which deployment is this?" without touching the gateway.

        Handled entirely locally: it obtains no token, opens no upstream
        connection and makes no network call, so a launcher can ask it cheaply
        before deciding to reuse this proxy. The body carries only what the user
        already typed — a deployment id, its name and its gateway URL.
        """
        payload = json.dumps(
            {
                "proxy": "adp-gateway-proxy",
                "deployment_id": self.deployment_id,
                "deployment": self.deployment_name,
                "gateway_url": self.gateway_url,
            }
        ).encode("utf-8")
        self.log_message("GET %s -> 200", IDENTITY_PATH)
        self.send_response_only(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_body(self, status: int, code: str, message: str) -> None:
        payload = json.dumps({"error": code, "message": message}).encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)


def write_identity(path: str, payload: dict[str, object]) -> None:
    """Publish where this proxy is listening and whose it is — 0600, atomically.

    Written ONLY after a successful bind, and replaced atomically (Issue #5413).
    Both matter for the same reason: this file is how a launcher decides to reuse
    a proxy instead of starting one. A file written before the bind would
    advertise a port that may never open, and a non-atomic write would let a
    concurrent reader see a half-written record and parse a truncated port.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".adp-proxy-", dir=directory)
    try:
        with os.fdopen(handle, "w") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def serve(
    gateway_url: str,
    port: int,
    helper_path: str,
    pidfile: str | None,
    identity_file: str | None = None,
    deployment_id: str = "",
    deployment_name: str = "",
) -> int:
    parsed = urllib.parse.urlsplit(gateway_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        sys.stderr.write(f"[proxy] gateway_url is not a usable http(s) URL: {gateway_url}\n")
        return 1

    handler = type(
        "BoundGatewayProxyHandler",
        (GatewayProxyHandler,),
        {
            "token_source": TokenSource(helper_path),
            "upstream_scheme": parsed.scheme,
            "upstream_host": parsed.hostname,
            "upstream_port": parsed.port,
            "upstream_base_path": parsed.path.rstrip("/"),
            "deployment_id": deployment_id,
            "deployment_name": deployment_name,
            "gateway_url": gateway_url,
        },
    )

    # ThreadingHTTPServer: Codex opens concurrent requests, and a single-threaded
    # server would serialize them behind one long SSE stream.
    with ThreadingHTTPServer((BIND_HOST, port), handler) as httpd:
        bound_host, bound_port = httpd.socket.getsockname()[:2]
        sys.stderr.write(f"[proxy] listening on {bound_host}:{bound_port} -> {gateway_url}\n")
        sys.stderr.write(f"[proxy] point Codex at http://127.0.0.1:{bound_port}/openai/v1 — Ctrl-C to stop\n")
        sys.stderr.flush()
        if identity_file:
            # The bind succeeded, so the port below is real and reachable.
            write_identity(
                identity_file,
                {
                    "pid": os.getpid(),
                    "port": bound_port,
                    "deployment_id": deployment_id,
                    "deployment": deployment_name,
                    "gateway_url": gateway_url,
                },
            )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("[proxy] stopped\n")
        finally:
            # Both files advertise a proxy that no longer exists once this
            # returns, so neither may outlive it on a clean shutdown.
            for stale in (pidfile, identity_file):
                if stale:
                    with contextlib.suppress(OSError):
                        os.unlink(stale)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bg-gateway-proxy.py",
        description="Localhost-only token-injecting proxy for the Bedrock Gateway (Issue #4154). Normally started via `bg-cognito-auth.sh serve`.",
    )
    parser.add_argument("--gateway-url", required=True, help="Upstream gateway base URL (from ~/.bedrock-gateway/config.json)")
    parser.add_argument("--auth-helper", required=True, help="Path to bg-cognito-auth.sh; its `token` subcommand is the only refresh implementation")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Loopback port to listen on (default: {DEFAULT_PORT}; {ANY_PORT} lets the OS assign a free one)",
    )
    parser.add_argument("--pidfile", default=None, help="Pidfile to remove on clean shutdown")
    parser.add_argument(
        "--identity-file",
        default=None,
        help="Where to publish the bound port and deployment identity, after a successful bind (Issue #5413)",
    )
    parser.add_argument("--deployment-id", default="", help="Stable id of the deployment this proxy serves (Issue #5413)")
    parser.add_argument("--deployment", default="", help="Name of the deployment this proxy serves (Issue #5413)")
    args = parser.parse_args(argv)

    try:
        return serve(
            args.gateway_url,
            args.port,
            args.auth_helper,
            args.pidfile,
            args.identity_file,
            args.deployment_id,
            args.deployment,
        )
    except OSError as exc:
        sys.stderr.write(f"[proxy] could not bind {BIND_HOST}:{args.port}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

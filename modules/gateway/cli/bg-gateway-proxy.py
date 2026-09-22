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

Who is allowed to use it (Issue #5686)
--------------------------------------
Binding to loopback decides *which machines* can reach this proxy. It does NOT
decide *which callers* may spend the user's token, and those are different
questions. Two callers reach loopback without being Codex:

1. **Any other process on the machine** — loopback is not a permission boundary
   between local processes.
2. **Any web page the user visits while the proxy runs** — a browser will happily
   issue ``fetch('http://127.0.0.1:9191/...')`` from ``https://evil.example``.
   The page cannot *read* the token (it is injected here, never sent to the
   client), but it does not need to: it makes the proxy spend the token on its
   behalf and reads the gateway's answer. A ``simple request`` (e.g.
   ``Content-Type: text/plain``) is not even preflighted, so a CORS policy alone
   would not stop the request from being *sent* and relayed.

So entitlement is proven, not assumed, by three independent checks:

* **A capability token** (``CAPABILITY_ENV_VAR``). Minted per process, published
  only into the 0600 identity file, and required on every relayed request. A web
  page cannot read that file, and neither can another user. This is the check
  that actually carries the security property; the two below are defence in
  depth for when a capability leaks.
* **Origin / Sec-Fetch-Site rejection.** Markers a browser attaches and a CLI
  never does. ``null`` (sandboxed/`file:` pages) is rejected like any other.
* **Host validation.** Defeats DNS rebinding, where ``evil.example`` resolves to
  127.0.0.1 so the packet legitimately arrives on loopback. The address a packet
  came from cannot distinguish that case; the name the caller *asked for* can.

Refusals happen BEFORE any token is obtained, so a refused request never reaches
the gateway and never spends anything. Nothing here ever emits an
``Access-Control-Allow-*`` header: there is no browser origin this proxy wants to
grant access to, so preflights are answered locally with a refusal rather than
being forwarded upstream.

stdlib only, single file: macOS and Linux both ship a usable python3, so the
helper keeps its zero-install property.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import http.client
import json
import os
import secrets
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

# Client-supplied credentials are dropped, not merged: whatever Codex was
# configured with (``env_key`` is mandatory in its config) must never reach the
# gateway and must never win over the token we inject. Since #5686 that value is
# the capability rather than a placeholder, which makes dropping it doubly
# important: the capability authenticates the caller TO this proxy and is
# meaningless to the gateway, so forwarding it would leak a local secret.
CLIENT_AUTH_HEADERS = frozenset({"authorization", "x-api-key", "api-key"})

# The env var Codex reads its credential from, and therefore how the capability
# reaches us. Codex requires `env_key` to name a set variable but never validates
# the value, so this was historically a placeholder ("unused") that we discarded.
# Reusing it means the capability needs no new client-side plumbing: the launcher
# exports it, Codex sends it as `Authorization: Bearer <capability>`, and a user
# whose shell still exports the old placeholder gets a clear 403 rather than a
# silent success. Env var, not a CLI flag: `ps` is world-readable on both macOS
# and Linux, so a capability on the command line would be readable by any local
# user — the exact audience the capability exists to exclude.
CAPABILITY_ENV_VAR = "ADP_GATEWAY_DUMMY"

# Values that must never be accepted as a capability, however they arrive.
#
# This matters because the variable had a previous life as a placeholder: the
# docs, the /setup page and many users' shell rc files export
# `ADP_GATEWAY_DUMMY=unused`. The proxy inherits its environment from the shell
# that started it, so honouring an inherited value verbatim would quietly set the
# capability to a word printed in the README — guessable by any web page, which
# is the whole vulnerability. A placeholder is therefore discarded and a real
# secret minted instead.
PLACEHOLDER_CAPABILITIES = frozenset({"unused", "dummy", "none", "placeholder", "changeme", "x"})

# Floor on an externally-supplied capability. Anything shorter is a placeholder
# by another name and is brute-forceable by a page that can issue requests in a
# loop, so it is discarded in favour of a minted secret.
MIN_CAPABILITY_LENGTH = 16

# Key under which the capability is published into the 0600 identity file. Only
# the launcher (running as the owner) can read it back. Deliberately NOT part of
# the IDENTITY_PATH response body — that route is answered without a capability,
# so echoing it there would hand the secret to precisely the callers it excludes.
IDENTITY_CAPABILITY_KEY = "capability"

# 256 bits of urandom, url-safe so it survives an env var and a header value
# unescaped. Minted per process and held only in memory: a capability that
# outlived the proxy would be a persistent credential on disk for no benefit,
# since a new proxy publishes a new one.
CAPABILITY_BYTES = 32

# Browsers set `Origin` on cross-origin requests (and on all POSTs); CLI clients
# do not. `null` is included explicitly because a sandboxed iframe, a `file://`
# page and a redirected request all send the literal string "null" — treating a
# missing value and "null" the same way is the point, since "null" is an origin
# we trust least, not an absent one.
#
# Any Origin at all is refused rather than matched against an allowlist: there is
# no web page that should be driving this proxy, so there is nothing to allow.
BROWSER_ORIGIN_HEADER = "origin"

# Fetch metadata: set by the browser, unforgeable by page JavaScript (it is a
# forbidden header name). `same-origin` and `none` are what a same-site page or a
# direct navigation sends; `cross-site` and `same-site` mean another site caused
# the request. Absent on CLI clients, which is why absence cannot be an error.
FETCH_SITE_HEADER = "sec-fetch-site"
FORBIDDEN_FETCH_SITES = frozenset({"cross-site", "same-site"})

# Host values that genuinely name this machine's loopback interface. A request
# whose Host is anything else — `evil.example` resolving to 127.0.0.1 — is the
# DNS-rebinding signal: the packet arrives on loopback legitimately, so only the
# requested name distinguishes it. An ABSENT Host is tolerated (a minimal CLI
# client may omit it); a PRESENT and unrecognised one is refused.
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# ``host`` is set by the upstream connection; ``content-length`` is recomputed
# from the body we actually read.
DROPPED_REQUEST_HEADERS = HOP_BY_HOP_HEADERS | CLIENT_AUTH_HEADERS | {"host", "content-length"}


class TokenError(RuntimeError):
    """The auth helper could not produce a usable access token."""


def mint_capability() -> str:
    """A fresh default capability proving a caller may spend the token."""
    return secrets.token_urlsafe(CAPABILITY_BYTES)


def usable_capability(supplied: str | None) -> str:
    """An externally-supplied capability, or "" if it must not be trusted.

    Rejects the legacy ``unused`` placeholder and anything too short to resist
    guessing, so an inherited shell export cannot silently weaken the proxy to a
    value published in the docs. Returning "" tells the caller to mint instead.
    """
    value = (supplied or "").strip()
    if len(value) < MIN_CAPABILITY_LENGTH or value.lower() in PLACEHOLDER_CAPABILITIES:
        return ""
    return value


def presented_capability(header_value: str | None) -> str:
    """Extract the capability from an ``Authorization`` header value.

    Accepts the ``Bearer <value>`` form Codex sends, and a bare value for a
    hand-rolled client using curl. Returns "" when there is nothing usable, so
    the caller compares against a non-empty secret and fails closed.
    """
    if not header_value:
        return ""
    value = header_value.strip()
    scheme, _, remainder = value.partition(" ")
    if scheme.lower() == "bearer":
        return remainder.strip()
    return value


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
    # The per-process secret a caller must present to spend the user's token.
    capability: str = ""

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
        """Answer CORS preflights here; never forward them.

        A preflight is a browser asking "may this page call you?". The answer is
        always no, and it is ours to give: forwarding it would spend a round trip
        to have the gateway answer a question about *this* proxy's policy, and a
        permissive gateway CORS policy would then be inherited as our own.

        Refused with 403 and, critically, with no ``Access-Control-Allow-*``
        header at all. A preflight without those headers fails closed in every
        browser, so the actual request is never sent.
        """
        self.log_message("OPTIONS %s -> 403 (preflight refused)", self._log_path())
        self._send_error_body(
            403,
            "proxy_forbidden_origin",
            "This proxy does not serve browsers. It is a local credential helper for CLI tools.",
        )

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

    # -- entitlement -----------------------------------------------------

    def _browser_refusal(self) -> tuple[str, str] | None:
        """Why this request looks browser-driven, or None if it does not.

        Checked on the actual request as well as the preflight: a ``simple
        request`` is never preflighted, so a policy enforced only at preflight
        time would not stop the request that does the damage.
        """
        origin = (self.headers.get(BROWSER_ORIGIN_HEADER) or "").strip()
        if origin:
            # Includes the literal "null" from sandboxed/file: pages. No origin
            # is allowlisted, so the value itself is never echoed back.
            return ("proxy_forbidden_origin", "This proxy does not serve browsers; it has no permitted web origin.")

        fetch_site = (self.headers.get(FETCH_SITE_HEADER) or "").strip().lower()
        if fetch_site in FORBIDDEN_FETCH_SITES:
            return ("proxy_forbidden_origin", "This proxy does not serve cross-site browser requests.")
        return None

    def _host_refusal(self) -> tuple[str, str] | None:
        """Why the requested Host is not this loopback interface, or None.

        Absent Host is allowed (a minimal CLI client may omit it on HTTP/1.0);
        a present-but-foreign one is the DNS-rebinding signal.
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return None

        # Strip the port: splitting on the LAST colon would corrupt a bare IPv6
        # literal, so bracketed forms are handled before the generic split.
        hostname = host
        if hostname.startswith("["):
            hostname = hostname.partition("]")[0] + "]"
        elif hostname.count(":") == 1:
            hostname = hostname.partition(":")[0]

        if hostname.lower() in ALLOWED_HOSTNAMES:
            return None
        return (
            "proxy_forbidden_host",
            "Request Host is not this machine's loopback interface. Point your client at 127.0.0.1.",
        )

    def _capability_refusal(self) -> tuple[str, str] | None:
        """Why the caller is not entitled to spend the user's token, or None."""
        presented = presented_capability(self.headers.get("Authorization"))
        # compare_digest, not ==: a short-circuiting comparison leaks the length
        # of the matching prefix to a local attacker who can time many attempts.
        if presented and hmac.compare_digest(presented, self.capability):
            return None
        return (
            "proxy_unauthorized",
            f"Missing or invalid local capability. Launch your tool with 'adp codex', which sets {CAPABILITY_ENV_VAR} for you.",
        )

    def _refuse_unentitled(self) -> bool:
        """Refuse a caller that has not proven entitlement. True if refused.

        Ordered cheapest-and-most-specific first so the log line names the most
        actionable reason, and run entirely before the token is fetched: a
        refused request must never reach the gateway or spend anything.
        """
        for refusal in (self._browser_refusal(), self._host_refusal(), self._capability_refusal()):
            if refusal is None:
                continue
            code, message = refusal
            # The offending Origin/Host/capability is deliberately not logged:
            # this line is written to a file the user may paste into an issue.
            self.log_message("%s %s -> 403 (%s)", self.command, self._log_path(), code)
            self._send_error_body(403, code, message)
            return True
        return False

    def _proxy(self) -> None:
        if self._refuse_unentitled():
            return

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

        Answered WITHOUT a capability, deliberately: a launcher for deployment B
        must be able to ask a running proxy for deployment A "whose are you?" in
        order to decline to reuse it, and it cannot know A's capability. That is
        the whole point of the route (#5413).

        It is still not open to the web. The browser and Host guards apply, so a
        page cannot read which gateway the user is pointed at, and the response
        never includes the capability (see IDENTITY_CAPABILITY_KEY) — otherwise
        this uncapability-gated route would hand out the secret that protects
        every other route.
        """
        for refusal in (self._browser_refusal(), self._host_refusal()):
            if refusal is None:
                continue
            code, message = refusal
            self.log_message("GET %s -> 403 (%s)", IDENTITY_PATH, code)
            self._send_error_body(403, code, message)
            return

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

    # Selected before the bind so the value published after it is the one enforced.
    # A caller learns it from the identity file, which only the owner can read.
    #
    # A parent that already knows the capability may pass it in through the
    # environment instead (never a CLI flag — `ps` is world-readable). The daemon
    # uses that path to keep one mode-0600 capability stable across launchd
    # restarts; direct invocation can use it to hand the same value to proxy and
    # client. The env var is cleared once read, so the auth-helper child never
    # inherits it.
    capability = usable_capability(os.environ.pop(CAPABILITY_ENV_VAR, None)) or mint_capability()

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
            "capability": capability,
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
            from adp_deployments import _process_start

            # The bind succeeded, so the port below is real and reachable.
            #
            # The capability rides in this same 0600 record because the launcher
            # already reads it to find the port, and the file's permissions are
            # exactly the boundary the capability needs: readable by this user,
            # unreadable by a web page or another local account.
            write_identity(
                identity_file,
                {
                    "pid": os.getpid(),
                    "process_start": _process_start(os.getpid()),
                    "proxy": "adp-gateway-proxy",
                    "port": bound_port,
                    "deployment_id": deployment_id,
                    "deployment": deployment_name,
                    "gateway_url": gateway_url,
                    IDENTITY_CAPABILITY_KEY: capability,
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

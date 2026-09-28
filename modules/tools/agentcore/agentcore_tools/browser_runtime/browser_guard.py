"""Connection-boundary enforcement for URL analysis.

The browser never opens target sockets. Every browser request is intercepted,
resolved and checked here, then fetched through a transport that connects to a
specific approved address while preserving the original HTTP Host and TLS SNI.
Redirects return to the browser and are intercepted as new requests.

Use :func:`open_guarded_browser`; it returns only after a fresh browser context
is offline, has service workers disabled and has all routes installed.
"""

from __future__ import annotations

import base64
import http.client
import io
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit

from agentcore_tools.browser_runtime.runtime_limits import LEASE_SECONDS, NAVIGATION_SECONDS
from agentcore_tools.browser_runtime.denylist import (
    REASON_SCHEME_NOT_ALLOWED,
    DenylistConfig,
    DenylistResult,
    canonical_hostname,
    check_connect_address,
    check_url,
    normalize_backslashes,
    scrub_url_credentials,
)

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 30

DEFAULT_ANALYSIS_TIMEOUT_SECONDS = LEASE_SECONDS
DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_ANALYSIS_BYTES = 100 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

REASON_RESPONSE_TOO_LARGE = "response_too_large"
REASON_FETCH_DEADLINE_EXCEEDED = "fetch_deadline_exceeded"

_REQUEST_HOP_BY_HOP_HEADERS = {
    "connection",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_RESPONSE_HOP_BY_HOP_HEADERS = _REQUEST_HOP_BY_HOP_HEADERS | {"content-length"}


class _FetchDeadlineExceeded(TimeoutError):
    pass


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline: float, timeout: float, clock) -> None:
        self._socket = sock
        self._deadline = deadline
        self._timeout = timeout
        self._clock = clock

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise _FetchDeadlineExceeded
        self._socket.settimeout(min(self._timeout, remaining))
        return self._socket.recv_into(buffer)


class _DeadlineSocket:
    def __init__(self, sock, deadline: float, timeout: float, clock) -> None:
        self._socket = sock
        self._deadline = deadline
        self._timeout = timeout
        self._clock = clock

    def makefile(self, mode="r", buffering=None, *args, **kwargs):
        if mode != "rb":
            return self._socket.makefile(mode, buffering, *args, **kwargs)
        buffer_size = (
            buffering if buffering and buffering > 0 else io.DEFAULT_BUFFER_SIZE
        )
        return io.BufferedReader(
            _DeadlineReader(self._socket, self._deadline, self._timeout, self._clock),
            buffer_size=buffer_size,
        )

    def __getattr__(self, name):
        return getattr(self._socket, name)


class DestinationRefused(Exception):
    """Raised before browsing when a destination cannot be safely reached."""

    def __init__(self, url: str, result: DenylistResult) -> None:
        safe_url = scrub_url_credentials(url)
        super().__init__(f"destination refused for {safe_url}: {result.reason}")
        self.url = safe_url
        self.result = result
        self.browser_start_unattempted = False

    @property
    def reason(self) -> str:
        return self.result.reason

    @property
    def reason_code(self) -> str:
        return self.result.reason_code


def _host_of(url: str) -> str:
    try:
        return canonical_hostname(urlsplit(normalize_backslashes(url)).hostname or "")
    except ValueError:
        return ""


def vet_destination(url: str, config: DenylistConfig | None = None) -> DenylistResult:
    """Return the approved addresses for ``url`` or raise before session start."""
    result = check_url(url, config)
    if not result.allowed:
        logger.warning(
            "url-analysis destination refused: code=%s reason=%s",
            result.reason_code,
            result.reason,
        )
        raise DestinationRefused(url, result)
    return result


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose socket target is an already-vetted IP literal."""

    def __init__(
        self,
        host: str,
        connect_ip: str,
        port: int,
        timeout: float,
        deadline: float | None = None,
        clock=time.monotonic,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self.connect_ip = connect_ip
        self.deadline = deadline
        self.clock = clock

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self.connect_ip, self.port), self.timeout, self.source_address
        )
        if self.deadline is not None:
            self.sock = _DeadlineSocket(
                self.sock, self.deadline, self.timeout, self.clock
            )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Pinned TLS connection preserving the original hostname as TLS SNI."""

    def __init__(
        self,
        host: str,
        connect_ip: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
        deadline: float | None = None,
        clock=time.monotonic,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self.connect_ip = connect_ip
        self.deadline = deadline
        self.clock = clock

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self.connect_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        if self.deadline is not None:
            self.sock = _DeadlineSocket(
                self.sock, self.deadline, self.timeout, self.clock
            )


@dataclass(frozen=True)
class PinnedResponse:
    """Response supplied to Playwright without allowing browser networking."""

    status: int
    headers: dict[str, str]
    body: bytes
    connected_ip: str = ""


class PinnedHTTPTransport:
    """Fetch requests over sockets opened only to the decision's approved IPs."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        analysis_timeout: float = DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_analysis_bytes: int = DEFAULT_MAX_ANALYSIS_BYTES,
        verify_tls: bool = True,
        clock=time.monotonic,
    ) -> None:
        self.timeout = timeout
        self.response_timeout = response_timeout
        self.max_response_bytes = max_response_bytes
        self.max_analysis_bytes = max_analysis_bytes
        self.verify_tls = verify_tls
        self._clock = clock
        self._analysis_deadline = clock() + analysis_timeout
        self._bytes_read = 0

    def _refuse(self, url: str, reason: str, reason_code: str) -> DestinationRefused:
        return DestinationRefused(
            url,
            DenylistResult(allowed=False, reason=reason, reason_code=reason_code),
        )

    def _remaining_bytes(self) -> int:
        return self.max_analysis_bytes - self._bytes_read

    def _deadline(self, url: str) -> float:
        now = self._clock()
        deadline = min(now + self.response_timeout, self._analysis_deadline)
        if now >= deadline:
            raise self._refuse(
                url,
                "the URL-analysis fetch exceeded its wall-clock deadline",
                REASON_FETCH_DEADLINE_EXCEEDED,
            )
        return deadline

    def _validate_content_length(
        self, url: str, headers: list[tuple[str, str]]
    ) -> None:
        lengths = [
            value.strip() for name, value in headers if name.lower() == "content-length"
        ]
        if not lengths:
            return
        try:
            declared_lengths = {int(value) for value in lengths}
        except ValueError as error:
            raise http.client.HTTPException("invalid Content-Length") from error
        if len(declared_lengths) != 1 or next(iter(declared_lengths)) < 0:
            raise http.client.HTTPException("conflicting Content-Length values")
        declared = next(iter(declared_lengths))
        if declared > self.max_response_bytes or declared > self._remaining_bytes():
            raise self._refuse(
                url,
                "the response exceeds the URL-analysis byte budget",
                REASON_RESPONSE_TOO_LARGE,
            )

    def _read_body(self, url: str, response, deadline: float, connection) -> bytes:
        chunks: list[bytes] = []
        response_bytes = 0
        while True:
            remaining_time = deadline - self._clock()
            if remaining_time <= 0:
                raise self._refuse(
                    url,
                    "the URL-analysis fetch exceeded its wall-clock deadline",
                    REASON_FETCH_DEADLINE_EXCEEDED,
                )
            if connection.sock is not None:
                connection.sock.settimeout(min(self.timeout, remaining_time))

            chunk = response.read1(READ_CHUNK_BYTES)
            if not chunk:
                break
            response_bytes += len(chunk)
            self._bytes_read += len(chunk)
            if (
                response_bytes > self.max_response_bytes
                or self._bytes_read > self.max_analysis_bytes
            ):
                raise self._refuse(
                    url,
                    "the response exceeds the URL-analysis byte budget",
                    REASON_RESPONSE_TOO_LARGE,
                )
            chunks.append(chunk)

        return b"".join(chunks)

    def _open_connection(
        self, scheme: str, host: str, port: int, connect_ip: str, deadline: float
    ) -> http.client.HTTPConnection:
        if scheme == "https":
            context = (
                ssl.create_default_context()
                if self.verify_tls
                else ssl._create_unverified_context()
            )
            return _PinnedHTTPSConnection(
                host,
                connect_ip,
                port,
                self.timeout,
                context,
                deadline,
                self._clock,
            )
        return _PinnedHTTPConnection(
            host, connect_ip, port, self.timeout, deadline, self._clock
        )

    def fetch(
        self,
        request,
        decision: DenylistResult,
        config: DenylistConfig | None = None,
    ) -> PinnedResponse:
        """Fetch one intercepted request through an explicitly pinned socket."""
        url = normalize_backslashes(getattr(request, "url", "") or "")
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        scheme = parsed.scheme.lower()
        if not decision.resolved_ips:
            raise OSError("destination decision contains no approved address")

        connect_ip = decision.resolved_ips[0]
        bound = check_connect_address(connect_ip, decision.resolved_ips, config)
        if not bound.allowed:
            raise DestinationRefused(url, bound)

        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        headers = {
            name: value
            for name, value in request.all_headers().items()
            if name.lower() not in _REQUEST_HOP_BY_HOP_HEADERS
        }
        body = getattr(request, "post_data_buffer", None)
        response_deadline = self._deadline(url)
        connection = self._open_connection(
            scheme, host, port, connect_ip, response_deadline
        )
        try:
            connection.request(
                getattr(request, "method", "GET"), path, body=body, headers=headers
            )
            response = connection.getresponse()
            response_headers_list = response.getheaders()
            self._validate_content_length(url, response_headers_list)
            response_body = self._read_body(
                url, response, response_deadline, connection
            )
            response_headers: dict[str, str] = {}
            for name, value in response_headers_list:
                lower_name = name.lower()
                if lower_name in _RESPONSE_HOP_BY_HOP_HEADERS:
                    continue
                if lower_name == "set-cookie" and name in response_headers:
                    response_headers[name] = f"{response_headers[name]}\n{value}"
                else:
                    response_headers[name] = value
            return PinnedResponse(
                response.status, response_headers, response_body, connect_ip
            )
        except TimeoutError as error:
            raise self._refuse(
                url,
                "the URL-analysis fetch exceeded its wall-clock deadline",
                REASON_FETCH_DEADLINE_EXCEEDED,
            ) from error
        finally:
            connection.close()


class _CDPRequest:
    """Request facade for Fetch interception, including redirected requests."""

    def __init__(self, event, main_frame_id=None):
        self._request = event["request"]
        self.url = self._request["url"]
        self.method = self._request["method"]
        self.post_data_buffer = None
        self._navigation = event.get("resourceType") == "Document"
        self.is_main_navigation = self._navigation and (
            main_frame_id is None or event.get("frameId") == main_frame_id
        )

    def all_headers(self):
        return self._request.get("headers", {})

    def is_navigation_request(self):
        return self._navigation


class _CDPRoute:
    def __init__(self, cdp, request_id):
        self._cdp = cdp
        self._request_id = request_id

    def abort(self, reason):
        self._cdp.send(
            "Fetch.failRequest",
            {"requestId": self._request_id, "errorReason": "BlockedByClient"},
        )

    def fulfill(self, *, status, headers, body):
        self._cdp.send(
            "Fetch.fulfillRequest",
            {
                "requestId": self._request_id,
                "responseCode": status,
                "responseHeaders": [
                    {"name": k, "value": part}
                    for k, v in headers.items()
                    for part in v.split("\n")
                ],
                "body": base64.b64encode(body).decode(),
            },
        )


@dataclass
class NavigationGuard:
    """Resolve, validate and proxy every request made by the guarded context."""

    vetted: DenylistResult
    target_host: str = ""
    config: DenylistConfig | None = None
    transport: PinnedHTTPTransport = field(default_factory=PinnedHTTPTransport)
    refusals: list[dict[str, str | bool]] = field(default_factory=list)
    read_only: bool = False
    connections: list[dict] = field(default_factory=list)
    transport_errors: list[dict] = field(default_factory=list)
    connections_dropped: int = 0
    refusals_dropped: int = 0
    navigation_check: object = None

    def decide(self, url: str) -> DenylistResult:
        """Re-resolve every request and bind the original host to its vetted set."""
        parsed_host = _host_of(url)
        result = check_url(url, self.config)
        if (
            result.allowed
            and parsed_host
            and parsed_host == self.target_host
            and self.vetted.resolved_ips
        ):
            # Public load balancers rotate their DNS answers between observations.
            # Keep only addresses approved at session start and still advertised
            # now. check_url above rejects the entire answer if any IP is unsafe;
            # this intersection never expands the session's approved addresses.
            retained = [
                address
                for address in result.resolved_ips
                if address in self.vetted.resolved_ips
            ]
            if not retained:
                return check_connect_address(
                    result.resolved_ips[0], self.vetted.resolved_ips, self.config
                )
            return DenylistResult(allowed=True, resolved_ips=retained)
        return result

    def _record_refusal(
        self, url: str, result: DenylistResult, *, navigation: bool = False
    ) -> None:
        if len(self.refusals) >= 200:
            self.refusals_dropped += 1
            return
        safe_url = scrub_url_credentials(url)
        logger.warning(
            "url-analysis blocked request: url=%s code=%s reason=%s",
            safe_url,
            result.reason_code,
            result.reason,
        )
        self.refusals.append(
            {
                "url": safe_url,
                "reason": result.reason,
                "reason_code": result.reason_code,
                "navigation": navigation,
            }
        )

    def handle_route(self, route, request=None) -> None:
        """Fulfill allowed requests through pinned transport; never continue raw."""
        target = request if request is not None else getattr(route, "request", None)
        url = getattr(target, "url", "") or ""
        is_navigation_request = getattr(target, "is_navigation_request", None)
        navigation = bool(is_navigation_request and is_navigation_request())
        if self.read_only and getattr(target, "method", "GET") not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            self._record_refusal(
                url,
                DenylistResult(
                    allowed=False,
                    reason="State-changing HTTP methods are disabled",
                    reason_code="method_not_allowed",
                ),
                navigation=navigation,
            )
            route.abort("blockedbyclient")
            return
        if (
            navigation
            and getattr(target, "is_main_navigation", True)
            and self.navigation_check is not None
        ):
            result = self.navigation_check(url)
            if not result.allowed:
                self._record_refusal(url, result, navigation=True)
                route.abort("blockedbyclient")
                return
        result = self.decide(url)
        if not result.allowed:
            self._record_refusal(url, result, navigation=navigation)
            route.abort("blockedbyclient")
            return

        try:
            response = self.transport.fetch(target, result, self.config)
        except DestinationRefused as refusal:
            self._record_refusal(url, refusal.result, navigation=navigation)
            route.abort("blockedbyclient")
            return
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            logger.warning(
                "url-analysis pinned fetch failed: url=%s error_type=%s",
                scrub_url_credentials(url),
                type(error).__name__,
            )
            if len(self.transport_errors) < 200:
                diagnostic = {
                    "url": scrub_url_credentials(url),
                    "error_type": type(error).__name__,
                }
                if isinstance(error, ssl.SSLCertVerificationError):
                    diagnostic.update(
                        tls_verify_code=error.verify_code,
                        tls_verify_message=error.verify_message,
                    )
                self.transport_errors.append(diagnostic)
            route.abort("connectionfailed")
            return

        if len(self.connections) < 200:
            self.connections.append(
                {
                    "url": scrub_url_credentials(url),
                    "connected_ip": response.connected_ip,
                    "status": response.status,
                    "response_bytes": len(response.body),
                }
            )
        else:
            self.connections_dropped += 1
        route.fulfill(
            status=response.status, headers=response.headers, body=response.body
        )

    def handle_websocket(self, websocket_route) -> None:
        """Block WebSockets because Playwright cannot proxy them to a pinned IP."""
        url = getattr(websocket_route, "url", "") or ""
        result = DenylistResult(
            allowed=False,
            reason="WebSocket connections are disabled for URL analysis",
            reason_code=REASON_SCHEME_NOT_ALLOWED,
        )
        self._record_refusal(url, result)
        websocket_route.close(code=1008, reason="destination refused")

    def install_cdp(self, context, page) -> None:
        """Fetch sees every redirect; Playwright routes handle only the first hop.

        Browser networking remains offline. Unattached targets fail closed.
        No continueRequest/continue route operation exists in this path.
        """
        cdp = context.new_cdp_session(page)
        tree = cdp.send("Page.getFrameTree") or {}
        main_frame_id = tree.get("frameTree", {}).get("frame", {}).get("id")
        cdp.on(
            "Fetch.requestPaused",
            lambda event: self.handle_route(
                _CDPRoute(cdp, event["requestId"]), _CDPRequest(event, main_frame_id)
            ),
        )
        cdp.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
        )
        context.route_web_socket("**/*", self.handle_websocket)

    def install(self, context) -> None:
        """Install policy before any page exists in the guarded context."""
        context.route("**/*", self.handle_route)
        route_web_socket = getattr(context, "route_web_socket", None)
        if route_web_socket is not None:
            route_web_socket("**/*", self.handle_websocket)


class GuardedBrowserSession:
    """Restricted URL-analysis facade over a fully guarded Playwright page."""

    def __init__(
        self,
        *,
        client,
        session_id: str,
        browser,
        context,
        page,
        guard: NavigationGuard,
    ) -> None:
        self._client = client
        self._browser = browser
        self._context = context
        self._page = page
        self.session_id = session_id
        self.guard = guard
        self._closed = False

    @property
    def refusals(self) -> list[dict[str, str | bool]]:
        return self.guard.refusals

    @property
    def url(self) -> str:
        return self._page.url

    @property
    def main_frame(self):
        return self._page.main_frame

    @property
    def frames(self):
        return self._page.frames

    @property
    def browser_version(self):
        return self._browser.version

    def wait_for_timeout(self, milliseconds: int):
        return self._page.wait_for_timeout(milliseconds)

    def goto(self, url: str, **kwargs):
        return self._page.goto(url, **kwargs)

    def go_back(self):
        return self._page.go_back(
            wait_until="domcontentloaded", timeout=NAVIGATION_SECONDS * 1000
        )

    def click_observed(self, selector: str, index: int, expected: dict):
        """Only broker-owned selectors and inspected elements enter this facade."""
        element = self._page.locator(selector).nth(index)
        actual = element.evaluate(
            "e => ({href:e.href || '', text:(e.innerText || e.textContent || '').slice(0,150), download:e.hasAttribute('download'), target:e.getAttribute('target') || ''})"
        )
        if actual != expected:
            raise ValueError("Observed element changed; inspect a fresh view")
        if actual["download"]:
            raise ValueError("Download clicks are disabled")
        same_tab = bool(
            actual["href"] and actual["target"] not in {"", "_self", "_top", "_parent"}
        )
        if same_tab:
            # Follow the selected link without allowing an unguarded new target.
            # The source click handler still runs, preserving its session state.
            element.evaluate("e => e.setAttribute('target', '_self')")
        element.click(timeout=10000, no_wait_after=False)
        self._page.wait_for_timeout(200)
        return {"target_rewritten_to_self": same_tab}

    def scroll_view(self):
        self._page.evaluate("window.scrollBy(0, Math.min(window.innerHeight, 900))")
        self._page.wait_for_timeout(200)

    def screenshot(self, **kwargs):
        return self._page.screenshot(**kwargs)

    def inner_text(self, selector: str, **kwargs):
        return self._page.inner_text(selector, **kwargs)

    def title(self) -> str:
        return self._page.title()

    def evaluate(self, expression: str, arg=None):
        if arg is None:
            return self._page.evaluate(expression)
        return self._page.evaluate(expression, arg)

    def on(self, event: str, callback) -> None:
        self._page.on(event, callback)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._page.close()
        finally:
            try:
                self._context.close()
            finally:
                try:
                    self._browser.close()
                finally:
                    self._client.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def open_guarded_browser(
    url: str,
    playwright,
    *,
    region: str = DEFAULT_REGION,
    config: DenylistConfig | None = None,
    client_factory=None,
    context_options: dict | None = None,
    transport: PinnedHTTPTransport | None = None,
    read_only: bool = False,
    navigation_check=None,
) -> GuardedBrowserSession:
    """Create and return the only supported URL-analysis browser interface."""
    try:
        vetted = vet_destination(url, config)
    except DestinationRefused as error:
        error.browser_start_unattempted = True
        raise

    if client_factory is None:
        from bedrock_agentcore.tools.browser_client import BrowserClient

        client = BrowserClient(region=region)
    else:
        client = client_factory(region)

    session_id = client.start(session_timeout_seconds=DEFAULT_ANALYSIS_TIMEOUT_SECONDS)
    browser = None
    context = None
    page = None
    try:
        ws_url, headers = client.generate_ws_headers()
        browser = playwright.chromium.connect_over_cdp(ws_url, headers=headers)
        options = dict(context_options or {})
        options["offline"] = True
        options["service_workers"] = "block"
        context = browser.new_context(**options)
        guard = NavigationGuard(
            read_only=read_only,
            navigation_check=navigation_check,
            vetted=vetted,
            target_host=_host_of(url),
            config=config,
            transport=transport
            or PinnedHTTPTransport(
                verify_tls=not bool(options.get("ignore_https_errors"))
            ),
        )
        if not read_only:
            guard.install(context)
        page = context.new_page()
        if read_only:
            guard.install_cdp(context, page)
            context.on("page", lambda other: other.close() if other != page else None)
            page.set_default_timeout(10000)
        return GuardedBrowserSession(
            client=client,
            session_id=session_id,
            browser=browser,
            context=context,
            page=page,
            guard=guard,
        )
    except Exception:
        try:
            if page is not None:
                page.close()
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                try:
                    if browser is not None:
                        browser.close()
                finally:
                    client.stop()
        raise

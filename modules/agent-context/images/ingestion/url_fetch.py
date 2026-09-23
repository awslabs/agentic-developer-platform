#!/usr/bin/env python3
"""SSRF-safe HTTP fetching for the ingestion pipeline (#5658).

What this module is for
-----------------------
Ingestion fetches attacker-influenced URLs: a crawl target supplied at
registration, every ``<loc>`` in a sitemap the target itself serves, and every
redirect hop those produce. Before this module the pipeline passed all of them
straight to ``requests.get`` with redirects followed by default, so a submitted
documentation URL could reach ``169.254.169.254`` and have the instance's
credentials stored as indexed content.

Why validation alone is not enough
----------------------------------
``url_denylist.check_url`` resolves the hostname and classifies the addresses,
but between that answer and the socket being opened, ``requests`` resolves the
name a *second* time. A DNS server that returns a public address on the first
lookup and a private one on the second (DNS rebinding) passes the check and
connects somewhere else — the check is then decoration.

So every fetch here opens its connection to one of the exact addresses the
decision approved. The hostname still travels in the ``Host`` header and TLS SNI,
so virtual hosting and certificate validation are unaffected; only the address
resolution is taken out of the fetch's hands.

Redirects
---------
Redirects are never followed by the transport. ``fetch`` handles each hop itself
so that every hop is a fresh admission decision. Following redirects inside
``requests`` would validate only hop 0 and leave the rest unchecked, which is
the same defect one level down.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3
from requests.structures import CaseInsensitiveDict
from urllib3.util import connection as urllib3_connection

from url_denylist import (
    DenylistConfig,
    DenylistResult,
    canonical_address,
    check_connect_address,
    check_url,
    normalize_backslashes,
    scrub_url_credentials,
)

# Redirect chains are bounded: each hop is a fetch of an attacker-chosen URL, and
# an unbounded chain is a denial-of-service vector even when every hop is allowed.
MAX_REDIRECTS = 5

# Responses larger than this are refused. Ingestion stores documents, not disk
# images, and an unbounded read is how a hostile endpoint exhausts the worker.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

DEFAULT_TIMEOUT = 30


class DestinationRefused(Exception):
    """A URL was refused before or during fetching.

    Carries the structured result so callers can log the reason code without
    re-deriving it, and so a refusal can never be mistaken for a transport error
    and retried into success.
    """

    def __init__(self, url: str, result: DenylistResult) -> None:
        # The URL is scrubbed: a refused URL can carry userinfo credentials, and
        # this message reaches logs.
        self.url = scrub_url_credentials(url)
        self.result = result
        super().__init__(f"refused {self.url}: {result.reason}")

    @property
    def reason_code(self) -> str:
        return self.result.reason_code


@dataclass
class FetchResponse:
    """The parts of a response ingestion actually consumes."""

    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    #: Every URL in the redirect chain, starting with the requested one. Each was
    #: independently validated.
    chain: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Preserve requests' header lookup contract for existing ETag and
        # Last-Modified callers as well as lower-case content-type lookups.
        self.headers = CaseInsensitiveDict(self.headers)

    @property
    def text(self) -> str:
        """Decode the body, preferring the charset the response declared."""
        encoding = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            encoding = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return self.content.decode(encoding, errors="replace")


def _pinned_connection_factory(approved_ips: list[str], config: DenylistConfig):
    """Build a ``create_connection`` replacement that dials only approved IPs.

    urllib3 calls ``create_connection((host, port), ...)`` from inside the
    connection object, after TLS parameters are already set up. Substituting the
    address here — rather than rewriting the URL to contain the IP — is what
    keeps SNI and certificate verification bound to the real hostname while the
    socket goes to a vetted address.
    """

    def create_connection(address, *args, **kwargs):
        host, port = address[0], address[1]

        # Re-validate at the moment of connecting. `host` here is whatever
        # urllib3 is about to dial; if a rebinding attack or an internal retry
        # changed it, this is the last point at which that is still catchable.
        verdict = check_connect_address(host, approved_ips, config)
        if not verdict.allowed:
            raise DestinationRefused(host, verdict)

        return urllib3_connection.create_connection((host, port), *args, **kwargs)

    return create_connection


def _build_pinned_pool(
    scheme: str,
    host: str,
    port: int,
    approved_ips: list[str],
    config: DenylistConfig,
    timeout: int,
):
    """Return a connection pool whose sockets only reach ``approved_ips``."""
    pin = _pinned_connection_factory(approved_ips, config)
    # One approved address is chosen for the connection. Choosing here (rather
    # than letting the resolver pick) is the whole point: the address that was
    # judged is the address that gets dialled.
    target_ip = approved_ips[0]

    if scheme == "https":

        class _PinnedHTTPSConnection(urllib3.connection.HTTPSConnection):
            def _new_conn(self):
                return pin(
                    (target_ip, self.port),
                    self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )

        class _PinnedHTTPSPool(urllib3.HTTPSConnectionPool):
            ConnectionCls = _PinnedHTTPSConnection

        return _PinnedHTTPSPool(host, port=port, timeout=timeout, retries=False)

    class _PinnedHTTPConnection(urllib3.connection.HTTPConnection):
        def _new_conn(self):
            return pin(
                (target_ip, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )

    class _PinnedHTTPPool(urllib3.HTTPConnectionPool):
        ConnectionCls = _PinnedHTTPConnection

    return _PinnedHTTPPool(host, port=port, timeout=timeout, retries=False)


def validate_url(url: str, config: DenylistConfig | None = None) -> DenylistResult:
    """Decide whether ``url`` may be fetched at all.

    Backslashes are normalised first: browsers and some parsers treat ``\\`` as
    ``/``, so ``http://evil.test\\@169.254.169.254/`` has one authority to
    ``urlsplit`` and a different one to a browser. Normalising before parsing
    removes that disagreement.
    """
    return check_url(normalize_backslashes(url), config)


def _single_fetch(
    method: str,
    url: str,
    headers: dict[str, str] | None,
    timeout: int,
    config: DenylistConfig,
    body: bytes | None = None,
) -> tuple[FetchResponse, DenylistResult]:
    """Validate one hop, then fetch it, following no redirects.

    The validation lives here and the socket work lives in ``_transport_fetch``.
    Keeping them in separate functions is deliberate: it means a test can replace
    the transport while the real admission decision still runs on every hop.
    Stubbing this function instead would remove the check under test.
    """
    normalized = normalize_backslashes(url)
    verdict = check_url(normalized, config)
    if not verdict.allowed:
        raise DestinationRefused(url, verdict)

    if body is None:
        return _transport_fetch(method, normalized, headers, timeout, config, verdict), verdict
    return _transport_fetch(method, normalized, headers, timeout, config, verdict, body), verdict


def _transport_fetch(
    method: str,
    normalized: str,
    headers: dict[str, str] | None,
    timeout: int,
    config: DenylistConfig,
    verdict: DenylistResult,
    body: bytes | None = None,
) -> FetchResponse:
    """Open a pinned connection to an approved address and read the response.

    Performs no admission decision of its own — it is handed the approved
    addresses and connects only to those.
    """
    url = normalized
    parts = urlsplit(normalized)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = urlunsplit(("", "", parts.path or "/", parts.query, ""))

    pool = _build_pinned_pool(
        parts.scheme, parts.hostname or "", port, verdict.resolved_ips, config, timeout
    )
    try:
        raw = pool.request(
            method,
            path,
            headers=dict(headers or {}),
            body=body,
            redirect=False,
            preload_content=False,
        )
        try:
            # Read one byte past the cap so an over-large body is detected rather
            # than silently truncated into what looks like a complete document.
            body = raw.read(MAX_RESPONSE_BYTES + 1)
        finally:
            raw.release_conn()

        if len(body) > MAX_RESPONSE_BYTES:
            raise DestinationRefused(
                url,
                DenylistResult(
                    allowed=False,
                    reason=f"response exceeds {MAX_RESPONSE_BYTES} bytes",
                    reason_code="response_too_large",
                ),
            )

        response = FetchResponse(
            url=normalized,
            status_code=raw.status,
            headers={k.lower(): v for k, v in raw.headers.items()},
            content=body if method != "HEAD" else b"",
        )
        return response
    finally:
        pool.close()


def fetch(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    config: DenylistConfig | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResponse:
    """Fetch ``url``, validating the initial URL and every redirect hop.

    Raises ``DestinationRefused`` if any hop is not allowed. A refusal mid-chain
    is still a refusal: the caller gets no content, because content from hop N is
    exactly what an open redirect to an internal address is trying to obtain.
    """
    if config is None:
        config = DenylistConfig()

    chain: list[str] = []
    current = url

    for _ in range(max_redirects + 1):
        response, _verdict = _single_fetch(method, current, headers, timeout, config)
        chain.append(response.url)

        location = response.headers.get("location", "")
        if not (300 <= response.status_code < 400 and location):
            response.chain = chain
            return response

        # Resolve the redirect against the hop that issued it, so a relative
        # Location is interpreted the way the server meant it — then re-validate
        # from scratch on the next iteration.
        current = requests.compat.urljoin(response.url, location)

    raise DestinationRefused(
        url,
        DenylistResult(
            allowed=False,
            reason=f"redirect chain exceeded {max_redirects} hops",
            reason_code="too_many_redirects",
        ),
    )


def same_origin(candidate: str, base: str) -> bool:
    """Whether ``candidate`` has the same scheme, host and port as ``base``.

    Used to keep a crawl inside the site that was actually submitted. Compared on
    the canonical host — an IP-literal host is compared as an address so that
    ``127.1``, ``2130706433`` and ``127.0.0.1`` cannot appear to be three
    different origins, which is how an origin check gets walked past.
    """
    try:
        c = urlsplit(normalize_backslashes(candidate))
        b = urlsplit(normalize_backslashes(base))
    except ValueError:
        return False

    if c.scheme != b.scheme:
        return False

    default_port = 443 if c.scheme == "https" else 80
    if (c.port or default_port) != (b.port or default_port):
        return False

    c_host, b_host = (c.hostname or "").lower(), (b.hostname or "").lower()
    c_addr, b_addr = canonical_address(c_host), canonical_address(b_host)
    if c_addr is not None or b_addr is not None:
        return c_addr is not None and b_addr is not None and c_addr == b_addr
    return c_host == b_host


def resolve_and_check(hostname: str, config: DenylistConfig | None = None) -> DenylistResult:
    """Validate a bare hostname or address with no URL around it.

    For callers holding a host rather than a URL (e.g. a configured endpoint).
    Fails closed when resolution fails: an unresolvable name is an unknown
    destination, and unknown is not permission.
    """
    if config is None:
        config = DenylistConfig()

    literal = canonical_address(hostname)
    if literal is not None:
        return check_connect_address(str(literal), [str(literal)], config)

    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        return DenylistResult(
            allowed=False,
            reason=f"could not resolve '{hostname}': {exc}",
            reason_code="resolution_failed",
        )

    addresses = {str(ipaddress.ip_address(info[4][0])) for info in infos}
    if not addresses:
        return DenylistResult(
            allowed=False,
            reason=f"'{hostname}' resolved to no addresses",
            reason_code="resolution_failed",
        )

    for address in sorted(addresses):
        verdict = check_connect_address(address, sorted(addresses), config)
        if not verdict.allowed:
            return verdict

    return DenylistResult(allowed=True, resolved_ips=sorted(addresses))

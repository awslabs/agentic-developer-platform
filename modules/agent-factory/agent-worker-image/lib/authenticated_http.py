"""HTTP transport that keeps authentication on the configured origin."""

from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("authenticated endpoint must be an HTTP origin without userinfo")
    port = parsed.port
    return (
        parsed.scheme,
        parsed.hostname,
        port if port is not None else (443 if parsed.scheme == "https" else 80),
    )


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Retain urllib's same-origin behavior, but never forward auth elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            permitted = _origin(req.full_url) == _origin(newurl)
        except ValueError:
            permitted = False
        if not permitted:
            # Do not expose a redirect URL or body in client diagnostics; either
            # may contain sensitive values. Closing also releases the response.
            if fp is not None:
                fp.close()
            raise HTTPError(
                req.full_url, code, "cross-origin authenticated redirect refused", headers, None
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_authenticated(request: Request, *, timeout: float = 30):
    """Open one authenticated request without allowing origin or scheme escape.

    Initial HTTP remains supported for the existing internal legacy transport.
    HTTPS-to-HTTP, other hosts/ports, userinfo and non-HTTP schemes are refused.
    No global opener is installed, so unrelated public HTTP behavior is unchanged.
    """
    try:
        _origin(request.full_url)
    except ValueError:
        raise URLError("invalid authenticated endpoint") from None
    return build_opener(SameOriginRedirectHandler()).open(request, timeout=timeout)

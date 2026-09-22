"""Unprivileged client for the guarded URL-analysis browser broker."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from browser_guard import DestinationRefused
from denylist import DenylistResult

DEFAULT_BROKER_URL = (
    "http://url-analysis-browser-broker.adp-agents.svc.cluster.local:8765"
)
DEFAULT_TIMEOUT_SECONDS = 360
MAX_ERROR_BYTES = 64 * 1024


class BrowserBrokerError(RuntimeError):
    """Raised when the trusted browser broker cannot complete an analysis."""


def _decode_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrowserBrokerError(
            "browser broker returned an invalid response"
        ) from error
    if not isinstance(value, dict):
        raise BrowserBrokerError("browser broker returned an invalid response")
    return value


def analyze_url(
    url: str,
    *,
    wait_until: str = "networkidle",
    timeout_ms: int = 30_000,
    ignore_https_errors: bool = False,
    broker_url: str | None = None,
    request_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Analyze ``url`` through the broker that exclusively owns browser access."""
    endpoint = (
        broker_url
        or os.environ.get("URL_ANALYSIS_BROWSER_BROKER")
        or DEFAULT_BROKER_URL
    ).rstrip("/")
    body = json.dumps(
        {
            "url": url,
            "wait_until": wait_until,
            "timeout_ms": timeout_ms,
            "ignore_https_errors": ignore_https_errors,
        }
    ).encode()
    request = Request(
        f"{endpoint}/v1/analyze",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=request_timeout_seconds) as response:
            result = _decode_json(response.read())
    except HTTPError as error:
        payload = _decode_json(error.read(MAX_ERROR_BYTES))
        if error.code == 403 and payload.get("error") == "destination_refused":
            decision = DenylistResult(
                allowed=False,
                reason=str(payload.get("reason", "destination refused")),
                reason_code=str(payload.get("reason_code", "blocked_address")),
            )
            raise DestinationRefused(url, decision) from error
        message = str(payload.get("message") or "browser broker request failed")
        raise BrowserBrokerError(message) from error
    except URLError as error:
        raise BrowserBrokerError("browser broker is unavailable") from error

    if result.get("status") != "ok" or not isinstance(result.get("analysis"), dict):
        raise BrowserBrokerError("browser broker returned an invalid response")
    return result["analysis"]

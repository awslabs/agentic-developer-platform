"""Trusted HTTP broker that exclusively owns AgentCore Browser credentials."""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from browser_guard import DEFAULT_REGION, DestinationRefused, open_guarded_browser
from denylist import DenylistResult, scrub_url_credentials

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 32 * 1024
MAX_VISIBLE_TEXT_CHARS = 100_000
ALLOWED_WAIT_STATES = {"commit", "domcontentloaded", "load", "networkidle"}
FORM_EXPRESSION = """
Array.from(document.querySelectorAll('form')).map(form => ({
  action: form.action,
  method: form.method,
  fields: Array.from(form.querySelectorAll('input')).map(input => ({
    name: input.name,
    type: input.type,
    hidden: input.type === 'hidden'
  }))
}))
"""
ORPHAN_INPUT_EXPRESSION = """
Array.from(document.querySelectorAll('input[type="password"], input[type="email"]'))
  .filter(input => !input.closest('form'))
  .map(input => ({name: input.name || '', type: input.type}))
"""


class InvalidBrokerRequest(ValueError):
    pass


def _validated_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidBrokerRequest("request body must be a JSON object")
    allowed_keys = {"url", "wait_until", "timeout_ms", "ignore_https_errors"}
    if set(payload) - allowed_keys:
        raise InvalidBrokerRequest("request contains unsupported fields")
    url = payload.get("url")
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise InvalidBrokerRequest(
            "url must be a non-empty string of at most 8192 characters"
        )
    wait_until = payload.get("wait_until", "networkidle")
    if wait_until not in ALLOWED_WAIT_STATES:
        raise InvalidBrokerRequest("wait_until is not supported")
    timeout_ms = payload.get("timeout_ms", 30_000)
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or not 1 <= timeout_ms <= 120_000
    ):
        raise InvalidBrokerRequest("timeout_ms must be between 1 and 120000")
    ignore_https_errors = payload.get("ignore_https_errors", False)
    if not isinstance(ignore_https_errors, bool):
        raise InvalidBrokerRequest("ignore_https_errors must be a boolean")
    return {
        "url": url,
        "wait_until": wait_until,
        "timeout_ms": timeout_ms,
        "ignore_https_errors": ignore_https_errors,
    }


def _log_analysis_failure(url: str, error: Exception) -> None:
    logger.error(
        "guarded browser analysis failed url=%s error_type=%s",
        scrub_url_credentials(url),
        type(error).__name__,
    )


def analyze_destination(request: dict[str, Any], playwright) -> dict[str, Any]:
    """Capture one destination while all browser and socket access stays local."""
    redirects: list[dict[str, Any]] = []
    frame_navigations: list[str] = []
    downloads: list[dict[str, str]] = []
    context_options = {"ignore_https_errors": request["ignore_https_errors"]}

    with open_guarded_browser(
        request["url"],
        playwright,
        region=os.environ.get("AWS_REGION", DEFAULT_REGION),
        context_options=context_options,
    ) as session:

        def raise_navigation_refusal() -> None:
            refusal = next(
                (item for item in session.refusals if item.get("navigation") is True),
                None,
            )
            if refusal:
                raise DestinationRefused(
                    str(refusal["url"]),
                    DenylistResult(
                        allowed=False,
                        reason=str(refusal["reason"]),
                        reason_code=str(refusal["reason_code"]),
                    ),
                )

        def on_response(response) -> None:
            if 300 <= response.status < 400:
                redirects.append(
                    {
                        "from_url": response.url,
                        "status": response.status,
                        "location": response.headers.get("location", ""),
                    }
                )

        def on_frame(frame) -> None:
            if frame == session.main_frame:
                frame_navigations.append(frame.url)

        def on_download(download) -> None:
            downloads.append(
                {"url": download.url, "suggested_filename": download.suggested_filename}
            )
            download.cancel()

        session.on("response", on_response)
        session.on("framenavigated", on_frame)
        session.on("download", on_download)
        try:
            response = session.goto(
                request["url"],
                wait_until=request["wait_until"],
                timeout=request["timeout_ms"],
            )
        except Exception:
            raise_navigation_refusal()
            if downloads:
                return {
                    "session_id": session.session_id,
                    "final_url": session.url,
                    "http_status": 0,
                    "page_title": "",
                    "screenshot_base64": "",
                    "visible_text": "",
                    "forms": [],
                    "orphan_inputs": [],
                    "redirects": redirects,
                    "frame_navigations": frame_navigations,
                    "downloads": downloads,
                    "refusals": session.refusals,
                }
            raise
        raise_navigation_refusal()
        screenshot = session.screenshot(full_page=True)
        visible_text = session.inner_text("body")
        return {
            "session_id": session.session_id,
            "final_url": session.url,
            "http_status": response.status if response else 0,
            "page_title": session.title(),
            "screenshot_base64": base64.b64encode(screenshot).decode(),
            "visible_text": visible_text[:MAX_VISIBLE_TEXT_CHARS],
            "forms": session.evaluate(FORM_EXPRESSION),
            "orphan_inputs": session.evaluate(ORPHAN_INPUT_EXPRESSION),
            "redirects": redirects,
            "frame_navigations": frame_navigations,
            "downloads": downloads,
            "refusals": session.refusals,
        }


class BrowserBrokerHandler(BaseHTTPRequestHandler):
    server_version = "URLAnalysisBrowserBroker/1"

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/v1/analyze":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if not 0 < content_length <= MAX_REQUEST_BYTES:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
            request = _validated_request(payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            InvalidBrokerRequest,
        ) as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": str(error)},
            )
            return

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                analysis = analyze_destination(request, playwright)
        except DestinationRefused as error:
            self._write_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "destination_refused",
                    "reason": error.reason,
                    "reason_code": error.reason_code,
                },
            )
            return
        except Exception as error:
            _log_analysis_failure(request["url"], error)
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "analysis_failed", "message": "guarded analysis failed"},
            )
            return
        self._write_json(HTTPStatus.OK, {"status": "ok", "analysis": analysis})

    def log_message(self, format: str, *args: object) -> None:
        logger.info("browser broker request: " + format, *args)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    host = os.environ.get("URL_ANALYSIS_BROKER_HOST", "0.0.0.0")
    port = int(os.environ.get("URL_ANALYSIS_BROKER_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), BrowserBrokerHandler)

    def stop_server(signum: int, frame: object) -> None:
        logger.info("URL-analysis browser broker stopping on signal %s", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    logger.info("URL-analysis browser broker listening on %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

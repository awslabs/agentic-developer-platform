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
from case_capture import collect_case, validate_options
from case_contract import MAX_RESPONSE_BYTES
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
    if not isinstance(wait_until, str) or wait_until not in ALLOWED_WAIT_STATES:
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
    _manager_lock = threading.Lock()

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if payload.get("retry_after_seconds"):
            self.send_header("Retry-After", str(payload["retry_after_seconds"]))
        self.end_headers()
        try:
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError):
            logger.info("browser broker caller disconnected before response delivery")
            return False

    def do_GET(self) -> None:
        if self.path in {"/healthz", "/readyz"}:
            manager = getattr(self.server, "investigation_manager", None)
            capacity = (
                manager.capacity()
                if manager
                else {"accepting_starts": True, "active_sessions": 0}
            )
            status = (
                HTTPStatus.OK
                if self.path == "/healthz" or capacity["accepting_starts"]
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._write_json(
                status,
                {"status": "ok" if status == HTTPStatus.OK else "busy", **capacity},
            )
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_investigation(self) -> None:
        from investigation_browser import InvestigationError, InvestigationManager

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("An object is required")
            with self._manager_lock:
                if not hasattr(self.server, "investigation_manager"):
                    self.server.investigation_manager = InvestigationManager()
                manager = self.server.investigation_manager
            operation = self.path.rsplit("/", 1)[-1]
            if operation == "start":
                result = manager.start(payload)
            elif operation == "close":
                if set(payload) != {"session_token"}:
                    raise ValueError("Only the browser lease is accepted for close")
                result = manager.request({**payload, "action": "close"})
            else:
                result = manager.request(payload)
            response = {"status": "ok", "analysis": result}
            if len(json.dumps(response).encode()) > MAX_RESPONSE_BYTES:
                token = result.get("session_token") or payload.get("session_token")
                if token:
                    manager.request({"session_token": token, "action": "close"})
                raise InvestigationError("Investigation response budget exceeded")
            raw_token = result.get("session_token") or payload.get("session_token")
            if operation == "start" and os.environ.get("URL_ANALYSIS_SESSION_OWNER"):
                result["session_token"] = (
                    os.environ["URL_ANALYSIS_SESSION_OWNER"] + "~" + raw_token
                )
            if not self._write_json(HTTPStatus.OK, response) and raw_token:
                manager.cancel(raw_token)
        except DestinationRefused as error:
            self._write_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "destination_refused",
                    "reason": error.reason,
                    "reason_code": error.reason_code,
                    "browser_start_unattempted": error.browser_start_unattempted,
                },
            )
        except InvestigationError as error:
            status = (
                HTTPStatus.SERVICE_UNAVAILABLE
                if error.code in {"capacity_busy", "action_pending"}
                else (
                    HTTPStatus.GATEWAY_TIMEOUT
                    if error.code.endswith("timeout")
                    else (
                        HTTPStatus.BAD_GATEWAY
                        if error.code == "worker_failed"
                        else HTTPStatus.BAD_REQUEST
                    )
                )
            )
            self._write_json(
                status,
                {
                    "error": error.code,
                    "message": str(error)[:500],
                    "retry_after_seconds": error.retry_after,
                    "cleanup": error.cleanup,
                    "browser_start_unattempted": error.browser_start_unattempted,
                },
            )
        except (ValueError, TypeError) as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_investigation", "message": str(error)[:500]},
            )
        except Exception as error:
            logger.error("investigation failed error_type=%s", type(error).__name__)
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "analysis_failed",
                    "message": "Investigation failed; retain earlier evidence and close the lease",
                },
            )

    def do_POST(self) -> None:
        if self.path in {
            "/v1/investigation/start",
            "/v1/investigation/step",
            "/v1/investigation/close",
        }:
            self.do_investigation()
            return
        if self.path not in {"/v1/analyze", "/v1/capture"}:
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
            if self.path == "/v1/capture" and isinstance(payload, dict):
                base = {
                    k: v
                    for k, v in payload.items()
                    if k not in {"profile", "wait_seconds"}
                }
                request = _validated_request(base)
                profile, delay = validate_options(payload)
                if request["ignore_https_errors"]:
                    raise InvalidBrokerRequest(
                        "Research capture requires TLS verification"
                    )
                request.update(profile=profile, wait_seconds=delay)
            else:
                request = _validated_request(payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": str(error)},
            )
            return

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                analysis = (
                    collect_case(request, playwright)
                    if self.path == "/v1/capture"
                    else analyze_destination(request, playwright)
                )
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
        response = {"status": "ok", "analysis": analysis}
        if len(json.dumps(response).encode()) > MAX_RESPONSE_BYTES:
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "analysis_failed",
                    "message": "capture exceeded response budget",
                },
            )
            return
        self._write_json(HTTPStatus.OK, response)

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
        if hasattr(server, "investigation_manager"):
            server.investigation_manager.close_all()
        server.server_close()


if __name__ == "__main__":
    main()

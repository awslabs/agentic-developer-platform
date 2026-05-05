"""
Thin wrapper around AWS Bedrock AgentCore Browser SDK.

Handles session lifecycle: create -> invoke (multiple) -> stop.
Includes retry logic with exponential backoff for transient errors.
Session cleanup is guaranteed via context manager / explicit stop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Retryable error codes from AgentCore Browser
RETRYABLE_ERRORS = (
    "ThrottlingException",
    "ServiceUnavailableException",
    "InternalServerException",
)

DEFAULT_SESSION_TIMEOUT = 300  # seconds
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds


@dataclass
class BrowserEvidence:
    """Evidence captured from a browser session."""

    screenshot_pre_scroll: bytes | None = None
    screenshot_post_scroll: bytes | None = None
    dom_snapshot: str = ""
    har_data: dict[str, Any] = field(default_factory=dict)
    network_requests: list[dict[str, Any]] = field(default_factory=list)
    redirect_chain: list[str] = field(default_factory=list)
    final_url: str = ""
    http_status: int = 0
    page_title: str = ""
    forms_detected: list[dict[str, Any]] = field(default_factory=list)
    auto_downloads: list[dict[str, Any]] = field(default_factory=list)
    anti_analysis_signals: list[str] = field(default_factory=list)
    javascript_sources: list[str] = field(default_factory=list)
    visible_text: str = ""
    error: str | None = None


@dataclass
class SessionInfo:
    """AgentCore Browser session metadata."""

    session_id: str
    live_view_url: str = ""
    status: str = "CREATED"


class AgentCoreBrowserClient:
    """
    Client for AWS Bedrock AgentCore Browser.

    Usage:
        client = AgentCoreBrowserClient(region="us-east-1")
        try:
            session = client.create_session()
            evidence = client.navigate_and_capture(session.session_id, url)
        finally:
            client.stop_session(session.session_id)
    """

    def __init__(
        self,
        region: str = "us-east-1",
        session_timeout: int = DEFAULT_SESSION_TIMEOUT,
        boto_client: Any | None = None,
    ):
        self._region = region
        self._session_timeout = session_timeout
        # Allow injection for testing
        self._client = boto_client or boto3.client(
            "bedrock-agentcore", region_name=region
        )
        self._active_session_id: str | None = None

    def _retry_call(self, method_name: str, **kwargs: Any) -> dict[str, Any]:
        """Call an AgentCore API method with retry logic for transient errors."""
        method = getattr(self._client, method_name)
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                return method(**kwargs)
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in RETRYABLE_ERRORS and attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE ** (attempt + 1)
                    logger.warning(
                        "AgentCore %s attempt %d failed (%s), retrying in %ds",
                        method_name,
                        attempt + 1,
                        error_code,
                        wait,
                    )
                    time.sleep(wait)
                    last_error = e
                else:
                    raise
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE ** (attempt + 1)
                    logger.warning(
                        "AgentCore %s attempt %d failed (%s), retrying in %ds",
                        method_name,
                        attempt + 1,
                        type(e).__name__,
                        wait,
                    )
                    time.sleep(wait)
                    last_error = e
                else:
                    raise

        # Should not reach here, but just in case
        raise last_error  # type: ignore[misc]

    def create_session(self) -> SessionInfo:
        """
        Create an ephemeral AgentCore Browser session.

        Returns SessionInfo with session_id and live_view_url.
        Raises on failure after retries.
        """
        response = self._retry_call(
            "start_browser_session",
            browserIdentifier="aws.browser.v1",
            sessionTimeoutSeconds=self._session_timeout,
            browserSettings={
                "headless": True,
                "persistentProfile": False,
            },
        )

        session_id = response["sessionId"]
        live_view_url = response.get("liveViewUrl", "")
        self._active_session_id = session_id

        logger.info("Created AgentCore Browser session: %s", session_id)
        return SessionInfo(
            session_id=session_id,
            live_view_url=live_view_url,
            status="CREATED",
        )

    def navigate_and_capture(self, session_id: str, url: str) -> BrowserEvidence:
        """
        Navigate to a URL and capture forensic evidence.

        Invokes the browser to visit the URL, then captures screenshots,
        DOM, network activity, and page structure.
        """
        evidence = BrowserEvidence()

        try:
            # Step 1: Navigate to URL
            nav_response = self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="navigate",
                parameters={"url": url},
            )

            evidence.final_url = nav_response.get("currentUrl", url)
            evidence.http_status = nav_response.get("httpStatus", 0)
            evidence.redirect_chain = nav_response.get("redirectChain", [])
            evidence.page_title = nav_response.get("pageTitle", "")

            # Step 2: Capture pre-scroll screenshot
            screenshot_resp = self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="screenshot",
                parameters={"fullPage": False},
            )
            evidence.screenshot_pre_scroll = screenshot_resp.get("data")

            # Step 3: Scroll and capture post-scroll screenshot
            self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="evaluate",
                parameters={
                    "expression": "window.scrollTo(0, document.body.scrollHeight)"
                },
            )

            screenshot_resp = self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="screenshot",
                parameters={"fullPage": True},
            )
            evidence.screenshot_post_scroll = screenshot_resp.get("data")

            # Step 4: Extract DOM
            dom_resp = self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="evaluate",
                parameters={"expression": "document.documentElement.outerHTML"},
            )
            evidence.dom_snapshot = dom_resp.get("result", "")[:5_000_000]  # 5MB cap

            # Step 5: Extract network requests from HAR
            har_resp = self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="getHar",
                parameters={},
            )
            evidence.har_data = har_resp.get("har", {})
            evidence.network_requests = self._parse_har_entries(evidence.har_data)

            # Step 6: Detect forms
            forms_resp = self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="evaluate",
                parameters={
                    "expression": """
                    JSON.stringify(Array.from(document.querySelectorAll('form')).map(f => ({
                        action: f.action,
                        method: f.method,
                        fields: Array.from(f.querySelectorAll('input')).map(i => ({
                            name: i.name, type: i.type, hidden: i.type === 'hidden'
                        }))
                    })))
                    """
                },
            )
            try:
                import json

                forms_raw = json.loads(forms_resp.get("result", "[]"))
                evidence.forms_detected = forms_raw
            except (json.JSONDecodeError, TypeError):
                pass

            # Step 7: Detect anti-analysis signals
            anti_resp = self._retry_call(
                "invoke_browser",
                sessionId=session_id,
                action="evaluate",
                parameters={
                    "expression": """
                    JSON.stringify({
                        navigatorWebdriver: navigator.webdriver,
                        userAgent: navigator.userAgent,
                        plugins: navigator.plugins.length,
                        languages: navigator.languages
                    })
                    """
                },
            )
            try:
                import json

                anti_data = json.loads(anti_resp.get("result", "{}"))
                if anti_data.get("navigatorWebdriver"):
                    evidence.anti_analysis_signals.append("webdriver_detected")
                if anti_data.get("plugins", 0) == 0:
                    evidence.anti_analysis_signals.append("no_plugins")
            except (json.JSONDecodeError, TypeError):
                pass

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            evidence.error = f"AgentCore Browser error: {error_code} - {e}"
            logger.error("Browser navigation failed: %s", e)
        except Exception as e:
            evidence.error = f"Unexpected error during capture: {type(e).__name__}: {e}"
            logger.error("Browser navigation failed unexpectedly: %s", e)

        return evidence

    def get_session_info(self, session_id: str) -> dict[str, Any]:
        """Get current session status and metadata."""
        return self._retry_call(
            "get_browser_session",
            sessionId=session_id,
        )

    def stop_session(self, session_id: str) -> bool:
        """
        Explicitly terminate a browser session.

        Returns True if session was stopped, False if it was already gone.
        Always safe to call — never raises on "session not found".
        """
        try:
            self._retry_call(
                "stop_browser_session",
                sessionId=session_id,
            )
            logger.info("Stopped AgentCore Browser session: %s", session_id)
            if self._active_session_id == session_id:
                self._active_session_id = None
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("ResourceNotFoundException", "ValidationException"):
                logger.info("Session %s already terminated", session_id)
                return False
            raise

    @staticmethod
    def _parse_har_entries(har: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract network request summaries from HAR data."""
        entries = []
        for entry in har.get("log", {}).get("entries", []):
            request = entry.get("request", {})
            response = entry.get("response", {})
            entries.append(
                {
                    "url": request.get("url", ""),
                    "method": request.get("method", "GET"),
                    "status": response.get("status", 0),
                    "mime": response.get("content", {}).get("mimeType", ""),
                    "size": response.get("content", {}).get("size", 0),
                }
            )
        return entries

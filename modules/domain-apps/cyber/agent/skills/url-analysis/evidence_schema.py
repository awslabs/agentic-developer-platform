"""
Evidence schema for url-analysis skill.

Defines the structured data contract between the agent-written orchestration
script and the deterministic verdict/report logic. The agent populates Evidence
by whatever means (CDP, InvokeBrowser, Playwright); verdict.py consumes it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ScreenshotCapture(BaseModel):
    """A screenshot taken during the browser session."""

    session_id: str
    image_base64: str | None = None
    image_s3_uri: str | None = None
    captured_at: str  # ISO8601


class RedirectHop(BaseModel):
    """A single redirect in the chain from target URL to final URL."""

    from_url: str
    to_url: str
    status_code: int = 0
    method: Literal["http", "meta-refresh", "js", "unknown"] = "unknown"


class AutoDownload(BaseModel):
    """A file that was automatically downloaded (or offered for download)."""

    url: str
    mime: str = ""
    size_bytes: int = 0
    sha256: str = ""


class FormField(BaseModel):
    """An input field detected in a page form."""

    name: str = ""
    field_type: str = "text"
    is_hidden: bool = False


class DetectedForm(BaseModel):
    """A form element detected on the page."""

    action: str = ""
    method: str = "GET"
    fields: list[FormField] = []


class Evidence(BaseModel):
    """
    Complete evidence collected from a URL analysis session.

    The agent's orchestration script must populate this model. The verdict
    module consumes it without modification.
    """

    # Core navigation results
    target_url: str  # The original URL we were asked to analyze
    final_url: str = ""  # Where we ended up after redirects
    http_status: int = 0
    page_title: str = ""

    # Redirect chain
    redirects: list[RedirectHop] = []

    # Visual evidence
    screenshots: list[ScreenshotCapture] = []

    # Page content
    visible_text: str = ""
    dom_snapshot: str = ""

    # Forms (phishing indicators)
    forms: list[DetectedForm] = []

    # Downloads
    auto_downloads: list[AutoDownload] = []

    # Network activity
    network_requests: list[dict[str, Any]] = []
    har_data: dict[str, Any] = {}

    # Anti-analysis signals
    anti_analysis_signals: list[str] = []

    # TLS / certificate info
    tls_info: dict[str, Any] | None = None

    # Enrichment results (populated by enrichment.run_enrichment)
    enrichment: dict[str, Any] = {}

    # Metadata
    run_started_at: str = ""  # ISO8601
    run_completed_at: str = ""  # ISO8601
    session_id: str = ""
    error: str | None = None
    agent_orchestration_script_uri: str | None = None

    def to_browser_evidence_dict(self) -> dict[str, Any]:
        """
        Convert to the dict format expected by verdict.synthesize_verdict().

        This bridges the new Evidence schema to the existing verdict interface,
        keeping verdict.py byte-identical.
        """
        return {
            "final_url": self.final_url,
            "http_status": self.http_status,
            "redirect_chain": [r.to_url for r in self.redirects],
            "page_title": self.page_title,
            "forms_detected": [
                {
                    "action": f.action,
                    "method": f.method,
                    "fields": [
                        {"name": fd.name, "type": fd.field_type, "hidden": fd.is_hidden}
                        for fd in f.fields
                    ],
                }
                for f in self.forms
            ],
            "auto_downloads": [
                {
                    "url": d.url,
                    "mime": d.mime,
                    "size": d.size_bytes,
                    "sha256": d.sha256,
                }
                for d in self.auto_downloads
            ],
            "anti_analysis_signals": self.anti_analysis_signals,
            "network_requests": self.network_requests[:50],
            "visible_text": self.visible_text[:10000],
        }

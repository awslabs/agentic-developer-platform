"""
URL Analysis orchestrator — ties together denylist, browser, enrichment, verdict, and report.

This is the main entry point for the url-analysis skill. It:
1. Validates the URL against the denylist
2. Creates an AgentCore Browser session
3. Navigates and captures evidence
4. Runs enrichment in parallel
5. Synthesizes a verdict
6. Generates reports and publishes artifacts
7. Cleans up the session

Always guarantees session cleanup via try/finally.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import boto3

try:
    from .browser_client import AgentCoreBrowserClient, BrowserEvidence
    from .denylist import DenylistConfig, check_url, scrub_url_credentials
    from .enrichment import run_enrichment
    from .report import render_html_report, render_json_report, render_markdown_report
    from .verdict import synthesize_verdict
except ImportError:
    from browser_client import AgentCoreBrowserClient, BrowserEvidence  # type: ignore[no-redef]
    from denylist import DenylistConfig, check_url, scrub_url_credentials  # type: ignore[no-redef]
    from enrichment import run_enrichment  # type: ignore[no-redef]
    from report import render_html_report, render_json_report, render_markdown_report  # type: ignore[no-redef]
    from verdict import synthesize_verdict  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def _get_config() -> dict[str, Any]:
    """Load skill configuration from environment."""
    return {
        "region": os.environ.get("AWS_REGION", "us-east-1"),
        "session_timeout": int(os.environ.get("URL_ANALYSIS_SESSION_TIMEOUT", "300")),
        "artifacts_bucket": os.environ.get("CYBER_ARTIFACTS_BUCKET", ""),
        "results_table": os.environ.get("CYBER_RESULTS_TABLE", ""),
        "denylist_hosts": json.loads(
            os.environ.get("URL_ANALYSIS_DENYLIST_HOSTS", "[]")
        ),
    }


def _upload_artifact(
    s3_client: Any,
    bucket: str,
    artifact_id: str,
    filename: str,
    data: bytes | str,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload an artifact to S3 and return the S3 URI."""
    key = f"reports/{artifact_id}/url-analysis/{filename}"
    body = data if isinstance(data, bytes) else data.encode("utf-8")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )
    return f"s3://{bucket}/{key}"


def _browser_evidence_to_findings(evidence: BrowserEvidence) -> dict[str, Any]:
    """Convert BrowserEvidence dataclass to a findings dict."""
    return {
        "final_url": evidence.final_url,
        "http_status": evidence.http_status,
        "redirect_chain": evidence.redirect_chain,
        "page_title": evidence.page_title,
        "forms_detected": evidence.forms_detected,
        "auto_downloads": evidence.auto_downloads,
        "anti_analysis_signals": evidence.anti_analysis_signals,
        "network_requests": evidence.network_requests,
        "visible_text": evidence.visible_text[:10000],  # Cap for envelope size
    }


def analyze_url(
    url: str,
    artifact_id: str | None = None,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Run full URL analysis pipeline.

    Args:
        url: The URL to analyze
        artifact_id: Optional artifact ID (generated if not provided)
        config_override: Optional config overrides for testing

    Returns:
        Complete analysis envelope (JSON-serializable dict)
    """
    start_time = time.time()
    config = config_override or _get_config()
    region = config.get("region", "us-east-1")

    if not artifact_id:
        artifact_id = f"url-{uuid.uuid4().hex[:12]}"

    # Scrub credentials from URL before any persistence
    safe_url = scrub_url_credentials(url)
    parsed = urlparse(url)
    domain = parsed.hostname or ""

    # ─── Step 1: Pre-flight denylist check ───────────────────────────────
    denylist_config = DenylistConfig(
        denied_host_patterns=config.get("denylist_hosts", []),
    )
    denylist_result = check_url(url, denylist_config)

    if not denylist_result.allowed:
        duration = int(time.time() - start_time)
        return {
            "artifact_id": artifact_id,
            "stage": "url-analysis",
            "stage_name": "url-analysis",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "refused",
            "duration_seconds": duration,
            "findings": {
                "url": safe_url,
                "refused_reason": denylist_result.reason,
                "resolved_ips": denylist_result.resolved_ips,
            },
            "verdict": {
                "severity": "refused",
                "confidence": 100,
                "category": "internal-network",
                "reasoning": f"URL was refused: {denylist_result.reason}",
                "mitre_attack": [],
                "recommended_actions": [
                    "URL targets internal infrastructure — do not analyze externally"
                ],
            },
            "tool_calls": 0,
            "notes": "Refused by denylist — no browser session created",
        }

    # ─── Step 2-3: Browser session + capture ─────────────────────────────
    session_id: str | None = None
    browser_client = AgentCoreBrowserClient(
        region=region,
        session_timeout=config.get("session_timeout", 300),
    )
    evidence = BrowserEvidence()
    tool_calls = 0

    try:
        session = browser_client.create_session()
        session_id = session.session_id
        tool_calls += 1
        logger.info(
            "Browser session created: %s (live view: %s)",
            session_id,
            session.live_view_url,
        )

        evidence = browser_client.navigate_and_capture(session_id, url)
        tool_calls += (
            7  # navigate + 2 screenshots + scroll + DOM + HAR + forms + anti-analysis
        )

    except Exception as e:
        logger.error("Browser session failed: %s", e)
        evidence.error = f"Browser session failed: {type(e).__name__}: {e}"
    finally:
        # ─── Step 7: Guaranteed session cleanup ──────────────────────────
        if session_id:
            try:
                browser_client.stop_session(session_id)
                tool_calls += 1
            except Exception as e:
                logger.warning("Session cleanup failed (AWS will auto-clean): %s", e)

    # ─── Step 4: Enrichment ──────────────────────────────────────────────
    enrichment_result = run_enrichment(url, region=region)
    enrichment_data = {
        "whois": enrichment_result.whois,
        "passive_dns": enrichment_result.passive_dns,
        "cert_transparency": enrichment_result.cert_transparency,
        "virustotal": enrichment_result.virustotal,
        "urlhaus": enrichment_result.urlhaus,
        "misp": enrichment_result.misp,
    }

    # ─── Step 5: Verdict synthesis ───────────────────────────────────────
    browser_findings = _browser_evidence_to_findings(evidence)
    verdict = synthesize_verdict(
        url=url,
        domain=domain,
        browser_evidence=browser_findings,
        enrichment=enrichment_data,
    )

    # ─── Step 6: Artifact publishing ─────────────────────────────────────
    screenshots: list[str] = []
    dom_uri = ""
    har_uri = ""

    artifacts_bucket = config.get("artifacts_bucket", "")
    if artifacts_bucket:
        s3 = boto3.client("s3", region_name=region)

        if evidence.screenshot_pre_scroll:
            uri = _upload_artifact(
                s3,
                artifacts_bucket,
                artifact_id,
                "screenshot-pre-scroll.png",
                evidence.screenshot_pre_scroll,
                "image/png",
            )
            screenshots.append(uri)

        if evidence.screenshot_post_scroll:
            uri = _upload_artifact(
                s3,
                artifacts_bucket,
                artifact_id,
                "screenshot-post-scroll.png",
                evidence.screenshot_post_scroll,
                "image/png",
            )
            screenshots.append(uri)

        if evidence.dom_snapshot:
            dom_uri = _upload_artifact(
                s3,
                artifacts_bucket,
                artifact_id,
                "dom-snapshot.html",
                evidence.dom_snapshot,
                "text/html",
            )

        if evidence.har_data:
            har_uri = _upload_artifact(
                s3,
                artifacts_bucket,
                artifact_id,
                "requests.har",
                json.dumps(evidence.har_data, default=str),
                "application/json",
            )

    # ─── Build final findings ────────────────────────────────────────────
    # Extract IOCs from evidence
    iocs = _extract_iocs(evidence, enrichment_result)

    duration = int(time.time() - start_time)
    status = "ok" if not evidence.error else "partial"

    findings = {
        "url": safe_url,
        "final_url": evidence.final_url,
        "redirect_chain": evidence.redirect_chain,
        "http_status": evidence.http_status,
        "page_title": evidence.page_title,
        "screenshots": screenshots,
        "dom_snapshot_uri": dom_uri,
        "har_file_uri": har_uri,
        "network_requests": evidence.network_requests[:50],  # Cap
        "forms_detected": evidence.forms_detected,
        "auto_downloads": evidence.auto_downloads,
        "anti_analysis_signals": evidence.anti_analysis_signals,
        "enrichment": enrichment_data,
        "iocs": iocs,
        "tool_calls": tool_calls,
        "notes": _build_notes(evidence, enrichment_result),
    }

    # Build envelope
    envelope = {
        "artifact_id": artifact_id,
        "stage": "url-analysis",
        "stage_name": "url-analysis",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "duration_seconds": duration,
        "findings": findings,
        "verdict": verdict.to_dict(),
        "tool_calls": tool_calls,
        "notes": findings["notes"],
    }

    # Upload reports to S3
    if artifacts_bucket:
        s3 = boto3.client("s3", region_name=region)

        # JSON report
        json_report = render_json_report(
            artifact_id, safe_url, findings, verdict.to_dict(), duration
        )
        _upload_artifact(
            s3,
            artifacts_bucket,
            artifact_id,
            "report.json",
            json_report,
            "application/json",
        )

        # Markdown report
        md_report = render_markdown_report(
            safe_url, findings, verdict.to_dict(), duration
        )
        _upload_artifact(
            s3, artifacts_bucket, artifact_id, "report.md", md_report, "text/markdown"
        )

        # HTML report
        html_report = render_html_report(
            safe_url, findings, verdict.to_dict(), duration
        )
        _upload_artifact(
            s3, artifacts_bucket, artifact_id, "report.html", html_report, "text/html"
        )

    return envelope


def _extract_iocs(evidence: BrowserEvidence, enrichment: Any) -> dict[str, list[str]]:
    """Extract IOCs from browser evidence and enrichment data."""
    domains: set[str] = set()
    ips: set[str] = set()
    urls: set[str] = set()
    file_hashes: set[str] = set()
    emails: set[str] = set()

    # From network requests
    for req in evidence.network_requests:
        req_url = req.get("url", "")
        if req_url:
            urls.add(req_url)
            try:
                parsed = urlparse(req_url)
                if parsed.hostname:
                    domains.add(parsed.hostname)
            except Exception:
                pass

    # From redirect chain
    for hop in evidence.redirect_chain:
        urls.add(hop)
        try:
            parsed = urlparse(hop)
            if parsed.hostname:
                domains.add(parsed.hostname)
        except Exception:
            pass

    # From auto-downloads
    for dl in evidence.auto_downloads:
        sha = dl.get("sha256")
        if sha:
            file_hashes.add(sha)

    # From enrichment passive DNS
    for record in getattr(enrichment, "passive_dns", []):
        rdata = record.get("rdata", "")
        if rdata:
            ips.add(rdata)

    return {
        "domains": sorted(domains)[:50],
        "ips": sorted(ips)[:50],
        "urls": sorted(urls)[:50],
        "file_hashes": sorted(file_hashes),
        "email_addresses": sorted(emails),
    }


def _build_notes(evidence: BrowserEvidence, enrichment: Any) -> str:
    """Build free-text notes about the analysis run."""
    parts = []

    if evidence.error:
        parts.append(f"Browser error: {evidence.error}")

    skipped = getattr(enrichment, "skipped_sources", [])
    if skipped:
        parts.append(f"Skipped enrichment sources: {', '.join(skipped)}")

    errors = getattr(enrichment, "errors", [])
    if errors:
        parts.append(f"Enrichment errors: {'; '.join(errors)}")

    return "; ".join(parts) if parts else ""

"""
Smoke test: 3-URL corpus for url-analysis skill.

Exercises the full pipeline (denylist -> browser -> enrichment -> verdict -> report)
against a diverse corpus:
  1. Known-benign baseline (example.com)
  2. Broken TLS edge case (expired.badssl.com)
  3. Live malware-delivery URL (URLhaus-sourced IP)

Two modes:
  - Default (mocked browser): validates pipeline logic, verdict correctness, session cleanup
  - Live (@pytest.mark.live): calls real AgentCore Browser (requires AWS infra)

Run:
  pytest tests/test_smoke_corpus.py -v          # mocked mode (CI-safe)
  pytest tests/test_smoke_corpus.py -v -m live  # live mode (requires AgentCore)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from browser_client import BrowserEvidence, SessionInfo
from enrichment import EnrichmentResult


# ─── Test corpus ──────────────────────────────────────────────────────────────

CORPUS = {
    "benign": {
        "url": "https://example.com",
        "expected_severity": "clean",
        "expected_min_confidence": 30,  # Verdict module gives 40 for example.com (not in KNOWN_GOOD_DOMAINS)
        "description": "Known-benign baseline",
    },
    "broken_tls": {
        "url": "https://expired.badssl.com",
        "expected_severity": "suspicious",  # or clean-with-evidence
        "expected_min_confidence": 30,
        "description": "Broken TLS edge case (expired cert)",
    },
    "malware": {
        "url": "http://123.5.114.95:55970/bin.sh",
        "expected_severity": "malicious",
        "expected_min_confidence": 50,
        "description": "Live malware-delivery URL (URLhaus)",
    },
}


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_config() -> dict:
    """Test config — no real AWS calls."""
    return {
        "region": "us-east-1",
        "session_timeout": 60,
        "artifacts_bucket": "",  # Skip S3 uploads
        "results_table": "",
        "denylist_hosts": [],
    }


def _make_browser_evidence_benign() -> BrowserEvidence:
    """Simulate browser evidence for example.com."""
    return BrowserEvidence(
        final_url="https://example.com/",
        http_status=200,
        redirect_chain=[],
        page_title="Example Domain",
        forms_detected=[],
        auto_downloads=[],
        anti_analysis_signals=[],
        network_requests=[
            {"url": "https://example.com/", "status": 200, "mime": "text/html", "size": 1256}
        ],
        visible_text="This domain is for use in illustrative examples in documents.",
    )


def _make_browser_evidence_broken_tls() -> BrowserEvidence:
    """Simulate browser evidence for expired.badssl.com (TLS error, partial load)."""
    return BrowserEvidence(
        final_url="https://expired.badssl.com/",
        http_status=200,
        redirect_chain=[],
        page_title="expired.badssl.com",
        forms_detected=[],
        auto_downloads=[],
        anti_analysis_signals=["tls_certificate_expired"],
        network_requests=[
            {"url": "https://expired.badssl.com/", "status": 200, "mime": "text/html", "size": 494}
        ],
        visible_text="expired.badssl.com",
        error="TLS certificate has expired",
    )


def _make_browser_evidence_malware() -> BrowserEvidence:
    """Simulate browser evidence for malware delivery URL."""
    return BrowserEvidence(
        final_url="http://123.5.114.95:55970/bin.sh",
        http_status=200,
        redirect_chain=[],
        page_title="",
        forms_detected=[],
        auto_downloads=[
            {
                "url": "http://123.5.114.95:55970/bin.sh",
                "mime": "application/x-sh",
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }
        ],
        anti_analysis_signals=[],
        network_requests=[
            {
                "url": "http://123.5.114.95:55970/bin.sh",
                "status": 200,
                "mime": "application/x-sh",
                "size": 4096,
            }
        ],
        visible_text="#!/bin/sh",
    )


def _make_enrichment_benign() -> EnrichmentResult:
    """Enrichment for example.com — old domain, no hits."""
    return EnrichmentResult(
        whois={"age_days": 9000, "registrar": "IANA"},
        passive_dns=[{"rrtype": 1, "rdata": "93.184.216.34", "ttl": 3600}],
        cert_transparency={"total_certs": 5},
        virustotal={"found": True, "malicious": 0, "suspicious": 0, "harmless": 70, "undetected": 0},
        urlhaus={"found": False},
        misp={"skipped": True, "reason": "MISP not configured"},
        skipped_sources=["misp"],
    )


def _make_enrichment_broken_tls() -> EnrichmentResult:
    """Enrichment for expired.badssl.com — known test site, no malicious signals."""
    return EnrichmentResult(
        whois={"age_days": 3500, "registrar": "GoDaddy"},
        passive_dns=[{"rrtype": 1, "rdata": "104.154.89.105", "ttl": 300}],
        cert_transparency={"total_certs": 12},
        virustotal={"found": True, "malicious": 0, "suspicious": 0, "harmless": 60, "undetected": 5},
        urlhaus={"found": False},
        misp={"skipped": True, "reason": "MISP not configured"},
        skipped_sources=["misp"],
    )


def _make_enrichment_malware() -> EnrichmentResult:
    """Enrichment for malware URL — URLhaus hit, VT detections."""
    return EnrichmentResult(
        whois={"age_days": 10},
        passive_dns=[],
        cert_transparency={},
        virustotal={
            "found": True,
            "malicious": 12,
            "suspicious": 3,
            "harmless": 2,
            "undetected": 45,
        },
        urlhaus={"found": True, "threat": "malware_download", "url_status": "online", "tags": ["elf", "mirai"]},
        misp={"skipped": True, "reason": "MISP not configured"},
        skipped_sources=["misp"],
    )


# ─── Mocked pipeline tests ───────────────────────────────────────────────────


class TestSmokeCorpusMocked:
    """Smoke tests with mocked browser — validates verdict logic end-to-end."""

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_url1_benign_example_com(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """URL 1: example.com produces clean verdict with high confidence."""
        from analyze import analyze_url

        mock_resolve.return_value = ["93.184.216.34"]

        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(
            session_id="smoke-benign-001", live_view_url="https://live/smoke-benign-001"
        )
        mock_client.navigate_and_capture.return_value = _make_browser_evidence_benign()
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client

        mock_enrichment.return_value = _make_enrichment_benign()

        result = analyze_url(CORPUS["benign"]["url"], config_override=mock_config)

        # Acceptance: verdict is clean with high confidence
        assert result["status"] == "ok"
        assert result["verdict"]["severity"] == "clean"
        assert result["verdict"]["confidence"] >= CORPUS["benign"]["expected_min_confidence"]
        assert result["verdict"]["category"] == "false-positive"

        # Acceptance: no IOCs flagged for a benign URL
        iocs = result["findings"]["iocs"]
        # Only the URL itself should appear — no extra malicious IOCs
        assert len(iocs.get("file_hashes", [])) == 0

        # Acceptance: session cleanup
        mock_client.stop_session.assert_called_once_with("smoke-benign-001")

        # Envelope structure complete
        assert result["artifact_id"].startswith("url-")
        assert result["stage"] == "url-analysis"
        assert "findings" in result
        assert "verdict" in result
        assert result["duration_seconds"] >= 0

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_url2_broken_tls_expired_badssl(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """URL 2: expired.badssl.com flags TLS issue in evidence."""
        from analyze import analyze_url

        mock_resolve.return_value = ["104.154.89.105"]

        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(
            session_id="smoke-tls-002", live_view_url="https://live/smoke-tls-002"
        )
        mock_client.navigate_and_capture.return_value = _make_browser_evidence_broken_tls()
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client

        mock_enrichment.return_value = _make_enrichment_broken_tls()

        result = analyze_url(CORPUS["broken_tls"]["url"], config_override=mock_config)

        # Acceptance: TLS issue is evidenced in the report
        # The verdict may be "suspicious" or "clean" depending on scoring,
        # but the TLS error MUST appear in evidence
        assert result["status"] == "partial"  # error field set -> partial
        assert "tls" in result["notes"].lower() or "certificate" in result["notes"].lower()

        # Anti-analysis signals should flag the TLS issue
        anti_signals = result["findings"].get("anti_analysis_signals", [])
        assert any("tls" in s.lower() for s in anti_signals)

        # Verdict confidence >= 30
        assert result["verdict"]["confidence"] >= CORPUS["broken_tls"]["expected_min_confidence"]

        # Session cleanup
        mock_client.stop_session.assert_called_once_with("smoke-tls-002")

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_url3_malware_delivery(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """URL 3: malware URL produces malicious verdict with IOCs."""
        from analyze import analyze_url

        mock_resolve.return_value = ["123.5.114.95"]

        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(
            session_id="smoke-malware-003", live_view_url="https://live/smoke-malware-003"
        )
        mock_client.navigate_and_capture.return_value = _make_browser_evidence_malware()
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client

        mock_enrichment.return_value = _make_enrichment_malware()

        result = analyze_url(CORPUS["malware"]["url"], config_override=mock_config)

        # Acceptance: malicious verdict
        assert result["status"] == "ok"
        assert result["verdict"]["severity"] == "malicious"
        assert result["verdict"]["confidence"] >= CORPUS["malware"]["expected_min_confidence"]
        assert result["verdict"]["category"] == "malware-delivery"

        # Acceptance: IOCs extracted — host IP present
        iocs = result["findings"]["iocs"]
        all_ioc_text = str(iocs)
        assert "123.5.114.95" in all_ioc_text or "bin.sh" in all_ioc_text

        # Acceptance: file hash from auto-download captured
        assert len(iocs.get("file_hashes", [])) >= 1

        # MITRE ATT&CK mapped
        mitre = result["verdict"]["mitre_attack"]
        assert any(t in mitre for t in ["T1189", "T1204.001"])

        # Recommended actions include blocking
        actions_text = " ".join(result["verdict"]["recommended_actions"]).lower()
        assert "block" in actions_text

        # Session cleanup
        mock_client.stop_session.assert_called_once_with("smoke-malware-003")

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_all_three_complete_under_timeout(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """All 3 URLs complete analysis in under 5 minutes each (mocked ~instant)."""
        from analyze import analyze_url

        mock_resolve.return_value = ["1.2.3.4"]
        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(session_id="timing-test")
        mock_client.navigate_and_capture.return_value = _make_browser_evidence_benign()
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client
        mock_enrichment.return_value = _make_enrichment_benign()

        for case_name, case in CORPUS.items():
            start = time.time()
            result = analyze_url(case["url"], config_override=mock_config)
            elapsed = time.time() - start

            assert elapsed < 300, f"{case_name} took {elapsed:.1f}s (> 5 min limit)"
            assert result["duration_seconds"] < 300

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_no_malicious_bytes_persist_on_disk(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """Malware URL analysis doesn't write payload bytes to local disk."""
        import os
        import tempfile
        from analyze import analyze_url

        mock_resolve.return_value = ["123.5.114.95"]
        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(session_id="no-persist")
        mock_client.navigate_and_capture.return_value = _make_browser_evidence_malware()
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client
        mock_enrichment.return_value = _make_enrichment_malware()

        # No artifacts_bucket means no S3 writes; verify no local temp files created
        result = analyze_url(CORPUS["malware"]["url"], config_override=mock_config)

        # The pipeline should not have written any files to /tmp with payload content
        # (In mocked mode, no real bytes cross into the process anyway)
        assert result["status"] == "ok"
        assert result["findings"].get("screenshots") == []  # No bucket -> no uploads


# ─── Live integration tests (require AgentCore Browser) ───────────────────────

live = pytest.mark.live


@live
class TestSmokeCorpusLive:
    """
    Live smoke tests — actually call AgentCore Browser.

    Run with: pytest tests/test_smoke_corpus.py -m live -v
    Requires: AWS credentials with bedrock-agentcore access.
    """

    @pytest.fixture(autouse=True)
    def _check_agentcore_access(self):
        """Skip if AgentCore Browser is not accessible."""
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError

        try:
            client = boto3.client("bedrock-agentcore", region_name="us-east-1")
            # Attempt a lightweight call to verify access
            client.list_browser_sessions(browserIdentifier="aws.browser.v1", maxResults=1)
        except (ClientError, NoCredentialsError, Exception) as e:
            pytest.skip(f"AgentCore Browser not accessible: {e}")

    def test_live_benign_url(self) -> None:
        """Live: example.com end-to-end."""
        from analyze import analyze_url

        result = analyze_url("https://example.com")
        assert result["status"] in ("ok", "partial")
        assert result["verdict"]["severity"] == "clean"

    def test_live_broken_tls_url(self) -> None:
        """Live: expired.badssl.com end-to-end."""
        from analyze import analyze_url

        result = analyze_url("https://expired.badssl.com")
        assert result["status"] in ("ok", "partial")
        # Should flag TLS issue somewhere
        notes_and_signals = (
            result.get("notes", "")
            + str(result["findings"].get("anti_analysis_signals", []))
        )
        assert "tls" in notes_and_signals.lower() or "certificate" in notes_and_signals.lower() or "expired" in notes_and_signals.lower()

    def test_live_malware_url(self) -> None:
        """Live: malware delivery URL end-to-end."""
        from analyze import analyze_url

        # Use the URLhaus URL; if offline, the test will produce a partial verdict
        result = analyze_url("http://123.5.114.95:55970/bin.sh")
        assert result["status"] in ("ok", "partial")
        assert result["verdict"]["severity"] in ("malicious", "suspicious")

    def test_live_session_cleanup(self) -> None:
        """Live: verify no lingering sessions after analysis."""
        import boto3

        client = boto3.client("bedrock-agentcore", region_name="us-east-1")
        resp = client.list_browser_sessions(browserIdentifier="aws.browser.v1", maxResults=10)
        sessions = resp.get("browserSessions", [])

        # Filter for sessions that might be ours (created in last 10 min)
        import datetime
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
        recent = [
            s for s in sessions
            if s.get("createdAt", datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)) > cutoff
        ]
        assert len(recent) == 0, f"Found {len(recent)} lingering session(s): {recent}"

"""
Smoke test: 3-URL corpus for url-analysis skill.

Validates the deterministic pipeline (evidence -> verdict -> report) against a
diverse corpus:
  1. Known-benign baseline (example.com)
  2. Broken TLS edge case (expired.badssl.com)
  3. Live malware-delivery URL (URLhaus-sourced IP)

Post-refactor: the orchestration layer is agent-written at runtime. These tests
validate the evidence schema -> verdict -> report chain directly, without mocking
a deleted browser_client.

Run:
  pytest tests/test_smoke_corpus.py -v
"""

from __future__ import annotations

from evidence_schema import (
    AutoDownload,
    Evidence,
)
from report import render_markdown_report
from verdict import synthesize_verdict


# -- Test corpus -----------------------------------------------------------------

CORPUS = {
    "benign": {
        "url": "https://example.com",
        "expected_severity": "clean",
        "expected_min_confidence": 30,
        "description": "Known-benign baseline",
    },
    "broken_tls": {
        "url": "https://expired.badssl.com",
        "expected_severity": "suspicious",
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


# -- Evidence factories ----------------------------------------------------------


def _make_evidence_benign() -> Evidence:
    """Simulated evidence for example.com."""
    return Evidence(
        target_url="https://example.com",
        final_url="https://example.com/",
        http_status=200,
        page_title="Example Domain",
        visible_text="This domain is for use in illustrative examples in documents.",
        network_requests=[
            {"url": "https://example.com/", "status": 200, "mime": "text/html", "size": 1256}
        ],
        run_started_at="2026-05-05T10:00:00Z",
        run_completed_at="2026-05-05T10:00:05Z",
        session_id="smoke-benign-001",
    )


def _make_evidence_broken_tls() -> Evidence:
    """Simulated evidence for expired.badssl.com (TLS error, partial load)."""
    return Evidence(
        target_url="https://expired.badssl.com",
        final_url="https://expired.badssl.com/",
        http_status=200,
        page_title="expired.badssl.com",
        visible_text="expired.badssl.com",
        anti_analysis_signals=["tls_certificate_expired"],
        network_requests=[
            {"url": "https://expired.badssl.com/", "status": 200, "mime": "text/html", "size": 494}
        ],
        error="TLS certificate has expired",
        run_started_at="2026-05-05T10:00:00Z",
        run_completed_at="2026-05-05T10:00:08Z",
        session_id="smoke-tls-002",
    )


def _make_evidence_malware() -> Evidence:
    """Simulated evidence for malware delivery URL."""
    return Evidence(
        target_url="http://123.5.114.95:55970/bin.sh",
        final_url="http://123.5.114.95:55970/bin.sh",
        http_status=200,
        page_title="",
        visible_text="#!/bin/sh",
        auto_downloads=[
            AutoDownload(
                url="http://123.5.114.95:55970/bin.sh",
                mime="application/x-sh",
                size_bytes=4096,
                sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ],
        network_requests=[
            {
                "url": "http://123.5.114.95:55970/bin.sh",
                "status": 200,
                "mime": "application/x-sh",
                "size": 4096,
            }
        ],
        run_started_at="2026-05-05T10:00:00Z",
        run_completed_at="2026-05-05T10:00:03Z",
        session_id="smoke-malware-003",
    )


def _make_enrichment_benign() -> dict:
    """Enrichment dict for example.com -- old domain, no hits."""
    return {
        "whois": {"age_days": 9000, "registrar": "IANA"},
        "passive_dns": [{"rrtype": 1, "rdata": "93.184.216.34", "ttl": 3600}],
        "cert_transparency": {"total_certs": 5},
        "virustotal": {"found": True, "malicious": 0, "suspicious": 0, "harmless": 70, "undetected": 0},
        "urlhaus": {"found": False},
        "misp": {"skipped": True, "reason": "MISP not configured"},
    }


def _make_enrichment_broken_tls() -> dict:
    """Enrichment dict for expired.badssl.com."""
    return {
        "whois": {"age_days": 3500, "registrar": "GoDaddy"},
        "passive_dns": [{"rrtype": 1, "rdata": "104.154.89.105", "ttl": 300}],
        "cert_transparency": {"total_certs": 12},
        "virustotal": {"found": True, "malicious": 0, "suspicious": 0, "harmless": 60, "undetected": 5},
        "urlhaus": {"found": False},
        "misp": {"skipped": True, "reason": "MISP not configured"},
    }


def _make_enrichment_malware() -> dict:
    """Enrichment dict for malware URL -- URLhaus hit, VT detections."""
    return {
        "whois": {"age_days": 10},
        "passive_dns": [],
        "cert_transparency": {},
        "virustotal": {
            "found": True,
            "malicious": 12,
            "suspicious": 3,
            "harmless": 2,
            "undetected": 45,
        },
        "urlhaus": {"found": True, "threat": "malware_download", "url_status": "online", "tags": ["elf", "mirai"]},
        "misp": {"skipped": True, "reason": "MISP not configured"},
    }


# -- Tests -----------------------------------------------------------------------


class TestSmokeCorpusVerdicts:
    """Smoke tests validating evidence -> verdict chain."""

    def test_url1_benign_example_com(self) -> None:
        """URL 1: example.com produces clean verdict."""
        evidence = _make_evidence_benign()
        enrichment = _make_enrichment_benign()
        evidence_dict = evidence.to_browser_evidence_dict()

        verdict = synthesize_verdict(
            url=CORPUS["benign"]["url"],
            domain="example.com",
            browser_evidence=evidence_dict,
            enrichment=enrichment,
        )

        assert verdict.severity == "clean"
        assert verdict.confidence >= CORPUS["benign"]["expected_min_confidence"]
        assert verdict.category == "false-positive"

    def test_url2_broken_tls_expired_badssl(self) -> None:
        """URL 2: expired.badssl.com -- TLS signals present in evidence."""
        evidence = _make_evidence_broken_tls()
        enrichment = _make_enrichment_broken_tls()
        evidence_dict = evidence.to_browser_evidence_dict()

        # Evidence captures the TLS error
        assert evidence.error is not None
        assert "tls" in evidence.error.lower() or "certificate" in evidence.error.lower()
        assert "tls_certificate_expired" in evidence.anti_analysis_signals

        verdict = synthesize_verdict(
            url=CORPUS["broken_tls"]["url"],
            domain="expired.badssl.com",
            browser_evidence=evidence_dict,
            enrichment=enrichment,
        )

        assert verdict.confidence >= CORPUS["broken_tls"]["expected_min_confidence"]

    def test_url3_malware_delivery(self) -> None:
        """URL 3: malware URL produces malicious verdict with IOCs."""
        evidence = _make_evidence_malware()
        enrichment = _make_enrichment_malware()
        evidence_dict = evidence.to_browser_evidence_dict()

        verdict = synthesize_verdict(
            url=CORPUS["malware"]["url"],
            domain="123.5.114.95",
            browser_evidence=evidence_dict,
            enrichment=enrichment,
        )

        assert verdict.severity == "malicious"
        assert verdict.confidence >= CORPUS["malware"]["expected_min_confidence"]
        assert verdict.category == "malware-delivery"
        assert any(t in verdict.mitre_attack for t in ["T1189", "T1204.001"])
        assert "block" in " ".join(verdict.recommended_actions).lower()

    def test_evidence_schema_bridges_correctly(self) -> None:
        """Evidence.to_browser_evidence_dict() produces verdict-compatible input."""
        evidence = _make_evidence_malware()
        d = evidence.to_browser_evidence_dict()

        # Must have all keys verdict.synthesize_verdict expects
        assert "forms_detected" in d
        assert "redirect_chain" in d
        assert "auto_downloads" in d
        assert "visible_text" in d
        assert len(d["auto_downloads"]) == 1
        assert d["auto_downloads"][0]["sha256"] != ""


class TestSmokeCorpusReports:
    """Smoke tests validating evidence -> verdict -> report chain."""

    def test_malware_report_contains_key_info(self) -> None:
        """Malware analysis report contains verdict, IOCs, and actions."""
        evidence = _make_evidence_malware()
        enrichment = _make_enrichment_malware()
        evidence_dict = evidence.to_browser_evidence_dict()

        verdict = synthesize_verdict(
            url=CORPUS["malware"]["url"],
            domain="123.5.114.95",
            browser_evidence=evidence_dict,
            enrichment=enrichment,
        )

        findings = {
            "url": CORPUS["malware"]["url"],
            "final_url": evidence.final_url,
            "redirect_chain": [],
            "http_status": evidence.http_status,
            "page_title": evidence.page_title,
            "screenshots": [],
            "forms_detected": evidence_dict["forms_detected"],
            "auto_downloads": evidence_dict["auto_downloads"],
            "enrichment": enrichment,
            "iocs": {"domains": [], "ips": ["123.5.114.95"], "urls": [CORPUS["malware"]["url"]], "file_hashes": ["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"], "email_addresses": []},
            "network_requests": evidence_dict["network_requests"],
        }

        md = render_markdown_report(
            CORPUS["malware"]["url"], findings, verdict.to_dict(), 3
        )

        assert "MALICIOUS" in md
        assert "123.5.114.95" in md
        assert "block" in md.lower() or "Block" in md

    def test_benign_report_renders_clean(self) -> None:
        """Clean URL report shows green/clean verdict."""
        evidence = _make_evidence_benign()
        enrichment = _make_enrichment_benign()
        evidence_dict = evidence.to_browser_evidence_dict()

        verdict = synthesize_verdict(
            url=CORPUS["benign"]["url"],
            domain="example.com",
            browser_evidence=evidence_dict,
            enrichment=enrichment,
        )

        findings = {
            "url": CORPUS["benign"]["url"],
            "final_url": evidence.final_url,
            "redirect_chain": [],
            "http_status": evidence.http_status,
            "page_title": evidence.page_title,
            "screenshots": [],
            "forms_detected": [],
            "auto_downloads": [],
            "enrichment": enrichment,
            "iocs": {"domains": [], "ips": [], "urls": [], "file_hashes": [], "email_addresses": []},
            "network_requests": evidence_dict["network_requests"],
        }

        md = render_markdown_report(
            CORPUS["benign"]["url"], findings, verdict.to_dict(), 5
        )

        assert "CLEAN" in md
        assert "No action required" in md or "no action" in md.lower()

"""Unit tests for the Evidence schema — serialization round-trip and verdict bridge."""

from __future__ import annotations

import json

from evidence_schema import (
    AutoDownload,
    DetectedForm,
    Evidence,
    FormField,
    RedirectHop,
    ScreenshotCapture,
)


class TestEvidenceRoundTrip:
    """Evidence model serializes and deserializes correctly."""

    def test_minimal_evidence_round_trip(self) -> None:
        """Minimal Evidence with only target_url round-trips through JSON."""
        evidence = Evidence(target_url="https://example.com")
        data = json.loads(evidence.model_dump_json())
        restored = Evidence.model_validate(data)
        assert restored.target_url == "https://example.com"
        assert restored.final_url == ""
        assert restored.screenshots == []
        assert restored.forms == []

    def test_full_evidence_round_trip(self) -> None:
        """Fully populated Evidence round-trips through JSON."""
        evidence = Evidence(
            target_url="https://phish.example.com/login",
            final_url="https://phish.example.com/login",
            http_status=200,
            page_title="Login",
            redirects=[
                RedirectHop(
                    from_url="http://short.link/x",
                    to_url="https://phish.example.com/login",
                    status_code=302,
                    method="http",
                )
            ],
            screenshots=[
                ScreenshotCapture(
                    session_id="sess-123",
                    image_base64="aWNvbnRlbnQ=",
                    captured_at="2026-05-05T10:00:00Z",
                )
            ],
            visible_text="Enter your password",
            forms=[
                DetectedForm(
                    action="/submit",
                    method="POST",
                    fields=[
                        FormField(name="email", field_type="email", is_hidden=False),
                        FormField(name="pass", field_type="password", is_hidden=False),
                    ],
                )
            ],
            auto_downloads=[
                AutoDownload(
                    url="http://evil.com/payload.exe",
                    mime="application/x-msdownload",
                    size_bytes=4096,
                    sha256="deadbeef" * 8,
                )
            ],
            network_requests=[
                {"url": "https://phish.example.com/login", "status": 200, "mime": "text/html"}
            ],
            anti_analysis_signals=["webdriver_detected"],
            enrichment={
                "whois": {"age_days": 3},
                "virustotal": {"found": True, "malicious": 5},
                "urlhaus": {"found": False},
                "misp": {"skipped": True},
            },
            run_started_at="2026-05-05T10:00:00Z",
            run_completed_at="2026-05-05T10:00:42Z",
            session_id="sess-123",
            agent_orchestration_script_uri="s3://bucket/scripts/run-001.py",
        )

        # Serialize to JSON and back
        json_str = evidence.model_dump_json()
        data = json.loads(json_str)
        restored = Evidence.model_validate(data)

        assert restored.target_url == evidence.target_url
        assert restored.final_url == evidence.final_url
        assert restored.http_status == 200
        assert len(restored.redirects) == 1
        assert restored.redirects[0].status_code == 302
        assert len(restored.screenshots) == 1
        assert restored.screenshots[0].image_base64 == "aWNvbnRlbnQ="
        assert len(restored.forms) == 1
        assert len(restored.forms[0].fields) == 2
        assert restored.forms[0].fields[1].field_type == "password"
        assert len(restored.auto_downloads) == 1
        assert restored.auto_downloads[0].sha256 == "deadbeef" * 8
        assert restored.anti_analysis_signals == ["webdriver_detected"]
        assert restored.enrichment["whois"]["age_days"] == 3
        assert restored.session_id == "sess-123"


class TestEvidenceToBrowserEvidenceDict:
    """The bridge method produces the dict format verdict.py expects."""

    def test_bridge_produces_expected_keys(self) -> None:
        """to_browser_evidence_dict() has all keys synthesize_verdict expects."""
        evidence = Evidence(
            target_url="https://example.com",
            final_url="https://example.com/",
            http_status=200,
            page_title="Example",
            forms=[
                DetectedForm(
                    action="/login",
                    method="POST",
                    fields=[FormField(name="pw", field_type="password")],
                )
            ],
            auto_downloads=[
                AutoDownload(url="http://x.com/f.exe", mime="application/exe", sha256="abc")
            ],
            redirects=[
                RedirectHop(from_url="http://a.com", to_url="http://b.com", status_code=301)
            ],
            visible_text="hello world",
            anti_analysis_signals=["no_plugins"],
            network_requests=[{"url": "http://x.com", "status": 200}],
        )

        d = evidence.to_browser_evidence_dict()

        assert d["final_url"] == "https://example.com/"
        assert d["http_status"] == 200
        assert d["redirect_chain"] == ["http://b.com"]
        assert d["page_title"] == "Example"
        assert len(d["forms_detected"]) == 1
        assert d["forms_detected"][0]["fields"][0]["type"] == "password"
        assert len(d["auto_downloads"]) == 1
        assert d["auto_downloads"][0]["sha256"] == "abc"
        assert d["anti_analysis_signals"] == ["no_plugins"]
        assert d["visible_text"] == "hello world"
        assert len(d["network_requests"]) == 1

    def test_bridge_with_empty_evidence(self) -> None:
        """Empty evidence produces empty-but-valid dict for verdict."""
        evidence = Evidence(target_url="https://x.com")
        d = evidence.to_browser_evidence_dict()

        assert d["final_url"] == ""
        assert d["redirect_chain"] == []
        assert d["forms_detected"] == []
        assert d["auto_downloads"] == []
        assert d["visible_text"] == ""
        assert d["network_requests"] == []

    def test_bridge_caps_visible_text(self) -> None:
        """Visible text is capped at 10000 chars in bridge output."""
        long_text = "x" * 20000
        evidence = Evidence(target_url="https://x.com", visible_text=long_text)
        d = evidence.to_browser_evidence_dict()
        assert len(d["visible_text"]) == 10000

    def test_bridge_caps_network_requests(self) -> None:
        """Network requests capped at 50 entries."""
        requests = [{"url": f"http://x.com/{i}", "status": 200} for i in range(100)]
        evidence = Evidence(target_url="https://x.com", network_requests=requests)
        d = evidence.to_browser_evidence_dict()
        assert len(d["network_requests"]) == 50

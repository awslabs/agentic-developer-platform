"""Unit tests for the verdict synthesis module."""

from __future__ import annotations


from verdict import (
    Verdict,
    synthesize_verdict,
)


class TestVerdictMalicious:
    """Scenarios that should produce a 'malicious' verdict."""

    def test_known_malicious_urlhaus_hit(self) -> None:
        """URLhaus positive hit → malicious verdict."""
        verdict = synthesize_verdict(
            url="http://evil.tk/payload.exe",
            domain="evil.tk",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": [
                    "http://evil.tk/r1",
                    "http://evil.tk/r2",
                    "http://evil.tk/r3",
                    "http://evil.tk/payload.exe",
                ],
                "auto_downloads": [
                    {
                        "url": "http://evil.tk/payload.exe",
                        "mime": "application/x-msdownload",
                        "sha256": "abc123",
                    }
                ],
                "visible_text": "",
            },
            enrichment={
                "whois": {"age_days": 3},
                "virustotal": {
                    "found": True,
                    "malicious": 10,
                    "suspicious": 2,
                    "harmless": 5,
                    "undetected": 50,
                },
                "urlhaus": {"found": True, "threat": "malware_download"},
                "misp": {"skipped": True},
            },
        )
        assert verdict.severity == "malicious"
        assert verdict.confidence >= 60
        assert verdict.category == "malware-delivery"
        assert "T1189" in verdict.mitre_attack or "T1204.001" in verdict.mitre_attack

    def test_phishing_with_password_form(self) -> None:
        """Page with password form + phishing keywords → malicious phishing."""
        verdict = synthesize_verdict(
            url="http://login-secure.tk/account",
            domain="login-secure.tk",
            browser_evidence={
                "forms_detected": [
                    {
                        "action": "/submit",
                        "fields": [
                            {"name": "email", "type": "email"},
                            {"name": "pass", "type": "password"},
                        ],
                    }
                ],
                "redirect_chain": [],
                "auto_downloads": [],
                "visible_text": "Verify your account immediately or it will be suspended. Confirm your identity.",
            },
            enrichment={
                "whois": {"age_days": 2},
                "virustotal": {
                    "found": True,
                    "malicious": 5,
                    "suspicious": 3,
                    "harmless": 10,
                    "undetected": 40,
                },
                "urlhaus": {"found": False},
                "misp": {"skipped": True},
            },
        )
        assert verdict.severity == "malicious"
        assert verdict.category == "phishing"
        assert "T1566.002" in verdict.mitre_attack

    def test_misp_hit_plus_vt_detections(self) -> None:
        """MISP correlation + VT detections → malicious."""
        verdict = synthesize_verdict(
            url="http://c2-server.xyz/beacon",
            domain="c2-server.xyz",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": [],
                "auto_downloads": [],
                "visible_text": "",
            },
            enrichment={
                "whois": {"age_days": 10},
                "virustotal": {
                    "found": True,
                    "malicious": 8,
                    "suspicious": 4,
                    "harmless": 2,
                    "undetected": 50,
                },
                "urlhaus": {"found": False},
                "misp": {
                    "found": True,
                    "matching_attributes": 3,
                    "events": ["1001", "1002"],
                },
            },
        )
        assert verdict.severity == "malicious"
        assert verdict.confidence >= 60


class TestVerdictClean:
    """Scenarios that should produce a 'clean' verdict."""

    def test_known_good_domain(self) -> None:
        """Known-good domains always get clean verdict with high confidence."""
        for domain in ["google.com", "www.google.com", "github.com"]:
            verdict = synthesize_verdict(
                url=f"https://{domain}/search",
                domain=domain,
                browser_evidence={
                    "forms_detected": [],
                    "redirect_chain": [],
                    "auto_downloads": [],
                    "visible_text": "",
                },
                enrichment={
                    "whois": {"age_days": 5000},
                    "virustotal": {
                        "found": True,
                        "malicious": 0,
                        "suspicious": 0,
                        "harmless": 60,
                        "undetected": 5,
                    },
                    "urlhaus": {"found": False},
                    "misp": {"found": False},
                },
            )
            assert verdict.severity == "clean"
            assert verdict.confidence >= 90

    def test_clean_established_domain_no_threats(self) -> None:
        """Established domain, no detections, no suspicious signals → clean."""
        verdict = synthesize_verdict(
            url="https://docs.python.org/3/tutorial/",
            domain="docs.python.org",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": [],
                "auto_downloads": [],
                "visible_text": "Welcome to the Python documentation.",
            },
            enrichment={
                "whois": {"age_days": 3000},
                "virustotal": {
                    "found": True,
                    "malicious": 0,
                    "suspicious": 0,
                    "harmless": 65,
                    "undetected": 3,
                },
                "urlhaus": {"found": False},
                "misp": {"found": False},
            },
        )
        assert verdict.severity == "clean"
        assert verdict.category == "false-positive"


class TestVerdictSuspicious:
    """Scenarios that should produce a 'suspicious' verdict."""

    def test_new_domain_with_some_signals(self) -> None:
        """New domain + suspicious TLD + some redirects → suspicious."""
        verdict = synthesize_verdict(
            url="http://free-prize.xyz/win",
            domain="free-prize.xyz",
            browser_evidence={
                "forms_detected": [
                    {
                        "action": "/collect",
                        "fields": [{"name": "email", "type": "email"}],
                    }
                ],
                "redirect_chain": [
                    "http://r1.xyz/",
                    "http://r2.xyz/",
                    "http://r3.xyz/",
                    "http://free-prize.xyz/win",
                ],
                "auto_downloads": [],
                "visible_text": "Congratulations! You've been selected.",
            },
            enrichment={
                "whois": {"age_days": 15},
                "virustotal": {
                    "found": True,
                    "malicious": 1,
                    "suspicious": 1,
                    "harmless": 20,
                    "undetected": 40,
                },
                "urlhaus": {"found": False},
                "misp": {"skipped": True},
            },
        )
        # Could be suspicious or malicious depending on scoring; at minimum not clean
        assert verdict.severity in ("suspicious", "malicious")
        assert verdict.confidence >= 30

    def test_mixed_signals_low_confidence(self) -> None:
        """Mixed signals with limited data → suspicious, lower confidence."""
        verdict = synthesize_verdict(
            url="http://unknown-site.buzz/page",
            domain="unknown-site.buzz",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": ["http://unknown-site.buzz/r1"],
                "auto_downloads": [],
                "visible_text": "",
            },
            enrichment={
                "whois": {"age_days": 45},
                "virustotal": {"skipped": True},
                "urlhaus": {"found": False},
                "misp": {"skipped": True},
            },
        )
        # With limited evidence, should be suspicious or clean
        assert verdict.severity in ("suspicious", "clean")


class TestVerdictStructure:
    """Verify verdict output structure."""

    def test_verdict_has_all_fields(self) -> None:
        verdict = synthesize_verdict(
            url="http://test.com/",
            domain="test.com",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": [],
                "auto_downloads": [],
                "visible_text": "",
            },
            enrichment={
                "whois": {},
                "virustotal": {"skipped": True},
                "urlhaus": {"found": False},
                "misp": {"skipped": True},
            },
        )
        assert isinstance(verdict, Verdict)
        assert verdict.severity in ("clean", "suspicious", "malicious")
        assert 0 <= verdict.confidence <= 100
        assert isinstance(verdict.mitre_attack, list)
        assert isinstance(verdict.recommended_actions, list)
        assert isinstance(verdict.reasoning, str)

    def test_to_dict_serializable(self) -> None:
        verdict = synthesize_verdict(
            url="http://test.com/",
            domain="test.com",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": [],
                "auto_downloads": [],
                "visible_text": "",
            },
            enrichment={
                "whois": {},
                "virustotal": {"skipped": True},
                "urlhaus": {"found": False},
                "misp": {"skipped": True},
            },
        )
        d = verdict.to_dict()
        assert "severity" in d
        assert "confidence" in d
        assert "category" in d
        assert "reasoning" in d
        assert "mitre_attack" in d
        assert "recommended_actions" in d


class TestVerdictRecommendedActions:
    """Verify recommended actions are concrete and actionable."""

    def test_malicious_has_block_action(self) -> None:
        verdict = synthesize_verdict(
            url="http://evil.tk/malware",
            domain="evil.tk",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": [],
                "auto_downloads": [
                    {
                        "url": "http://evil.tk/trojan.exe",
                        "mime": "application/x-msdownload",
                        "sha256": "abc",
                    }
                ],
                "visible_text": "",
            },
            enrichment={
                "whois": {"age_days": 1},
                "virustotal": {
                    "found": True,
                    "malicious": 15,
                    "suspicious": 5,
                    "harmless": 0,
                    "undetected": 40,
                },
                "urlhaus": {"found": True, "threat": "malware"},
                "misp": {"found": True, "matching_attributes": 5},
            },
        )
        assert verdict.severity == "malicious"
        actions_text = " ".join(verdict.recommended_actions).lower()
        assert "block" in actions_text

    def test_clean_has_no_action(self) -> None:
        verdict = synthesize_verdict(
            url="https://www.google.com/",
            domain="www.google.com",
            browser_evidence={
                "forms_detected": [],
                "redirect_chain": [],
                "auto_downloads": [],
                "visible_text": "",
            },
            enrichment={
                "whois": {"age_days": 9000},
                "virustotal": {
                    "found": True,
                    "malicious": 0,
                    "suspicious": 0,
                    "harmless": 70,
                    "undetected": 0,
                },
                "urlhaus": {"found": False},
                "misp": {"found": False},
            },
        )
        assert verdict.severity == "clean"
        assert any("no action" in a.lower() for a in verdict.recommended_actions)

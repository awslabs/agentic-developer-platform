"""Researcher-visible regressions: evidence loss, false clearance and provenance."""

import base64
import json

import pytest

from case_contract import Assessment, SCHEMA_VERSION, content_digest, digest
from research_case import add_probe, assess_case, new_case, verify_case
from verdict import synthesize_verdict

URL = "https://research.example/path?campaign=private-value"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBv8AAAAASUVORK5CYII="
)


def bundle(url=URL, status="complete", text="A documentation page", **kwargs):
    o = {
        "id": "obs-001",
        "captured_at": "2026-09-23T09:00:00+00:00",
        "action": "initial",
        "profile": kwargs.get("profile", "desktop"),
        "subject_sha256": digest(url),
        "http_status": 200,
        "status": status,
        "visible_text": text,
        "page_title": "Documentation",
        "forms": [],
        "frames": [],
        "errors": [],
        "screenshot_base64": base64.b64encode(PNG).decode(),
        "screenshot_sha256": digest(PNG),
        "network_requests": [{"url": url, "status": 200}],
        "dom_snapshot": "<html><body>Documentation</body></html>",
    }
    o["content_sha256"] = content_digest(o)
    return {
        "schema_version": SCHEMA_VERSION,
        "subject_sha256": digest(url),
        "cleanup_status": "stopped",
        "observations": [o],
        "limitations": ["Single view"],
    }


def assessment(verdict="no_adverse_behavior_observed", ids=None):
    return {
        "verdict": verdict,
        "assessor": "researcher",
        "findings": [
            {
                "kind": "benign_context",
                "basis": "observation",
                "statement": "Documentation in this view",
                "evidence_ids": ids or ["obs-001"],
            }
        ],
    }


def test_case_retains_provenance_and_does_not_automatically_clear(tmp_path):
    out = tmp_path / "case"
    new_case(out, URL)
    case = add_probe(out, URL, capture=bundle)
    assert case["assessment"]["verdict"] == "inconclusive"
    assert (out / "obs-001.png").read_bytes() == PNG
    assert "screenshot_base64" not in (out / "case.json").read_text()
    assert "private-value" not in (out / "case.json").read_text()
    manifest = json.loads((out / "manifest.json").read_text())
    for name, info in manifest["files"].items():
        assert digest((out / name).read_bytes()) == info["sha256"]
    assert "obs-001" in (out / "indicators.csv").read_text()
    assert "unassessed" in (out / "indicators.csv").read_text()
    assessed = assess_case(out, assessment())
    assert assessed["assessment"]["verdict"] == "no_adverse_behavior_observed"


def test_failed_followup_keeps_old_evidence_and_invalidates_clearance(tmp_path):
    out = tmp_path / "case"
    new_case(out, URL)
    add_probe(out, URL, capture=bundle)
    assess_case(out, assessment())

    def unavailable(*args, **kwargs):
        raise ConnectionError("broker unavailable")

    case = add_probe(out, URL, capture=unavailable, reason="Test a fresh view")
    assert len(case["observations"]) == 1
    assert (out / "obs-001.png").exists()
    assert case["probes"][-1]["status"] == "failed"
    assert case["assessment"]["verdict"] == "inconclusive"
    with pytest.raises(ValueError, match="Incomplete probes"):
        assess_case(out, assessment())


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_incomplete_capture_cannot_be_cleared(tmp_path, status):
    out = tmp_path / "case"
    new_case(out, URL)
    add_probe(out, URL, capture=lambda *args, **kwargs: bundle(status=status))
    with pytest.raises(ValueError, match="Incomplete collection"):
        assess_case(out, assessment())


def test_unknown_evidence_and_unsupported_variation_are_rejected(tmp_path):
    out = tmp_path / "case"
    new_case(out, URL)
    add_probe(out, URL, capture=bundle)
    with pytest.raises(ValueError, match="unknown observation"):
        assess_case(out, assessment(ids=["invented"]))
    data = assessment("suspicious")
    data["findings"][0]["kind"] = "content_variation"
    with pytest.raises(ValueError, match="two observations"):
        assess_case(out, data)


def test_different_subject_and_excess_probes_are_rejected(tmp_path):
    out = tmp_path / "case"
    new_case(out, URL)
    with pytest.raises(ValueError, match="exact original"):
        add_probe(out, "https://other.example/", capture=bundle)
    for _ in range(4):
        add_probe(out, URL, capture=bundle)
    with pytest.raises(ValueError, match="budget exhausted"):
        add_probe(out, URL, capture=bundle)


def test_corrupt_screenshot_is_not_accepted_as_evidence(tmp_path):
    out = tmp_path / "case"
    new_case(out, URL)
    data = bundle()
    data["observations"][0]["screenshot_sha256"] = "bad"
    case = add_probe(out, URL, capture=lambda *args, **kwargs: data)
    assert case["probes"][0]["status"] == "failed"
    assert not (out / "obs-001.png").exists()


def test_rendered_case_does_not_execute_or_fetch_page_content(tmp_path):
    out = tmp_path / "case"
    new_case(out, URL)
    hostile = '<script>alert(1)</script><img src="https://attacker.example/x">'
    add_probe(out, URL, capture=lambda *args, **kwargs: bundle(text=hostile))
    report = (out / "report.html").read_text()
    assert "<script>" not in report
    assert '<img src="https://attacker.example' not in report
    assert "default-src" in report
    assert "&lt;script&gt;" in report


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"http_status": 0, "error": "timeout"},
        {
            "http_status": 200,
            "visible_text": "Checking",
            "collection_status": "partial",
        },
    ],
)
def test_legacy_scorer_never_clears_missing_or_partial_evidence(evidence):
    verdict = synthesize_verdict(URL, "research.example", evidence, {})
    assert verdict.severity == "inconclusive"
    assert verdict.confidence == 0
    assert "No action required" not in verdict.recommended_actions


def test_popular_domain_does_not_override_observed_risk():
    verdict = synthesize_verdict(
        "https://github.com/some/path",
        "github.com",
        {
            "http_status": 200,
            "visible_text": "Verify your account and confirm your identity",
            "forms_detected": [{"fields": [{"type": "password"}]}],
            "auto_downloads": [{"url": "https://payload.example/file"}],
        },
        {},
    )
    assert verdict.severity == "malicious"


def test_assessment_rejects_fabricated_fields():
    with pytest.raises(ValueError):
        Assessment.model_validate({**assessment(), "confidence": 99})


@pytest.mark.parametrize(
    "field,value",
    [
        ("screenshot_base64", ""),
        ("content_sha256", "bad"),
        ("dom_snapshot", ""),
        ("network_requests", []),
        ("errors", ["timeout"]),
        ("profile", '<img src="https://bad.test/">'),
    ],
)
def test_false_completeness_is_rejected(tmp_path, field, value):
    out = tmp_path / "case"
    new_case(out, URL)
    data = bundle()
    data["observations"][0][field] = value
    case = add_probe(out, URL, capture=lambda *a, **k: data)
    assert case["probes"][0]["status"] == "failed"
    assert case["assessment"]["verdict"] == "inconclusive"


def test_changed_artifact_prevents_assessment(tmp_path):
    out = tmp_path / "case"
    new_case(out, URL)
    add_probe(out, URL, capture=bundle)
    assert verify_case(out) >= 5
    (out / "obs-001.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        assess_case(out, assessment())


def test_refusal_reason_survives_in_case_without_secrets(tmp_path):
    from browser_guard import DestinationRefused
    from denylist import DenylistResult

    out = tmp_path / "case"
    new_case(out, URL)

    def refuse(*a, **k):
        raise DestinationRefused(
            URL,
            DenylistResult(
                allowed=False, reason="Blocked " + URL, reason_code="blocked_address"
            ),
        )

    case = add_probe(out, URL, capture=refuse)
    assert case["probes"][0]["reason_code"] == "blocked_address"
    assert "private-value" not in (out / "case.json").read_text()

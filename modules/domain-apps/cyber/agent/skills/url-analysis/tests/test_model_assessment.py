"""Model-owned assessments over synthetic archive/browser evidence, without AWS."""

import json
from urllib.parse import urlsplit

import domain_investigation as cli
import pytest
from browser_client import BrowserBrokerError
from browser_guard import DestinationRefused, PinnedResponse
from research_case import verify_case

from .test_domain_investigation import live_fixture as browser_fixture

live_fixture = browser_fixture


def prepare(output):
    cli.prepare(
        output,
        "https://public.test/seed",
        "Assess the available evidence",
        lookup_fn=lambda *a: {
            "source": "common_crawl_athena",
            "kind": "archive_index",
            "status": "available",
            "found": True,
            "verdict_effect": "model_assessed",
            "records": [{"url": "https://public.test/products", "fetch_status": 200}],
        },
    )
    cli.hypothesize(
        output,
        {
            "hypothesis": "Historical product paths suggest a site whose purpose can be examined live",
            "source_ids": ["corroboration-001"],
            "limitations": ["Index metadata does not reveal page content"],
            "next_question": "What does the current site present?",
        },
    )


def assessment(verdict="no_specific_concern"):
    return {
        "verdict": verdict,
        "assessor": "synthetic-model",
        "context_assessment": {
            "risk": "no_specific_concern",
            "findings": [
                {
                    "statement": "The archive index includes a product URL with HTTP 200.",
                    "basis": "reported",
                    "source_ids": ["corroboration-001"],
                }
            ],
            "limitations": ["Archive page bodies were not retrieved."],
        },
        "limitations": ["Limited synthetic evidence; no certification of safety."],
    }


@pytest.mark.parametrize(
    "verdict", ["no_specific_concern", "suspicious", "malicious", "inconclusive"]
)
def test_archive_and_browser_reach_final_assessment_without_verdict_veto(
    live_fixture, tmp_path, verdict
):
    request, _, clients = live_fixture
    output = tmp_path / "combined"
    prepare(output)
    case = cli.browse(output, request=request)
    value = assessment(verdict)
    value["findings"] = [
        {
            "kind": "other",
            "basis": "observation",
            "statement": "The captured landing page presents a support portal.",
            "evidence_ids": [case["observations"][0]["id"]],
        }
    ]
    # No separate final review call is needed to save the model's synthesis.
    result = cli.finish(
        output, value, "Model has assessed both sources", request=request
    )
    assert result["assessment"]["verdict"] == verdict
    assert result["assessment"]["context_assessment"]["findings"][0]["source_ids"] == [
        "corroboration-001"
    ]
    assert result["assessment"]["findings"][0]["evidence_ids"] == ["obs-001"]
    assert result["browser_cleanup"] == "stopped" and clients[0].stopped
    assert verify_case(output) > 0


def test_archive_assessment_survives_browser_startup_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_lease_path", lambda _: tmp_path / "private.json")
    output = tmp_path / "archive-only"
    prepare(output)

    def unavailable(*a):
        raise BrowserBrokerError("Synthetic browser startup timeout")

    with pytest.raises(BrowserBrokerError):
        cli.browse(output, request=unavailable)
    result = cli.finish(
        output,
        assessment(),
        "Historical assessment; live browser unavailable",
        request=unavailable,
    )
    assert result["assessment"]["verdict"] == "no_specific_concern"
    assert not result["observations"] and result["browser_cleanup"] == "unknown"
    for name in ("report.md", "report.html"):
        text = (output / name).read_text()
        assert "no_specific_concern" in text and "Browser cleanup: unknown" in text
        assert "corroboration-001" in text


def test_cleanup_failure_preserves_verdict_and_capture(live_fixture, tmp_path):
    request, _, _ = live_fixture
    output = tmp_path / "cleanup"
    prepare(output)
    case = cli.browse(output, request=request)
    observation = case["observations"][0]

    def lost_close(operation, payload):
        # Clean up the synthetic browser, but simulate loss of its acknowledgement.
        request(operation, payload)
        raise BrowserBrokerError("Synthetic close acknowledgement lost")

    result = cli.finish(output, assessment(), "Assessment complete", request=lost_close)
    assert result["assessment"]["verdict"] == "no_specific_concern"
    assert result["observations"][0] == observation
    assert result["browser_cleanup"] == "unknown"
    assert result["operational_errors"][0]["operation"] == "close"
    assert (
        json.loads((output / "case.json").read_text())["assessment"]
        == result["assessment"]
    )
    assert verify_case(output) > 0


@pytest.mark.parametrize("scope", [None, "host"])
def test_related_host_redirect_allowed_by_default_but_explicit_scope_honored(
    live_fixture, scope
):
    request, transport, clients = live_fixture
    fetch = transport.fetch

    def redirect(request, decision, config=None):
        if urlsplit(request.url).path == "/redirect-related":
            return PinnedResponse(
                302,
                {"location": "https://www.public.test/related"},
                b"",
                "93.184.216.34",
            )
        return fetch(request, decision, config)

    transport.fetch = redirect
    payload = {"url": "https://malware.public.test/redirect-related"}
    if scope:
        payload["scope"] = scope
        with pytest.raises(DestinationRefused):
            request("start", payload)
    else:
        result = request("start", payload)
        try:
            assert (
                result["observations"][-1]["final_url"]
                == "https://www.public.test/related"
            )
        finally:
            request("close", {"session_token": result["session_token"]})
    assert all(c.stopped for c in clients)

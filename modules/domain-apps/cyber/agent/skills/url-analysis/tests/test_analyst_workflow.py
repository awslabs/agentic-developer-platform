"""Browser and model-protocol regressions using only synthetic data."""

import copy
import json

import analyst_context as context
import domain_investigation as cli
import live_evaluation as live
import pytest
from browser_guard import DestinationRefused
from case_contract import Assessment, EvidenceValidationError
from denylist import DenylistResult
from evidence_items import evidence_eligibility

from .test_domain_investigation import decision, review
from .test_domain_investigation import live_fixture as browser_fixture
from .test_live_evaluation import ProtocolModel, finish, last_view, row, tool_use

REPORT = {
    "source": "Synthetic helpdesk report",
    "reported_at": "2026-09-20T10:00:00Z",
    "summary": "A fictional user reports an unsolicited account-verification SMS.",
}
live_fixture = browser_fixture


def warning_assessment():
    return {
        "verdict": "suspicious",
        "assessor": "synthetic",
        "findings": [
            {
                "kind": "threat_warning",
                "basis": "observation",
                "statement": "The captured page displays a suspected-phishing warning; provider identity is unverified.",
                "evidence_ids": ["obs-001"],
                "evidence_refs": [
                    {"observation_id": "obs-001", "item_id": "warning-001"}
                ],
            }
        ],
        "limitations": [
            "Hidden page not inspected; no independent provider verification."
        ],
    }


def test_threat_warning_is_reportable_but_cannot_be_bypassed_or_prove_theft(
    live_fixture, tmp_path
):
    request, transport, _ = live_fixture
    transport.overrides = {
        "/seed": '<title>Suspected phishing</title><body>Warning: suspected phishing <a href="/portal">Proceed</a>'
    }
    out = tmp_path / "warning"
    case = cli.start(
        out, row()["url"], "Inspect this fictional warning", request=request
    )
    obs = case["observations"][0]
    assert "threat_warning" in obs["errors"]
    assert "human_verification_challenge" not in obs["errors"]
    assert evidence_eligibility(obs)["warning_finding_supported"]
    review(case, out)
    value = warning_assessment()
    value["verdict"] = "malicious"
    with pytest.raises(ValueError, match="warning alone"):
        cli.finish(out, value, "Finished", request=request)
    with pytest.raises(Exception, match="Challenge encountered"):
        cli.step(out, "root", decision(case), request=request)
    review(live.load_case(out), out)
    case = cli.finish(
        out, warning_assessment(), "Report the warning without bypass", request=request
    )
    assert case["assessment"]["verdict"] == "suspicious"
    assert not any("/portal" in url for _, url in transport.requests)


def test_prior_finding_survives_later_challenge_with_precise_correction(
    live_fixture, tmp_path
):
    request, transport, _ = live_fixture
    transport.overrides = {
        "/seed": '<body>Verification form<form><input type="password"></form><a href="/challenge">Continue</a>',
        "/challenge": "<body>Verify you are human",
    }
    out = tmp_path / "later-challenge"
    case = cli.start(out, row()["url"], "Inspect this fictional flow", request=request)
    review(case, out)
    case = cli.step(
        out,
        "follow",
        decision(case),
        candidate_id=case["browser_view"]["choices"][0]["id"],
        request=request,
    )
    review(case, out)
    value = {
        "verdict": "suspicious",
        "assessor": "synthetic",
        "limitations": ["Later challenge blocked investigation."],
        "findings": [
            {
                "kind": "credential_collection",
                "basis": "observation",
                "statement": "An earlier page contained a password field; its purpose remains unresolved.",
                "evidence_ids": ["obs-001", "obs-002"],
            }
        ],
    }
    with pytest.raises(EvidenceValidationError) as error:
        cli.finish(out, value, "Challenge prevented more browsing", request=request)
    assert error.value.detail["observation_id"] == "obs-002"
    assert error.value.detail["finding_index"] == 0
    value["findings"][0]["evidence_ids"] = ["obs-001"]
    value["findings"].append(
        {
            "kind": "coverage_limitation",
            "basis": "observation",
            "statement": "The later view was a human-verification challenge.",
            "evidence_ids": ["obs-002"],
        }
    )
    case = cli.finish(
        out, value, "Preserve earlier evidence and later coverage gap", request=request
    )
    assert case["assessment"]["verdict"] == "suspicious"
    assert len(case["assessment"]["findings"]) == 2


def test_context_and_enrichment_reach_model_and_report_without_reopening_browser(
    live_fixture, tmp_path
):
    request, _, clients = live_fixture
    out = tmp_path / "context"

    def enrich(output, source, reason):
        return cli.enrich(
            output,
            source,
            reason,
            lookup_fn=lambda *a, **k: {
                "source": "dns",
                "status": "available",
                "checked_at": "2026-09-24T12:00:00Z",
                "answers": [],
            },
        )

    def choose(turn, kwargs):
        view = last_view(kwargs)
        assert "URL analyst reasoning" in kwargs["system"][0]["text"]
        assert view["incident_context"][0]["source"] == REPORT["source"]
        if turn == 1:
            return [
                tool_use(
                    "enrich",
                    {
                        "source": "dns",
                        "reason": "Identify current resolution for this reported incident",
                    },
                )
            ]
        assert view["valid_context_ids"] == ["incident-001", "corroboration-001"]
        call = finish("obs-001")
        call["toolUse"]["input"]["assessment"]["context_assessment"] = {
            "risk": "suspicious",
            "findings": [
                {
                    "basis": "reported",
                    "statement": "The helpdesk reports an unsolicited SMS.",
                    "source_ids": ["incident-001"],
                }
            ],
            "limitations": ["Synthetic submitter report; not independently verified."],
        }
        return [call]

    result = live.investigate(
        out,
        {**row(), "incident_context": [REPORT]},
        ProtocolModel(choose),
        request=request,
        enrich_fn=enrich,
    )
    assert result["model_completed"] and result["verdict"] == "inconclusive"
    assert len(clients) == 1 and clients[0].stopped
    assert "Context risk: suspicious" in (out / "report.md").read_text()
    assert "corroboration-001" in (out / "report.html").read_text()


def test_unavailable_page_can_still_receive_sourced_context_assessment(tmp_path):
    def unavailable(operation, payload):
        raise DestinationRefused(
            payload["url"],
            DenylistResult(
                allowed=False,
                reason="Synthetic DNS failure",
                reason_code="resolution_failed",
            ),
        )

    def choose(turn, kwargs):
        view = last_view(kwargs)
        assert view["observation"] is None
        return [
            tool_use(
                "finish",
                {
                    "reason": "No page available; preserve incident report",
                    "assessment": {
                        "verdict": "inconclusive",
                        "assessor": "synthetic",
                        "context_assessment": {
                            "risk": "suspicious",
                            "findings": [
                                {
                                    "basis": "reported",
                                    "statement": "An unsolicited verification SMS was reported.",
                                    "source_ids": ["incident-001"],
                                }
                            ],
                            "limitations": ["Report unverified; no browser evidence."],
                        },
                    },
                },
            )
        ]

    result = live.investigate(
        tmp_path / "case",
        {**row(), "incident_context": [REPORT]},
        ProtocolModel(choose),
        request=unavailable,
    )
    assert result["model_completed"] and result["verdict"] == "inconclusive"
    assert result["model_turns"] == 1


def test_missing_or_invented_context_sources_cannot_support_reported_facts():
    value = Assessment(
        verdict="inconclusive",
        assessor="synthetic",
        context_assessment={
            "risk": "suspicious",
            "findings": [
                {
                    "basis": "reported",
                    "statement": "A provider reported a threat",
                    "source_ids": ["corroboration-001"],
                }
            ],
            "limitations": ["Not current behavior"],
        },
    )
    with pytest.raises(ValueError, match="unknown source"):
        value.validate_context([])
    with pytest.raises(ValueError, match="Unavailable"):
        value.validate_context([{"id": "corroboration-001", "status": "skipped"}])


def test_unavailable_page_without_incident_still_allows_model_selected_enrichment(
    tmp_path,
):
    def unavailable(operation, payload):
        raise DestinationRefused(
            payload["url"],
            DenylistResult(
                allowed=False,
                reason="Synthetic DNS failure",
                reason_code="resolution_failed",
            ),
        )

    def enrich(output, source, reason):
        return cli.enrich(
            output,
            source,
            reason,
            lookup_fn=lambda *a, **k: {
                "source": "rdap",
                "status": "available",
                "events": [],
                "checked_at": "2026-09-24T12:00:00Z",
            },
        )

    def choose(turn, kwargs):
        view = last_view(kwargs)
        assert view["observation"] is None
        if turn == 1:
            assert view["target_url"] and not view["incident_context"]
            return [
                tool_use(
                    "enrich",
                    {
                        "source": "rdap",
                        "reason": "Check registration despite unavailable page",
                    },
                )
            ]
        assert view["valid_context_ids"] == ["corroboration-001"]
        return [
            tool_use(
                "finish",
                {
                    "reason": "Record unavailability and the registration coverage gap",
                    "assessment": {"verdict": "inconclusive", "assessor": "synthetic"},
                },
            )
        ]

    result = live.investigate(
        tmp_path / "case",
        row(),
        ProtocolModel(choose),
        request=unavailable,
        enrich_fn=enrich,
    )
    assert result["model_completed"] and result["model_turns"] == 2


@pytest.mark.parametrize("unattempted", [True, False])
def test_refusal_only_confirms_no_browser_when_broker_proves_preflight(
    tmp_path, unattempted
):
    def unavailable(operation, payload):
        refusal = DestinationRefused(
            payload["url"],
            DenylistResult(
                allowed=False,
                reason="Synthetic DNS refusal",
                reason_code="resolution_failed",
            ),
        )
        refusal.browser_start_unattempted = unattempted
        raise refusal

    case = cli.start(
        tmp_path / "case", row()["url"], "Inspect availability", request=unavailable
    )
    assert case["unconfirmed_browser_start"] is (not unattempted)


def test_provider_lookups_use_fixed_endpoints_and_preserve_gaps():
    calls = []

    def provider(url):
        calls.append(url)
        if "iana.org" in url:
            return {"services": [[["test"], ["https://registry.test/rdap/"]]]}
        return {
            "ldhName": "public.test",
            "events": [{"eventAction": "registration", "eventDate": "2020-01-01"}],
        }

    result = context.lookup("rdap", "https://public.test/path", get_json=provider)
    assert result["status"] == "available" and result["events"][0]["eventDate"]
    assert calls == [
        "https://data.iana.org/rdap/dns.json",
        "https://registry.test/rdap/domain/public.test",
    ]
    assert (
        context.lookup("virustotal", "https://public.test", api_key=None)["status"]
        == "skipped"
    )
    assert (
        context.lookup(
            "rdap",
            "https://93.184.216.34",
            get_json=lambda _: pytest.fail("No DNS lookup"),
        )["status"]
        == "skipped"
    )


def test_rejected_finish_attempts_retain_valid_findings_on_model_failure(
    live_fixture, tmp_path
):
    request, _, _ = live_fixture
    out = tmp_path / "retained"

    def choose(turn, kwargs):
        if turn > 1:
            raise RuntimeError("Synthetic model outage during correction")
        call = finish("obs-001", "suspicious")
        good = {
            "kind": "other",
            "basis": "observation",
            "statement": "The page offers an account-verification link.",
            "evidence_ids": ["obs-001"],
        }
        bad = copy.deepcopy(good)
        bad["evidence_ids"] = ["obs-999"]
        call["toolUse"]["input"]["assessment"]["findings"] = [good, bad]
        return [call]

    result = live.investigate(out, row(), ProtocolModel(choose), request=request)
    case = json.loads((out / "case.json").read_text())
    assert result["verdict"] == "inconclusive" and not result["model_completed"]
    assert len(case["assessment"]["findings"]) == 1
    assert case["assessment_attempts"][0]["assessment"]["findings"][1][
        "evidence_ids"
    ] == ["obs-999"]

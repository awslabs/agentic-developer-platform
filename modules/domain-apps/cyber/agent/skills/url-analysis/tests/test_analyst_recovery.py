"""Behavioral regressions for evidence-led analyst decisions and collection recovery."""

import json
from dataclasses import replace

import pytest

import domain_investigation as cli
from analyst_context import lookup
from browser_guard import GuardedBrowserSession
from common_crawl import lookup_common_crawl, query_for
from research_case import verify_case
from .test_common_crawl import Athena, CONFIG
from .test_domain_investigation import live_fixture as live_fixture


def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "_lease_path", lambda p: tmp_path / (p.name + "-lease.json")
    )
    path = tmp_path / "case"
    cli.prepare(
        path,
        "https://example.com/",
        "Assess this URL",
        lookup_fn=lambda *a: {
            "kind": "archive_index",
            "status": "available",
            "captures": [],
            "source": "fixture",
        },
    )
    return path


@pytest.mark.parametrize(
    "verdict", ["clean", "suspicious", "malicious", "inconclusive"]
)
def test_one_verdict_can_be_supported_by_context_without_browser(
    tmp_path, monkeypatch, verdict
):
    path = prepared(tmp_path, monkeypatch)
    case = json.loads((path / "case.json").read_text())
    assert cli.status(case)["assessment"] is None
    assert cli.status(case)["assessment_status"] == "pending"
    assert "inconclusive" not in (path / "report.md").read_text()
    result = cli.finish(
        path,
        {
            "verdict": verdict,
            "assessor": "synthetic-analyst",
            "confidence": "medium",
            "findings": [
                {
                    "kind": "other",
                    "basis": "reported",
                    "statement": "Synthetic sourced finding.",
                    "source_ids": ["corroboration-001"],
                }
            ],
        },
        "Assessment complete",
    )
    assert result["assessment"]["verdict"] == verdict
    assert result["assessment_status"] == "complete"
    for report in ("report.md", "report.html"):
        text = (path / report).read_text()
        assert verdict in text and "medium" in text and "corroboration-001" in text
    assert verify_case(path) > 0


def test_queued_query_survives_old_deadline_and_keeps_execution_budget():
    now = [0]
    client = Athena()
    original = client.get_query_execution

    def execution(**kwargs):
        client.state = "QUEUED" if now[0] < 65 else "SUCCEEDED"
        return original(**kwargs)

    client.get_query_execution = execution
    result = lookup_common_crawl(
        "https://example.com/",
        config=replace(CONFIG, queue_seconds=100, execution_seconds=10),
        client=client,
        clock=lambda: now[0],
        sleep=lambda n: now.__setitem__(0, now[0] + n),
    )
    assert result["status"] == "available" and now[0] == 65
    assert not client.cancelled and result["queue_wait_seconds"] >= 60


def test_execution_deadline_is_named_and_cancels_only_that_query():
    now = [0]
    client = Athena(state="RUNNING")
    result = lookup_common_crawl(
        "https://example.com/",
        config=replace(CONFIG, queue_seconds=100, execution_seconds=3),
        client=client,
        clock=lambda: now[0],
        sleep=lambda n: now.__setitem__(0, now[0] + n),
    )
    assert result["budget_exceeded"] == "execution" and now[0] == 3
    assert client.cancelled == [{"QueryExecutionId": "query-test"}]


def test_exact_url_and_ip_discovery_use_bound_parameters():
    sql, values = query_for(
        CONFIG, "example.com", url="https://example.com/a'b", match="exact"
    )
    assert "url = ?" in sql and "example.com" not in sql
    assert values[-1] == "'https://example.com/a''b'"
    sql, values = query_for(CONFIG, "93.184.216.34")
    assert "url_host_registered_domain" not in sql and "url_host_tld" not in sql
    assert values[-1] == "'93.184.216.34'"
    result = lookup_common_crawl(
        "http://93.184.216.34/p",
        config=CONFIG,
        client=Athena(
            [("93.184.216.34", "http://93.184.216.34/p", "2026-09-01", "200")]
        ),
    )
    assert result["found"]


@pytest.mark.parametrize(
    "host,parent,tld",
    [
        ("login.example.com", "example.com", "com"),
        ("www.example.co.uk", "example.co.uk", "uk"),
        ("tenant.webflow.io", "webflow.io", "io"),
    ],
)
def test_rdap_uses_registry_parent_without_credentials(host, parent, tld):
    calls = []

    def provider(url):
        calls.append(url)
        if url.endswith("dns.json"):
            return {"services": [[[tld], ["https://registry.example/rdap/"]]]}
        return {"ldhName": parent, "events": []}

    result = lookup("rdap", "https://" + host, get_json=provider)
    assert result["status"] == "available"
    assert result["subject"] == host and result["queried_domain"] == parent
    assert calls[-1] == "https://registry.example/rdap/domain/" + parent


def test_urlhaus_is_lookup_only_and_records_missing_key():
    assert lookup("urlhaus", "http://93.184.216.34/p")["status"] == "skipped"
    calls = []

    def provider(url, **kwargs):
        calls.append((url, kwargs))
        return {"query_status": "no_results"}

    result = lookup(
        "urlhaus", "http://93.184.216.34/p", api_key="synthetic", get_json=provider
    )
    assert result["status"] == "available" and result["query_status"] == "no_results"
    assert calls[0][0] == "https://urlhaus-api.abuse.ch/v1/url/"
    assert "synthetic" not in json.dumps(result)


def test_navigation_and_screenshot_timeout_preserve_the_context(
    live_fixture, tmp_path, monkeypatch
):
    request, _, _ = live_fixture

    def screenshot(*a, **k):
        raise TimeoutError("synthetic slow screenshot")

    monkeypatch.setattr(GuardedBrowserSession, "screenshot", screenshot)
    path = tmp_path / "case"
    case = cli.start(path, "https://public.test/seed", "Investigate", request=request)
    first_sid = case["sessions"][0]["id"]
    assert case["observations"][0]["visible_text"]
    assert case["observations"][0]["screenshot_status"] == "not_requested"
    case = cli.step(path, "screenshot", "Inspect appearance", request=request)
    assert case["browser_view"]["session_open"]
    assert "screenshot_capture:TimeoutError" in case["observations"][-1]["errors"]
    case = cli.step(
        path,
        "navigate",
        "Check a separately discovered public reference",
        url="https://external.test/docs",
        request=request,
    )
    assert case["observations"][-1]["final_url"] == "https://external.test/docs"
    assert case["sessions"][0]["id"] == first_sid and len(case["sessions"]) == 1
    cli.finish(
        path, {"verdict": "clean", "assessor": "fixture"}, "Done", request=request
    )


def test_same_run_import_copies_verified_observations_without_verdict(
    live_fixture, tmp_path
):
    request, _, _ = live_fixture
    source, dest = tmp_path / "source", tmp_path / "dest"
    cli.start(source, "https://public.test/seed", "Collect", request=request)
    cli.step(source, "screenshot", "Retain visual evidence", request=request)
    cli.finish(
        source,
        {"verdict": "suspicious", "assessor": "fixture"},
        "Done",
        request=request,
    )
    cli.prepare(
        dest,
        "https://public.test/",
        "Assess related root",
        lookup_fn=lambda *a: {"status": "skipped"},
    )
    case = cli.import_evidence(dest, source, "Relevant same-run content")
    imported = [
        r for r in case["corroboration"] if r.get("kind") == "imported_observation"
    ]
    assert len(imported) == 2
    assert all(r["source_case_id"] == "source" for r in imported)
    assert "verdict" not in json.dumps(imported)
    assert (dest / imported[1]["observation"]["screenshot"]).exists()
    assert not case["sessions"] and not case["observations"]
    assert verify_case(dest) > 0
    with pytest.raises(ValueError, match="same run"):
        cli.import_evidence(dest, tmp_path / "another-run" / "source", "Wrong run")


def test_replay_strips_prior_analysis_and_keeps_sources(tmp_path):
    from replay_assessment import evidence_input

    (tmp_path / "manifest.json").write_text(json.dumps({"files": {"page.txt": {}}}))
    (tmp_path / "page.txt").write_text("Synthetic archived page")
    case = {
        "target_url": "https://example.test/",
        "observations": [],
        "probes": [],
        "assessment": {"verdict": "malicious"},
        "objective": "Expected malicious",
        "initial_hypothesis": {"hypothesis": "malicious"},
        "reviews": [{"verdict": "malicious"}],
        "corroboration": [
            {
                "id": "corroboration-001",
                "kind": "archived_page",
                "status": "available",
                "content_file": "page.txt",
                "selection_reason": "Expected malicious",
            }
        ],
        "incident_context": [{"id": "incident-001", "summary": "Expected malicious"}],
    }
    blocks, fingerprint, data = evidence_input(case, tmp_path)
    assert "malicious" not in blocks[0]["text"]
    assert data["sources"][0]["archived_content"] == "Synthetic archived page"
    case["assessment"]["verdict"] = "clean"
    assert evidence_input(case, tmp_path)[1] == fingerprint
    (tmp_path / "page.txt").write_text("Changed synthetic evidence")
    assert evidence_input(case, tmp_path)[1] != fingerprint


def test_replay_packages_receive_identical_evidence(monkeypatch, tmp_path):
    import replay_assessment as replay

    monkeypatch.setattr(
        replay, "materialize", lambda *a, **kw: {"observations": [], "probes": []}
    )
    blocks = [{"text": "same synthetic evidence"}]
    monkeypatch.setattr(
        replay,
        "evidence_input",
        lambda *a: (blocks, "fingerprint", {"observations": [], "sources": []}),
    )
    calls = []

    def assess(model, model_id, prompt, schema, supplied):
        calls.append((prompt, supplied))
        return {"assessment": {"verdict": "clean", "assessor": "fixture"}}

    monkeypatch.setattr(replay, "assess", assess)
    result = replay.compare(
        None,
        None,
        "same-model",
        {"id": "case", "case_uri": "s3://fixture/case.json", "sha256": "a" * 64},
        {
            "old": {"prompt": "old", "schema": {}},
            "new": {"prompt": "new", "schema": {}},
        },
    )
    assert calls == [("old", blocks), ("new", blocks)]
    assert all(v["structure_and_references_valid"] for v in result["packages"].values())


def test_new_source_invalidates_previous_verdict_without_selecting_another():
    case = {"assessment": {"verdict": "clean"}, "corroboration": []}
    cli._append_context(case, {"kind": "enrichment", "status": "available"})
    assert case["assessment"]["verdict"] is None
    assert case["assessment"]["assessor"] == "collection-system"


def test_new_assessment_schema_has_four_labels_but_reads_legacy_reports():
    from case_contract import Assessment, assessment_schema

    assert assessment_schema()["properties"]["verdict"]["enum"] == [
        "clean",
        "suspicious",
        "malicious",
        "inconclusive",
    ]
    legacy = Assessment.model_validate(
        {"verdict": "no_specific_concern", "assessor": "legacy"}
    )
    assert legacy.verdict == "no_specific_concern"

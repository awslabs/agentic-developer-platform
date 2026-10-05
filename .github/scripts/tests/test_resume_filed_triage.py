"""Recovery validates real filing artifacts; it never regroups existing issues."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import resume_filed_triage as recovery
import security_traceability as traceability
import triage_group_findings as triage

FIXTURES = Path(__file__).parent / "fixtures/triage-3984"


@pytest.fixture
def filed(tmp_path):
    findings = FIXTURES / "new-findings.json"
    plan = json.loads((FIXTURES / "grouping-plan.json").read_text())
    source, date = plan["source"], plan["run_date"]
    trace = traceability.build_grouping(plan, traceability.severity_by_finding(findings, source), run_id="original")
    items = [{"number": i + 100, "finding_ids": group["finding_ids"]} for i, group in enumerate(plan["groups"])]
    trace = traceability.enrich_filed(trace, items, repo="aws-e/adp")
    traceability.write_ledger(tmp_path / f"traceability.{source}.json", trace)
    triage.write_marker(tmp_path, run_date=date, source=source, generated_at=date + "T00:00:00Z", fields=triage.ledger_fields(99, items))
    return tmp_path, findings, source, date


def test_completed_filing_is_reused_without_changing_a_byte(filed):
    before = {p.name: p.read_bytes() for p in filed[0].iterdir()}
    assert recovery.can_resume(*filed)
    assert before == {p.name: p.read_bytes() for p in filed[0].iterdir()}


def test_new_night_still_runs_normal_triage(tmp_path):
    assert not recovery.can_resume(tmp_path, FIXTURES / "new-findings.json", "code-review", "2026-09-30")


@pytest.mark.parametrize("mutation", ["missing_finding", "changed_severity", "new_finding"])
def test_different_findings_do_not_reuse_or_overwrite_old_issues(filed, mutation):
    document = json.loads(filed[1].read_text())
    if mutation == "missing_finding":
        document["new_findings"].pop()
    elif mutation == "new_finding":
        document["new_findings"].append({**document["new_findings"][0], "finding_id": "f-aaaaaaaa"})
    else:
        document["new_findings"][0]["risk_level"] = "LOW"
    path = filed[0] / "changed.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="Findings differ"):
        recovery.can_resume(filed[0], path, filed[2], filed[3])


@pytest.mark.parametrize("mutation", ["missing_trace", "missing_marker", "wrong_date", "wrong_issues", "partial_trace"])
def test_incomplete_or_conflicting_filing_fails_closed(filed, mutation):
    directory, _, source, _ = filed
    trace_path = directory / f"traceability.{source}.json"
    marker_path = directory / f"shard-triage.{source}.json"
    if mutation == "missing_trace":
        trace_path.unlink()
    elif mutation == "missing_marker":
        marker_path.unlink()
    else:
        trace = json.loads(trace_path.read_text())
        if mutation == "wrong_date":
            trace["run_date"] = "2020-01-01"
        elif mutation == "wrong_issues":
            trace["groups"][0]["issue_number"] = 9999
        else:
            trace["stage"] = "grouping"
        trace_path.write_text(json.dumps(trace))
    with pytest.raises((ValueError, traceability.TraceabilityError)):
        recovery.can_resume(*filed)

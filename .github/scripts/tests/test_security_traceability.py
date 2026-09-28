"""Tests for the nightly finding-to-issue traceability ledger (intent #4290).

The ledger is the one artifact that answers "where did each finding end up, and
what has happened to it since". These tests pin the two properties that make it
trustworthy: severity is COMPUTED from the findings (never guessed), and the
filed-stage invariant proves every finding reached exactly one real issue.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import security_traceability as st

FIXTURES = Path(__file__).parent / "fixtures" / "triage-3984"
PLAN_FIXTURE = FIXTURES / "grouping-plan.json"
FINDINGS_FIXTURE = FIXTURES / "new-findings.json"
RUN_ID = "99830451698"
REPO = "aws-e/adp"


def load_plan() -> dict:
    return json.loads(PLAN_FIXTURE.read_text(encoding="utf-8"))


def a_grouping_ledger() -> dict:
    """The grouping-stage ledger built from the #3984 fixture — the shape the
    filing step is handed to enrich."""
    severities = st.severity_by_finding(FINDINGS_FIXTURE, "code-review")
    return st.build_grouping(load_plan(), severities, run_id=RUN_ID)


def filed_work_items(plan: dict, *, base=9100) -> list[dict]:
    """Synthesize `run_triage`'s per-cluster filing result: one issue number per
    group, each carrying that group's finding ids (the key enrich_filed matches on)."""
    return [
        {"number": base + i, "finding_ids": sorted(g["finding_ids"])}
        for i, g in enumerate(plan["groups"])
    ]


# --------------------------------------------------------------------------
# severity is computed, not guessed
# --------------------------------------------------------------------------


def test_severity_normalizes_case_and_whitespace():
    assert st.normalize_severity(" high ") == "HIGH"
    assert st.normalize_severity("critical") == "CRITICAL"


@pytest.mark.parametrize("bad", ["", "  ", None, 3, "SEV1", "urgent"])
def test_an_unknown_risk_level_is_an_error_not_a_default(bad):
    """Coercing an unrecognized level to LOW would understate exactly the finding
    a reader most needs to see, on the run that files un-recallable issues."""
    with pytest.raises(st.TraceabilityError):
        st.normalize_severity(bad)


def test_a_cluster_severity_is_the_worst_of_its_findings():
    assert st.rollup_severity(["LOW", "CRITICAL", "MEDIUM"]) == "CRITICAL"
    assert st.rollup_severity(["low", "medium"]) == "MEDIUM"
    assert st.rollup_severity(["HIGH"]) == "HIGH"


def test_a_severity_over_no_findings_is_an_error():
    with pytest.raises(st.TraceabilityError, match="over no findings"):
        st.rollup_severity([])


def test_severity_map_reads_only_ids_and_levels_from_the_findings():
    """NEV-2: the projection carries risk levels (a label), never reproduction
    detail. It also honours the source filter, like load_new_findings."""
    sev = st.severity_by_finding(FINDINGS_FIXTURE, "code-review")
    assert sev["f-42dca300"] == "CRITICAL"
    assert set(sev.values()) <= set(st.SEVERITY_ORDER)
    assert len(sev) == 12
    # No pentest findings in this fixture, so a pentest projection is empty rather
    # than crossing the two scanners.
    assert st.severity_by_finding(FINDINGS_FIXTURE, "pentest") == {}


# --------------------------------------------------------------------------
# stage 1: grouping
# --------------------------------------------------------------------------


def test_the_grouping_stage_records_clusters_severity_and_reverse_index():
    trace = a_grouping_ledger()
    assert trace["stage"] == "grouping"
    assert trace["findings_total"] == 12
    assert trace["groups_total"] == 5
    assert trace["run_date"] == "2026-08-30"
    assert trace["run_id"] == RUN_ID
    assert trace["source"] == "code-review"

    # The group that covers the one CRITICAL finding rolls up to CRITICAL; every
    # other group in this fixture is all-HIGH.
    by_slug = {g["slug"]: g for g in trace["groups"]}
    assert by_slug["edge-and-internal-plane-header-trust"]["severity"] == "CRITICAL"
    assert by_slug["server-side-role-resolution"]["severity"] == "HIGH"

    # Nothing is filed yet.
    assert all(g["issue_number"] is None for g in trace["groups"])
    assert all(g["fix_status"] == "PLANNED" for g in trace["groups"])

    # The reverse index covers every finding exactly once and points at its group.
    assert len(trace["findings_index"]) == 12
    assert trace["findings_index"]["f-42dca300"] == {
        "group": "edge-and-internal-plane-header-trust",
        "severity": "CRITICAL",
        "issue_number": None,
        "fix_status": "PLANNED",
    }


def test_a_group_covering_a_finding_with_no_known_severity_is_an_error():
    plan = load_plan()
    severities = st.severity_by_finding(FINDINGS_FIXTURE, "code-review")
    severities.pop("f-42dca300")
    with pytest.raises(st.TraceabilityError, match="no known severity"):
        st.build_grouping(plan, severities, run_id=RUN_ID)


def test_a_quiet_night_grouping_is_valid_and_empty():
    plan = {"schema_version": "1", "source": "code-review", "run_date": "2026-08-30", "groups": []}
    trace = st.build_grouping(plan, {}, run_id=RUN_ID)
    assert trace["findings_total"] == 0
    assert trace["groups_total"] == 0
    assert trace["groups"] == [] and trace["findings_index"] == {}


# --------------------------------------------------------------------------
# stage 2: filed
# --------------------------------------------------------------------------


def test_filing_folds_issue_numbers_into_every_group_and_finding():
    plan = load_plan()
    trace = a_grouping_ledger()
    filed = st.enrich_filed(trace, filed_work_items(plan), repo=REPO)

    assert filed["stage"] == "filed"
    for group in filed["groups"]:
        assert isinstance(group["issue_number"], int)
        assert group["issue_url"] == f"https://github.com/{REPO}/issues/{group['issue_number']}"
        assert group["fix_status"] == "FILED"
    for row in filed["findings_index"].values():
        assert isinstance(row["issue_number"], int)
        assert row["fix_status"] == "FILED"


def test_filing_does_not_mutate_the_grouping_ledger():
    trace = a_grouping_ledger()
    before = json.dumps(trace, sort_keys=True)
    st.enrich_filed(trace, filed_work_items(load_plan()), repo=REPO)
    assert json.dumps(trace, sort_keys=True) == before


def test_a_filed_issue_matching_no_planned_group_is_an_error():
    trace = a_grouping_ledger()
    bogus = [{"number": 9999, "finding_ids": ["f-deadbeef"]}]
    with pytest.raises(st.TraceabilityError, match="match no planned group"):
        st.enrich_filed(trace, bogus, repo=REPO)


def test_enrich_refuses_a_ledger_that_is_not_the_grouping_stage():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    with pytest.raises(st.TraceabilityError, match="grouping-stage"):
        st.enrich_filed(filed, filed_work_items(load_plan()), repo=REPO)


# --------------------------------------------------------------------------
# the invariant: nothing fell through the cracks
# --------------------------------------------------------------------------


def test_a_fully_filed_night_accounts_for_every_finding_exactly_once():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    st.assert_fully_traced(filed)  # does not raise


def test_the_invariant_rejects_a_grouping_stage_ledger():
    with pytest.raises(st.TraceabilityError, match="not 'filed'"):
        st.assert_fully_traced(a_grouping_ledger())


def test_the_invariant_rejects_a_group_that_never_got_an_issue_number():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    filed["groups"][0]["issue_number"] = None
    with pytest.raises(st.TraceabilityError, match="no issue number"):
        st.assert_fully_traced(filed)


def test_the_invariant_rejects_a_count_that_disagrees_with_the_contents():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    filed["findings_total"] = 11
    with pytest.raises(st.TraceabilityError, match="findings_total"):
        st.assert_fully_traced(filed)


# --------------------------------------------------------------------------
# lifecycle (forward-compatible; nightly writes only up to FILED)
# --------------------------------------------------------------------------


def test_a_fix_pr_advances_the_issue_and_its_findings_to_pr_open():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    number = filed["groups"][0]["issue_number"]
    updated = st.update_fix_status(filed, issue_number=number, status="PR_OPEN", pr=4750)
    group = next(g for g in updated["groups"] if g["issue_number"] == number)
    assert group["fix_status"] == "PR_OPEN"
    assert group["fix_prs"] == [4750]
    for fid in group["finding_ids"]:
        assert updated["findings_index"][fid]["fix_status"] == "PR_OPEN"


def test_a_merged_fix_stamps_fixed_and_a_timestamp():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    number = filed["groups"][0]["issue_number"]
    updated = st.update_fix_status(
        filed, issue_number=number, status="FIXED", pr=4750, fixed_at="2026-09-02T10:00:00Z"
    )
    group = next(g for g in updated["groups"] if g["issue_number"] == number)
    assert group["fix_status"] == "FIXED"
    assert group["fixed_at"] == "2026-09-02T10:00:00Z"


def test_a_pr_is_not_recorded_twice():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    number = filed["groups"][0]["issue_number"]
    once = st.update_fix_status(filed, issue_number=number, status="PR_OPEN", pr=4750)
    twice = st.update_fix_status(once, issue_number=number, status="PR_OPEN", pr=4750)
    group = next(g for g in twice["groups"] if g["issue_number"] == number)
    assert group["fix_prs"] == [4750]


def test_an_unknown_fix_state_is_rejected():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    number = filed["groups"][0]["issue_number"]
    with pytest.raises(st.TraceabilityError, match="not one of"):
        st.update_fix_status(filed, issue_number=number, status="DONE")


def test_advancing_an_issue_not_in_the_ledger_is_an_error():
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    with pytest.raises(st.TraceabilityError, match="no group in the ledger"):
        st.update_fix_status(filed, issue_number=123456, status="PR_OPEN")


# --------------------------------------------------------------------------
# CLI round-trip
# --------------------------------------------------------------------------


def test_the_cli_writes_a_grouping_ledger_and_verify_gates_a_filed_one(tmp_path):
    out = tmp_path / "traceability.json"
    rc = st.main(
        [
            "grouping",
            "--plan", str(PLAN_FIXTURE),
            "--new-findings", str(FINDINGS_FIXTURE),
            "--source", "code-review",
            "--run-id", RUN_ID,
            "--output", str(out),
        ]
    )
    assert rc == 0
    trace = json.loads(out.read_text())
    assert trace["stage"] == "grouping" and trace["groups_total"] == 5

    # A grouping-stage ledger must not pass verify — it has not reached filing.
    assert st.main(["verify", "--traceability", str(out)]) == 1

    # Enrich to filed and write it back; now verify passes.
    filed = st.enrich_filed(trace, filed_work_items(load_plan()), repo=REPO)
    st.write_ledger(out, filed)
    assert st.main(["verify", "--traceability", str(out)]) == 0


def test_the_ledger_is_written_deterministically(tmp_path):
    """A re-run of the same night produces a byte-identical file, so a diff means
    a real change — the reproducibility the U2 shard writer also keeps."""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    st.write_ledger(a, a_grouping_ledger())
    st.write_ledger(b, a_grouping_ledger())
    assert a.read_bytes() == b.read_bytes()


# --------------------------------------------------------------------------
# the filed record must survive a re-run (#4792)
# --------------------------------------------------------------------------


def test_a_grouping_stage_write_refuses_to_erase_a_filed_stage_ledger(tmp_path):
    """The regression that lost the 2026-08-30 record.

    A re-run's authoring step wrote a fresh grouping-stage ledger to the same path
    and erased the filed stage that mapped 62 findings to issues #4701-#4731. The
    file that exists to answer "where did this finding end up" then answered it
    wrongly, and the issue numbers were only recoverable from GitHub.
    """
    path = tmp_path / "traceability.json"
    filed = st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO)
    st.write_ledger(path, filed)

    with pytest.raises(st.TraceabilityError, match="refusing to overwrite the filed-stage"):
        st.write_ledger(path, a_grouping_ledger())

    # The filed record is intact — issue numbers still there.
    assert st.read_ledger(path)["stage"] == "filed"
    st.assert_fully_traced(st.read_ledger(path))


def test_the_refusal_can_be_overridden_explicitly(tmp_path):
    """A caller that genuinely means to replace a filed record says so."""
    path = tmp_path / "traceability.json"
    st.write_ledger(path, st.enrich_filed(a_grouping_ledger(), filed_work_items(load_plan()), repo=REPO))
    st.write_ledger(path, a_grouping_ledger(), allow_stage_regression=True)
    assert st.read_ledger(path)["stage"] == "grouping"


def test_a_grouping_stage_write_is_fine_when_nothing_is_filed_yet(tmp_path):
    """The normal first run, and a re-run of a night that never reached filing."""
    path = tmp_path / "traceability.json"
    st.write_ledger(path, a_grouping_ledger())
    st.write_ledger(path, a_grouping_ledger())  # grouping over grouping is fine
    assert st.read_ledger(path)["stage"] == "grouping"


def test_an_unreadable_existing_ledger_is_not_a_filed_record_to_protect(tmp_path):
    """A corrupt file must not wedge the night: there is no filed record in it."""
    path = tmp_path / "traceability.json"
    path.write_text("{broken", encoding="utf-8")
    st.write_ledger(path, a_grouping_ledger())
    assert st.read_ledger(path)["stage"] == "grouping"

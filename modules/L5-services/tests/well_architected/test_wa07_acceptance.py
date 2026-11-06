"""Offline WA07 ledger tests use fictional, not operational, evidence."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from l5_services.wa_acceptance import expected_criteria, sanitized_summary, validate_ledger


REPOSITORY = Path(__file__).resolve().parents[4]
SPECS = json.loads((REPOSITORY / "docs/l5-services/well-architected/story-specs.json").read_text())
REVISION = "a" * 40


def ledger():
    criteria, _ = expected_criteria(SPECS)
    rows = []
    for criterion in sorted(criteria):
        alias = criterion.split("/", 1)[0]
        rows.append({"criterion": criterion, "status": "not_run",
                     "phase": "installed" if alias == "WA07" else "before_review",
                     "owner_alias": "acceptance-owner" if criterion.startswith("EC-") else alias,
                     "tested_revision": None, "evidence_ref": None, "blocker_code": "not_executed"})
    return {"schema_version": 1, "candidate_revision": REVISION,
            "installed_revision": None, "installed_readback_ref": None,
            "rows": rows, "allowance_renewed": False,
            "costs": {bucket: {"state": "unknown", "amount_usd": None,
                               "reason": "pending eligible fixtures"} for bucket in (
                                   "adp_accounting", "provider_charges", "ancillary_charges", "unattributed")},
            "resources": [{"private_ref": "private/never-echo-resource-marker", "ownership": "adopted",
                           "state": "retained", "owner_alias": "WA07",
                           "evidence_ref": "private/never-echo-receipt-marker"}]}


def row_by_id(private, criterion):
    return next(row for row in private["rows"] if row["criterion"] == criterion)


def mark_pass(row, revision=REVISION):
    row.update(status="pass", tested_revision=revision,
               evidence_ref="private/never-echo-pass-marker", blocker_code=None)


def test_all_epic_story_and_scenario_rows_are_required_and_incomplete():
    private = ledger()
    assert len(private["rows"]) == 60
    assert len(validate_ledger(private, SPECS)) == 60
    report = sanitized_summary(private, SPECS)
    assert report["acceptance"] == "incomplete"
    assert report["installed_revision_recorded"] is False
    assert len(report["remaining"]) == 60
    assert report["cost_states"]["provider_charges"] == "unknown"
    for criterion in ("WA07/AC-05", "WA07/SCN-05", "EC-08", "WA01/AC-01"):
        assert any(item["criterion"] == criterion for item in report["remaining"])
    assert "never-echo" not in json.dumps(report)
    assert "candidate_revision" not in report
    assert "verified_installed_revision" not in report
    private["rows"].pop()
    with pytest.raises(ValueError, match="every epic"):
        validate_ledger(private, SPECS)


@pytest.mark.parametrize("change,error", [
    (lambda private: private["rows"].append(deepcopy(private["rows"][0])), "every epic"),
    (lambda private: row_by_id(private, "WA04/SCN-01").update(
        status="blocked", blocker_code="not_executed"), "acceptance row"),
    (lambda private: row_by_id(private, "WA04/SCN-01").update(
        tested_revision=REVISION), "acceptance row"),
    (lambda private: mark_pass(row_by_id(private, "EC-01")), "epic cannot pass"),
    (lambda private: mark_pass(row_by_id(private, "WA07/AC-01")), "WA07 live pass"),
    (lambda private: (private.update(installed_revision=REVISION,
                                    installed_readback_ref="private/installed-receipt"),
                      mark_pass(row_by_id(private, "WA07/AC-05")),
                      row_by_id(private, "WA07/AC-05").update(phase="handoff")), "WA07 handoff"),
    (lambda private: mark_pass(row_by_id(private, "WA01/AC-01"), "b" * 40),
     "acceptance row"),
    (lambda private: private.update(installed_revision="b" * 40,
                                   installed_readback_ref="private/installed-receipt"),
     "code revisions"),
    (lambda private: private["costs"]["provider_charges"].update(amount_usd="0.00"), "unknown cost"),
    (lambda private: private["costs"].pop("ancillary_charges"), "cost buckets"),
    (lambda private: private.update(allowance_renewed=True), "cost buckets"),
    (lambda private: private["resources"][0].update(state="removed"), "owned removal"),
    (lambda private: private["resources"].append(deepcopy(private["resources"][0])), "duplicated"),
])
def test_ledger_rejects_missing_coverage_false_pass_and_unsafe_accounting(change, error):
    private = ledger()
    change(private)
    with pytest.raises(ValueError, match=error):
        validate_ledger(private, SPECS)


def test_known_zero_needs_receipt_and_owned_removal_needs_independent_proof():
    private = ledger()
    private["costs"]["provider_charges"] = {"state": "known", "amount_usd": "0.00",
                                            "evidence_ref": "private/no-charge-readback"}
    private["resources"][0].update(ownership="owned", state="removed",
                                   terminal_receipt_ref="private/native-terminal",
                                   absence_readback_ref="private/owned-absence",
                                   result_retained_ref="private/retained-result")
    assert validate_ledger(private, SPECS)
    assert sanitized_summary(private, SPECS)["resource_states"]["removed"] == 1
    private["resources"][0]["absence_readback_ref"] = None
    with pytest.raises(ValueError, match="owned removal"):
        validate_ledger(private, SPECS)


def test_cli_outputs_only_allowlisted_summary_and_rejects_private_repo_path(tmp_path):
    private = ledger()
    private["operator_private_note"] = "never-echo-operator-marker"
    path = tmp_path / "private-ledger.json"
    path.write_text(json.dumps(private))
    script = REPOSITORY / "modules/L5-services/scripts/wa07-acceptance.py"
    command = [sys.executable, str(script), "--private-ledger"]
    result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and json.loads(result.stdout)["acceptance"] == "incomplete"
    assert "never-echo" not in result.stdout + result.stderr
    refused = subprocess.run([*command, str(REPOSITORY / "docs/PUBLISHING.md")],
                             capture_output=True, text=True, check=False)
    assert refused.returncode == 2 and "outside the repository" in refused.stderr
    assert "PUBLISHING" not in refused.stderr


def test_manual_scn_05_handoff_keeps_unverified_rows_incomplete():
    procedure = (REPOSITORY / "docs/l5-services/well-architected/backlog.md").read_text()
    story = (REPOSITORY / "docs/l5-services/well-architected/stories/wa07.md").read_text()
    assert "### Manual SCN-05 acceptance and cleanup reconciliation" in procedure
    assert "(../backlog.md#manual-scn-05-acceptance-and-cleanup-reconciliation)" in story

    private = ledger()
    private.update(installed_revision=REVISION, installed_readback_ref="private/never-echo-installed")
    mark_pass(row_by_id(private, "WA01/AC-01"))
    failed_case = row_by_id(private, "WA07/AC-03")
    failed_case.update(status="fail", tested_revision=REVISION,
                       evidence_ref="private/never-echo-failure", blocker_code="test_failed")
    handoff_case = row_by_id(private, "WA07/SCN-05")
    handoff_case.update(status="blocked", phase="handoff", owner_alias="acceptance-owner",
                        blocker_code="selected_role_unverified")
    private["resources"][0].update(ownership="unknown", state="unknown")
    report = sanitized_summary(private, SPECS)
    remaining = {item["criterion"]: item for item in report["remaining"]}
    assert report["acceptance"] == "incomplete"
    assert report["installed_revision_recorded"] is True
    assert report["resource_states"]["unknown"] == 1
    assert remaining["WA07/SCN-05"]["phase"] == "handoff"
    assert remaining["WA07/SCN-05"]["blocker_code"] == "selected_role_unverified"
    assert remaining["WA07/AC-03"]["status"] == "fail"
    assert remaining["EC-08"]["status"] == "not_run"
    assert "never-echo" not in json.dumps(report)

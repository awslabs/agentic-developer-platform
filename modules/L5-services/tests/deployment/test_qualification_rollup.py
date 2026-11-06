"""Offline checks that the qualification rollup refuses unverified evidence.

The rollup only transcribes an independent reader's observations into the
machine report. These checks pin the required record sets to the registered
proposal and prove that fabricated, stale, wrong-source, incomplete,
self-reported and cleanup-failed bundles never produce accepted evidence.
"""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "modules/L5-services/scripts/qualification-rollup.py"
SOURCE = "a" * 40
RUN_ID = "4242"
RUN_ATTEMPT = "1"


def rollup_module():
    specification = importlib.util.spec_from_file_location("qualification_rollup", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


MODULE = rollup_module()


def complete_bundle():
    observed_at = datetime.now(UTC).isoformat()
    bundle = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "source_revision": SOURCE,
        "definition_revision": "d" * 40,
        "workflow_path": MODULE.WORKFLOW_PATH,
        "gateway_revision": SOURCE,
        "reader": {"identity": "independent-reader", "revision": "b" * 40},
        "target": {
            "connection_id": "connection-example",
            "resource_kind": "eks-namespace",
            "resource_id": "cluster-example/namespace-example",
        },
        "cleanup": {
            "outcome": "passed",
            "readback": ["private://owned-resource-readback"],
            "observed_at": observed_at,
        },
    }
    for section, identifiers in MODULE.expected_records().items():
        bundle[section] = [
            {
                "id": identifier,
                "outcome": "passed",
                "evidence_verified": True,
                "requirement_revision": MODULE.proposal_revision(),
                "source_revision": SOURCE,
                "definition_revision": "d" * 40,
                "workflow_path": MODULE.WORKFLOW_PATH,
                "runtime_revision": SOURCE,
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
                "verification_layer": "independent-live",
                "evidence_ref": ("private://evidence/" + identifier + "#sha256:" + "f" * 64
                                 if section == "live_checks" or identifier.startswith(("SA15/", "SA16/"))
                                 else "private://evidence/" + identifier),
                "observed_at": observed_at,
                **({"observation": "independent-live"} if section == "live_checks" else {}),
            }
            for identifier in identifiers
        ]
    return bundle


def run_rollup(tmp_path, bundle, selection=()):
    observations = tmp_path / "independent-observations.json"
    observations.write_text("{ not json" if bundle is None else json.dumps(bundle))
    qualification = tmp_path / "l5-security-qualification/qualification.json"
    evidence = tmp_path / "delivery-evidence/delivery-evidence.json"
    completed = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            *selection,
            "--observations", str(observations),
            "--source-revision", SOURCE,
            "--definition-revision", "d" * 40,
            "--qualification", str(qualification),
            "--delivery-evidence", str(evidence),
        ],
        capture_output=True, text=True,
        env={**os.environ, "GITHUB_RUN_ID": RUN_ID, "GITHUB_RUN_ATTEMPT": RUN_ATTEMPT},
    )
    report = json.loads(qualification.read_text()) if qualification.exists() else None
    delivery = json.loads(evidence.read_text()) if evidence.exists() else None
    return completed, report, delivery


def test_required_record_sets_come_from_the_registered_proposal():
    expected = MODULE.expected_records()
    assert expected["epic_criteria"] == ["EC-0%d" % number for number in range(1, 10)]
    assert len(expected["story_criteria"]) == 49
    assert expected["story_criteria"][0] == "SA01/AC-01"
    assert expected["story_criteria"][-1] == "SA16/AC-04"
    assert len(expected["scenarios"]) == 49
    assert expected["scenarios"][-1] == "SA16/SCN-04"
    assert "owned-cleanup" in expected["live_checks"]
    assert len(expected["live_checks"]) == 14


def test_shared_entrypoint_preserves_explicit_security_contract(tmp_path):
    completed, report, delivery = run_rollup(tmp_path, complete_bundle(), ("--service", "security"))
    assert completed.returncode == 0, completed.stderr
    assert report["component"] == "l5-security"
    assert report["unresolved"] == []
    assert delivery["qualification_artifact"] == "l5-security-qualification/qualification.json"


def test_complete_independent_evidence_satisfies_every_required_record(tmp_path):
    completed, report, delivery = run_rollup(tmp_path, complete_bundle())
    assert completed.returncode == 0, completed.stdout + completed.stderr
    expected = MODULE.expected_records()
    assert report["schema_version"] == 1
    assert report["unresolved"] == []
    assert report["cleanup"]["outcome"] == "passed"
    assert report["source_revision"] == SOURCE
    assert report["definition_revision"] == "d" * 40
    assert report["workflow_path"] == MODULE.WORKFLOW_PATH
    assert report["gateway_revision"] == SOURCE
    assert report["run_id"] == RUN_ID
    assert report["run_attempt"] == RUN_ATTEMPT
    assert report["job"] == "L5 release and live qualification"
    for section, identifiers in expected.items():
        records = report[section]
        assert [record["id"] for record in records] == identifiers
        assert all(record["outcome"] == "passed" for record in records)
        assert all(record["evidence_verified"] is True for record in records)
        assert all(record["evidence_ref"] for record in records)
    assert all(record["observation"] == "independent-live" for record in report["live_checks"])
    assert delivery["schema_version"] == 2
    assert [check["name"] for check in delivery["required_checks"]] == expected["live_checks"]
    assert delivery["cleanup"]["outcome"] == "passed"
    assert delivery["unresolved"] == []
    assert delivery["qualification_artifact"] == "l5-security-qualification/qualification.json"


def test_independent_ci_criteria_may_accompany_independent_live_checks(tmp_path):
    bundle = complete_bundle()
    bundle["story_criteria"][0]["verification_layer"] = "independent-ci"
    completed, report, delivery = run_rollup(tmp_path, bundle)
    assert completed.returncode == 0, completed.stderr
    assert report["unresolved"] == delivery["unresolved"] == []


@pytest.mark.parametrize(
    "fault",
    ["fabricated", "unbound-evidence", "foreign-evidence", "invalid-digest",
     "wrong-definition", "wrong-workflow", "missing-definition", "missing-workflow",
     "wrong-bundle-definition", "wrong-bundle-workflow",
     "stale", "wrong-source", "wrong-runtime", "wrong-requirement", "wrong-run", "incomplete",
     "self-reported", "self-reported-criterion", "ci-layer-on-live", "ci-layer-on-live-scenario",
     "unbound-live-criterion", "ci-layer-on-release-scenario", "unbound-release-criterion",
     "reader-unverified",
     "cleanup-failed", "cleanup-unread", "cleanup-stale",
     "foreign-gateway", "unreadable", "duplicate", "malformed", "non-list"],
)
def test_rollup_refuses_unverified_or_incomplete_reports(tmp_path, fault):
    bundle = complete_bundle()
    if fault == "fabricated":
        bundle["live_checks"][0].pop("evidence_ref")
    elif fault == "unbound-evidence":
        bundle["live_checks"][0]["evidence_ref"] = "private://evidence/check"
    elif fault == "foreign-evidence":
        bundle["live_checks"][0]["evidence_ref"] = "https://example.invalid/receipt#sha256:" + "f" * 64
    elif fault == "invalid-digest":
        bundle["live_checks"][0]["evidence_ref"] = "private://evidence/check#sha256:not-a-digest"
    elif fault == "stale":
        bundle["scenarios"][0]["observed_at"] = (
            datetime.now(UTC) - timedelta(days=2)).isoformat()
    elif fault == "wrong-source":
        bundle["story_criteria"][0]["source_revision"] = "e" * 40
    elif fault == "wrong-bundle-definition":
        bundle["definition_revision"] = "e" * 40
    elif fault == "wrong-bundle-workflow":
        bundle["workflow_path"] = ".github/workflows/other.yml"
    elif fault in {"wrong-definition", "missing-definition"}:
        if fault == "wrong-definition":
            bundle["story_criteria"][0]["definition_revision"] = "e" * 40
        else:
            bundle["story_criteria"][0].pop("definition_revision")
    elif fault in {"wrong-workflow", "missing-workflow"}:
        if fault == "wrong-workflow":
            bundle["live_checks"][0]["workflow_path"] = ".github/workflows/other.yml"
        else:
            bundle["live_checks"][0].pop("workflow_path")
    elif fault == "wrong-runtime":
        bundle["story_criteria"][0]["runtime_revision"] = "e" * 40
    elif fault == "wrong-requirement":
        bundle["story_criteria"][0]["requirement_revision"] = "e" * 40
    elif fault == "wrong-run":
        bundle["epic_criteria"][0]["run_attempt"] = "2"
    elif fault == "incomplete":
        bundle["epic_criteria"].pop()
    elif fault == "self-reported":
        bundle["live_checks"][1]["observation"] = "workflow-reported"
    elif fault == "self-reported-criterion":
        bundle["scenarios"][0]["verification_layer"] = "workflow-reported"
    elif fault == "ci-layer-on-live":
        bundle["live_checks"][0]["verification_layer"] = "independent-ci"
    elif fault == "ci-layer-on-live-scenario":
        next(item for item in bundle["scenarios"] if item["id"] == "SA16/SCN-03")[
            "verification_layer"] = "independent-ci"
    elif fault == "unbound-live-criterion":
        next(item for item in bundle["story_criteria"] if item["id"] == "SA16/AC-03")[
            "evidence_ref"] = "private://evidence/SA16/AC-03"
    elif fault == "ci-layer-on-release-scenario":
        next(item for item in bundle["scenarios"] if item["id"] == "SA15/SCN-03")[
            "verification_layer"] = "independent-ci"
    elif fault == "unbound-release-criterion":
        next(item for item in bundle["story_criteria"] if item["id"] == "SA15/AC-01")[
            "evidence_ref"] = "private://evidence/SA15/AC-01"
    elif fault == "reader-unverified":
        bundle["scenarios"][1]["evidence_verified"] = False
    elif fault == "cleanup-failed":
        bundle["cleanup"]["outcome"] = "failed"
    elif fault == "cleanup-unread":
        bundle["cleanup"]["readback"] = []
    elif fault == "cleanup-stale":
        bundle["cleanup"]["observed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    elif fault == "foreign-gateway":
        bundle["gateway_revision"] = "f" * 40
    elif fault == "duplicate":
        bundle["scenarios"].append(dict(bundle["scenarios"][0]))
    elif fault == "malformed":
        bundle["story_criteria"].append({"outcome": "passed"})
    elif fault == "non-list":
        bundle["epic_criteria"] = {"id": "EC-01", "outcome": "passed"}
    else:
        bundle = None

    completed, report, delivery = run_rollup(tmp_path, bundle)
    assert completed.returncode != 0
    assert report["unresolved"], report
    assert delivery["unresolved"] == report["unresolved"]
    if fault == "unreadable":
        assert "observations:unreadable" in report["unresolved"]
    if fault == "incomplete":
        assert "epic_criteria/EC-09:not-observed" in report["unresolved"]
        assert report["epic_criteria"][-1]["outcome"] == "incomplete"
    if fault == "cleanup-failed":
        assert report["cleanup"]["outcome"] == "failed"
    if fault == "foreign-gateway":
        assert "gateway_revision:not-the-qualified-candidate" in report["unresolved"]
    if fault in {"fabricated", "unbound-evidence", "foreign-evidence", "invalid-digest"}:
        assert "live_checks/same-wheel:unbound-evidence" in report["unresolved"]
        assert report["live_checks"][0]["evidence_verified"] is False
    if fault == "wrong-runtime":
        assert "story_criteria/SA01/AC-01:wrong-runtime" in report["unresolved"]
        assert report["story_criteria"][0]["evidence_verified"] is False
        assert report["story_criteria"][0]["outcome"] == "incomplete"
    if fault == "wrong-source":
        assert "story_criteria/SA01/AC-01:wrong-source" in report["unresolved"]
        assert report["story_criteria"][0]["outcome"] == "incomplete"
    if fault == "wrong-requirement":
        assert "story_criteria/SA01/AC-01:wrong-requirement" in report["unresolved"]
        assert report["story_criteria"][0]["evidence_verified"] is False
    if fault == "self-reported-criterion":
        assert "scenarios/SA01/SCN-01:unverified-layer" in report["unresolved"]
    if fault == "ci-layer-on-live":
        assert "live_checks/same-wheel:unverified-layer" in report["unresolved"]
    if fault == "ci-layer-on-live-scenario":
        assert "scenarios/SA16/SCN-03:unverified-layer" in report["unresolved"]
    if fault == "unbound-live-criterion":
        assert "story_criteria/SA16/AC-03:unbound-evidence" in report["unresolved"]
    if fault == "ci-layer-on-release-scenario":
        assert "scenarios/SA15/SCN-03:unverified-layer" in report["unresolved"]
    if fault == "unbound-release-criterion":
        assert "story_criteria/SA15/AC-01:unbound-evidence" in report["unresolved"]
    if fault == "duplicate":
        assert "scenarios/SA01/SCN-01:duplicate" in report["unresolved"]
        assert report["scenarios"][0]["evidence_verified"] is False
        assert report["scenarios"][0]["outcome"] == "incomplete"
    if fault == "malformed":
        assert "story_criteria:malformed-observations" in report["unresolved"]
    if fault == "non-list":
        assert "epic_criteria:malformed-observations" in report["unresolved"]
    if fault in {"cleanup-unread", "cleanup-stale"}:
        assert report["cleanup"]["outcome"] == delivery["cleanup"]["outcome"] == "incomplete"

"""Network-free report validation. All simulated runs are explicitly non-live."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import pytest

from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.report import (
    Intervention,
    Result,
    ScenarioReport,
    assess,
    now,
    write_report,
)
from tests.e2e.orchestration.scenarios.definitions import CRITERIA, DEFINITION_HASH
from tests.e2e.orchestration.scenarios.delivery import Evidence


@pytest.fixture
def simulated_report(valid_config):
    inventory = Inventory.create(
        valid_config.artifact_directory,
        "q-offline-0123456789",
        valid_config.environment,
    )
    inventory.record_planned(
        fixture_id="issue",
        kind="qualification-issue",
        intended_identity="q-offline-0123456789/story-1",
        ownership_tags=valid_config.ownership_tags(inventory.qualification_id),
        idempotency_token="unit-test",
    )
    inventory.mark_created("issue", "123")
    started = now()
    artifact = Evidence(inventory).save(
        "simulation", {"live": False}, "offline-unit-simulation"
    )
    report = ScenarioReport(
        qualification_id=inventory.qualification_id,
        definition_hash=DEFINITION_HASH,
        manifest_hash="a" * 64,
        live=False,
        started_at=started,
        completed_at=now(),
        versions=valid_config.versions,
        policy_id="simulated-policy",
        policy_hash="b" * 64,
        plan_version=1,
        spend_usd=0.0,
        results=[
            Result(
                id=c.id,
                status="PASS",
                detail="SIMULATED assertion only",
                evidence={k: artifact for k in c.evidence},
            )
            for c in CRITERIA
        ],
        cleanup_inventory=[r.to_json() for r in inventory.fixtures],
        interventions=[],
        interventions_complete=True,
        planned_gates=["release", "refusal"],
    )
    return report, inventory, valid_config


def problems(ctx):
    report, inventory, config = ctx
    return assess(report, config=config, inventory=inventory, manifest_hash="a" * 64)[1]


def test_simulation_can_never_qualify(simulated_report):
    assert "non-live simulation" in problems(simulated_report)
    report, inventory, config = simulated_report
    written = write_report(report, config=config, inventory=inventory)
    assert written["overall"] == "INCOMPLETE" and written["live"] is False
    assert "Live: False" in (inventory.path.parent / "summary.md").read_text()


@pytest.mark.parametrize("status", ["NOT_RUN", "FAIL"])
@pytest.mark.parametrize("criterion", [c.id for c in CRITERIA])
def test_every_mandatory_criterion_blocks(simulated_report, criterion, status):
    report = simulated_report[0]
    next(r for r in report.results if r.id == criterion).status = status
    assert f"{criterion}: {status}" in problems(simulated_report)


@pytest.mark.parametrize("change", ["omit", "duplicate", "unknown"])
def test_manifest_cannot_silently_shrink(simulated_report, change):
    report = simulated_report[0]
    if change == "omit":
        report.results.pop()
    elif change == "duplicate":
        report.results.append(deepcopy(report.results[0]))
    else:
        report.results[0].id = "unreviewed-criterion"
    assert any("criterion matrix" in p for p in problems(simulated_report))


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("definition_hash", "changed", "scenario definition changed"),
        ("manifest_hash", "changed", "accepted manifest changed"),
        (
            "versions",
            {"engine": "wrong"},
            "actual engine/worker/harness revision mismatch",
        ),
        ("spend_usd", None, "spend unknown or exceeded"),
        ("spend_usd", float("nan"), "spend unknown or exceeded"),
        ("spend_usd", 100.0, "spend unknown or exceeded"),
        ("policy_hash", None, "actual accepted policy provenance missing"),
        ("interventions_complete", False, "intervention accounting incomplete"),
        ("cleanup_inventory", [], "cleanup inventory incomplete"),
    ],
)
def test_report_rejects_missing_provenance(simulated_report, field, value, expected):
    setattr(simulated_report[0], field, value)
    assert expected in problems(simulated_report)


def test_omitted_control_proof_fails(simulated_report):
    row = next(r for r in simulated_report[0].results if r.id == "A6-4.halt-stop")
    del row.evidence["worker"]
    assert "A6-4.halt-stop: mandatory evidence missing" in problems(simulated_report)


@pytest.mark.parametrize("attack", ["missing", "changed", "escape", "symlink", "stale"])
def test_evidence_must_be_current_hashed_local_file(simulated_report, attack, tmp_path):
    report, inventory, _ = simulated_report
    artifact = next(iter(report.results[0].evidence.values()))
    path = inventory.path.parent / artifact.path
    if attack == "missing":
        path.unlink()
    elif attack == "changed":
        path.write_text("changed")
    elif attack == "escape":
        artifact.path = "../other.json"
    elif attack == "symlink":
        original = path.read_text()
        path.unlink()
        other = tmp_path / "outside.json"
        other.write_text(original)
        path.symlink_to(other)
    else:
        artifact.observed_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert len(problems(simulated_report)) > 1


@pytest.mark.parametrize(
    "kind,target,reason",
    [
        ("coordinator_retrigger", "run", "unattended completion violated"),
        ("unplanned", "run", "unattended completion violated"),
        ("planned_gate", "different", "unplanned human gate"),
    ],
)
def test_interventions_are_not_hidden(simulated_report, kind, target, reason):
    report = simulated_report[0]
    artifact = next(iter(report.results[0].evidence.values()))
    report.interventions.append(
        Intervention(
            at=artifact.observed_at,
            actor="offline-actor",
            kind=kind,
            target=target,
            evidence=artifact,
        )
    )
    assert reason in problems(simulated_report)


def test_machine_and_human_artifacts_are_consistent(simulated_report):
    report, inventory, config = simulated_report
    write_report(report, config=config, inventory=inventory)
    stored = json.loads((inventory.path.parent / "report.json").read_text())
    assert len(stored["results"]) == len(CRITERIA)
    assert all(
        r["id"] in (inventory.path.parent / "summary.md").read_text()
        for r in stored["results"]
    )


@pytest.fixture
def bound_provenance(simulated_report, monkeypatch):
    from types import SimpleNamespace
    from tests.e2e.orchestration.scenarios import manifest
    from tests.e2e.orchestration.test_scenarios import reviewed_rows

    report, _, config = simulated_report
    report.pull_requests = reviewed_rows()
    manifest_value = SimpleNamespace(
        required_checks=["fixture-unittests"],
        deployment_workflows=[".github/workflows/fixture.yml"],
    )
    monkeypatch.setattr(
        manifest, "load_manifest", lambda config: (manifest_value, "a" * 64)
    )
    for row in report.pull_requests:
        row["pr"]["base"] = {"repo": {"full_name": config.repository}}
        revision = row["pr"]["merge_commit_sha"]
        report.deployments.append(
            {
                "source_revision": revision,
                "runtime": {
                    "actual_revision": revision,
                    "account_id": config.expected_account_id,
                    "scope": "ready deployed replicas",
                    "ready_replicas": 1,
                    "pods": ["observed-pod-uid"],
                    "digest": "sha256:" + "a" * 64,
                },
                "workflows": [
                    {
                        "path": ".github/workflows/fixture.yml",
                        "head_sha": revision,
                        "conclusion": "success",
                    }
                ],
            }
        )
    return simulated_report


def test_consistent_provenance_still_does_not_qualify_a_simulation(bound_provenance):
    assert problems(bound_provenance) == ["non-live simulation"]


@pytest.mark.parametrize(
    "attack",
    ["head", "merge", "runtime", "account", "workflow", "approval", "missing-deploy"],
)
def test_report_rejects_inconsistent_delivery_provenance(bound_provenance, attack):
    report = bound_provenance[0]
    if attack == "head":
        report.pull_requests[0]["pr"]["head"]["sha"] = "f" * 40
    elif attack == "merge":
        report.pull_requests[0]["pr"]["merge_commit_sha"] = "f" * 40
    elif attack == "runtime":
        report.deployments[0]["runtime"]["actual_revision"] = "f" * 40
    elif attack == "account":
        report.deployments[0]["runtime"]["account_id"] = "999988887777"
    elif attack == "workflow":
        report.deployments[0]["workflows"][0]["conclusion"] = "failure"
    elif attack == "approval":
        report.pull_requests[0]["reviews"] = []
    else:
        report.deployments.pop()
    assert (
        "PR/head/review/merge/deployment provenance incomplete or inconsistent"
        in problems(bound_provenance)
    )

"""Recorded evidence must not become PASS when Python removes assertions."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify_v4_evidence.py"
PRIVATE_SENTINEL = "synthetic-private-report-detail-do-not-echo"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True))


def fixture_directory(directory, mutation=None):
    payload = {"finding": "synthetic accepted report"}
    content = json.dumps(payload, sort_keys=True).encode()
    (directory / "held-completion").mkdir(parents=True)
    artifact = directory / "held-completion/artifact-fixture.json"
    artifact.write_bytes(content)
    evidence = directory / "evidence.txt"
    evidence.write_text("synthetic retained observation")
    report = {
        "overall_outcome": "PASS",
        "criteria": {
            f"V4-{i:02d}": {"outcome": "PASS", "evidence": ["evidence.txt"]}
            for i in range(1, 8)
        },
    }
    observation = {
        "snapshot": {
            "status": "completed",
            "queue_ack_status": "confirmed",
            "result": {"report": payload},
        },
        "artifacts": [
            {
                "artifact_id": "artifact-fixture",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    progress = {
        "measurements": [
            {
                "type": "progress.updated",
                "conservative_upper_seconds": 1,
                "message": message,
            }
            for message in ("first", "second")
        ],
        "terminal_observed": False,
        "release_gate_present": False,
    }
    if mutation == "missing_criterion":
        del report["criteria"]["V4-07"]
    elif mutation == "overall_failure":
        report["overall_outcome"] = "FAIL"
    elif mutation == "criterion_failure":
        report["criteria"]["V4-01"].update(
            outcome="FAIL", private_detail=PRIVATE_SENTINEL
        )
    elif mutation == "empty_evidence":
        report["criteria"]["V4-01"]["evidence"] = []
    elif mutation == "missing_evidence_file":
        report["criteria"]["V4-01"]["evidence"] = ["absent.txt"]
    elif mutation == "incomplete_task":
        observation["snapshot"]["status"] = "running"
    elif mutation == "unconfirmed_ack":
        observation["snapshot"]["queue_ack_status"] = "pending"
    elif mutation == "wrong_artifact_size":
        observation["artifacts"][0]["bytes"] += 1
    elif mutation == "wrong_artifact_digest":
        observation["artifacts"][0]["sha256"] = "0" * 64
    elif mutation == "different_report":
        observation["snapshot"]["result"]["report"] = {"different": "report"}
    elif mutation == "duplicate_progress":
        progress["measurements"][1]["message"] = "first"
    elif mutation == "terminal_before_release":
        progress["terminal_observed"] = True
    elif mutation == "release_gate_present":
        progress["release_gate_present"] = True
    write_json(directory / "v4-criterion-report.json", report)
    write_json(directory / "held-completion/final-observation.json", observation)
    write_json(directory / "held-completion/held-progress-proof.json", progress)
    manifest = {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }
    if mutation == "manifest_digest":
        manifest["evidence.txt"] = "0" * 64
    write_json(directory / "sha256-manifest.json", manifest)


def run_verifier(directory, optimization):
    return subprocess.run(
        [sys.executable, *optimization, str(SCRIPT), "--directory", str(directory)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("optimization", [[], ["-O"], ["-OO"]])
def test_valid_recorded_evidence_passes(tmp_path, optimization):
    fixture_directory(tmp_path)
    result = run_verifier(tmp_path, optimization)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "outcome": "PASS",
        "mandatory_criteria": 7,
        "hashed_files": 5,
        "lane": "recorded-evidence-integrity-only",
    }


@pytest.mark.parametrize("optimization", [[], ["-O"], ["-OO"]])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_criterion",
        "overall_failure",
        "criterion_failure",
        "empty_evidence",
        "missing_evidence_file",
        "manifest_digest",
        "incomplete_task",
        "unconfirmed_ack",
        "wrong_artifact_size",
        "wrong_artifact_digest",
        "different_report",
        "duplicate_progress",
        "terminal_before_release",
        "release_gate_present",
    ],
)
def test_tampered_evidence_never_reports_pass(tmp_path, optimization, mutation):
    fixture_directory(tmp_path, mutation)
    result = run_verifier(tmp_path, optimization)
    assert result.returncode != 0, "tampered recorded evidence was accepted"
    assert '"outcome": "PASS"' not in result.stdout
    assert PRIVATE_SENTINEL not in result.stdout + result.stderr

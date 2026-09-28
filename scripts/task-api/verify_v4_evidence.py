#!/usr/bin/env python3
"""Verify recorded V4 evidence integrity; does not execute live qualification."""

import argparse
import hashlib
import json
from pathlib import Path


def _require(condition: bool, message: str) -> None:
    """Enforce receipt integrity even when Python omits assert statements."""
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    report = json.loads((directory / "v4-criterion-report.json").read_text())
    expected = {f"V4-{i:02d}" for i in range(1, 8)}
    _require(set(report["criteria"]) == expected, "All seven mandatory IDs required")
    _require(report["overall_outcome"] == "PASS", "Live matrix remains incomplete")
    for criterion in report["criteria"].values():
        _require(criterion["outcome"] == "PASS", "Criterion outcome must be PASS")
        _require(criterion["evidence"], "Missing evidence")
        for name in criterion["evidence"]:
            _require(
                (directory / name).is_file(), "Referenced evidence file is missing"
            )
    hashes = json.loads((directory / "sha256-manifest.json").read_text())
    for name, digest in hashes.items():
        _require(
            hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest,
            "Evidence file digest does not match manifest",
        )
    observation = json.loads(
        (directory / "held-completion/final-observation.json").read_text()
    )
    _require(
        observation["snapshot"]["status"] == "completed",
        "Task snapshot must be completed",
    )
    _require(
        observation["snapshot"]["queue_ack_status"] == "confirmed",
        "Task queue acknowledgement must be confirmed",
    )
    for artifact in observation["artifacts"]:
        content = (
            directory / "held-completion" / (artifact["artifact_id"] + ".json")
        ).read_bytes()
        _require(
            len(content) == artifact["bytes"],
            "Artifact byte length does not match its receipt",
        )
        _require(
            hashlib.sha256(content).hexdigest() == artifact["sha256"],
            "Artifact digest does not match its receipt",
        )
        _require(
            json.loads(content) == observation["snapshot"]["result"]["report"],
            "Artifact content does not match the completed report",
        )
    progress = json.loads(
        (directory / "held-completion/held-progress-proof.json").read_text()
    )
    timely = [
        entry
        for entry in progress["measurements"]
        if entry["type"] == "progress.updated"
        and entry["conservative_upper_seconds"] < 5
    ]
    _require(
        len({entry["message"] for entry in timely}) >= 2,
        "At least two distinct timely progress updates required",
    )
    _require(
        not progress["terminal_observed"] and (not progress["release_gate_present"]),
        "Terminal progress or a release gate invalidates held progress evidence",
    )
    print(
        json.dumps(
            {
                "outcome": "PASS",
                "mandatory_criteria": 7,
                "hashed_files": len(hashes),
                "lane": "recorded-evidence-integrity-only",
            }
        )
    )


if __name__ == "__main__":
    main()

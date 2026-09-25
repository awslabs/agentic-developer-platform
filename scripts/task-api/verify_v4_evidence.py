#!/usr/bin/env python3
"""Verify recorded V4 evidence integrity; does not execute live qualification."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    report = json.loads((directory / "v4-criterion-report.json").read_text())
    expected = {f"V4-{i:02d}" for i in range(1, 8)}
    assert set(report["criteria"]) == expected, "All seven mandatory IDs required"
    assert report["overall_outcome"] == "PASS", "Live matrix remains incomplete"
    for criterion in report["criteria"].values():
        assert criterion["outcome"] == "PASS", criterion
        assert criterion["evidence"], "Missing evidence"
        for name in criterion["evidence"]:
            assert (directory / name).is_file(), f"Missing evidence: {name}"
    hashes = json.loads((directory / "sha256-manifest.json").read_text())
    for name, digest in hashes.items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest, (
            name
        )
    observation = json.loads(
        (directory / "held-completion/final-observation.json").read_text()
    )
    assert observation["snapshot"]["status"] == "completed"
    assert observation["snapshot"]["queue_ack_status"] == "confirmed"
    for artifact in observation["artifacts"]:
        content = (
            directory / "held-completion" / (artifact["artifact_id"] + ".json")
        ).read_bytes()
        assert len(content) == artifact["bytes"]
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        assert json.loads(content) == observation["snapshot"]["result"]["report"]
    progress = json.loads(
        (directory / "held-completion/held-progress-proof.json").read_text()
    )
    timely = [
        entry
        for entry in progress["measurements"]
        if entry["type"] == "progress.updated"
        and entry["conservative_upper_seconds"] < 5
    ]
    assert len({entry["message"] for entry in timely}) >= 2
    assert not progress["terminal_observed"] and not progress["release_gate_present"]
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

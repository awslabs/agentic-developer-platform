"""Synthetic candidate/review data; no private evidence is checked in."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from review_security_occurrences import review_candidate

SCRIPT = Path(__file__).parent.parent / "review_security_occurrences.py"
IMAGE = "sha256:" + "f" * 64
HASH = "e" * 64


def entry(index, path, advisory="CVE-1", severity="critical", suppressed=False):
    return {
        "id": f"{'a' * 64}:0:{index}",
        "advisory": advisory,
        "severity": severity,
        "paths": [path],
        "accepted_suppression": suppressed,
    }


def inventory(rows, digest="a" * 64):
    return {"report_sha256": digest, "occurrences": rows, "accepted_suppression_count": 1}


def evidence(tmp_path):
    path = tmp_path / "review.txt"
    path.write_text("verified synthetic fixture\n")
    return [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]


def fixed(row, proof):
    return {
        "status": "fixed", "candidate_image": IMAGE, "advisory": row["advisory"],
        "paths": row["paths"], "installed_paths": row["paths"],
        "package": "curl", "installed_version": "8.22.0-0+adp1",
        "reviewer": "fixture reviewer",
        "binary_sha256": HASH, "source_or_patch_sha256": HASH, "evidence": proof,
    }


def test_repeated_occurrences_remain_open_without_individual_review(tmp_path):
    first, second = entry(0, "/usr/bin/curl"), entry(1, "/usr/bin/curl")
    ignored = entry(2, "/usr/bin/curl", suppressed=True)
    result = review_candidate(
        inventory([first, second, ignored]), inventory([second]),
        {first["id"]: fixed(first, evidence(tmp_path))}, IMAGE, tmp_path,
    )
    assert result["assigned_count"] == 2
    assert [row["status"] for row in result["dispositions"]] == ["fixed", "unresolved"]
    assert result["new_critical_high"] == []
    assert result["complete"] is False


def test_no_finding_does_not_auto_close_and_no_blanket_reviews(tmp_path):
    row = entry(0, "/opt/venv/lib/jwt")
    result = review_candidate(inventory([row]), inventory([]), {}, IMAGE, tmp_path)
    assert result["dispositions"][0]["status"] == "unresolved"
    with pytest.raises(ValueError, match="unassigned"):
        review_candidate(inventory([row]), inventory([]), {"*": fixed(row, evidence(tmp_path))}, IMAGE, tmp_path)


def test_review_requires_exact_candidate_and_verified_proof(tmp_path):
    row = entry(0, "/usr/bin/curl")
    proof = fixed(row, evidence(tmp_path))
    assert review_candidate(inventory([row]), inventory([]), {row["id"]: proof}, IMAGE, tmp_path)["complete"]
    for change in (
        {"candidate_image": "sha256:" + "0" * 64},
        {"paths": ["/different"]},
        {"source_or_patch_sha256": ""},
        {"package": ""},
        {"installed_paths": ["/different"]},
        {"evidence": [{"path": "review.txt", "sha256": "0" * 64}]},
    ):
        with pytest.raises(ValueError):
            review_candidate(inventory([row]), inventory([]), {row["id"]: proof | change}, IMAGE, tmp_path)
    not_applicable = proof | {"status": "evidence-reviewed-not-applicable", "reason": "synthetic absent package"}
    assert review_candidate(inventory([row]), inventory([]), {row["id"]: not_applicable}, IMAGE, tmp_path)["complete"]
    with pytest.raises(ValueError, match="reason"):
        review_candidate(inventory([row]), inventory([]), {row["id"]: not_applicable | {"reason": ""}}, IMAGE, tmp_path)


def test_new_critical_high_regressions_are_reported_even_with_suppressions(tmp_path):
    old = entry(0, "/usr/bin/curl")
    new = entry(0, "/usr/bin/curl", severity="high")
    ignored = entry(1, "/usr/bin/ignored", suppressed=True)
    result = review_candidate(
        inventory([old]), inventory([new, ignored]),
        {old["id"]: fixed(old, evidence(tmp_path))}, IMAGE, tmp_path,
    )
    assert result["new_critical_high"] == [new]
    assert not result["complete"]


def test_cli_retains_incomplete_report_and_exits_nonzero(tmp_path):
    baseline, candidate, reviewed = (tmp_path / name for name in ("old.json", "new.json", "proof.json"))
    baseline.write_text(json.dumps(inventory([entry(0, "/usr/bin/curl")])))
    candidate.write_text(json.dumps(inventory([])))
    reviewed.write_text("{}")
    output = tmp_path / "dispositions.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(baseline), str(candidate), str(reviewed),
         "--image-digest", IMAGE, "--output", str(output)], capture_output=True, text=True,
    )
    assert completed.returncode == 1
    assert json.loads(output.read_text())["complete"] is False

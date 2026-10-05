"""Synthetic occurrence fixtures; no private scan inputs are committed."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from inventory_security_occurrences import inventory

SCRIPT = Path(__file__).parent.parent / "inventory_security_occurrences.py"


def sample_report():
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {
                                "id": "CVE-ONE",
                                "properties": {"security-severity": "7.8"},
                                "help": {"text": "Package: curl\nSeverity: Critical"},
                            },
                            {"id": "CVE-TWO"},
                        ]
                    }
                },
                "results": [
                    {
                        "ruleId": "CVE-ONE",
                        "locations": [
                            {"physicalLocation": {"artifactLocation": {"uri": "usr/bin/curl"}}},
                            {"physicalLocation": {"artifactLocation": {"uri": "usr/lib/libcurl.so"}}},
                        ],
                    },
                    {
                        "ruleId": "CVE-ONE",
                        "locations": [
                            {"physicalLocation": {"artifactLocation": {"uri": "usr/bin/curl"}}}
                        ],
                    },
                    {
                        "ruleId": "CVE-ONE",
                        "suppressions": [{"status": "accepted"}],
                    },
                    {
                        "ruleId": "CVE-TWO",
                        "level": "error",
                        "suppressions": [{"status": "underReview"}],
                    },
                ],
            }
        ],
    }


def test_preserves_repeated_results_and_all_paths():
    result = inventory(sample_report(), "a" * 64)
    assert result["active_counts"] == {"critical": 2, "unrated": 1}
    assert result["accepted_suppression_count"] == 1
    rows = result["occurrences"]
    assert len(rows) == 4
    assert len({row["id"] for row in rows}) == 4
    assert rows[0]["paths"] == ["usr/bin/curl", "usr/lib/libcurl.so"]
    assert rows[0]["severity_source"] == "native"
    assert rows[0]["rule_help"] == "Package: curl\nSeverity: Critical"
    assert rows[2]["accepted_suppression"] is True
    assert rows[3]["severity"] == "unrated"
    assert rows[3]["severity_source"] == "default-level"


def test_refuses_missing_rules_and_invalid_results():
    report = sample_report()
    report["runs"][0]["tool"]["driver"]["rules"] = []
    with pytest.raises(ValueError, match="matching rule"):
        inventory(report, "a" * 64)
    report = sample_report()
    report["runs"][0]["results"] = None
    with pytest.raises(ValueError, match="results list"):
        inventory(report, "a" * 64)


def test_cli_checks_hash_and_counts_before_output(tmp_path):
    raw = json.dumps(sample_report()).encode()
    report = tmp_path / "scan.sarif"
    output = tmp_path / "inventory.json"
    report.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    def invoke(sha, critical):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(report),
                "--output", str(output),
                "--expected-sha256", sha,
                "--expected-critical", str(critical),
                "--expected-high", "0",
            ],
            capture_output=True,
            text=True,
        )

    assert invoke("0" * 64, 2).returncode != 0
    assert not output.exists()
    assert invoke(digest, 3).returncode != 0
    assert not output.exists()
    completed = invoke(digest, 2)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text())["report_sha256"] == digest


@pytest.mark.parametrize(
    "tamper",
    [None, "report", "revision", "digest", "duplicate", "raw", "summary", "metadata", "provenance", "missing"],
)
def test_cli_binds_assigned_report_to_coverage_and_companions(tmp_path, tamper):
    target = "modules-agent-context-images-deepwiki"
    revision = "a" * 40
    image_digest = "sha256:" + "b" * 64
    report = tmp_path / f"{target}.sarif"
    report.write_text(json.dumps(sample_report()))
    report_sha256 = hashlib.sha256(report.read_bytes()).hexdigest()
    checksums = {}
    for field, suffix in (
        ("raw_artifact_sha256", ".raw.sarif"),
        ("suppression_summary_sha256", ".suppression-summary.json"),
        ("scanner_metadata_sha256", ".scanner-metadata.json"),
    ):
        companion = tmp_path / f"{target}{suffix}"
        companion.write_text(f"synthetic {field}")
        checksums[field] = hashlib.sha256(companion.read_bytes()).hexdigest()

    entry = {
        "name": target,
        "status": "succeeded",
        "digest": image_digest,
        "artifact_sha256": report_sha256,
        "build_args": {"PYTHON_IMAGE": "python@sha256:" + "c" * 64},
        **checksums,
    }
    coverage = {
        "tool": "grype", "commit": revision, "expected": 1, "succeeded": 1,
        "targets": [entry],
    }
    provenance = {
        "artifact_sha256": report_sha256,
        "digest": image_digest,
        "name": target,
        "source_revision": revision,
        "tool": "grype",
        "build_args": entry["build_args"],
        **checksums,
    }
    if tamper == "report":
        entry["artifact_sha256"] = "0" * 64
    elif tamper == "revision":
        coverage["commit"] = "0" * 40
    elif tamper == "digest":
        entry["digest"] = "sha256:invalid"
    elif tamper == "duplicate":
        coverage["targets"].append(entry.copy())
        coverage["expected"] = coverage["succeeded"] = 2
    elif tamper in ("raw", "summary", "metadata"):
        suffix = {"raw": ".raw.sarif", "summary": ".suppression-summary.json", "metadata": ".scanner-metadata.json"}[tamper]
        (tmp_path / f"{target}{suffix}").write_text("tampered")
    elif tamper == "provenance":
        provenance["digest"] = "sha256:" + "0" * 64
    elif tamper == "missing":
        (tmp_path / f"{target}.raw.sarif").unlink()

    coverage_path = tmp_path / "coverage.json"
    provenance_path = tmp_path / "provenance.json"
    output = tmp_path / "inventory.json"
    coverage_path.write_text(json.dumps(coverage))
    provenance_path.write_text(json.dumps(provenance))
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(report), "--output", str(output),
         "--expected-sha256", report_sha256, "--expected-critical", "2", "--expected-high", "0",
         "--coverage", str(coverage_path), "--provenance", str(provenance_path),
         "--source-revision", revision, "--target", target],
        capture_output=True, text=True,
    )
    if tamper:
        assert completed.returncode != 0
        assert not output.exists()
    else:
        assert completed.returncode == 0, completed.stderr
        receipt = json.loads(output.read_text())["scan_provenance"]
        assert receipt["image_digest"] == image_digest
        assert receipt["companion_sha256"] == checksums

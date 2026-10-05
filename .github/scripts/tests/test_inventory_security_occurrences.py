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

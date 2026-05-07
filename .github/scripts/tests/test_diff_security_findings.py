"""Tests for diff-security-findings.py."""

import json
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from diff_security_findings import (
    diff_findings,
    extract_json_fingerprints,
    extract_sarif_fingerprints,
    load_json_safe,
    process_tool_findings,
)


def test_diff_findings_new_only():
    """Identifies new findings when baseline is empty."""
    current = {"rule1:file.py:10", "rule2:file.py:20"}
    baseline = set()
    result = diff_findings(current, baseline)
    assert result["new_count"] == 2
    assert result["resolved_count"] == 0
    assert result["stable_count"] == 0


def test_diff_findings_resolved():
    """Identifies resolved findings."""
    current = {"rule1:file.py:10"}
    baseline = {"rule1:file.py:10", "rule2:file.py:20"}
    result = diff_findings(current, baseline)
    assert result["new_count"] == 0
    assert result["resolved_count"] == 1
    assert result["stable_count"] == 1


def test_diff_findings_mixed():
    """Handles mix of new, resolved, and stable."""
    current = {"a", "b", "c"}
    baseline = {"b", "c", "d"}
    result = diff_findings(current, baseline)
    assert result["new_count"] == 1  # a
    assert result["resolved_count"] == 1  # d
    assert result["stable_count"] == 2  # b, c


def test_diff_findings_empty_both():
    """Empty baseline and empty current produces zero counts."""
    result = diff_findings(set(), set())
    assert result["new_count"] == 0
    assert result["resolved_count"] == 0
    assert result["stable_count"] == 0


def test_extract_sarif_fingerprints():
    """Extracts fingerprints from SARIF data."""
    sarif = {
        "runs": [
            {
                "results": [
                    {
                        "ruleId": "CKV_AWS_18",
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "main.tf"},
                                    "region": {"startLine": 42},
                                }
                            }
                        ],
                    }
                ]
            }
        ]
    }
    fps = extract_sarif_fingerprints(sarif)
    assert "CKV_AWS_18:main.tf:42" in fps
    assert len(fps) == 1


def test_extract_sarif_fingerprints_empty():
    """Returns empty set for empty SARIF."""
    assert extract_sarif_fingerprints({}) == set()
    assert extract_sarif_fingerprints({"runs": []}) == set()


def test_extract_json_fingerprints_npm_format():
    """Handles npm audit vulnerability format."""
    data = {
        "vulnerabilities": {
            "lodash": {"severity": "high"},
            "express": {"severity": "critical"},
        }
    }
    fps = extract_json_fingerprints(data)
    assert len(fps) == 2
    assert "lodash:high" in fps
    assert "express:critical" in fps


def test_extract_json_fingerprints_list_format():
    """Handles list-based findings format."""
    data = [{"rule": "W28", "file": "template.yaml"}]
    fps = extract_json_fingerprints(data)
    assert len(fps) == 1


def test_load_json_safe_missing_file():
    """Returns empty dict for missing file."""
    result = load_json_safe(Path("/nonexistent/path.json"))
    assert result == {}


def test_load_json_safe_empty_file():
    """Returns empty dict for empty file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("")
        f.flush()
        result = load_json_safe(Path(f.name))
    assert result == {}


def test_load_json_safe_valid():
    """Loads valid JSON correctly."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"key": "value"}, f)
        f.flush()
        result = load_json_safe(Path(f.name))
    assert result == {"key": "value"}


def test_process_tool_no_findings():
    """Returns zero counts when no findings files exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        findings = Path(tmpdir) / "findings"
        findings.mkdir()
        baseline = Path(tmpdir) / "baseline"
        baseline.mkdir()
        # Create empty baseline
        (baseline / "checkov-baseline.json").write_text("{}")

        result = process_tool_findings("checkov", findings, baseline)
        assert result["new_count"] == 0
        assert result["resolved_count"] == 0

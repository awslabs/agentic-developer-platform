"""Tests for diff-security-findings.py."""

import json
import subprocess
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from diff_security_findings import (
    _sarif_rule_index,
    diff_findings,
    extract_detect_secrets_fingerprints,
    extract_json_fingerprints,
    extract_sarif_fingerprints,
    load_json_safe,
    process_tool_findings,
    resolve_sarif_severity,
)

SCRIPT_PATH = Path(__file__).parent.parent / "diff_security_findings.py"


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


def test_extract_sarif_skips_only_explicitly_accepted_suppressions():
    results = []
    for status in ("accepted", "rejected", "underReview", None):
        suppression = {"kind": "inSource"}
        if status is not None:
            suppression["status"] = status
        results.append(
            {
                "ruleId": status or "missing",
                "suppressions": [suppression],
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "main.py"},
                            "region": {"startLine": 1},
                        }
                    }
                ],
            }
        )

    fingerprints = extract_sarif_fingerprints({"runs": [{"results": results}]})

    assert fingerprints == {
        "rejected:main.py:1",
        "underReview:main.py:1",
        "missing:main.py:1",
    }


def test_locationless_sarif_uses_provided_fingerprints_stably():
    result = {
        "ruleId": "R",
        "fingerprints": {"stable/v1": "abc"},
        "partialFingerprints": {"primaryLocationLineHash": "def"},
        "level": "error",
    }
    severities = {}
    sources = {}

    first = extract_sarif_fingerprints(
        {"runs": [{"results": [result]}]}, severities, sources
    )
    reordered = extract_sarif_fingerprints(
        {
            "runs": [
                {
                    "results": [
                        {
                            "partialFingerprints": {
                                "primaryLocationLineHash": "def"
                            },
                            "fingerprints": {"stable/v1": "abc"},
                            "ruleId": "R",
                        }
                    ]
                }
            ]
        }
    )

    assert first == reordered
    assert len(first) == 1
    fingerprint = next(iter(first))
    assert fingerprint.startswith("R:locationless:")
    assert severities[fingerprint] == "unrated"
    assert sources[fingerprint] == "default-level"


def test_locationless_sarif_without_provided_fingerprint_is_not_dropped():
    first = {"ruleId": "R", "message": {"text": "first finding"}}
    second = {"ruleId": "R", "message": {"text": "second finding"}}

    fingerprints = extract_sarif_fingerprints(
        {"runs": [{"results": [first, second]}]}
    )

    assert len(fingerprints) == 2
    assert all(fp.startswith("R:locationless:") for fp in fingerprints)


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


def test_extract_detect_secrets_scan_and_audit_match_without_false_positives():
    """The raw scan and grouped audit schemas reconcile to the same candidates."""
    scan = {
        "results": {
            "app.py": [
                {"type": "AWS Access Key", "line_number": 7},
                {"type": "Secret Keyword", "line_number": 7},
                {"type": "Basic Auth Credentials", "line_number": 9, "is_secret": False},
            ]
        }
    }
    audit = {
        "results": [
            {
                "category": "UNVERIFIED",
                "filename": "app.py",
                "lines": {"7": "redacted from the private report"},
                "types": ["AWS Access Key", "Secret Keyword"],
            },
            {
                "category": "FALSE_POSITIVE",
                "filename": "app.py",
                "lines": {"9": "redacted from the private report"},
                "types": ["Basic Auth Credentials"],
            },
        ]
    }
    expected = {"AWS Access Key:app.py:7", "Secret Keyword:app.py:7"}

    severities: dict = {}
    sources: dict = {}
    assert extract_detect_secrets_fingerprints(scan, severities, sources) == expected
    assert extract_detect_secrets_fingerprints(audit) == expected
    assert set(severities.values()) == {"unrated"}
    assert set(sources.values()) == {"tool-unrated"}


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


# ---------------------------------------------------------------------------
# Severity resolution (S19, #5618)
#
# Two distinct failure modes are covered here:
#   1. Preferring a raw CVSS number over the scanner's own rating, which
#      reported low-rated CVEs as high and failed the gate on them.
#   2. Treating a SARIF `level` (a format default, usually "error" for every
#      result a tool emits) as an explicit high severity, which promoted whole
#      tools' output into the gate.
# The fix must narrow what may be called high WITHOUT dropping anything, so
# each test also asserts the finding is still counted/reported.
# ---------------------------------------------------------------------------


def _sarif(rule: dict, result: dict) -> dict:
    """Build a one-result SARIF document around a rule/result pair."""
    return {
        "runs": [
            {
                "tool": {"driver": {"rules": [rule]}},
                "results": [result],
            }
        ]
    }


def _severity_of(rule: dict, result: dict) -> tuple[str, str]:
    run = _sarif(rule, result)["runs"][0]
    return resolve_sarif_severity(run["results"][0], _sarif_rule_index(run))


def test_native_rating_wins_over_cvss_cve_2020_15778():
    """Native 'low' beats CVSS 7.8 — the real CVE-2020-15778 shape.

    The feed maintainer rates this low; the raw CVSS base score is 7.8. Reading
    the number first reported a low CVE as high and failed the gate on it.
    """
    rule = {
        "id": "CVE-2020-15778",
        "properties": {"security-severity": "7.8"},
        "help": {"text": "Severity: low\nscp in OpenSSH allows command injection"},
    }
    result = {"ruleId": "CVE-2020-15778", "level": "error"}
    sev, source = _severity_of(rule, result)
    assert sev == "low"
    assert source == "native"


def test_cvss_used_when_no_native_rating():
    """A numeric score is still honoured when the scanner published no rating."""
    rule = {"id": "CVE-9", "properties": {"security-severity": "9.4"}}
    sev, source = _severity_of(rule, {"ruleId": "CVE-9"})
    assert sev == "critical"
    assert source == "cvss"


def test_cvss_bands():
    """CVSS bands map onto the CVSS v3 qualitative ratings."""
    for score, expected in (
        ("9.0", "critical"), ("7.0", "high"), ("4.0", "medium"), ("3.9", "low"),
    ):
        rule = {"id": "R", "properties": {"security-severity": score}}
        assert _severity_of(rule, {"ruleId": "R"}) == (expected, "cvss")


def test_qualitative_security_severity_is_native():
    """A word-valued security-severity is a native rating, not a score."""
    rule = {"id": "R", "properties": {"security-severity": "Medium"}}
    assert _severity_of(rule, {"ruleId": "R"}) == ("medium", "native")


def test_bandit_issue_severity_is_native():
    """bandit's result-level issue_severity counts as a native rating."""
    result = {"ruleId": "B101", "properties": {"issue_severity": "HIGH"}}
    assert _severity_of({"id": "B101"}, result) == ("high", "native")


def test_sarif_error_level_is_not_promoted_to_high():
    """`level: error` alone is NOT high — it is most tools' default label."""
    sev, source = _severity_of({"id": "R"}, {"ruleId": "R", "level": "error"})
    assert sev == "unrated"
    assert source == "default-level"


def test_rule_default_configuration_level_is_not_a_rating():
    """A level inherited from the rule's defaultConfiguration is not a rating."""
    rule = {"id": "R", "defaultConfiguration": {"level": "error"}}
    assert _severity_of(rule, {"ruleId": "R"}) == ("unrated", "default-level")


def test_missing_level_is_unrated():
    """No level and no rating at all is unrated, never high."""
    assert _severity_of({"id": "R"}, {"ruleId": "R"}) == ("unrated", "default-level")


def test_unparseable_cvss_falls_back_to_unrated_not_high():
    """A malformed score does not silently become high via the level."""
    rule = {"id": "R", "properties": {"security-severity": "not-a-number"}}
    sev, source = _severity_of(rule, {"ruleId": "R", "level": "error"})
    assert sev == "unrated"
    assert source == "default-level"


def test_negligible_native_rating_preserved():
    """grype's 'negligible' is preserved rather than coerced into a gated band."""
    rule = {"id": "R", "help": {"text": "Severity: Negligible"}}
    assert _severity_of(rule, {"ruleId": "R"}) == ("negligible", "native")


def test_extract_sarif_populates_severity_sources():
    """Extraction records provenance alongside severity for every finding."""
    sarif = _sarif(
        {"id": "R", "properties": {"security-severity": "8.1"}},
        {
            "ruleId": "R",
            "locations": [
                {"physicalLocation": {
                    "artifactLocation": {"uri": "a.py"},
                    "region": {"startLine": 5},
                }}
            ],
        },
    )
    severities: dict = {}
    sources: dict = {}
    fps = extract_sarif_fingerprints(sarif, severities, sources)
    assert fps == {"R:a.py:5"}
    assert severities["R:a.py:5"] == "high"
    assert sources["R:a.py:5"] == "cvss"


def _write_sarif_findings(tmpdir: str, sarif: dict) -> tuple[Path, Path]:
    findings = Path(tmpdir) / "findings" / "semgrep"
    findings.mkdir(parents=True)
    (findings / "semgrep.sarif").write_text(json.dumps(sarif))
    baseline = Path(tmpdir) / "baseline"
    baseline.mkdir()
    return Path(tmpdir) / "findings", baseline


def test_unrated_findings_are_still_counted_and_reported():
    """An unrated finding stays in the summary — narrowing must not drop it."""
    sarif = _sarif(
        {"id": "R"},
        {
            "ruleId": "R",
            "level": "error",
            "locations": [
                {"physicalLocation": {
                    "artifactLocation": {"uri": "x.py"},
                    "region": {"startLine": 1},
                }}
            ],
        },
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        findings, baseline = _write_sarif_findings(tmpdir, sarif)
        res = process_tool_findings("semgrep", findings, baseline)

    # Still a finding: counted, listed, and flagged as needing manual triage.
    assert res["new_count"] == 1
    assert "R:x.py:1" in res["new"]
    assert res["new_severities"]["R:x.py:1"] == "unrated"
    assert res["new_severity_sources"]["R:x.py:1"] == "default-level"
    assert res["new_unrated_count"] == 1


def test_unrated_fingerprint_stays_private_and_stdout_is_aggregate_only(tmp_path):
    """Logs report triage counts without exposing rule ids, paths, or lines."""
    sarif = _sarif(
        {"id": "PRIVATE_RULE"},
        {
            "ruleId": "PRIVATE_RULE",
            "level": "error",
            "locations": [
                {"physicalLocation": {
                    "artifactLocation": {"uri": "private/module.py"},
                    "region": {"startLine": 73},
                }}
            ],
        },
    )
    findings = tmp_path / "findings" / "semgrep"
    findings.mkdir(parents=True)
    (findings / "semgrep.sarif").write_text(json.dumps(sarif))
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    output = tmp_path / "summary.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--findings-dir", str(tmp_path / "findings"),
            "--baseline-dir", str(baseline),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "semgrep: 1 unrated finding(s)" in proc.stdout
    assert "PRIVATE_RULE" not in proc.stdout
    assert "private/module.py" not in proc.stdout
    assert "PRIVATE_RULE:private/module.py:73" in output.read_text()


def test_baseline_update_uses_detect_secrets_scan_not_audit_report(tmp_path):
    """The audit schema must never replace the native scan-baseline schema."""
    findings = tmp_path / "findings" / "detect-secrets"
    findings.mkdir(parents=True)
    (findings / "detect-secrets-audit.json").write_text(
        json.dumps({"results": [{"category": "UNVERIFIED"}]})
    )
    scan = {
        "version": "1.5.0",
        "plugins_used": [{"name": "AWSKeyDetector"}],
        "results": {"app.py": [{"type": "AWS Access Key", "line_number": 4}]},
    }
    (findings / "detect-secrets-results.json").write_text(json.dumps(scan))
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / ".secrets.baseline").write_text(json.dumps({"results": {}}))

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--findings-dir", str(tmp_path / "findings"),
            "--baseline-dir", str(baseline),
            "--output", str(tmp_path / "summary.json"),
            "--update-baselines",
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads((baseline / ".secrets.baseline").read_text()) == scan


def test_native_low_finding_does_not_trip_critical_high_gate():
    """End-to-end: the CVE-2020-15778 shape is reported low and does not gate."""
    sarif = _sarif(
        {
            "id": "CVE-2020-15778",
            "properties": {"security-severity": "7.8"},
            "help": {"text": "Severity: low"},
        },
        {
            "ruleId": "CVE-2020-15778",
            "level": "error",
            "locations": [
                {"physicalLocation": {
                    "artifactLocation": {"uri": "img"},
                    "region": {"startLine": 0},
                }}
            ],
        },
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        findings, baseline = _write_sarif_findings(tmpdir, sarif)
        res = process_tool_findings("semgrep", findings, baseline)

    fp = "CVE-2020-15778:img:0"
    assert res["new_severities"][fp] == "low"
    assert res["new_severity_sources"][fp] == "native"
    # The gate fails on critical/high only; low must not appear there.
    assert not {s for s in res["new_severities"].values()} & {"critical", "high"}


def test_extract_json_fingerprints_records_validated_npm_native_severity():
    """npm's rating and its provenance are available to the hard gate."""
    severities = {}
    sources = {}
    fps = extract_json_fingerprints(
        {"vulnerabilities": {"lodash": {"severity": " HIGH "}}},
        severities,
        sources,
        "gateway-frontend",
    )

    assert fps == {"gateway-frontend:lodash:high"}
    assert severities == {"gateway-frontend:lodash:high": "high"}
    assert sources == {"gateway-frontend:lodash:high": "native"}


def _run_npm_gate(tmp_path: Path, vulnerabilities: dict) -> subprocess.CompletedProcess:
    findings = tmp_path / "findings" / "npm-audit"
    findings.mkdir(parents=True)
    (findings / "npm-audit-gateway-frontend.json").write_text(
        json.dumps({"vulnerabilities": vulnerabilities})
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    output = tmp_path / "summary.json"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--findings-dir", str(tmp_path / "findings"),
            "--baseline-dir", str(baseline),
            "--output", str(output),
            "--fail-on", "critical,high",
        ],
        capture_output=True,
        text=True,
    )


def test_new_npm_critical_and_high_findings_fail_gate(tmp_path):
    proc = _run_npm_gate(
        tmp_path,
        {
            "critical-package": {"severity": "critical"},
            "high-package": {"severity": "high"},
        },
    )

    assert proc.returncode == 1
    assert "npm-audit: 1 critical finding(s)" in proc.stdout
    assert "npm-audit: 1 high finding(s)" in proc.stdout
    summary = json.loads((tmp_path / "summary.json").read_text())["npm-audit"]
    assert set(summary["new_severities"].values()) == {"critical", "high"}
    assert set(summary["new_severity_sources"].values()) == {"native"}


def test_new_npm_medium_and_low_findings_do_not_fail_gate(tmp_path):
    proc = _run_npm_gate(
        tmp_path,
        {
            "medium-package": {"severity": "medium"},
            "low-package": {"severity": "low"},
        },
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Security gate PASSED" in proc.stdout
    summary = json.loads((tmp_path / "summary.json").read_text())["npm-audit"]
    assert set(summary["new_severities"].values()) == {"medium", "low"}
    assert summary["new_unrated_count"] == 0


def test_npm_findings_from_distinct_projects_do_not_collapse(tmp_path):
    findings = tmp_path / "findings" / "npm-audit"
    findings.mkdir(parents=True)
    report = json.dumps({"vulnerabilities": {"shared-package": {"severity": "high"}}})
    (findings / "npm-audit-gateway-frontend.json").write_text(report)
    (findings / "npm-audit-agent-factory-agent.json").write_text(report)
    baseline = tmp_path / "baseline"
    baseline.mkdir()

    result = process_tool_findings("npm-audit", tmp_path / "findings", baseline)

    assert result["new_count"] == 2
    assert result["new"] == [
        "agent-factory-agent:shared-package:high",
        "gateway-frontend:shared-package:high",
    ]
    assert set(result["new_severities"].values()) == {"high"}


def test_grype_findings_from_distinct_images_do_not_collapse(tmp_path):
    findings = tmp_path / "findings" / "grype"
    findings.mkdir(parents=True)
    report = _sarif(
        {
            "id": "CVE-EXAMPLE",
            "help": {"text": "Severity: high"},
        },
        {
            "ruleId": "CVE-EXAMPLE",
            "locations": [
                {"physicalLocation": {
                    "artifactLocation": {"uri": "/usr/lib/shared-package"},
                    "region": {"startLine": 0},
                }}
            ],
        },
    )
    for image in ("image-a", "image-b"):
        (findings / f"{image}.sarif").write_text(json.dumps(report))
    baseline = tmp_path / "baseline"
    baseline.mkdir()

    result = process_tool_findings("grype", tmp_path / "findings", baseline)

    expected = {
        "image-a:CVE-EXAMPLE:/usr/lib/shared-package:0",
        "image-b:CVE-EXAMPLE:/usr/lib/shared-package:0",
    }
    assert result["new_count"] == 2
    assert set(result["new"]) == expected
    assert result["new_severities"] == {fingerprint: "high" for fingerprint in expected}
    assert result["new_severity_sources"] == {
        fingerprint: "native" for fingerprint in expected
    }


def test_legacy_grype_baseline_matches_unambiguous_image_occurrence(tmp_path):
    findings = tmp_path / "findings" / "grype"
    findings.mkdir(parents=True)
    report = _sarif(
        {"id": "CVE-EXAMPLE"},
        {
            "ruleId": "CVE-EXAMPLE",
            "locations": [
                {"physicalLocation": {
                    "artifactLocation": {"uri": "/usr/lib/shared-package"},
                    "region": {"startLine": 0},
                }}
            ],
        },
    )
    (findings / "image-a.sarif").write_text(json.dumps(report))
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "grype-baseline.json").write_text(json.dumps(report))

    result = process_tool_findings("grype", tmp_path / "findings", baseline)

    assert result["new_count"] == 0
    assert result["stable_count"] == 1
    assert result["resolved_count"] == 0


def test_legacy_grype_baseline_does_not_hide_ambiguous_image_occurrences(tmp_path):
    findings = tmp_path / "findings" / "grype"
    findings.mkdir(parents=True)
    report = _sarif(
        {"id": "CVE-EXAMPLE"},
        {
            "ruleId": "CVE-EXAMPLE",
            "locations": [
                {"physicalLocation": {
                    "artifactLocation": {"uri": "/usr/lib/shared-package"},
                    "region": {"startLine": 0},
                }}
            ],
        },
    )
    for image in ("image-a", "image-b"):
        (findings / f"{image}.sarif").write_text(json.dumps(report))
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "grype-baseline.json").write_text(json.dumps(report))

    result = process_tool_findings("grype", tmp_path / "findings", baseline)

    assert result["new_count"] == 2
    assert result["stable_count"] == 0
    assert result["resolved_count"] == 1

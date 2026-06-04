"""Unit tests for shape.py — JSON evidence shape definitions."""

import json

from platform_deploy_mgmt.checks.shape import (
    CheckResult,
    CostClass,
    PhaseEvidence,
    Result,
    RunMetadata,
    RunTarget,
    Severity,
    now_iso,
)


class TestEnums:
    """Verify enum values match the spec."""

    def test_severity_values(self):
        assert Severity.HARD.value == "hard"
        assert Severity.SOFT.value == "soft"

    def test_cost_class_values(self):
        assert CostClass.CHEAP.value == "cheap"
        assert CostClass.EXPENSIVE.value == "expensive"

    def test_result_values(self):
        assert Result.PASS.value == "pass"
        assert Result.FAIL.value == "fail"
        assert Result.SKIP.value == "skip"

    def test_enums_are_string_enums(self):
        """Enums should serialize to their string values."""
        assert str(Severity.HARD) == "Severity.HARD"
        assert Severity.HARD == "hard"
        assert Result.PASS == "pass"


class TestCheckResult:
    """Verify CheckResult dataclass."""

    def test_basic_construction(self):
        r = CheckResult(
            id="1.1",
            name="Test check",
            result=Result.PASS,
            severity=Severity.HARD,
            duration_ms=42,
            detail="All good",
            evidence={"key": "value"},
        )
        assert r.id == "1.1"
        assert r.result == Result.PASS
        assert r.severity == Severity.HARD
        assert r.duration_ms == 42
        assert r.evidence == {"key": "value"}

    def test_evidence_is_optional(self):
        r = CheckResult(
            id="1.2",
            name="Test",
            result=Result.FAIL,
            severity=Severity.SOFT,
            duration_ms=0,
            detail="Missing",
        )
        assert r.evidence is None


class TestPhaseEvidence:
    """Verify PhaseEvidence construction and serialization."""

    def _make_results(self) -> list[CheckResult]:
        return [
            CheckResult("1.1", "Check A", Result.PASS, Severity.HARD, 100, "OK"),
            CheckResult("1.2", "Check B", Result.PASS, Severity.HARD, 50, "OK"),
            CheckResult("1.3", "Check C", Result.FAIL, Severity.SOFT, 30, "Warn"),
        ]

    def test_from_results_pass_when_no_hard_failures(self):
        results = self._make_results()
        target = RunTarget("123456789012", "us-east-1", "dev")
        run_meta = RunMetadata("run-1", "http://example.com", "t1", "t2", 1.5)
        evidence = PhaseEvidence.from_results(1, "Bootstrap", target, run_meta, results)

        assert evidence.result == "pass"
        assert evidence.summary.passed == 2
        assert evidence.summary.failed == 1
        assert evidence.summary.skipped == 0
        assert evidence.summary.total == 3

    def test_from_results_fail_when_hard_failure(self):
        results = [
            CheckResult("1.1", "Check A", Result.FAIL, Severity.HARD, 100, "Bad"),
            CheckResult("1.2", "Check B", Result.PASS, Severity.SOFT, 50, "OK"),
        ]
        target = RunTarget("123456789012", "us-east-1", "dev")
        run_meta = RunMetadata()
        evidence = PhaseEvidence.from_results(1, "Bootstrap", target, run_meta, results)

        assert evidence.result == "fail"
        assert evidence.summary.passed == 1
        assert evidence.summary.failed == 1

    def test_to_json_roundtrip(self):
        results = self._make_results()
        target = RunTarget("123456789012", "us-east-1", "dev")
        run_meta = RunMetadata("run-1", "http://example.com", "t1", "t2", 1.5)
        evidence = PhaseEvidence.from_results(1, "Bootstrap", target, run_meta, results)

        json_str = evidence.to_json()
        parsed = json.loads(json_str)

        assert parsed["phase"] == 1
        assert parsed["phase_name"] == "Bootstrap"
        assert parsed["result"] == "pass"
        assert parsed["target"]["account_id"] == "123456789012"
        assert parsed["run"]["workflow_run_id"] == "run-1"
        assert parsed["summary"]["passed"] == 2
        assert parsed["summary"]["failed"] == 1
        assert len(parsed["checks"]) == 3
        assert parsed["checks"][0]["result"] == "pass"
        assert parsed["checks"][0]["severity"] == "hard"

    def test_to_json_required_fields(self):
        """All required fields per the spec must be present."""
        results = [CheckResult("1.1", "Check", Result.PASS, Severity.HARD, 10, "OK", {"k": "v"})]
        target = RunTarget("111222333444", "us-east-1", "dev")
        run_meta = RunMetadata("r1", "http://url", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", 1.0)
        evidence = PhaseEvidence.from_results(1, "Bootstrap state backend", target, run_meta, results)

        parsed = json.loads(evidence.to_json())
        # Top-level required fields
        assert "phase" in parsed
        assert "phase_name" in parsed
        assert "target" in parsed
        assert "run" in parsed
        assert "result" in parsed
        assert "summary" in parsed
        assert "checks" in parsed
        # Target fields
        assert "account_id" in parsed["target"]
        assert "region" in parsed["target"]
        assert "environment" in parsed["target"]
        # Run fields
        assert "workflow_run_id" in parsed["run"]
        assert "workflow_run_url" in parsed["run"]
        assert "started_at" in parsed["run"]
        assert "finished_at" in parsed["run"]
        assert "duration_seconds" in parsed["run"]
        # Summary fields
        assert "passed" in parsed["summary"]
        assert "failed" in parsed["summary"]
        assert "skipped" in parsed["summary"]
        assert "total" in parsed["summary"]
        # Check fields
        check = parsed["checks"][0]
        assert "id" in check
        assert "name" in check
        assert "result" in check
        assert "severity" in check
        assert "duration_ms" in check
        assert "detail" in check
        assert "evidence" in check


class TestNowIso:
    """Verify now_iso helper."""

    def test_returns_iso_format(self):
        ts = now_iso()
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z")

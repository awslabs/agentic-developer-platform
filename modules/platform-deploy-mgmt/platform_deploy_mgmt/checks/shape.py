"""JSON evidence shape definitions, severity/cost/result enums, and serialization."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Check severity — HARD failures fail the workflow, SOFT are warnings."""

    HARD = "hard"
    SOFT = "soft"


class CostClass(str, Enum):
    """How expensive a check is to run (informs monitor cadence decisions)."""

    CHEAP = "cheap"
    EXPENSIVE = "expensive"


class Result(str, Enum):
    """Outcome of a single check."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    """Result of executing a single verification check."""

    id: str
    name: str
    result: Result
    severity: Severity
    duration_ms: int
    detail: str
    evidence: dict[str, Any] | None = None


@dataclass
class RunTarget:
    """Identifies the deployment target being verified."""

    account_id: str
    region: str
    environment: str


@dataclass
class RunMetadata:
    """Metadata about the verification run itself."""

    workflow_run_id: str = ""
    workflow_run_url: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0


@dataclass
class CheckSummary:
    """Aggregate counts of check results."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0


@dataclass
class PhaseEvidence:
    """Top-level JSON evidence document for a phase verification run."""

    phase: int
    phase_name: str
    target: RunTarget
    run: RunMetadata
    result: str  # "pass" or "fail"
    summary: CheckSummary
    checks: list[CheckResult] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialize to JSON string with enum values as strings."""
        return json.dumps(asdict(self), default=_enum_serializer, indent=2)

    @staticmethod
    def from_results(
        phase: int,
        phase_name: str,
        target: RunTarget,
        run_meta: RunMetadata,
        results: list[CheckResult],
    ) -> "PhaseEvidence":
        """Construct evidence from a list of check results."""
        summary = CheckSummary(
            passed=sum(1 for r in results if r.result == Result.PASS),
            failed=sum(1 for r in results if r.result == Result.FAIL),
            skipped=sum(1 for r in results if r.result == Result.SKIP),
            total=len(results),
        )
        # Overall result: FAIL if any HARD check failed
        has_hard_failure = any(r.result == Result.FAIL and r.severity == Severity.HARD for r in results)
        overall = "fail" if has_hard_failure else "pass"
        return PhaseEvidence(
            phase=phase,
            phase_name=phase_name,
            target=target,
            run=run_meta,
            result=overall,
            summary=summary,
            checks=results,
        )


def _enum_serializer(obj: Any) -> Any:
    """JSON serializer for Enum values."""
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

"""Sanitized dedicated-workspace report from linked diagnostic observations."""

from __future__ import annotations

from hashlib import sha256

from .demo1_evidence import DemoInput, EvidenceError, PhaseEvidence, link_phases
from .demo1_provider import ProviderReader, observe_provider


def reference(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def _phase_record(phase: PhaseEvidence) -> dict:
    return {
        "phase_name": phase.phase_name,
        "admitted_at": phase.admitted_at.isoformat(),
        "operation_ref": reference(phase.operation_id),
        "approval_ref": reference(phase.approval_id),
        "plan_ref": reference(phase.plan_revision),
        "artifact_ref": reference(phase.artifact_digest),
        "attempt_ref": reference(phase.attempt_id),
        "fence_ref": reference(phase.fence),
        "observed_at": phase.observed_at.isoformat(),
        "source": phase.source,
        "state": phase.state,
    }


def _check(status: str, detail: str) -> dict:
    return {"status": status, "detail": detail}


def _overall(checks: dict) -> str:
    statuses = {check["status"] for check in checks.values()}
    if "FAIL" in statuses:
        return "FAIL"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    return "NOT RUN"


def assemble_report(
    selected: DemoInput,
    records: list[object],
    *,
    expected_owned: tuple[str, ...] = (),
    reader: ProviderReader | None = None,
) -> dict:
    """Inspect input diagnostics without treating any submitted record as authority.

    Without a registered authenticated browser and provider adapter, even a
    complete fixture cannot prove Ready, removal or zero cost. The reader is
    invoked only after all phase records have been validated and linked.
    """
    report = {
        "version": "demo1-report-v1",
        "scenario": "existing-account-dedicated-workspace",
        "evidence_mode": "offline-fixture",
        "live_acceptance": False,
        "source_ref": reference(selected.release_source),
        "image_ref": reference(selected.image_digest),
        "schema_ref": reference(selected.schema_revision),
        "request_ref": reference(selected.request_id),
        "workspace_ref": None,
        "phases": [],
        "checks": {
            "browser_reentry": _check(
                "NOT RUN", "verified sign-in and browser journey unavailable"
            ),
            "removal": _check(
                "NOT RUN", "user-visible retirement and admission unavailable"
            ),
            "creation": _check("NOT RUN", "no verified lifecycle observation"),
            "recovery": _check("NOT RUN", "no verified recovery observation"),
            "cleanup": _check("NOT RUN", "no provider inventory"),
            "survivors": _check("NOT RUN", "no provider inventory"),
            "cost": _check("NOT RUN", "no provider cost observation"),
            "serving": _check(
                "NOT RUN", "batch or lifecycle evidence cannot prove serving"
            ),
        },
    }
    if not records:
        report["overall"] = "NOT RUN"
        return report
    try:
        phases = link_phases(selected, records)
    except EvidenceError as error:
        report["checks"]["creation"] = _check("FAIL", str(error))
        report["overall"] = "FAIL"
        return report
    report["workspace_ref"] = reference(phases[0].workspace_id)
    report["phases"] = [_phase_record(phase) for phase in phases]
    if any(phase.state == "failed" for phase in phases):
        report["checks"]["creation"] = _check("FAIL", "lifecycle phase failed")
    else:
        report["checks"]["creation"] = _check(
            "BLOCKED",
            "linked phases are diagnostic; readiness not independently verified",
        )
    if len(phases) > 1:
        report["checks"]["recovery"] = _check(
            "BLOCKED",
            "original request retained; replay and recovered state not authenticated",
        )
    if reader is not None:
        try:
            assessment = observe_provider(
                selected,
                phases[0].workspace_id,
                expected_owned,
                reader,
            )
        except EvidenceError as error:
            for check in ("cleanup", "survivors", "cost"):
                report["checks"][check] = _check("FAIL", str(error))
        else:
            for check in ("cleanup", "survivors", "cost"):
                report["checks"][check] = _check(
                    getattr(assessment, check),
                    assessment.reason,
                )
            report["provider_observed_at"] = assessment.observed_at
            report["provider_origin"] = assessment.origin
    report["overall"] = _overall(report["checks"])
    return report

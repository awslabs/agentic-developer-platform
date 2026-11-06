"""Build a sanitized B04 packet from independently retained live surface reports."""

from __future__ import annotations

import hashlib
import json
import re
from tests.e2e.product_onboarding.test_v05 import PROGRESS_SELECTORS, RECOVERY_SELECTORS, RESUME_SELECTORS

SCHEMA = "product-v05/v1"
BOLT_ID = "B04"
DECISION_PROCEDURE = (
    "docs/delivery-flow/plans/product-onboarding-8141/human-checks.md"
    "#record-one-decision-using-the-existing-verification-stories"
)
CASE_DIRECTIONS = {
    "AC-01": ("SCN-01", "live_bidirectional_resume", {
        "ui_to_cli": RESUME_SELECTORS["browser_ui_to_cli"],
        "cli_to_ui": RESUME_SELECTORS["browser_cli_to_ui"]}),
    "AC-02": ("SCN-02", "live_progress_math", {"progress": PROGRESS_SELECTORS["browser"]}),
    "AC-03": ("SCN-03", "live_resume_recovery", {"recovery": RECOVERY_SELECTORS["browser"]}),
}
REVISION = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"(?:sha256:)?[a-f0-9]{64}\Z")
REFERENCE = re.compile(r"(?:evidence|receipts)/(?:[a-z0-9][a-z0-9._-]{0,99}/)*[a-z0-9][a-z0-9._-]{0,99}\Z")
ARTIFACTS = ("api_image_digest", "ui_manifest_sha256", "cli_sha256", "api_workflow_revision",
             "browser_workflow_revision", "cli_workflow_revision", "schema_head")
ATTEMPT_KEYS = ("attempt_id", "run_id", "surface", "direction", "test_ref", "result",
                "source_revision", "requirements_revision", "workflow_run_id", "workflow_attempt",
                "artifact_ref", "readback_ref", "expected", "actual", "screenshot_ref", "trace_ref",
                "command", "stdout_ref", "stderr_ref", "installed_cli_sha256", "exit_code")
SURFACES = ("browser", "cli", "api")


def build_packet(inputs):
    """Preserve every attempt but never turn a missing, stale or unsafe observation green."""
    if not isinstance(inputs, dict):
        raise TypeError("V05 evidence inputs must be a mapping")
    issues = []

    def reference(value, name):
        if not isinstance(value, str) or not REFERENCE.fullmatch(value) or ".." in value:
            issues.append(f"{name} lacks a protected evidence reference")
            return None
        return value

    def summary(value, name):
        if (not isinstance(value, str) or not value.strip() or len(value) > 500
                or not re.fullmatch(r"[A-Za-z0-9 ,.:%()'\-]+", value)
                or re.search(r"[A-Za-z0-9_-]{24,}|[0-9]{7,}", value)):
            issues.append(f"{name} lacks a sanitized expected/actual summary")
            return None
        return value

    run_id = inputs.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id):
        issues.append("packet run identity is invalid")
        run_id = None
    pinned = {name: inputs.get(name) for name in ("plan_revision", "requirements_revision", "source_revision")}
    for name, value in pinned.items():
        if not isinstance(value, str) or not REVISION.fullmatch(value):
            issues.append(f"{name} is not a full commit SHA")
            pinned[name] = None
    provided = inputs.get("artifacts") if isinstance(inputs.get("artifacts"), dict) else {}
    artifacts = {}
    for name in ARTIFACTS:
        value = provided.get(name)
        pattern = DIGEST if name.endswith("digest") or name.endswith("sha256") else REVISION
        if name == "schema_head":
            pattern = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
        if not isinstance(value, str) or not pattern.fullmatch(value):
            issues.append(f"{name} is not a pinned artifact")
            value = None
        artifacts[name] = value
    supplied_deployment = inputs.get("deployment") if isinstance(inputs.get("deployment"), dict) else {}
    deployment = {}
    for name in (*ARTIFACTS, "source_revision"):
        value = supplied_deployment.get(name)
        pattern = DIGEST if name.endswith(("digest", "sha256")) else REVISION
        if name == "schema_head":
            pattern = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
        deployment[name] = value if isinstance(value, str) and pattern.fullmatch(value) else None
    deployment["product_onboarding_enabled"] = supplied_deployment.get("product_onboarding_enabled") is True
    if (deployment["source_revision"] != pinned["source_revision"]
            or any(deployment[name] != artifacts[name] for name in ARTIFACTS)
            or deployment["product_onboarding_enabled"] is not True):
        issues.append("deployed source, schema, clients or onboarding admission differs from pinned artifacts")
    deployment["receipt_ref"] = reference(supplied_deployment.get("receipt_ref"), "deployment receipt")
    deployment["observation_ref"] = reference(supplied_deployment.get("observation_ref"), "deployment observation")
    if deployment["receipt_ref"] == deployment["observation_ref"]:
        issues.append("deployment observation cannot reuse its release receipt")

    supplied_reports = inputs.get("surfaces") if isinstance(inputs.get("surfaces"), dict) else {}
    if set(supplied_reports) != set(SURFACES):
        issues.append("separate browser, CLI and API reports are required")
    reports = {}
    report_refs = []
    for surface in SURFACES:
        report = supplied_reports.get(surface) if isinstance(supplied_reports.get(surface), dict) else {}
        workflow_revision = artifacts[f"{('browser' if surface == 'browser' else surface)}_workflow_revision"]
        selected = {"run_id": report.get("run_id") if report.get("run_id") == run_id else None,
                    "status": report.get("status") if report.get("status") in ("pass", "fail", "skip", "not_run") else None}
        for name in ("workflow_run_id", "workflow_attempt"):
            value = report.get(name)
            selected[name] = value if type(value) is int and value > 0 else None
        for name in ("source_revision", "workflow_revision"):
            value = report.get(name)
            selected[name] = value if isinstance(value, str) and REVISION.fullmatch(value) else None
        selected["report_ref"] = reference(report.get("report_ref"), surface + " report")
        report_refs.append(selected["report_ref"])
        if (selected["run_id"] != run_id or selected["source_revision"] != pinned["source_revision"]
                or selected["workflow_revision"] != workflow_revision or selected["status"] != "pass"
                or type(selected["workflow_run_id"]) is not int or selected["workflow_run_id"] <= 0
                or type(selected["workflow_attempt"]) is not int or selected["workflow_attempt"] <= 0):
            issues.append(f"{surface} report is missing, failed or from another run/revision")
        reports[surface] = selected
    if len(set(report_refs)) != len(SURFACES):
        issues.append("browser, CLI and API reports cannot share an artifact")

    supplied_cases = inputs.get("cases") if isinstance(inputs.get("cases"), dict) else {}
    if set(supplied_cases) != set(CASE_DIRECTIONS):
        issues.append("V05 case inventory is incomplete or unexpected")
    cases = {}
    seen_attempts = set()
    for criterion, (scenario, selector_name, directions) in CASE_DIRECTIONS.items():
        source = supplied_cases.get(criterion) if isinstance(supplied_cases.get(criterion), dict) else {}
        selector = "tests/e2e/product_onboarding/test_v05.py::" + selector_name
        if (source.get("scenario_id"), source.get("test_ref")) != (scenario, selector):
            issues.append(f"{criterion} does not select its reviewed scenario")
        expected = summary(source.get("expected"), criterion + ".expected")
        actual = summary(source.get("actual"), criterion + ".actual")
        raw = source.get("attempts") if isinstance(source.get("attempts"), list) else []
        attempts = []
        for item in raw:
            if not isinstance(item, dict) or set(item) - set(ATTEMPT_KEYS):
                issues.append(f"{criterion} has an unsafe or malformed attempt")
                continue
            attempt = {name: item.get(name) for name in ATTEMPT_KEYS}
            direction, surface = attempt["direction"], attempt["surface"]
            direction = direction if isinstance(direction, str) and direction in directions else None
            surface = surface if isinstance(surface, str) and surface in SURFACES else None
            attempt["direction"], attempt["surface"] = direction, surface
            report = reports.get(surface) if surface in SURFACES else None
            if direction not in directions or report is None or attempt["test_ref"] != directions.get(direction):
                issues.append(f"{criterion} includes an unselected direction or surface")
            identity = attempt["attempt_id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,128}", identity) or identity in seen_attempts:
                issues.append(f"{criterion} attempt ID is missing or repeated")
                attempt["attempt_id"] = None
            else:
                seen_attempts.add(identity)
            attempt["run_id"] = attempt["run_id"] if attempt["run_id"] == run_id else None
            for name in ("source_revision", "requirements_revision"):
                value = attempt[name]
                attempt[name] = value if isinstance(value, str) and REVISION.fullmatch(value) else None
            for name in ("workflow_run_id", "workflow_attempt"):
                value = attempt[name]
                attempt[name] = value if type(value) is int and value > 0 else None
            if attempt["test_ref"] != directions.get(direction):
                attempt["test_ref"] = None
            if (attempt["run_id"] != run_id or attempt["source_revision"] != pinned["source_revision"]
                    or attempt["requirements_revision"] != pinned["requirements_revision"]
                    or report is None or (attempt["workflow_run_id"], attempt["workflow_attempt"]) !=
                    (report["workflow_run_id"], report["workflow_attempt"])):
                issues.append(f"{criterion} attempt has a stale run, workflow or requirements revision")
            for field in ("artifact_ref", "readback_ref"):
                attempt[field] = reference(attempt[field], f"{criterion}.{surface}.{field}")
            if attempt["artifact_ref"] == attempt["readback_ref"]:
                issues.append(f"{criterion}.{surface} needs an independent readback")
            for field in ("expected", "actual"):
                attempt[field] = summary(attempt[field], f"{criterion}.{surface}.{field}")
            if surface == "browser":
                for field in ("screenshot_ref", "trace_ref"):
                    attempt[field] = reference(attempt[field], f"{criterion}.browser.{field}")
            elif surface == "cli":
                for field in ("stdout_ref", "stderr_ref"):
                    attempt[field] = reference(attempt[field], f"{criterion}.cli.{field}")
                attempt["command"] = summary(attempt["command"], f"{criterion}.cli.command")
                if attempt["installed_cli_sha256"] != artifacts["cli_sha256"] or type(attempt["exit_code"]) is not int or attempt["exit_code"] != 0:
                    issues.append(f"{criterion}.cli command failed or differs from the pinned CLI")
                attempt["exit_code"] = attempt["exit_code"] if type(attempt["exit_code"]) is int else None
            if surface != "browser":
                attempt["screenshot_ref"] = attempt["trace_ref"] = None
            if surface != "cli":
                for name in ("stdout_ref", "stderr_ref", "command", "installed_cli_sha256", "exit_code"):
                    attempt[name] = None
            elif not isinstance(attempt["installed_cli_sha256"], str) or not DIGEST.fullmatch(attempt["installed_cli_sha256"]):
                attempt["installed_cli_sha256"] = None
            if attempt["result"] not in ("pass", "fail", "skip", "not_run", "interrupted"):
                issues.append(f"{criterion}.{surface} attempt has an unknown result")
                attempt["result"] = None
            attempts.append(attempt)
        for direction in directions:
            for surface in SURFACES:
                matching = [item for item in attempts if item["direction"] == direction and item["surface"] == surface]
                if not matching or matching[-1]["result"] != "pass":
                    issues.append(f"{criterion}.{direction}.{surface} has no final passing attempt")
        cases[criterion] = {"scenario_id": scenario, "test_ref": selector, "expected": expected,
                            "actual": actual, "attempts": attempts}

    supplied_cleanup = inputs.get("cleanup") if isinstance(inputs.get("cleanup"), dict) else {}
    cleanup = {"run_id": supplied_cleanup.get("run_id") if supplied_cleanup.get("run_id") == run_id else None,
               "status": supplied_cleanup.get("status") if supplied_cleanup.get("status") == "verified" else None}
    for name in ("owned_absent", "original_projects_preserved", "reused_assets_preserved"):
        cleanup[name] = supplied_cleanup.get(name) is True
    cleanup["receipt_ref"] = reference(supplied_cleanup.get("receipt_ref"), "cleanup receipt")
    owned = supplied_cleanup.get("cases") if isinstance(supplied_cleanup.get("cases"), dict) else {}
    required = {selector.rsplit("::", 1)[-1] for _, _, directions in CASE_DIRECTIONS.values() for selector in directions.values()}
    if set(owned) != required:
        issues.append("cleanup is missing an owned browser fixture")
    cleanup["cases"] = {}
    for name in sorted(required):
        entry = owned.get(name) if isinstance(owned.get(name), dict) else {}
        receipt_ref = reference(entry.get("receipt_ref"), name + " cleanup receipt")
        cleanup["cases"][name] = {"receipt_ref": receipt_ref, "owned_absent": entry.get("owned_absent") is True,
                                   "shared_unchanged": entry.get("shared_unchanged") is True}
        if entry.get("owned_absent") is not True or entry.get("shared_unchanged") is not True:
            issues.append(f"{name} run-owned cleanup is incomplete")
    if (cleanup["run_id"] != run_id or cleanup["status"] != "verified" or cleanup["owned_absent"] is not True
            or cleanup["original_projects_preserved"] is not True or cleanup["reused_assets_preserved"] is not True):
        issues.append("run-owned cleanup and reused assets are unverified")

    packet = {"evidence_schema": SCHEMA, "bolt_id": BOLT_ID, "run_id": run_id, **pinned,
              "artifacts": artifacts, "deployment": deployment, "surfaces": reports,
              "cases": cases, "cleanup": cleanup, "status": "review_ready" if not issues else "incomplete", "issues": issues}
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
    ).hexdigest()
    packet["review"] = {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet["plan_revision"],
        "packet_revision": packet["packet_revision"],
        "status": "pending" if packet["status"] == "review_ready" else "not_ready",
    }
    return packet


def review_binding(packet):
    """Identify the review target; never manufacture a human decision."""
    if not isinstance(packet, dict) or packet.get("status") != "review_ready":
        raise ValueError("Incomplete B04 packet cannot be sent to human review")
    checked = build_packet(packet)
    if checked != packet:
        raise ValueError("B04 packet or decision binding differs from its verified revision")
    return dict(packet["review"])


def decision_binding(packet, decisions):
    """Validate an authoritative ADP decision readback, including prior rejections."""
    pending = review_binding(packet)
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("B04 needs a recorded decision for this packet revision")
    seen = set()
    seen_receipts = set()
    current = None
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {
            "bolt_id", "plan_revision", "packet_revision", "decision", "decision_ref",
        } or decision.get("bolt_id") != BOLT_ID:
            raise ValueError("B04 decision readback has an unknown or malformed binding")
        revision = decision["packet_revision"]
        reference = decision["decision_ref"]
        if (not isinstance(revision, str) or not DIGEST.fullmatch(revision)
                or not revision.startswith("sha256:") or revision in seen
                or not isinstance(decision["plan_revision"], str) or not REVISION.fullmatch(decision["plan_revision"])
                or decision["decision"] not in ("accept", "reject")
                or not isinstance(reference, str) or not REFERENCE.fullmatch(reference) or ".." in reference
                or reference in seen_receipts):
            raise ValueError("B04 decision readback is duplicated, stale or unsafe")
        seen.add(revision)
        seen_receipts.add(reference)
        if revision == pending["packet_revision"]:
            if decision["plan_revision"] != pending["plan_revision"]:
                raise ValueError("B04 decision uses a different plan revision")
            current = decision
        elif decision["decision"] == "accept":
            raise ValueError("B04 already has an accepted packet revision")
    if current is None:
        raise ValueError("B04 needs a recorded decision for this packet revision")
    return dict(current)

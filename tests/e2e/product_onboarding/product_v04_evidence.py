"""Assemble private scope evidence; local assembly is not deployed acceptance."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

SCHEMA = "product-v04/v1"
BOLT_ID = "B03"
DECISION_PROCEDURE = (
    "docs/delivery-flow/plans/product-onboarding-8141/human-checks.md"
    "#record-one-decision-using-the-existing-verification-stories"
)
CASES = {
    "AC-01": ("SCN-01", "tests/e2e/product_onboarding/test_v04.py::live_scope_isolation", ("api", "browser", "cli")),
    "AC-02": ("SCN-02", "tests/e2e/product_onboarding/test_v04.py::live_scope_denials", ("api", "browser", "cli")),
    "AC-03": ("SCN-03", "tests/e2e/product_onboarding/test_v04.py::live_scope_history_cleanup", ("api", "cli")),
}
ISOLATION_SELECTORS = {
    "api": "tests/e2e/new_ui/test_product_scopes.py::test_scope_isolation_api",
    "browser": "tests/e2e/new_ui/test_product_scopes.py::test_scope_isolation",
    "cli": "case:PR04-C01",
}
DENIAL_SELECTORS = {
    "api": "tests/e2e/new_ui/test_product_scopes.py::test_scope_denials_api",
    "browser": "tests/e2e/new_ui/test_product_scopes.py::test_scope_denials",
    "cli": "case:PR04-C02",
}
HISTORY_SELECTORS = {"api": "case:PR04-C03", "cli": "case:PR04-C03"}
SURFACE_SELECTORS = {"AC-01": ISOLATION_SELECTORS, "AC-02": DENIAL_SELECTORS,
                     "AC-03": HISTORY_SELECTORS}
REVISION = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
REFERENCE = re.compile(r"(?:evidence|receipts)/[a-z0-9][a-z0-9._/-]{0,199}\Z")
ATTEMPT_FIELDS = (
    "attempt_id", "run_id", "surface", "test_ref", "result", "requirements_revision",
    "source_revision", "artifact_ref", "readback_ref", "expected", "actual",
    "screenshot_ref", "trace_ref", "stdout_ref", "installed_cli_sha256", "exit_code",
    "workflow_run_id", "workflow_attempt",
)
ARTIFACTS = (
    "api_image_digest", "frontend_manifest_sha256", "installed_cli_sha256",
    "api_workflow_revision", "browser_workflow_revision", "cli_workflow_revision", "schema_head",
)


def build_packet(inputs):
    """Keep all attempts, compute separate surface reports and fail closed on missing proof."""
    if not isinstance(inputs, dict):
        raise TypeError("V04 evidence inputs must be a mapping")
    issues = []
    run_id = inputs.get("run_id")
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        issues.append("packet has no run identity")
    pinned = {name: inputs.get(name) for name in ("plan_revision", "requirements_revision", "source_revision")}
    for name, value in pinned.items():
        if not isinstance(value, str) or not REVISION.fullmatch(value):
            issues.append(f"{name} is not pinned")
    supplied_artifacts = inputs.get("artifacts") if isinstance(inputs.get("artifacts"), dict) else {}
    artifacts = {name: supplied_artifacts.get(name) for name in ARTIFACTS}
    for name in ARTIFACTS:
        value = artifacts.get(name)
        pattern = DIGEST if name.endswith("digest") or name.endswith("sha256") else REVISION
        if name == "schema_head":
            pattern = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
        if not isinstance(value, str) or not pattern.fullmatch(value):
            issues.append(f"{name} is not pinned")
    supplied_deployment = inputs.get("deployment") if isinstance(inputs.get("deployment"), dict) else {}
    observed = {name: supplied_deployment.get(name) for name in
                (*ARTIFACTS, "source_revision", "receipt_ref", "observation_ref")}
    if observed.get("source_revision") != pinned["source_revision"] or any(
        observed.get(name) != artifacts.get(name) for name in ARTIFACTS
    ):
        issues.append("deployed source, schema or clients differ from pinned artifacts")
    def reference(value, label):
        if not isinstance(value, str) or not REFERENCE.fullmatch(value) or ".." in value:
            issues.append(f"{label} needs an opaque protected evidence reference")
            return None
        return value

    def summary(value, label):
        if not isinstance(value, str) or not value.strip() or re.search(r"https?://|arn:aws|\b[0-9]{12}\b", value, re.I):
            issues.append(f"{label} needs a sanitized outcome summary")
            return None
        return value

    observed["receipt_ref"] = reference(observed.get("receipt_ref"), "deployment receipt")
    observed["observation_ref"] = reference(observed.get("observation_ref"), "independent deployment observation")
    if observed.get("receipt_ref") == observed.get("observation_ref"):
        issues.append("deployment observation cannot be its release receipt")
    supplied_cleanup = inputs.get("cleanup") if isinstance(inputs.get("cleanup"), dict) else {}
    cleanup = {name: supplied_cleanup.get(name) for name in (
        "run_id", "status", "receipt_ref", "owned_absent", "original_projects_preserved",
        "reused_assets_preserved",
    )}
    cleanup["receipt_ref"] = reference(cleanup.get("receipt_ref"), "cleanup")
    if (cleanup.get("run_id") != run_id or cleanup.get("status") != "verified"
            or cleanup.get("owned_absent") is not True or cleanup.get("original_projects_preserved") is not True
            or cleanup.get("reused_assets_preserved") is not True):
        issues.append("run-owned cleanup and original asset preservation are unverified")
    selected = inputs.get("cases") if isinstance(inputs.get("cases"), dict) else {}
    if set(selected) != set(CASES):
        issues.append("V04 case inventory is missing or contains unexpected cases")
    cases = {}
    surfaces = {name: {"status": "not_run", "attempts": []} for name in ("api", "browser", "cli")}
    for criterion, (scenario, selector, required_surfaces) in CASES.items():
        supplied = selected.get(criterion) if isinstance(selected.get(criterion), dict) else {}
        if (supplied.get("scenario_id"), supplied.get("test_ref")) != (scenario, selector):
            issues.append(f"{criterion} scenario/test binding differs from the plan")
        expected = summary(supplied.get("expected"), f"{criterion}.expected")
        actual = summary(supplied.get("actual"), f"{criterion}.actual")
        raw_attempts = supplied.get("attempts") if isinstance(supplied.get("attempts"), list) else []
        attempts = [
            {key: deepcopy(item[key]) for key in ATTEMPT_FIELDS if key in item} if isinstance(item, dict) else item
            for item in raw_attempts
        ]
        seen = set()
        for surface in required_surfaces:
            records = [item for item in attempts if isinstance(item, dict) and item.get("surface") == surface]
            if not records:
                issues.append(f"{criterion}.{surface} has no attempts")
                continue
            for attempt in records:
                attempt_id = attempt.get("attempt_id")
                if not isinstance(attempt_id, str) or not attempt_id or (surface, attempt_id) in seen:
                    issues.append(f"{criterion}.{surface} has a missing or duplicate attempt identity")
                seen.add((surface, attempt_id))
                selected_ref = SURFACE_SELECTORS.get(criterion, {}).get(surface, selector)
                if attempt.get("run_id") != run_id or attempt.get("test_ref") != selected_ref or any(
                    attempt.get(name) != pinned[name] for name in ("requirements_revision", "source_revision")
                ):
                    issues.append(f"{criterion}.{surface} attempt is from another run or revision")
                if any(type(attempt.get(key)) is not int or attempt[key] <= 0
                       for key in ("workflow_run_id", "workflow_attempt")):
                    issues.append(f"{criterion}.{surface} lacks a workflow run and attempt")
                for key in ("artifact_ref", "readback_ref"):
                    attempt[key] = reference(attempt.get(key), f"{criterion}.{surface}.{key}")
                if attempt.get("artifact_ref") == attempt.get("readback_ref"):
                    issues.append(f"{criterion}.{surface} needs an independent readback")
                for key in ("expected", "actual"):
                    attempt[key] = summary(attempt.get(key), f"{criterion}.{surface}.{key}")
                if surface == "browser":
                    for key in ("screenshot_ref", "trace_ref"):
                        attempt[key] = reference(attempt.get(key), f"{criterion}.browser.{key}")
                elif surface == "cli":
                    attempt["stdout_ref"] = reference(attempt.get("stdout_ref"), f"{criterion}.cli.stdout_ref")
                    if attempt.get("installed_cli_sha256") != artifacts.get("installed_cli_sha256"):
                        issues.append(f"{criterion}.cli installed artifact differs from the pin")
                    if type(attempt.get("exit_code")) is not int or attempt["exit_code"] != 0:
                        issues.append(f"{criterion}.cli command did not exit successfully")
                if attempt.get("result") not in ("pass", "fail", "skip", "not_run", "interrupted"):
                    issues.append(f"{criterion}.{surface} has an unknown result")
                surfaces[surface]["attempts"].append({
                    "ac_id": criterion, "scenario_id": scenario, "attempt_id": attempt_id,
                    "result": attempt.get("result"), "artifact_ref": attempt.get("artifact_ref"),
                })
            if records[-1].get("result") != "pass":
                issues.append(f"{criterion}.{surface} final attempt did not pass")
        if any(not isinstance(item, dict) or item.get("surface") not in required_surfaces for item in attempts):
            issues.append(f"{criterion} includes an unknown surface attempt")
        cases[criterion] = {
            "scenario_id": scenario, "test_ref": selector, "expected": expected,
            "actual": actual, "attempts": attempts,
        }
    for result in surfaces.values():
        latest = {attempt["ac_id"]: attempt for attempt in result["attempts"]}
        result["status"] = "pass" if latest and all(
            attempt["result"] == "pass" for attempt in latest.values()
        ) else "incomplete"
    packet = {"evidence_schema": SCHEMA, "run_id": run_id, **pinned, "artifacts": deepcopy(artifacts),
              "deployment": deepcopy(observed), "cases": cases, "surfaces": surfaces, "cleanup": cleanup,
              "status": "review_ready" if not issues else "incomplete", "issues": issues}
    encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    packet["packet_revision"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    packet["review"] = {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet["plan_revision"],
        "packet_revision": packet["packet_revision"],
        "status": "pending" if packet["status"] == "review_ready" else "not_ready",
    }
    return packet


def review_binding(packet):
    """Return the existing B03 decision reference, never a human decision."""
    if not isinstance(packet, dict) or packet.get("status") != "review_ready" or packet.get("issues") != []:
        raise ValueError("Incomplete scope evidence cannot be sent to the BOLT decision")
    content = {key: value for key, value in packet.items() if key not in ("packet_revision", "review")}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    expected_revision = "sha256:" + hashlib.sha256(encoded).hexdigest()
    expected = {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet.get("plan_revision"),
        "packet_revision": expected_revision, "status": "pending",
    }
    if packet.get("packet_revision") != expected_revision or packet.get("review") != expected:
        raise ValueError("BOLT decision binding differs from the published packet and plan revisions")
    return deepcopy(expected)

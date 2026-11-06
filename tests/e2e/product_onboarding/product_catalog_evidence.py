"""Assemble V12 evidence without treating local fixtures as live acceptance."""

import hashlib
import json
import re
from copy import deepcopy

from tests.e2e.product_onboarding.product_catalog_fixture import MEMBERSHIP_SELECTORS

SCHEMA = "product-v12/v1"
BOLT_ID = "B10"
DECISION_PROCEDURE = (
    "docs/delivery-flow/plans/product-onboarding-8141/human-checks.md"
    "#record-one-decision-using-the-existing-verification-stories"
)
CASES = {
    "AC-01": ("SCN-01", {"api": "tests/e2e/product_onboarding/test_v12.py::live_catalog_membership",
                         "browser": MEMBERSHIP_SELECTORS["browser"]}),
    "AC-02": ("SCN-02", {"api": "tests/e2e/product_onboarding/test_v12.py::live_catalog_history_access",
                         "access": "tests/e2e/product_onboarding/test_v12.py::live_catalog_history_access"}),
    "AC-03": ("SCN-03", {"api": "tests/e2e/product_onboarding/test_v12.py::live_catalog_recovery"}),
}
REVISION = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
REFERENCE = re.compile(r"(?:evidence|receipts)/[a-z0-9][a-z0-9._/-]{0,199}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
SUMMARY = re.compile(r"https?://|arn:aws|\b[0-9]{12}\b", re.I)
ARTIFACTS = ("api_image_digest", "frontend_manifest_sha256", "installed_cli_sha256",
             "api_workflow_revision", "browser_workflow_revision", "cli_workflow_revision", "schema_head")


def build_packet(inputs):
    """Keep all attempts and separate surface reports; missing proof remains incomplete."""
    if not isinstance(inputs, dict):
        raise TypeError("V12 evidence inputs must be a mapping")
    issues = []

    def checked(value, label, pattern):
        if not isinstance(value, str) or not pattern.fullmatch(value) or ".." in value:
            issues.append(f"{label} is missing or invalid")
            return None
        return value

    def reference(value, label):
        return checked(value, label, REFERENCE)

    def summary(value, label):
        if not isinstance(value, str) or not value.strip() or len(value) > 500 or SUMMARY.search(value):
            issues.append(f"{label} needs a sanitized expected/actual result")
            return None
        return value

    run_id = checked(inputs.get("run_id"), "run_id", RUN_ID)
    pins = {key: checked(inputs.get(key), key, REVISION)
            for key in ("plan_revision", "requirements_revision", "source_revision")}
    supplied_artifacts = inputs.get("artifacts") if isinstance(inputs.get("artifacts"), dict) else {}
    artifacts = {key: checked(supplied_artifacts.get(key), key,
                              re.compile(r"[A-Za-z0-9_-]{1,64}\Z") if key == "schema_head" else
                              DIGEST if key.endswith(("digest", "sha256")) else REVISION)
                 for key in ARTIFACTS}
    supplied_deployment = inputs.get("deployment") if isinstance(inputs.get("deployment"), dict) else {}
    deployment = {key: reference(supplied_deployment.get(key), f"deployment.{key}")
                  for key in ("receipt_ref", "observation_ref")}
    deployment.update({key: checked(supplied_deployment.get(key), f"deployment.{key}",
                                    REVISION if key == "source_revision" or key.endswith("revision") else
                                    re.compile(r"[A-Za-z0-9_-]{1,64}\Z") if key == "schema_head" else DIGEST)
                       for key in ("source_revision", *ARTIFACTS)})
    if (deployment["receipt_ref"] == deployment["observation_ref"]
            or deployment["source_revision"] != pins["source_revision"]
            or any(deployment[key] != artifacts[key] for key in ARTIFACTS)):
        issues.append("independently observed deployment differs from selected release")

    supplied_cleanup = inputs.get("cleanup") if isinstance(inputs.get("cleanup"), dict) else {}
    cleanup = {"run_id": checked(supplied_cleanup.get("run_id"), "cleanup.run_id", RUN_ID),
               "status": "verified" if supplied_cleanup.get("status") == "verified" else None,
               "receipt_ref": reference(supplied_cleanup.get("receipt_ref"), "cleanup.receipt_ref"),
               "observation_ref": reference(supplied_cleanup.get("observation_ref"), "cleanup.observation_ref"),
               "owned_absent": supplied_cleanup.get("owned_absent") is True,
               "unowned_unchanged": supplied_cleanup.get("unowned_unchanged") is True,
               "evidence_retained": supplied_cleanup.get("evidence_retained") is True}
    if (cleanup["run_id"] != run_id or cleanup["status"] != "verified"
            or cleanup["receipt_ref"] == cleanup["observation_ref"]
            or not all(cleanup[key] for key in ("owned_absent", "unowned_unchanged", "evidence_retained"))):
        issues.append("independent owned-only cleanup and evidence retention are unverified")

    supplied_cli = inputs.get("cli") if isinstance(inputs.get("cli"), dict) else {}
    cli = {key: reference(supplied_cli.get(key), f"cli.{key}")
           for key in ("artifact_ref", "readback_ref", "stdout_ref", "stderr_ref")}
    cli.update({"run_id": checked(supplied_cli.get("run_id"), "cli.run_id", RUN_ID),
                "source_revision": checked(supplied_cli.get("source_revision"), "cli.source_revision", REVISION),
                "installed_cli_sha256": checked(supplied_cli.get("installed_cli_sha256"), "cli.checksum", DIGEST),
                "workflow_revision": checked(supplied_cli.get("workflow_revision"), "cli.workflow_revision", REVISION),
                "workflow_run_id": supplied_cli.get("workflow_run_id") if type(supplied_cli.get("workflow_run_id")) is int else None,
                "workflow_attempt": supplied_cli.get("workflow_attempt") if type(supplied_cli.get("workflow_attempt")) is int else None,
                "exit_code": supplied_cli.get("exit_code") if type(supplied_cli.get("exit_code")) is int else None,
                "status": supplied_cli.get("status") if supplied_cli.get("status") == "pass" else None,
                "command": summary(supplied_cli.get("command"), "cli.command"),
                "expected": summary(supplied_cli.get("expected"), "cli.expected"),
                "actual": summary(supplied_cli.get("actual"), "cli.actual")})
    if (cli["run_id"] != run_id or cli["source_revision"] != pins["source_revision"]
            or cli["installed_cli_sha256"] != artifacts["installed_cli_sha256"]
            or cli["workflow_revision"] != artifacts["cli_workflow_revision"]
            or cli["readback_ref"] in (cli["artifact_ref"], cli["stdout_ref"], deployment["receipt_ref"])
            or any(type(cli[key]) is not int or cli[key] <= 0 for key in ("workflow_run_id", "workflow_attempt"))
            or cli["status"] != "pass" or type(cli["exit_code"]) is not int or cli["exit_code"] != 0):
        issues.append("installed CLI result does not match the selected run and artifact")

    supplied_cases = inputs.get("cases") if isinstance(inputs.get("cases"), dict) else {}
    if set(supplied_cases) != set(CASES):
        issues.append("V12 case inventory is incomplete or unexpected")
    cases = {}
    surfaces = {surface: {"status": "not_run", "attempts": []} for surface in ("api", "browser", "cli")}
    for criterion, (scenario, selectors) in CASES.items():
        supplied = supplied_cases.get(criterion) if isinstance(supplied_cases.get(criterion), dict) else {}
        if supplied.get("scenario_id") != scenario:
            issues.append(f"{criterion} scenario differs from the selected plan")
        expected = summary(supplied.get("expected"), f"{criterion}.expected")
        actual = summary(supplied.get("actual"), f"{criterion}.actual")
        raw_attempts = supplied.get("attempts") if isinstance(supplied.get("attempts"), list) else []
        attempts = []
        seen = set()
        seen_runs = set()
        for item in raw_attempts:
            if not isinstance(item, dict):
                issues.append(f"{criterion} attempt is invalid")
                continue
            surface = item.get("surface") if isinstance(item.get("surface"), str) else None
            attempt_id = checked(item.get("attempt_id"), f"{criterion}.attempt_id", RUN_ID)
            if surface not in selectors or (surface, attempt_id) in seen:
                issues.append(f"{criterion} has an unknown surface or duplicate attempt")
                continue
            seen.add((surface, attempt_id))
            result = item.get("result")
            attempt = {"attempt_id": attempt_id, "surface": surface,
                       "result": result if result in ("pass", "fail", "interrupted", "skip", "not_run") else None,
                       "run_id": checked(item.get("run_id"), f"{criterion}.run_id", RUN_ID),
                       "test_ref": item.get("test_ref") if item.get("test_ref") == selectors[surface] else None,
                       "requirements_revision": checked(item.get("requirements_revision"),
                                                        f"{criterion}.requirements_revision", REVISION),
                       "source_revision": checked(item.get("source_revision"), f"{criterion}.source_revision", REVISION),
                       "artifact_ref": reference(item.get("artifact_ref"), f"{criterion}.artifact_ref"),
                       "readback_ref": reference(item.get("readback_ref"), f"{criterion}.readback_ref"),
                       "expected": summary(item.get("expected"), f"{criterion}.expected attempt"),
                       "actual": summary(item.get("actual"), f"{criterion}.actual attempt"),
                       "workflow_run_id": item.get("workflow_run_id") if type(item.get("workflow_run_id")) is int else None,
                       "workflow_attempt": item.get("workflow_attempt") if type(item.get("workflow_attempt")) is int else None}
            if surface == "browser":
                attempt.update({key: reference(item.get(key), f"{criterion}.browser.{key}")
                                for key in ("screenshot_ref", "trace_ref")})
            run_attempt = (surface, attempt["workflow_run_id"], attempt["workflow_attempt"])
            if run_attempt in seen_runs:
                issues.append(f"{criterion}.{surface} repeats the same workflow attempt")
            seen_runs.add(run_attempt)
            if attempt["result"] in ("skip", "not_run"):
                attempt["reason"] = summary(item.get("reason"), f"{criterion}.{surface}.reason")
            if (attempt["test_ref"] is None or attempt["run_id"] != run_id
                    or any(attempt[key] != pins[key] for key in ("requirements_revision", "source_revision"))
                    or attempt["artifact_ref"] == attempt["readback_ref"]
                    or attempt["readback_ref"] == deployment["receipt_ref"]
                    or any(type(attempt[key]) is not int or attempt[key] <= 0
                           for key in ("workflow_run_id", "workflow_attempt"))
                    or attempt["result"] is None):
                issues.append(f"{criterion}.{surface} attempt lacks independent matching observations")
            attempts.append(attempt)
            surfaces["api" if surface == "access" else surface]["attempts"].append(
                {"ac_id": criterion, "scenario_id": scenario, "attempt_id": attempt_id,
                 "result": attempt["result"], "artifact_ref": attempt["artifact_ref"]})
        for surface in selectors:
            records = [attempt for attempt in attempts if attempt["surface"] == surface]
            if not records or records[-1]["result"] != "pass":
                issues.append(f"{criterion}.{surface} has no passing final attempt")
        cases[criterion] = {"scenario_id": scenario, "expected": expected, "actual": actual, "attempts": attempts}
    surfaces["cli"]["attempts"].append({"result": cli["status"], "artifact_ref": cli["artifact_ref"]})
    for name, surface in surfaces.items():
        latest = {attempt.get("ac_id"): attempt for attempt in surface["attempts"]}
        surface["status"] = "pass" if surface["attempts"] and all(
            attempt["result"] == "pass" for attempt in latest.values()
        ) and (name != "cli" or cli["status"] == "pass") else "incomplete"
    packet = {"evidence_schema": SCHEMA, "run_id": run_id, **pins, "artifacts": deepcopy(artifacts),
              "deployment": deployment, "cases": cases, "surfaces": surfaces, "cli": cli, "cleanup": cleanup,
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


def review_binding(packet, expected_plan_revision):
    """Reference B10's one pending review; never record a human decision."""
    if (not isinstance(packet, dict) or not isinstance(expected_plan_revision, str)
            or not REVISION.fullmatch(expected_plan_revision)
            or packet.get("plan_revision") != expected_plan_revision):
        raise ValueError("B10 review requires the selected plan revision")
    verified = build_packet(packet)
    if verified["status"] != "review_ready" or packet != verified:
        raise ValueError("B10 decision binding differs from the complete packet and plan revision")
    return deepcopy(verified["review"])

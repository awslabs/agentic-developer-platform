"""Validate private B01 evidence before offering a packet for human review."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from .product_profile_api import CASES

PACKET_SCHEMA = "product-b01/v1"
SCENARIOS = {
    "api_product_lifecycle": ("AC-01", "SCN-01"),
    "api_denial_oracle": ("AC-02", "SCN-02"),
    "api_recovery_cleanup": ("AC-03", "SCN-03"),
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
CHECKSUM = re.compile(r"[0-9a-f]{64}\Z")


def present(value):
    return isinstance(value, str) and bool(value.strip())


def require(document, key, label, issues):
    value = document.get(key) if isinstance(document, dict) else None
    if not present(value):
        issues.append(f"{label}.{key} is missing")
    return value


def build_packet(inputs):
    """Keep every attempt but never mark missing or stale B01 evidence complete."""
    if not isinstance(inputs, dict):
        raise TypeError("Product evidence inputs must be a mapping")
    fields = ("run_id", "plan_revision", "identities", "deployment", "cases",
              "surfaces", "cleanup", "report_artifact")
    packet = {key: deepcopy(inputs.get(key)) for key in fields}
    issues = []
    run_id = require(packet, "run_id", "packet", issues)
    require(packet, "plan_revision", "packet", issues)
    require(packet, "report_artifact", "packet", issues)
    identities = packet["identities"] if isinstance(packet["identities"], dict) else {}
    for key in ("requirements_revision", "source_revision", "workflow_revision"):
        value = require(identities, key, "identities", issues)
        if present(value) and not SHA.fullmatch(value):
            issues.append(f"identities.{key} is not a full revision")
    require(identities, "schema_head", "identities", issues)
    for key, pattern in (("api_image_digest", IMAGE), ("cli_checksum", CHECKSUM)):
        value = require(identities, key, "identities", issues)
        if present(value) and not pattern.fullmatch(value):
            issues.append(f"identities.{key} is not a valid artifact identity")
    deployment = packet["deployment"] if isinstance(packet["deployment"], dict) else {}
    receipt_ref = require(deployment, "receipt_ref", "deployment", issues)
    observation_ref = require(deployment, "observation_ref", "deployment", issues)
    if present(receipt_ref) and receipt_ref == observation_ref:
        issues.append("independent observation must differ from the deployment receipt")
    observed = deployment.get("observed") if isinstance(deployment.get("observed"), dict) else {}
    for key in ("source_revision", "workflow_revision", "schema_head", "api_image_digest", "cli_checksum"):
        value = require(observed, key, "observation", issues)
        if present(value) and value != identities.get(key):
            issues.append(f"observation.{key} differs from the pinned release")
    pods = observed.get("serving_pods")
    if not isinstance(pods, list) or not pods:
        issues.append("independent serving-pod observations are missing")
    else:
        for index, pod in enumerate(pods):
            label = f"serving_pods[{index}]"
            require(pod, "pod_ref", label, issues)
            for key in ("source_revision", "api_image_digest"):
                if require(pod, key, label, issues) != identities.get(key):
                    issues.append(f"{label}.{key} differs from the pinned release")
            if not isinstance(pod, dict) or pod.get("product_profile_enabled") is not True:
                issues.append(f"{label} does not serve the approved core Product capability")
            if not isinstance(pod, dict) or pod.get("product_onboarding_enabled") is not False:
                issues.append(f"{label} enables unsupported onboarding")
    surfaces = packet["surfaces"] if isinstance(packet["surfaces"], dict) else {}
    for surface in ("api", "cli"):
        report = surfaces.get(surface) if isinstance(surfaces.get(surface), dict) else {}
        if report.get("status") != "pass":
            issues.append(f"{surface} surface is missing or incomplete")
        for key in ("artifact_ref", "expected", "actual"):
            require(report, key, surface, issues)
        if require(report, "source_revision", surface, issues) != identities.get("source_revision"):
            issues.append(f"{surface} surface has a stale source")
        if surface == "api":
            require(report, "readback_ref", surface, issues)
        else:
            for key in ("command", "stdout_ref", "stderr_ref"):
                require(report, key, surface, issues)
            if (type(report.get("exit_code")) is not int or report["exit_code"] != 0
                    or report.get("cli_checksum") != identities.get("cli_checksum")):
                issues.append("installed CLI exit or checksum does not match")
    if "ui" in surfaces:
        ui = surfaces["ui"] if isinstance(surfaces["ui"], dict) else {}
        if (ui.get("status") != "pass" or not present(ui.get("artifact_ref"))
                or not present(ui.get("screenshot_ref"))
                or ui.get("source_revision") != identities.get("source_revision")):
            issues.append("UI evidence is incomplete or stale")
    cases = packet["cases"] if isinstance(packet["cases"], dict) else {}
    cleanup = packet["cleanup"] if isinstance(packet["cleanup"], dict) else {}
    for name in CASES:
        case = cases.get(name) if isinstance(cases.get(name), dict) else {}
        if (case.get("ac_id"), case.get("scenario_id")) != SCENARIOS[name]:
            issues.append(f"{name} AC/scenario mapping is missing or wrong")
        if case.get("run_id") != run_id or not present(case.get("test_ref")):
            issues.append(f"{name} run/test identity is missing or wrong")
        for key in ("expected", "actual", "evidence_ref", "readback_ref"):
            require(case, key, name, issues)
        attempts = case.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            issues.append(f"{name} attempts are missing")
        else:
            seen = set()
            for attempt in attempts:
                attempt_id = require(attempt, "attempt_id", name, issues)
                if not present(attempt_id):
                    issues.append(f"{name} has an invalid attempt ID")
                elif attempt_id in seen:
                    issues.append(f"{name} has duplicate attempt IDs")
                else:
                    seen.add(attempt_id)
                if not isinstance(attempt, dict) or attempt.get("result") not in ("pass", "fail", "interrupted"):
                    issues.append(f"{name} has a missing, skipped or unknown attempt result")
                for key in ("expected", "actual", "evidence_ref"):
                    require(attempt, key, name, issues)
            if not isinstance(attempts[-1], dict) or attempts[-1].get("result") != "pass":
                issues.append(f"{name} last attempt did not pass")
            if name == "api_recovery_cleanup" and not any(
                isinstance(attempt, dict) and attempt.get("result") == "interrupted" for attempt in attempts
            ):
                issues.append("recovery has no recorded interrupted write")
        if case.get("status") != "pass":
            issues.append(f"{name} did not pass")
        result = cleanup.get(name) if isinstance(cleanup.get(name), dict) else {}
        if (result.get("status") != "verified" or result.get("owned_absent") is not True
                or result.get("shared_unchanged") is not True):
            issues.append(f"{name} owned cleanup or shared-resource check is incomplete")
        receipt = require(result, "receipt_ref", f"{name}.cleanup", issues)
        if case.get("cleanup_ref") != receipt or not present(receipt):
            issues.append(f"{name} cleanup receipt does not match the case")
    packet["evidence_schema"] = PACKET_SCHEMA
    packet["status"] = "pass" if not issues else "incomplete"
    packet["issues"] = issues
    digest = hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    packet["packet_revision"] = f"sha256:{digest}"
    return packet

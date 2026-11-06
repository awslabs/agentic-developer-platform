"""Assemble a private B02 packet without turning absent live evidence into a pass."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

SCHEMA = "product-b02/v1"
CASES = {
    "live_browser_create": ("AC-01", "SCN-01", "tests/e2e/new_ui/test_product_profile.py::test_product_create"),
    "live_browser_conflict_denial": ("AC-02", "SCN-02", "tests/e2e/new_ui/test_product_profile.py::test_product_conflict_denial"),
    "live_browser_evidence": ("AC-03", "SCN-03", "tests/e2e/new_ui/test_product_profile.py::test_product_evidence"),
}
REVISION = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def build_packet(inputs, *, case_specs=CASES, require_traces=True):
    """Preserve incomplete and previous attempts; only complete live observations can be review-ready."""
    if not isinstance(inputs, dict):
        raise TypeError("B02 evidence inputs must be a mapping")
    packet = deepcopy({key: inputs.get(key) for key in (
        "run_id", "plan_revision", "identities", "deployment", "cases", "surfaces", "cleanup", "report_artifact",
    )})
    problems = []

    def require(mapping, key, label, pattern=None):
        value = mapping.get(key) if isinstance(mapping, dict) else None
        if not isinstance(value, str) or not value.strip() or (pattern and not pattern.fullmatch(value)):
            problems.append(f"{label}.{key} is missing or invalid")
        return value

    run_id = require(packet, "run_id", "packet")
    require(packet, "plan_revision", "packet", REVISION)
    require(packet, "report_artifact", "packet")
    identities = packet["identities"] if isinstance(packet["identities"], dict) else {}
    for key in (
        "requirements_revision", "source_revision", "browser_code_revision",
        "browser_workflow_revision", "api_workflow_revision", "frontend_workflow_revision",
    ):
        require(identities, key, "identities", REVISION)
    require(identities, "schema_head", "identities")
    require(identities, "api_image_digest", "identities", IMAGE)
    for key in ("frontend_release_sha256", "cli_checksum"):
        require(identities, key, "identities", DIGEST)
    require(identities, "frontend_release_ref", "identities")

    deployment = packet["deployment"] if isinstance(packet["deployment"], dict) else {}
    receipt = require(deployment, "receipt_ref", "deployment")
    observation = require(deployment, "observation_ref", "deployment")
    if receipt == observation:
        problems.append("independent deployment observation must differ from the release receipt")
    observed = deployment.get("observed") if isinstance(deployment.get("observed"), dict) else {}
    for key in (
        "source_revision", "api_workflow_revision", "frontend_workflow_revision",
        "schema_head", "api_image_digest", "frontend_release_sha256", "cli_checksum",
    ):
        if require(observed, key, "observation") != identities.get(key):
            problems.append(f"observation.{key} differs from the pinned artifact")
    if require(observed, "frontend_release_ref", "observation") != identities.get("frontend_release_ref"):
        problems.append("served frontend release differs from the signed receipt")
    if observed.get("product_profile_enabled") is not True:
        problems.append("the observed backend does not admit Product Profile")

    surfaces = packet["surfaces"] if isinstance(packet["surfaces"], dict) else {}
    for name in ("ui", "api"):
        surface = surfaces.get(name) if isinstance(surfaces.get(name), dict) else {}
        for key in ("artifact_ref", "expected", "actual", "report_ref"):
            require(surface, key, name)
        workflow_run = surface.get("workflow_run_id")
        workflow_attempt = surface.get("workflow_attempt")
        if type(workflow_run) is not int or workflow_run <= 0 or type(workflow_attempt) is not int or workflow_attempt <= 0:
            problems.append(f"{name} workflow run/attempt identity is missing")
        if surface.get("status") != "pass" or surface.get("run_id") != run_id:
            problems.append(f"{name} has no passing report for the selected run")
        if require(surface, "source_revision", name) != identities.get("source_revision"):
            problems.append(f"{name} source differs from the selected release")
        if require(surface, "browser_workflow_revision", name) != identities.get("browser_workflow_revision"):
            problems.append(f"{name} runner revision differs from the selected workflow")
    ui = surfaces.get("ui") if isinstance(surfaces.get("ui"), dict) else {}
    if require(ui, "frontend_release_ref", "ui") != identities.get("frontend_release_ref"):
        problems.append("UI report does not identify the signed frontend release")
    viewports = ui.get("viewports") if isinstance(ui.get("viewports"), dict) else {}
    for width in ("390", "1440"):
        viewport = viewports.get(width)
        for key in (("screenshot_ref", "trace_ref") if require_traces else ("screenshot_ref",)):
            require(viewport, key, f"ui.viewport.{width}")
        if not isinstance(viewport, dict) or viewport.get("overflow") is not False or viewport.get("keyboard") != "pass":
            problems.append(f"ui.viewport.{width} failed keyboard or overflow checks")
    api = surfaces.get("api") if isinstance(surfaces.get("api"), dict) else {}
    readback = require(api, "readback_ref", "api")
    if readback == ui.get("artifact_ref") or readback == ui.get("report_ref"):
        problems.append("API readback must be independent from the browser report")
    case_readbacks = api.get("case_readback_refs") if isinstance(api.get("case_readback_refs"), dict) else {}
    if (api.get("workflow_run_id"), api.get("workflow_attempt")) != (ui.get("workflow_run_id"), ui.get("workflow_attempt")):
        problems.append("API and UI reports come from different attempts")
    cli = surfaces.get("cli") if isinstance(surfaces.get("cli"), dict) else {}
    require(cli, "artifact_ref", "cli")
    if cli.get("cli_checksum") != identities.get("cli_checksum") or cli.get("source_revision") != identities.get("source_revision"):
        problems.append("CLI artifact identity differs from the selected release")
    if cli.get("status") not in ("artifact_only", "pass"):
        problems.append("CLI artifact status is missing or failed")
    if cli.get("status") == "pass" and (
        not isinstance(cli.get("exit_code"), int) or isinstance(cli.get("exit_code"), bool) or cli["exit_code"] != 0
        or any(not isinstance(cli.get(key), str) or not cli[key] for key in ("command", "stdout_ref", "stderr_ref"))
    ):
        problems.append("installed CLI report is incomplete")

    cleanup = packet["cleanup"] if isinstance(packet["cleanup"], dict) else {}
    cleanup_ref = require(cleanup, "receipt_ref", "cleanup")
    if cleanup.get("run_id") != run_id or cleanup.get("status") != "verified" or any(
        cleanup.get(key) is not True for key in ("fixture_removed", "owned_absent", "shared_unchanged")
    ):
        problems.append("run-owned cleanup and independent absence are not verified")
    for key in ("product_id", "project_id"):
        require(cleanup, key, "cleanup")

    cases = packet["cases"] if isinstance(packet["cases"], dict) else {}
    if set(cases) != set(case_specs):
        problems.append("required B02 case inventory is incomplete or contains unexpected cases")
    for name, (criterion, scenario, test_ref) in case_specs.items():
        case = cases.get(name) if isinstance(cases.get(name), dict) else {}
        if (case.get("ac_id"), case.get("scenario_id"), case.get("test_ref")) != (criterion, scenario, test_ref):
            problems.append(f"{name} AC/scenario/test mapping differs from the reviewed case")
        if case.get("run_id") != run_id or case.get("status") != "pass":
            problems.append(f"{name} did not pass in the selected run")
        for key in ("expected", "actual", "evidence_ref", "readback_ref"):
            require(case, key, name)
        if (
            case.get("readback_ref") != case_readbacks.get(name)
            or case.get("readback_ref") in (ui.get("artifact_ref"), ui.get("report_ref"))
            or case.get("cleanup_ref") != cleanup_ref
        ):
            problems.append(f"{name} independent readback or cleanup receipt differs from the run")
        attempts = case.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            problems.append(f"{name} has no recorded attempts")
            continue
        seen = set()
        for attempt in attempts:
            attempt_id = require(attempt, "attempt_id", name)
            if attempt_id in seen:
                problems.append(f"{name} has duplicate attempt IDs")
            seen.add(attempt_id)
            if not isinstance(attempt, dict) or any(
                type(attempt.get(key)) is not int or attempt[key] <= 0
                for key in ("workflow_run_id", "workflow_attempt")
            ):
                problems.append(f"{name} attempt has no workflow run/attempt identity")
            if not isinstance(attempt, dict) or attempt.get("result") not in ("pass", "fail", "interrupted", "skip", "not_run"):
                problems.append(f"{name} has an unknown attempt result")
                continue
            for key in ("expected", "actual", "evidence_ref"):
                require(attempt, key, f"{name}.{attempt_id}")
            if attempt.get("result") in ("skip", "not_run"):
                require(attempt, "reason", f"{name}.{attempt_id}")
        if not isinstance(attempts[-1], dict) or attempts[-1].get("result") != "pass":
            problems.append(f"{name} last attempt is not a pass")
        elif (attempts[-1].get("workflow_run_id"), attempts[-1].get("workflow_attempt")) != (ui.get("workflow_run_id"), ui.get("workflow_attempt")):
            problems.append(f"{name} final attempt differs from the UI/API report")
    packet["evidence_schema"] = SCHEMA
    packet["status"] = "review_ready" if not problems else "incomplete"
    packet["issues"] = problems
    encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    packet["packet_revision"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return packet

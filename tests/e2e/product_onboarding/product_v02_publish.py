"""Bind private B02 evidence to the maintained browser runner's actual result files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.e2e.product_onboarding.product_v02_evidence import CASES, REVISION, build_packet

MAX_INPUT = 2 * 1024 * 1024


def read_json(path, label, issues):
    try:
        selected = Path(path)
        if selected.stat().st_size > MAX_INPUT:
            raise ValueError("evidence file exceeds the size bound")
        return json.loads(selected.read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
    except (OSError, ValueError, TypeError):
        issues.append(f"{label} is missing, invalid or oversized")
        return None


def check_browser_results(junit_path, inventory, report, *, checkout_revision, issues, case_specs=CASES):
    expected = set(spec[2] for spec in case_specs.values())
    if not isinstance(inventory, list) or not all(isinstance(value, str) for value in inventory) or not expected <= set(inventory):
        issues.append("browser inventory does not select every Product scenario")
        return
    if len(inventory) != len(set(inventory)) or len(inventory) > 500:
        issues.append("browser inventory is duplicated or unbounded")
        return
    if not isinstance(report, dict) or any(
        report.get(key) != value for key, value in (("status", "pass"), ("suite", "new-ui"), ("source_revision", checkout_revision))
    ) or any(report.get(key) != value for key, value in (
        ("expected", len(inventory)), ("executed", len(inventory)), ("passed", len(inventory)),
        ("failed", 0), ("skipped", 0), ("missing", 0),
    )):
        issues.append("maintained browser suite did not pass every selected test")
        return
    try:
        source = Path(junit_path)
        if source.stat().st_size > MAX_INPUT:
            raise ValueError("browser JUnit exceeds the size bound")
        cases = ET.parse(source).getroot().findall(".//testcase")
        found = []
        for case in cases:
            identities = [item.get("value") for item in case.findall("./properties/property") if item.get("name") == "adp_case_id"]
            if len(identities) != 1 or case.find("skipped") is not None or case.find("failure") is not None or case.find("error") is not None:
                raise ValueError("browser JUnit has missing or nonpassing scenario evidence")
            found.extend(identities)
        if len(found) != len(inventory) or set(found) != set(inventory) or len(set(found)) != len(found):
            raise ValueError("browser JUnit differs from the selected inventory")
    except (OSError, ValueError, TypeError, ET.ParseError):
        issues.append("browser JUnit is missing, incomplete or differs from the inventory")


def check_private_files(packet, output_path, issues, *, case_specs=CASES, require_traces=True):
    root = Path(output_path).parent.resolve()
    surfaces = packet.get("surfaces") if isinstance(packet.get("surfaces"), dict) else {}
    ui = surfaces.get("ui") if isinstance(surfaces.get("ui"), dict) else {}
    api = surfaces.get("api") if isinstance(surfaces.get("api"), dict) else {}
    cleanup = packet.get("cleanup") if isinstance(packet.get("cleanup"), dict) else {}
    cases = packet.get("cases") if isinstance(packet.get("cases"), dict) else {}
    files = {"ui artifact": ui.get("artifact_ref"), "ui report": ui.get("report_ref"),
             "api artifact": api.get("artifact_ref"), "api report": api.get("report_ref"),
             "api readback": api.get("readback_ref"), "cleanup receipt": cleanup.get("receipt_ref")}
    for width in ("390", "1440"):
        viewports = ui.get("viewports") if isinstance(ui.get("viewports"), dict) else {}
        viewport = viewports.get(width) if isinstance(viewports.get(width), dict) else {}
        for key in (("screenshot_ref", "trace_ref") if require_traces else ("screenshot_ref",)):
            files[f"viewport {width} {key}"] = viewport.get(key)
    if not require_traces:
        for key in ("inventory_ref", "suite_ref"):
            files[f"V11 {key}"] = ui.get(key)
        cli = surfaces.get("cli") if isinstance(surfaces.get("cli"), dict) else {}
        for key in ("artifact_ref", "stdout_ref", "stderr_ref"):
            files[f"V11 CLI {key}"] = cli.get(key)
    for name in case_specs:
        case = cases.get(name) if isinstance(cases.get(name), dict) else {}
        files[f"{name} API readback"] = case.get("readback_ref")
        files[f"{name} browser evidence"] = case.get("evidence_ref")
        if not require_traces:
            files[f"{name} scenario report"] = case.get("report_ref")
            if name == "v11_resume":
                files["V11 CLI event"] = case.get("cli_event_ref")
        attempts = case.get("attempts") if isinstance(case.get("attempts"), list) else []
        for index, attempt in enumerate(attempts):
            files[f"{name} attempt {index}"] = attempt.get("evidence_ref") if isinstance(attempt, dict) else None
    for label, reference in files.items():
        try:
            path = Path(reference)
            if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("evidence is outside the private result directory")
            record = path.stat()
            if not path.is_file() or not 0 < record.st_size <= 32 * 1024 * 1024 or record.st_uid != os.getuid() or record.st_mode & 0o077:
                raise ValueError("private evidence is missing, oversized or readable by others")
        except (OSError, TypeError, ValueError):
            issues.append(f"{label} has no retained private file")


def publish(inputs_path, junit_path, inventory_path, suite_path, output_path, *,
            release_revision, checkout_revision, workflow_revision, workflow_run_id, workflow_attempt):
    selected_output = Path(output_path)
    output_path = selected_output.parent.resolve() / selected_output.name
    issues = []
    inputs = read_json(inputs_path, "private B02 inputs", issues)
    if not isinstance(inputs, dict):
        inputs = {}
    packet = build_packet(inputs)
    inventory = read_json(inventory_path, "browser inventory", issues)
    report = read_json(suite_path, "browser suite report", issues)
    if any(not isinstance(value, str) or not REVISION.fullmatch(value) for value in (
        release_revision, checkout_revision, workflow_revision,
    )) or type(workflow_run_id) is not int or workflow_run_id <= 0 or type(workflow_attempt) is not int or workflow_attempt <= 0:
        issues.append("browser dispatch has no pinned source, workflow or run/attempt")
    identities = packet.get("identities") if isinstance(packet.get("identities"), dict) else {}
    for key, value in (
        ("source_revision", release_revision), ("browser_code_revision", checkout_revision),
        ("browser_workflow_revision", workflow_revision),
    ):
        if identities.get(key) != value:
            issues.append(f"packet {key} differs from the reviewed browser dispatch")
    if packet.get("report_artifact") != str(output_path):
        issues.append("packet output path differs from the selected private artifact")
    check_browser_results(junit_path, inventory, report, checkout_revision=checkout_revision, issues=issues)
    check_private_files(packet, output_path, issues)
    surfaces = packet.get("surfaces") if isinstance(packet.get("surfaces"), dict) else {}
    for name in ("ui", "api"):
        surface = surfaces.get(name) if isinstance(surfaces.get(name), dict) else {}
        if (surface.get("workflow_run_id"), surface.get("workflow_attempt")) != (workflow_run_id, workflow_attempt):
            issues.append(f"{name} report differs from the selected workflow run/attempt")
    packet["dispatch"] = {
        "selected_cases": sorted(CASES), "release_revision": release_revision,
        "browser_code_revision": checkout_revision, "browser_workflow_revision": workflow_revision,
        "workflow_run_id": workflow_run_id, "workflow_attempt": workflow_attempt,
        "junit_ref": str(junit_path), "inventory_ref": str(inventory_path), "suite_ref": str(suite_path),
    }
    packet["issues"].extend(issues)
    packet["status"] = "review_ready" if not packet["issues"] else "incomplete"
    packet.pop("packet_revision")
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(packet, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    return packet


def review_status(packet, step_statuses, manifest_version):
    if not isinstance(packet, dict) or packet.get("status") != "review_ready":
        return "incomplete"
    revision = packet.get("packet_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", revision):
        return "incomplete"
    content = {key: value for key, value in packet.items() if key != "packet_revision"}
    try:
        expected = "sha256:" + hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    except (TypeError, ValueError):
        return "incomplete"
    if (
        revision != expected or not isinstance(step_statuses, dict)
        or any(step_statuses.get(name) != "success" for name in ("browser", "report", "bind", "upload"))
        or not isinstance(manifest_version, str) or manifest_version in ("", "null")
    ):
        return "incomplete"
    return "review_ready"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "junit", "inventory", "suite", "output", "release-revision", "checkout-revision", "workflow-revision"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-attempt", required=True, type=int)
    args = parser.parse_args(argv)
    packet = publish(
        args.inputs, args.junit, args.inventory, args.suite, args.output,
        release_revision=args.release_revision, checkout_revision=args.checkout_revision,
        workflow_revision=args.workflow_revision, workflow_run_id=args.workflow_run_id,
        workflow_attempt=args.workflow_attempt,
    )
    return 0 if packet["status"] == "review_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())

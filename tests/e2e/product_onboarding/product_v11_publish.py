"""Bind private V11 browser reports to the maintained runner without claiming B09 acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from tests.e2e.product_onboarding.product_v02_publish import check_browser_results, check_private_files, read_json
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.product_v11_evidence import CASES, build_packet
from tests.e2e.product_onboarding.product_v11_selection import require_v11_inventory
from tests.e2e.product_onboarding.test_v11 import verify_cross_client_report, verify_full_journey_report, verify_release_report


def private_report(path, directory):
    if (path.parent != directory or path.is_symlink() or not path.is_file()
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077
            or not 0 < path.stat().st_size <= 2 * 1024 * 1024):
        raise ValueError("V11 case report is absent, oversized or not private")
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def version(packet):
    packet.pop("packet_revision", None)
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return packet


def publish(directory, inventory_path, junit_path, suite_path, output_path, *,
            release_revision, checkout_revision, workflow_revision, workflow_run_id, workflow_attempt):
    directory = Path(directory).resolve()
    output = Path(output_path).absolute()
    root = output.parent
    if (not directory.is_dir() or directory.is_symlink() or directory.stat().st_mode & 0o077
            or not root.is_dir() or root.is_symlink() or root.stat().st_mode & 0o077
            or not directory.is_relative_to(root) or output.name != "product-v11-packet.json"):
        raise ValueError("V11 needs a private owned result directory")
    run_id = f"product-{workflow_run_id}-{workflow_attempt}"
    if (not re.fullmatch(r"product-[1-9][0-9]*-[1-9][0-9]*", run_id)
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value)
                   for value in (release_revision, checkout_revision, workflow_revision))):
        raise ValueError("V11 workflow or release revisions are not pinned")
    issues = []
    inventory = read_json(inventory_path, "V11 browser inventory", issues)
    try:
        require_v11_inventory(inventory)
    except ValueError as failure:
        issues.append(str(failure))
    suite = read_json(suite_path, "V11 browser suite", issues)
    for path in (inventory_path, junit_path, suite_path):
        selected = Path(path).absolute()
        if (selected.parent != root or selected.is_symlink() or not selected.is_file()
                or selected.stat().st_uid != os.getuid() or selected.stat().st_mode & 0o077):
            issues.append("V11 runner inventory, JUnit or suite report is not private and owned")
    check_browser_results(junit_path, inventory, suite, checkout_revision=checkout_revision,
                          issues=issues, case_specs=CASES)
    reports = {}
    context = None
    for name in CASES:
        suffix = "v11-release" if name == "v11_release" else name
        path = directory / f"{run_id}-{suffix}-report.json"
        try:
            report = private_report(path, directory)
            if not isinstance(report, dict) or report.get("run_id") != run_id:
                raise ValueError("V11 case differs from the selected run")
            if name.startswith("v11_") and name not in ("v11_resume", "v11_release"):
                verify_full_journey_report(report, directory)
                if report["starting_point"] != name.removeprefix("v11_"):
                    raise ValueError("V11 journey starting point differs from its selector")
            elif name == "v11_resume":
                verify_cross_client_report(report, directory)
            else:
                verify_release_report(report, directory)
            observed_context = report.get("evidence_context")
            if (not isinstance(observed_context, dict) or observed_context.get("deployment", {}).get("observed") != report["observed"]
                    or observed_context.get("identities", {}).get("source_revision") != release_revision
                    or report["source_revision"] != release_revision
                    or report["requirements_revision"] != observed_context["identities"].get("requirements_revision")
                    or (context is not None and observed_context != context)):
                raise ValueError("V11 case differs from the independently observed deployment")
            context = observed_context
            reports[name] = (report, str(path))
        except (OSError, ValueError, KeyError, TypeError, AssertionError) as failure:
            issues.append(f"{name} lacks a valid private browser/API/cleanup report: {type(failure).__name__}")
    scopes = [(report["product_id"], report["project_id"]) for report, _ in reports.values()]
    if len(scopes) != len(set(scopes)):
        issues.append("V11 cases reused an owned Product scope")
    if context is None:
        context = {}
    identities = {**(context.get("identities") or {}), "browser_code_revision": checkout_revision,
                  "browser_workflow_revision": workflow_revision}
    cases, readbacks, cleanups = {}, {}, []
    for name, (criterion, scenario, selector) in CASES.items():
        report, report_ref = reports.get(name, ({}, None))
        readbacks[name] = private_json(root / f"{run_id}-{name}-api.json", report.get("api_reads", []))
        evidence_ref = private_json(root / f"{run_id}-{name}-ui.json", {
            "status": report.get("browser_status", "not_run"), "viewports": report.get("viewports", []),
        })
        cleanup = report.get("cleanup")
        if isinstance(cleanup, dict):
            cleanups.append(cleanup)
        status = "pass" if name in reports else "not_run"
        actual = f"Browser {status}; {len(report.get('api_reads', []))} independent API reads"
        attempt = {"attempt_id": f"{workflow_run_id}/{workflow_attempt}/{name}",
                   "workflow_run_id": workflow_run_id, "workflow_attempt": workflow_attempt,
                   "result": status, "expected": criterion + " browser and API assertions",
                   "actual": actual, "evidence_ref": report_ref or evidence_ref}
        if status != "pass":
            attempt["reason"] = "No verified V11 case report for this workflow attempt"
        cases[name] = {
            "ac_id": criterion, "scenario_id": scenario, "test_ref": selector,
            "run_id": run_id, "status": status, "expected": attempt["expected"], "actual": actual,
            "evidence_ref": evidence_ref, "report_ref": report_ref,
            "readback_ref": readbacks[name], "attempts": [attempt],
            **{key: report.get(key) for key in ("source_revision", "requirements_revision",
                                               "frontend_release_ref", "api_image_digest", "schema_head")},
        }
        if name == "v11_resume":
            session = report.get("session_after") or {}
            profile = report.get("profile_after") or {}
            cases[name].update(session_id=session.get("id"), operation_ids=session.get("operation_ids"),
                               peer_availability=profile.get("peer_availability"))
    cleanup_ref = private_json(root / f"{run_id}-v11-cleanup.json", cleanups)
    for case in cases.values():
        case["cleanup_ref"] = cleanup_ref
    cli_event = (reports.get("v11_resume", ({}, None))[0].get("cli_events") or [None])[0]
    cli_event = cli_event if isinstance(cli_event, dict) else {}
    cli_ref = private_json(root / f"{run_id}-v11-cli.json", cli_event)
    stdout_ref = private_json(root / f"{run_id}-v11-cli-stdout.json", cli_event.get("stdout"))
    stderr_ref = private_json(root / f"{run_id}-v11-cli-stderr.json", cli_event.get("stderr"))
    cases["v11_resume"]["cli_event_ref"] = cli_ref
    ui_ref = private_json(root / f"{run_id}-v11-ui.json", {name: case["evidence_ref"] for name, case in cases.items()})
    api_ref = private_json(root / f"{run_id}-v11-api.json", readbacks)
    api_report_ref = private_json(root / f"{run_id}-v11-api-status.json", {
        name: len(reports.get(name, ({}, None))[0].get("api_reads", [])) for name in CASES
    })
    selected = reports.get("v11_idea", ({}, None))[0].get("viewports") or []
    viewports = {str(item["width"]): {"screenshot_ref": item["screenshot"], "overflow": False,
                                      "keyboard": item.get("keyboard")}
                 for item in selected if isinstance(item, dict) and item.get("width") in (390, 1440)}
    cleaned = len(cleanups) == len(CASES) and all(
        item.get("run_id") == run_id and all(item.get(field) is True for field in (
            "fixture_removed", "owned_absent", "shared_unchanged")) for item in cleanups
    )
    common = {"run_id": run_id, "workflow_run_id": workflow_run_id, "workflow_attempt": workflow_attempt,
              "source_revision": release_revision, "browser_workflow_revision": workflow_revision,
              "expected": "All V11 browser and independent API assertions pass",
              "actual": "See private per-case reports and owned cleanup receipts"}
    all_pass = len(reports) == len(CASES) and not issues
    inputs = {"run_id": run_id, "plan_revision": context.get("plan_revision"),
              "report_artifact": str(output), "identities": identities,
              "deployment": context.get("deployment"), "cases": cases,
              "surfaces": {
                  "ui": {**common, "status": "pass" if all_pass else "incomplete", "artifact_ref": ui_ref,
                         "report_ref": str(junit_path), "frontend_release_ref": identities.get("frontend_release_ref"),
                         "viewports": viewports, "inventory_ref": str(Path(inventory_path).absolute()),
                         "suite_ref": str(Path(suite_path).absolute())},
                  "api": {**common, "status": "pass" if all_pass else "incomplete", "artifact_ref": api_ref,
                          "report_ref": api_report_ref, "readback_ref": api_ref, "case_readback_refs": readbacks},
                  "cli": {"status": "pass" if cli_event.get("exit_code") == 0 else "incomplete",
                          "source_revision": release_revision, "cli_checksum": identities.get("cli_checksum"),
                          "artifact_ref": cli_ref, "command": " ".join(cli_event.get("command", [])),
                          "stdout_ref": stdout_ref, "stderr_ref": stderr_ref,
                          "exit_code": cli_event.get("exit_code")},
              },
              "cleanup": {"run_id": run_id, "status": "verified" if cleaned else "incomplete",
                          "receipt_ref": cleanup_ref, "fixture_removed": cleaned, "owned_absent": cleaned,
                          "shared_unchanged": cleaned,
                          "product_id": cleanups[-1].get("product_id") if cleaned else None,
                          "project_id": cleanups[-1].get("project_id") if cleaned else None},
              }
    packet = build_packet(inputs)
    check_private_files(packet, output, issues, case_specs=CASES, require_traces=False)
    if issues:
        packet["issues"].extend(issues)
        packet["status"] = "incomplete"
        version(packet)
    private_json(output, packet)
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish a private, fail-closed V11 browser contribution")
    for name in ("directory", "inventory", "junit", "suite", "output", "release_revision",
                 "checkout_revision", "workflow_revision", "workflow_run_id", "workflow_attempt"):
        parser.add_argument("--" + name.replace("_", "-"), required=True)
    values = vars(parser.parse_args(argv))
    packet = publish(values.pop("directory"), values.pop("inventory"), values.pop("junit"),
                     values.pop("suite"), values.pop("output"),
                     **{**values, "workflow_run_id": int(values["workflow_run_id"]),
                        "workflow_attempt": int(values["workflow_attempt"])})
    if packet["status"] != "contribution_ready":
        raise SystemExit("V11 browser evidence is incomplete; private result retained")


if __name__ == "__main__":
    main()


def contribution_status(packet, steps, manifest_version):
    if (not isinstance(packet, dict) or packet.get("status") != "contribution_ready"
            or not isinstance(steps, dict) or set(steps) != {
                "browser", "report", "inventory", "bind", "cleanup", "upload"
            } or any(status != "success" for status in steps.values())
            or not isinstance(manifest_version, str) or not manifest_version
            or build_packet(packet) != packet):
        return "incomplete"
    return "contribution_ready"

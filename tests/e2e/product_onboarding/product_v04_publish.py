"""Bind private B03 browser, API and installed-CLI reports to one attempt."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from tests.e2e.product_onboarding.product_v02_publish import check_browser_results
from tests.e2e.product_onboarding.product_v04_evidence import (
    CASES,
    SURFACE_SELECTORS,
    build_packet,
    review_binding,
)

REVISION = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LIMIT = 2 * 1024 * 1024
BROWSER_CASES = {
    criterion + "-" + surface: (criterion, CASES[criterion][0], selector)
    for criterion, selections in SURFACE_SELECTORS.items()
    for surface, selector in selections.items()
    if criterion != "AC-03" and surface in ("api", "browser")
}


def private_file(root, reference):
    path = Path(reference)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.resolve().is_relative_to(root)
        or not path.is_file()
    ):
        raise ValueError("B03 evidence is outside the private attempt")
    stat = path.stat()
    if (
        not 0 < stat.st_size <= LIMIT
        or stat.st_uid != os.getuid()
        or stat.st_mode & 0o077
    ):
        raise ValueError("B03 evidence is missing, oversized or not private")
    return path


def load(root, reference, issues, label):
    try:
        return json.loads(
            private_file(root, reference).read_text(),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("invalid JSON")
            ),
        )
    except (OSError, TypeError, ValueError):
        issues.append(label + " is missing, invalid or not private")
        return None


def artifact(root, reference, issues, label):
    try:
        path = private_file(root, reference)
        return "evidence/" + path.relative_to(root).as_posix()
    except (OSError, TypeError, ValueError):
        issues.append(label + " has no retained private artifact")
        return None


def publish(
    browser_path,
    cli_path,
    junit_path,
    inventory_path,
    suite_path,
    output_path,
    *,
    release_revision,
    checkout_revision,
    workflow_revision,
    workflow_run_id,
    workflow_attempt,
):
    output = Path(output_path).resolve()
    root = output.parent
    issues = []
    browser = load(root, browser_path, issues, "browser attempt")
    cli = load(root, cli_path, issues, "installed CLI attempt")
    if not isinstance(browser, dict):
        issues.append("browser attempt is not a report")
        browser = {}
    if not isinstance(cli, dict):
        issues.append("installed CLI attempt is not a report")
        cli = {}
    inventory = load(root, inventory_path, issues, "browser inventory")
    suite = load(root, suite_path, issues, "browser suite report")
    if (
        not all(
            isinstance(value, str) and REVISION.fullmatch(value)
            for value in (release_revision, checkout_revision, workflow_revision)
        )
        or type(workflow_run_id) is not int
        or workflow_run_id <= 0
        or type(workflow_attempt) is not int
        or workflow_attempt <= 0
    ):
        issues.append("B03 dispatch lacks a pinned source, workflow or attempt")
    if (
        browser.get("source_revision"),
        browser.get("checkout_revision"),
        browser.get("workflow_revision"),
        browser.get("workflow_run_id"),
        browser.get("workflow_attempt"),
    ) != (
        release_revision,
        checkout_revision,
        workflow_revision,
        workflow_run_id,
        workflow_attempt,
    ):
        issues.append("browser attempt differs from the reviewed release dispatch")
    if checkout_revision != release_revision:
        issues.append("browser code differs from the published source revision")
    if inventory != [spec[2] for spec in BROWSER_CASES.values()]:
        issues.append("B03 inventory differs from the reviewed API/browser cases")
    check_browser_results(
        junit_path,
        inventory,
        suite,
        checkout_revision=checkout_revision,
        issues=issues,
        case_specs=BROWSER_CASES,
    )
    run_id = browser.get("run_id")
    expected_run = f"product-{workflow_run_id}-{workflow_attempt}"
    if run_id != expected_run or any(
        cli.get(key) != expected
        for key, expected in (
            ("run_id", expected_run),
            ("source_revision", release_revision),
            ("workflow_run_id", workflow_run_id),
            ("workflow_attempt", workflow_attempt),
            ("workflow_revision", workflow_revision),
        )
    ):
        issues.append("B03 installed CLI and browser belong to different attempts")
    context = browser.get("context") if isinstance(browser.get("context"), dict) else {}
    identity = (
        context.get("identities") if isinstance(context.get("identities"), dict) else {}
    )
    deployment = (
        context.get("deployment") if isinstance(context.get("deployment"), dict) else {}
    )
    observed = (
        deployment.get("observed")
        if isinstance(deployment.get("observed"), dict)
        else {}
    )
    if identity.get("source_revision") != release_revision or identity.get(
        "cli_checksum"
    ) != cli.get("release_manifest_sha256"):
        issues.append("signed Product releases differ from the selected clients")
    if not isinstance(cli.get("workflow_revision"), str) or not REVISION.fullmatch(
        cli["workflow_revision"]
    ):
        issues.append("installed CLI workflow revision is not pinned")
    if not isinstance(cli.get("installed_cli_sha256"), str) or not SHA256.fullmatch(
        cli["installed_cli_sha256"]
    ):
        issues.append("installed CLI artifact hash is missing")
    artifacts = {
        "api_image_digest": identity.get("api_image_digest"),
        "frontend_manifest_sha256": identity.get("frontend_release_sha256"),
        "installed_cli_sha256": cli.get("installed_cli_sha256"),
        "api_workflow_revision": identity.get("api_workflow_revision"),
        "browser_workflow_revision": workflow_revision,
        "cli_workflow_revision": cli.get("workflow_revision"),
        "schema_head": identity.get("schema_head"),
    }
    observed_artifacts = {
        name: observed.get(
            "frontend_release_sha256" if name == "frontend_manifest_sha256" else name
        )
        for name in artifacts
        if name not in ("installed_cli_sha256", "cli_workflow_revision")
    }
    observed_artifacts["browser_workflow_revision"] = workflow_revision
    observed_artifacts["installed_cli_sha256"] = cli.get("installed_cli_sha256")
    observed_artifacts["cli_workflow_revision"] = cli.get("workflow_revision")
    receipt = load(root, browser.get("cleanup_ref"), issues, "scope cleanup")
    if not isinstance(receipt, dict):
        issues.append("scope cleanup receipt is not a record")
        receipt = {}
    cli_cleanup = cli.get("cleanup") if isinstance(cli.get("cleanup"), dict) else {}
    cleanup_ref = artifact(root, browser.get("cleanup_ref"), issues, "scope cleanup")
    cases = {}
    browser_cases = (
        browser.get("cases") if isinstance(browser.get("cases"), dict) else {}
    )
    cli_cases = cli.get("cases") if isinstance(cli.get("cases"), dict) else {}
    if set(browser_cases) != set(CASES):
        issues.append("browser case inventory is incomplete")
    for criterion in CASES:
        selected = browser_cases.get(criterion)
        expected = {
            surface
            for surface in SURFACE_SELECTORS[criterion]
            if criterion != "AC-03" and surface in ("api", "browser")
        }
        if not isinstance(selected, dict) or set(selected) != expected:
            issues.append(f"{criterion} browser/API surfaces are missing or unreviewed")
    if set(cli_cases) != set(CASES):
        issues.append("installed CLI case inventory is incomplete")
    for criterion, (scenario, selector, surfaces) in CASES.items():
        attempts = []
        for surface in surfaces:
            selected = SURFACE_SELECTORS[criterion][surface]
            if surface == "cli" or criterion == "AC-03":
                cli_result = (
                    cli_cases.get(criterion)
                    if isinstance(cli_cases.get(criterion), dict)
                    else {}
                )
                field = "attempts" if surface == "cli" else "api_attempts"
                records = (
                    cli_result.get(field)
                    if isinstance(cli_result.get(field), list)
                    else []
                )
                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        issues.append("installed CLI attempt is not a record")
                        continue
                    copied = dict(record)
                    for key in (
                        ("artifact_ref", "readback_ref", "stdout_ref")
                        if surface == "cli"
                        else ("artifact_ref", "readback_ref")
                    ):
                        copied[key] = artifact(
                            root,
                            record.get(key),
                            issues,
                            f"{criterion}.{surface}.{key}",
                        )
                    attempts.append(copied)
            else:
                result = (
                    browser_cases.get(criterion, {}).get(surface, {})
                    if isinstance(browser_cases.get(criterion), dict)
                    else {}
                )
                if not isinstance(result, dict):
                    result = {}
                if (
                    result.get("selector") != selected
                    or not isinstance(result.get("reads"), int)
                    or result["reads"] <= 0
                ):
                    issues.append(
                        f"{criterion}.{surface} has no independent executed API reads"
                    )
                result_file = (
                    load(
                        root,
                        result.get("artifact"),
                        issues,
                        f"{criterion}.{surface} result",
                    )
                    or {}
                )
                phases = (
                    result_file.get("phases") if isinstance(result_file, dict) else {}
                )
                if (
                    not isinstance(phases, dict)
                    or result.get("status") != "pass"
                    or any(
                        phases.get(phase) != "passed"
                        for phase in ("setup", "call", "teardown")
                    )
                ):
                    issues.append(
                        f"{criterion}.{surface} case was skipped, failed or interrupted"
                    )
                reads = load(
                    root,
                    result.get("readback"),
                    issues,
                    f"{criterion}.{surface} readback",
                )
                if (
                    not isinstance(reads, list)
                    or type(result.get("reads")) is not int
                    or len(reads) != result["reads"]
                    or not all(
                        isinstance(read, dict) and type(read.get("status")) is int
                        for read in reads
                    )
                ):
                    issues.append(
                        f"{criterion}.{surface} has no complete independent API observations"
                    )
                attempt = {
                    "attempt_id": f"{run_id}/{criterion}/{surface}",
                    "run_id": run_id,
                    "surface": surface,
                    "test_ref": selected,
                    "result": result.get("status"),
                    "requirements_revision": identity.get("requirements_revision"),
                    "source_revision": release_revision,
                    "workflow_run_id": workflow_run_id,
                    "workflow_attempt": workflow_attempt,
                    "expected": "Shared identity and isolated scope data",
                    "actual": f"{surface} result {result.get('status')}",
                    "artifact_ref": artifact(
                        root,
                        result.get("artifact"),
                        issues,
                        f"{criterion}.{surface}.artifact",
                    ),
                    "readback_ref": artifact(
                        root,
                        result.get("readback"),
                        issues,
                        f"{criterion}.{surface}.readback",
                    ),
                }
                if surface == "browser":
                    captures = (
                        result.get("viewports")
                        if isinstance(result.get("viewports"), list)
                        else []
                    )
                    latest = (
                        captures[-1]
                        if captures and isinstance(captures[-1], dict)
                        else {}
                    )
                    for key, field in (
                        ("screenshot_ref", "screenshot"),
                        ("trace_ref", "trace"),
                    ):
                        attempt[key] = artifact(
                            root,
                            latest.get(field),
                            issues,
                            f"{criterion}.browser.{field}",
                        )
                attempts.append(attempt)
        cases[criterion] = {
            "scenario_id": scenario,
            "test_ref": selector,
            "expected": "Scope identity, permissions and history verified",
            "actual": "See separate surface reports",
            "attempts": attempts,
        }
    inputs = {
        "run_id": run_id,
        "source_revision": release_revision,
        "plan_revision": context.get("plan_revision"),
        "requirements_revision": identity.get("requirements_revision"),
        "artifacts": artifacts,
        "deployment": {
            "source_revision": observed.get("source_revision"),
            **observed_artifacts,
            "receipt_ref": deployment.get("receipt_ref"),
            "observation_ref": artifact(
                root, deployment.get("observation_ref"), issues, "runtime observation"
            ),
        },
        "cases": cases,
        "cleanup": {
            "run_id": receipt.get("run_id"),
            "status": "verified"
            if receipt.get("verified") is True
            and cli_cleanup.get("status") == "verified"
            else "incomplete",
            "receipt_ref": cleanup_ref,
            "owned_absent": receipt.get("receipt", {}).get("owned_absent") is True
            if isinstance(receipt.get("receipt"), dict)
            else False,
            "original_projects_preserved": cli_cleanup.get(
                "original_projects_preserved"
            )
            is True,
            "reused_assets_preserved": cli_cleanup.get("reused_assets_preserved")
            is True,
        },
    }
    packet = build_packet(inputs)
    packet["issues"].extend(issues)
    if issues:
        packet["status"] = "incomplete"
        from hashlib import sha256

        encoded = json.dumps(
            {
                key: value
                for key, value in packet.items()
                if key not in ("packet_revision", "review")
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        packet["packet_revision"] = "sha256:" + sha256(encoded).hexdigest()
        packet["review"]["packet_revision"] = packet["packet_revision"]
        packet["review"]["status"] = "not_ready"
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(packet, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return packet


def review_status(packet, steps, manifest_version):
    try:
        review_binding(packet)
    except (ValueError, TypeError):
        return "incomplete"
    if (
        not isinstance(steps, dict)
        or any(
            steps.get(name) != "success"
            for name in ("browser", "cleanup", "report", "inventory", "bind", "upload")
        )
        or not isinstance(manifest_version, str)
        or manifest_version in ("", "null")
    ):
        return "incomplete"
    return "review_ready"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "browser",
        "cli",
        "junit",
        "inventory",
        "suite",
        "output",
        "release-revision",
        "checkout-revision",
        "workflow-revision",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--workflow-attempt", type=int, required=True)
    args = parser.parse_args(argv)
    packet = publish(
        args.browser,
        args.cli,
        args.junit,
        args.inventory,
        args.suite,
        args.output,
        release_revision=args.release_revision,
        checkout_revision=args.checkout_revision,
        workflow_revision=args.workflow_revision,
        workflow_run_id=args.workflow_run_id,
        workflow_attempt=args.workflow_attempt,
    )
    return 0 if packet["status"] == "review_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())

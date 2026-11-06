"""Assemble protected B06 guidance evidence; local fixtures never establish deployment."""

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from tests.e2e.new_ui.product_session_recovery import assert_cleanup_receipt
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.test_v07 import (
    GUIDANCE_EFFECTS_SELECTOR, GUIDANCE_RESUME_SELECTOR, GUIDED_SELECTOR,
    GUIDANCE_TURN_ORDER, RECOVERY_STAGES, pinned_guidance_effects, pinned_guidance_resume,
    pinned_guided_journeys, protected_report, verify_live_guidance_effects,
    verify_live_guidance_resume, verify_live_guided_journeys,
)

SCHEMA = "product-v07/v1"
BOLT_ID = "B06"
DECISION_PROCEDURE = (
    "docs/delivery-flow/plans/product-onboarding-8141/human-checks.md"
    "#record-one-decision-using-the-existing-verification-stories"
)
CASES = {"AC-01": ("SCN-01", GUIDED_SELECTOR, ("idea", "existing_assets", "running_product")),
         "AC-02": ("SCN-02", GUIDANCE_EFFECTS_SELECTOR, GUIDANCE_TURN_ORDER),
         "AC-03": ("SCN-03", GUIDANCE_RESUME_SELECTOR, tuple(sorted(RECOVERY_STAGES)))}
IDENTITY_FIELDS = ("source_revision", "requirements_revision", "api_workflow_revision",
                   "frontend_workflow_revision", "schema_head", "api_image_digest",
                   "frontend_release_sha256", "frontend_release_ref", "cli_checksum")


def private_image(path, directory):
    path = Path(path)
    if path.is_symlink() or path.parent != directory:
        raise ValueError("Browser evidence screenshot is outside the protected run")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as image:
        info = os.fstat(image.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 10 * 1024 * 1024):
            raise ValueError("Browser evidence screenshot is not private and bounded")


def build_packet(inputs, directory):
    if not isinstance(inputs, dict) or not isinstance(inputs.get("run_id"), str) or not re.fullmatch(
        r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", inputs["run_id"]
    ):
        raise ValueError("B06 packet needs a bounded workflow run and attempt")
    directory = Path(directory)
    info = directory.stat()
    if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("B06 packet directory must be private and owned")
    run_id = inputs["run_id"]
    issues = []
    pins = {name: inputs.get(name) for name in ("source_revision", "requirements_revision", "plan_revision")}
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value) for value in pins.values()):
        issues.append("source, requirements and plan revisions are not pinned")
    identities = inputs.get("identities") if isinstance(inputs.get("identities"), dict) else {}
    deployment = inputs.get("deployment") if isinstance(inputs.get("deployment"), dict) else {}
    observed = deployment.get("observed") if isinstance(deployment.get("observed"), dict) else {}
    if (any(not isinstance(identities.get(key), str) or not identities[key]
            or identities[key] != observed.get(key) for key in IDENTITY_FIELDS)
            or any(identities.get(key) != pins[key] for key in ("source_revision", "requirements_revision"))
            or any(not isinstance(identities.get(key), str) or not re.fullmatch(r"[a-f0-9]{40}", identities[key])
                   for key in ("api_workflow_revision", "frontend_workflow_revision"))
            or any(not isinstance(identities.get(key), str) or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", identities[key])
                   for key in ("api_image_digest", "frontend_release_sha256", "cli_checksum"))
            or not isinstance(identities.get("schema_head"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identities["schema_head"])
            or observed.get("product_onboarding_enabled") is not True
            or any(not isinstance(deployment.get(key), str) or not deployment[key]
                   for key in ("receipt_ref", "observation_ref"))
            or deployment.get("receipt_ref") == deployment.get("observation_ref")):
        issues.append("deployment receipt and independent observation do not match pinned API, UI, CLI and schema")
    cli_hashes = inputs.get("cli_hashes") if isinstance(inputs.get("cli_hashes"), dict) else {}
    if set(cli_hashes) != {"adp", "adp-onboard.py"} or any(
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in cli_hashes.values()
    ):
        issues.append("installed CLI and helper checksums are missing")

    reports = {}
    guided = {}
    try:
        case = pinned_guided_journeys(pins["source_revision"], pins["requirements_revision"], run_id)
        guided = {point: protected_report(directory / f"{run_id}-guidance_{point}-report.json", directory)
                  for point in case["starting_points"]}
        verify_live_guided_journeys(case, guided, directory)
        for point, report in guided.items():
            browser = protected_report(report["artifacts"]["browser"], directory)
            if Path(browser["screenshot"]) != directory / f"{run_id}-guidance_{point}.png":
                raise ValueError("guided screenshot does not belong to this starting point")
            private_image(browser["screenshot"], directory)
            cli = protected_report(report["artifacts"]["installed_cli"], directory)
            if any(event.get("installed_cli_sha256") != cli_hashes.get("adp")
                   or not isinstance(event.get("stdout"), str) or not isinstance(event.get("stderr"), str)
                   for event in cli):
                raise ValueError("guided CLI output or installed binary differs from the pin")
            cleanup = report["cleanup"]
            if (type(cleanup.get("cleanup_revision")) is not int or cleanup["cleanup_revision"] < 1
                    or cleanup.get("run_id") != run_id
                    or any(not isinstance(cleanup.get(field), str) or not cleanup[field]
                           for field in ("product_id", "project_id"))):
                raise ValueError("guided owned-fixture cleanup lacks a durable receipt")
            api = protected_report(report["artifacts"]["api"], directory)
            if not any(isinstance(read.get("body"), dict) and all(
                read["body"].get(field) == cleanup[field] for field in ("product_id", "project_id")
            ) for read in api):
                raise ValueError("guided cleanup does not match independent Product readback")
            assert_cleanup_receipt(cleanup, {"run_id": run_id, "product_id": cleanup["product_id"],
                                            "project_id": cleanup["project_id"]})
        reports["AC-01"] = {"status": "pass", "cases": {point: {
            "report_ref": str(directory / f"{run_id}-guidance_{point}-report.json"),
            "artifacts": guided[point]["artifacts"], "cleanup": guided[point]["cleanup"]}
            for point in case["starting_points"]}}
    except (OSError, KeyError, TypeError, ValueError, AssertionError) as error:
        issues.append(f"AC-01 guided browser, installed CLI, API or cleanup evidence is incomplete: {type(error).__name__}")
        reports["AC-01"] = {"status": "incomplete", "cases": {}}

    effects = None
    try:
        case = pinned_guidance_effects(pins["source_revision"], pins["requirements_revision"], run_id)
        effects_ref = directory / f"{run_id}-guidance-effects.json"
        effects = protected_report(effects_ref, directory)
        verify_live_guidance_effects(case, effects)
        fixture = inputs["effects_fixture"]
        if not isinstance(fixture, dict) or any(
            not isinstance(fixture.get(key), str) or not fixture[key]
            for key in ("tenant_id", "actor_id", "product_id", "project_id", "usage_ref", "cleanup_ref")
        ) or any(turn["tenant_id"] != fixture["tenant_id"] or turn["actor_id"] != fixture["actor_id"]
                 for turn in effects["turns"].values()):
            raise ValueError("metering does not belong to the owned Product actor")
        usage = protected_report(fixture["usage_ref"], directory)
        cleanup = protected_report(fixture["cleanup_ref"], directory)
        if (usage != {"run_id": run_id, "turns": effects["turns"]}
                or not isinstance(cleanup, dict) or cleanup.get("run_id") != run_id
                or type(cleanup.get("cleanup_revision")) is not int or cleanup["cleanup_revision"] < 1
                or fixture["usage_ref"] == fixture["cleanup_ref"]):
            raise ValueError("independent usage or run-owned metering cleanup is missing")
        assert_cleanup_receipt(cleanup, {**fixture, "run_id": run_id})
        reports["AC-02"] = {"status": "pass", "report_ref": str(effects_ref),
                            "usage_ref": fixture["usage_ref"], "cleanup_ref": fixture["cleanup_ref"]}
    except (OSError, KeyError, TypeError, ValueError, AssertionError) as error:
        issues.append(f"AC-02 metering, provider or cleanup evidence is incomplete: {type(error).__name__}")
        reports["AC-02"] = {"status": "incomplete"}

    resume = None
    try:
        case = pinned_guidance_resume(pins["source_revision"], pins["requirements_revision"], run_id)
        resume_ref = directory / f"{run_id}-guidance-resume-report.json"
        resume = protected_report(resume_ref, directory)
        verify_live_guidance_resume(case, resume, directory)
        cli = protected_report(resume["artifacts"]["installed_cli"], directory)
        if any(event.get("installed_cli_sha256") != cli_hashes.get("adp")
               or not isinstance(event.get("stdout"), str) or not isinstance(event.get("stderr"), str)
               for event in cli):
            raise ValueError("resumed CLI output or installed binary differs from the pin")
        reports["AC-03"] = {"status": "pass", "report_ref": str(resume_ref),
                            "artifacts": resume["artifacts"], "cleanup": resume["cleanup"]}
    except (OSError, KeyError, TypeError, ValueError, AssertionError) as error:
        issues.append(f"AC-03 cross-client recovery or cleanup evidence is incomplete: {type(error).__name__}")
        reports["AC-03"] = {"status": "incomplete"}

    manifest_ref = inputs.get("attempt_manifest_ref")
    try:
        manifest = protected_report(manifest_ref, directory)
        if not isinstance(manifest, dict) or manifest.get("run_id") != run_id or not isinstance(manifest.get("attempts"), dict):
            raise ValueError("attempt inventory differs from the selected run")
        raw_attempts = manifest["attempts"]
    except (OSError, TypeError, ValueError, AssertionError) as error:
        issues.append(f"attempt inventory is missing or not protected: {type(error).__name__}")
        raw_attempts = {}
    if set(raw_attempts) != set(CASES):
        issues.append("attempt inventory must cover every B06 acceptance criterion")
    results = {}
    seen = set()
    workflow_run, workflow_attempt = (int(value) for value in run_id.split("-")[1:])
    for criterion, (scenario, selector, required) in CASES.items():
        supplied = raw_attempts.get(criterion)
        attempts = supplied if isinstance(supplied, list) else []
        observed_cases = set()
        prior_issues = len(issues)
        for attempt in attempts:
            if not isinstance(attempt, dict):
                issues.append(f"{criterion} has an invalid attempt")
                continue
            label = attempt.get("case_id")
            identity = attempt.get("attempt_id")
            if (not isinstance(identity, str) or not identity.startswith(run_id + "/") or identity in seen
                    or label not in required or not isinstance(label, str)
                    or attempt.get("run_id") != run_id or attempt.get("scenario_id") != scenario
                    or attempt.get("test_ref") != selector
                    or any(attempt.get(key) != pins[key] for key in ("source_revision", "requirements_revision"))
                    or (attempt.get("workflow_run_id"), attempt.get("workflow_attempt")) != (workflow_run, workflow_attempt)
                    or attempt.get("result") != "pass"
                    or any(not isinstance(attempt.get(key), str) or not attempt[key].strip()
                           or re.search(r"https?://|arn:aws|\b[0-9]{12}\b", attempt[key], re.I)
                           for key in ("expected", "actual"))):
                issues.append(f"{criterion} has a failed, crossed or unreviewable attempt")
                continue
            seen.add(identity)
            observed_cases.add(label)
            report = reports[criterion]
            artifact = report.get("cases", {}).get(label, {}).get("report_ref") if criterion == "AC-01" else report.get("report_ref")
            readback = (report.get("cases", {}).get(label, {}).get("artifacts", {}).get("api") if criterion == "AC-01"
                        else report.get("usage_ref") if criterion == "AC-02" else report.get("artifacts", {}).get("api"))
            if (attempt.get("artifact_ref") != artifact or attempt.get("readback_ref") != readback
                    or not artifact or not readback or artifact == readback):
                issues.append(f"{criterion}.{label} lacks separate executed evidence and independent readback")
            if criterion == "AC-03" and (resume is None or not any(
                item["stage"] == label and item["attempt_id"] == attempt.get("source_attempt_id")
                for item in resume.get("attempts", [])
            )):
                issues.append(f"{criterion}.{label} is not in the recovery attempt report")
        if observed_cases != set(required):
            issues.append(f"{criterion} lacks a recorded attempt for every required case")
        results[criterion] = {"scenario_id": scenario, "test_ref": selector,
                              "expected": f"{criterion} guided acceptance assertions pass on pinned deployed clients",
                              "actual": f"{len(observed_cases)} of {len(required)} required cases have recorded attempts; "
                              f"independent reports {reports[criterion]['status']}",
                              "status": "pass" if reports[criterion]["status"] == "pass" and observed_cases == set(required)
                              and len(issues) == prior_issues else "incomplete",
                              "attempts": attempts, "evidence": reports[criterion]}
    packet = {"evidence_schema": SCHEMA, "bolt_id": BOLT_ID, "run_id": run_id,
              "source_revision": pins["source_revision"], "requirements_revision": pins["requirements_revision"],
              "plan_revision": pins["plan_revision"], "identities": identities, "deployment": deployment,
              "cli_hashes": cli_hashes, "attempt_manifest_ref": manifest_ref,
              "cases": results, "status": "review_ready" if not issues else "incomplete", "issues": issues}
    packet["outcome_summary"] = ("All B06 guidance reports and cleanup are ready for human evidence review"
                                 if not issues else "B06 guidance evidence is incomplete; see issues and retained attempts")
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
    """Return the existing B06 decision reference, never a human decision."""
    if not isinstance(packet, dict) or packet.get("status") != "review_ready" or packet.get("issues") != []:
        raise ValueError("Incomplete guidance evidence cannot be sent to the BOLT decision")
    content = {key: value for key, value in packet.items() if key not in ("packet_revision", "review")}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    expected_revision = "sha256:" + hashlib.sha256(encoded).hexdigest()
    expected = {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": BOLT_ID,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet.get("plan_revision"),
        "packet_revision": expected_revision, "status": "pending",
    }
    if packet.get("packet_revision") != expected_revision or packet.get("review") != expected:
        raise ValueError("BOLT decision binding differs from the packet and plan revisions")
    return expected.copy()


def write_packet(inputs, directory):
    packet = build_packet(inputs, directory)
    revision = packet["packet_revision"].removeprefix("sha256:")
    path = Path(directory) / f"{packet['run_id']}-v07-{revision}.json"
    private_json(path, packet)
    return path, packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--directory", required=True)
    arguments = parser.parse_args(argv)
    directory = Path(arguments.directory)
    inputs = protected_report(arguments.inputs, directory)
    _, packet = write_packet(inputs, directory)
    print(json.dumps({"status": packet["status"], "packet_revision": packet["packet_revision"]}))
    return 0 if packet["status"] == "review_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())

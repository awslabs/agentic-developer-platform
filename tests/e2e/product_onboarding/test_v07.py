"""Fixture assertions are pre-merge checks, not deployed V07 evidence."""

import json
import os
import stat
from copy import deepcopy
from pathlib import Path
import re

import pytest

from tests.e2e.cli_uplift.product_guidance import (
    REQUIRED_CHECKS, capture_guidance_effects, verify_direct_save, verify_guided_journey,
    verify_pending_guidance_resume, verify_revoked_guidance_resume,
)

GUIDED_SELECTOR = "tests/e2e/new_ui/test_product_guidance.py::test_guided_journeys"
GUIDED_SURFACES = ("browser", "installed_cli", "api", "cleanup")
GUIDANCE_EFFECTS_SELECTOR = "tests/e2e/product_onboarding/test_v07.py::live_guidance_effects"
GUIDANCE_RESUME_SELECTOR = "tests/e2e/product_onboarding/test_v07.py::live_guidance_resume"
GUIDANCE_TURN_ORDER = ("deterministic", "model", "direct")
GUIDANCE_TURN_FIELDS = {"mode", "tenant_id", "actor_id", "model_id", "metered_requests", "request_ids",
                        "usage_row_ids_before", "usage_row_ids_after", "proposal_ids", "profile_revision_before",
                        "profile_revision_after", "provider_before", "provider_after"}


def pinned_guided_journeys(source_revision, requirements_revision, run_id, selector=GUIDED_SELECTOR):
    if (any(not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision)
            for revision in (source_revision, requirements_revision))
            or not isinstance(run_id, str) or not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
            or selector != GUIDED_SELECTOR):
        raise ValueError("Guided journeys need a pinned release, run and reviewed browser selector")
    return {"scenario_id": "SCN-01", "source_revision": source_revision,
            "requirements_revision": requirements_revision, "run_id": run_id,
            "selector": selector, "starting_points": tuple(REQUIRED_CHECKS)}


def protected_report(path, directory):
    path = Path(path)
    if path.parent.resolve() != directory.resolve() or path.is_symlink():
        raise ValueError("Guided artifact must belong to the protected run directory")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        properties = os.fstat(stream.fileno())
        if (not stat.S_ISREG(properties.st_mode) or properties.st_uid != os.getuid()
                or stat.S_IMODE(properties.st_mode) != 0o600 or properties.st_size > 2 * 1024 * 1024):
            raise ValueError("Guided artifact must be private, owned and bounded")
        return json.load(stream)


def pinned_guidance_effects(source_revision, requirements_revision, run_id, selector=GUIDANCE_EFFECTS_SELECTOR):
    case = pinned_guided_journeys(source_revision, requirements_revision, run_id)
    if selector != GUIDANCE_EFFECTS_SELECTOR:
        raise ValueError("Guidance effects require the reviewed live selector")
    return {key: case[key] for key in ("source_revision", "requirements_revision", "run_id")} | {
        "scenario_id": "SCN-02", "selector": selector,
    }


def verify_live_guidance_effects(case, report):
    if (not isinstance(report, dict) or report.get("run_id") != case["run_id"]
            or report.get("source_revision") != case["source_revision"]
            or report.get("requirements_revision") != case["requirements_revision"]):
        raise ValueError("Guidance effects report does not match the pinned deployed run")
    return verify_guidance_effects_report(report)


@pytest.mark.live_only
def live_guidance_effects():
    if os.environ.get("PRODUCT_GUIDANCE_EFFECTS_LIVE_ENABLED") != "1":
        pytest.skip("SCN-02 needs a protected deployed model, CLI and independent provider run")
    try:
        run_id = f"product-{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}"
        case = pinned_guidance_effects(os.environ["PRODUCT_EXPECTED_REVISION"],
                                       os.environ["PRODUCT_REQUIREMENTS_REVISION"], run_id)
        directory = Path(os.environ["PRODUCT_GUIDANCE_REPORT_DIR"])
    except (KeyError, ValueError) as error:
        raise ValueError("Live guidance effects need a pinned run, source, requirements and report directory") from error
    properties = directory.stat()
    if (not stat.S_ISDIR(properties.st_mode) or properties.st_uid != os.getuid()
            or stat.S_IMODE(properties.st_mode) != 0o700):
        raise ValueError("Guidance effects report directory must be private")
    report = protected_report(directory / f"{run_id}-guidance-effects.json", directory)
    assert verify_live_guidance_effects(case, report)


live_guidance_effects.__test__ = True


def pinned_guidance_resume(source_revision, requirements_revision, run_id, selector=GUIDANCE_RESUME_SELECTOR):
    case = pinned_guided_journeys(source_revision, requirements_revision, run_id)
    if selector != GUIDANCE_RESUME_SELECTOR:
        raise ValueError("Guidance recovery requires the reviewed live selector")
    return {key: case[key] for key in ("source_revision", "requirements_revision", "run_id")} | {
        "scenario_id": "SCN-03", "selector": selector,
    }


def verify_live_guidance_resume(case, report, directory):
    directory = Path(directory)
    properties = directory.stat()
    if (not stat.S_ISDIR(properties.st_mode) or properties.st_uid != os.getuid()
            or stat.S_IMODE(properties.st_mode) != 0o700):
        raise ValueError("Guidance recovery report directory must be private")
    if (not isinstance(report, dict) or any(report.get(field) != case[field]
            for field in ("run_id", "source_revision", "requirements_revision"))):
        raise ValueError("Guidance recovery report differs from the pinned run or revisions")
    verify_guidance_resume_report(report)
    name = f"{case['run_id']}-guidance-resume"
    references = report["artifacts"]
    surfaces = {}
    for surface, reference in references.items():
        filename = f"{name}-{surface.replace('installed_cli', 'cli')}.json"
        if Path(reference) != directory / filename:
            raise ValueError("Guidance recovery surface artifact differs from the selected run")
        surfaces[surface] = protected_report(reference, directory)
    browser, cli, api, cleanup = (surfaces[surface] for surface in GUIDED_SURFACES)
    screenshot = browser.get("screenshot") if isinstance(browser, dict) else None
    if (not isinstance(screenshot, str) or Path(screenshot) != directory / f"{name}.png"
            or Path(screenshot).is_symlink()):
        raise ValueError("Guidance recovery screenshot differs from the selected run")
    with os.fdopen(os.open(screenshot, os.O_RDONLY | os.O_NOFOLLOW), "rb") as image:
        image_stat = os.fstat(image.fileno())
        if (not stat.S_ISREG(image_stat.st_mode) or image_stat.st_uid != os.getuid()
                or stat.S_IMODE(image_stat.st_mode) != 0o600 or not 0 < image_stat.st_size <= 10 * 1024 * 1024):
            raise ValueError("Guidance recovery screenshot must be private, owned and bounded")
    def scoped_cli(event):
        command = event.get("command")
        if (not isinstance(command, list) or command[:2] != ["adp", "onboard"]
                or event.get("exit_code") != 0):
            return False
        if len(command) > 2 and command[2] in ("guide", "resume"):
            return all(command.count(flag) == 1 and command.index(flag) + 1 < len(command)
                       and command[command.index(flag) + 1] == expected
                       for flag, expected in (("--product", report["product_id"]),
                                              ("--project", report["project_id"]),
                                              ("--session", report["pending"]["session_id"])))
        return True

    if (browser.get("status") != report["browser_status"]
            or browser.get("run_id") != case["run_id"]
            or browser.get("source_revision") != case["source_revision"]
            or browser.get("phases") != {"setup": "passed", "call": "passed", "teardown": "passed"}
            or not isinstance(cli, list) or len(cli) != report["cli_events"]
            or any(not isinstance(event, dict) or not scoped_cli(event) for event in cli)
            or not {"guide", "resume"} <= {event["command"][2] for event in cli if len(event["command"]) > 2}
            or not isinstance(api, list) or len(api) != report["api_reads"]
            or any(not isinstance(read, dict) or read.get("status") != 200 for read in api)
            or not any(read.get("body") == report["revocation"]["api_session"] for read in api)
            or cleanup != [report["cleanup"]]):
        raise ValueError("Guidance recovery surfaces do not support successful resume and cleanup")
    expected_surfaces = {"proposal": "installed_cli", "interruption": "browser", "resume": "api",
                         "outage": "installed_cli", "revocation": "api", "cleanup": "cleanup"}
    if any(attempt["evidence_ref"] != references[expected_surfaces[attempt["stage"]]]
           for attempt in report["attempts"]):
        raise ValueError("Guidance recovery attempts refer to the wrong client artifacts")
    return True


@pytest.mark.live_only
def live_guidance_resume():
    if os.environ.get("PRODUCT_GUIDANCE_RESUME_LIVE_ENABLED") != "1":
        pytest.skip("SCN-03 needs a protected deployed cross-client recovery and cleanup run")
    try:
        run_id = f"product-{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}"
        case = pinned_guidance_resume(os.environ["PRODUCT_EXPECTED_REVISION"],
                                      os.environ["PRODUCT_REQUIREMENTS_REVISION"], run_id)
        directory = Path(os.environ["PRODUCT_GUIDANCE_REPORT_DIR"])
    except (KeyError, ValueError) as error:
        raise ValueError("Live guidance recovery needs pinned run, source, requirements and report directory") from error
    report = protected_report(directory / f"{run_id}-guidance-resume-report.json", directory)
    assert verify_live_guidance_resume(case, report, directory)


live_guidance_resume.__test__ = True


def verify_live_guided_journeys(case, reports, directory):
    directory = Path(directory)
    properties = directory.stat()
    if (not stat.S_ISDIR(properties.st_mode) or properties.st_uid != os.getuid()
            or stat.S_IMODE(properties.st_mode) != 0o700):
        raise ValueError("Guided report directory must be private")
    if not isinstance(reports, dict) or set(reports) != set(case["starting_points"]):
        raise ValueError("All three guided starting points need separate reports")
    seen = set()
    for starting_point, report in reports.items():
        if (not isinstance(report, dict) or report.get("starting_point") != starting_point
                or report.get("run_id") != case["run_id"]
                or report.get("source_revision") != case["source_revision"]
                or report.get("requirements_revision") != case["requirements_revision"]):
            raise ValueError("Guided report differs from the selected run or revision")
        verify_guided_case_report(report)
        references = report.get("artifacts")
        if not isinstance(references, dict) or set(references) != set(GUIDED_SURFACES):
            raise ValueError("Guided report needs separate browser, CLI, API and cleanup artifacts")
        data = {}
        for surface, reference in references.items():
            expected = f"{case['run_id']}-guidance_{starting_point}-{surface.replace('installed_cli', 'cli')}.json"
            if not isinstance(reference, str) or Path(reference).name != expected or reference in seen:
                raise ValueError("Guided surface artifact is missing, repeated or mismatched")
            seen.add(reference)
            data[surface] = protected_report(reference, directory)
        browser, cli, api, cleanup = (data[surface] for surface in GUIDED_SURFACES)
        if (not isinstance(browser, dict) or browser.get("status") != "pass"
                or browser.get("run_id") != case["run_id"]
                or browser.get("source_revision") != case["source_revision"]
                or browser.get("phases") != {"setup": "passed", "call": "passed", "teardown": "passed"}
                or not isinstance(cli, list) or len(cli) != report["cli_events"]
                or any(not isinstance(event, dict) or event.get("exit_code") != 0
                       or event.get("command", [])[:2] != ["adp", "onboard"] for event in cli)
                or not isinstance(api, list) or len(api) != report["api_reads"]
                or any(not isinstance(read, dict) or read.get("status") != 200 for read in api)
                or not isinstance(cleanup, list) or cleanup != [report["cleanup"]]):
            raise ValueError("Guided surface observations do not support a passing report")
    return True


@pytest.mark.live_only
def live_guided_journeys():
    if os.environ.get("PRODUCT_GUIDANCE_LIVE_ENABLED") != "1":
        pytest.skip("SCN-01 needs a protected deployed browser and installed CLI run")
    try:
        run_id = f"product-{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}"
        directory = Path(os.environ["PRODUCT_GUIDANCE_REPORT_DIR"])
        case = pinned_guided_journeys(os.environ["PRODUCT_EXPECTED_REVISION"],
                                      os.environ["PRODUCT_REQUIREMENTS_REVISION"], run_id)
    except (KeyError, ValueError) as error:
        raise ValueError("Live guided journeys need pinned run, source, requirements and report directory") from error
    reports = {starting_point: protected_report(
        directory / f"{run_id}-guidance_{starting_point}-report.json", directory)
        for starting_point in case["starting_points"]}
    assert verify_live_guided_journeys(case, reports, directory)


live_guided_journeys.__test__ = True


def verify_guided_case_report(report):
    if report.get("starting_point") not in REQUIRED_CHECKS or not re.fullmatch(r"[a-f0-9]{40}", report.get("source_revision", "")):
        raise ValueError("Guided case needs a pinned starting point and source")
    if (report.get("browser_status") != "pass" or report.get("cli_events") != 2
            or report.get("api_reads", 0) < 4 or report.get("cleanup", {}).get("run_id") != report.get("run_id")
            or report["cleanup"].get("fixture_removed") is not True
            or report["cleanup"].get("shared_unchanged") is not True):
        raise ValueError("Guided case needs browser, installed CLI, independent API and verified cleanup")
    return report


RECOVERY_STAGES = {"proposal", "interruption", "resume", "outage", "revocation", "cleanup"}


def verify_guidance_resume_report(report):
    from tests.e2e.new_ui.product_session_recovery import assert_cleanup_receipt

    if (not isinstance(report, dict)
            or not isinstance(report.get("run_id"), str)
            or not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", report["run_id"])
            or any(not isinstance(report.get(field), str) or not re.fullmatch(r"[a-f0-9]{40}", report[field])
                   for field in ("source_revision", "requirements_revision"))
            or not all(isinstance(report.get(field), str) and report[field]
                       for field in ("tenant_id", "actor_id", "product_id", "project_id"))):
        raise ValueError("Guidance recovery needs a pinned owned run and revisions")
    attempts = report.get("attempts")
    artifacts = report.get("artifacts")
    if (not isinstance(artifacts, dict) or set(artifacts) != set(GUIDED_SURFACES)
            or any(not isinstance(path, str) or not path for path in artifacts.values())
            or len(set(artifacts.values())) != len(artifacts)):
        raise ValueError("Guidance recovery needs separate browser, CLI, API and cleanup artifacts")
    if (not isinstance(attempts, list) or not 6 <= len(attempts) <= 24
            or not all(isinstance(item, dict) and set(item) == {"attempt_id", "stage", "result", "evidence_ref"}
                       and isinstance(item["attempt_id"], str) and item["attempt_id"].startswith(report["run_id"] + "/")
                       and isinstance(item["stage"], str) and item["stage"] in RECOVERY_STAGES
                       and item["result"] == "pass"
                       and item["evidence_ref"] in artifacts.values()
                       for item in attempts)
            or len({item["attempt_id"] for item in attempts}) != len(attempts)
            or {item["stage"] for item in attempts} != RECOVERY_STAGES):
        raise ValueError("Guidance recovery lacks successful, distinct attempts for every required stage")
    pending = report.get("pending")
    outage = report.get("outage")
    revocation = report.get("revocation")
    if (not isinstance(pending, dict) or not isinstance(pending.get("session_id"), str) or not pending["session_id"]
            or type(pending.get("revision")) is not int or pending["revision"] < 1
            or not isinstance(pending.get("proposal_ids"), list) or not pending["proposal_ids"]
            or not isinstance(pending.get("remaining_paths"), dict)
            or set(pending["remaining_paths"]) != set(pending["proposal_ids"])
            or any(not isinstance(paths, list) or not paths for paths in pending["remaining_paths"].values())
            or not isinstance(outage, dict) or outage.get("mode") != "deterministic"
            or type(outage.get("metered_requests")) is not int or outage["metered_requests"] != 0
            or outage.get("proposal_ids") != []
            or outage.get("pending_after") != pending
            or not isinstance(revocation, dict)):
        raise ValueError("Guidance recovery lost pending paths or deterministic fallback")
    try:
        verify_revoked_guidance_resume(revocation, pending, tenant_id=report["tenant_id"],
                                       product_id=report["product_id"], project_id=report["project_id"])
    except AssertionError as error:
        raise ValueError("Guidance recovery disclosed a stale or revoked source") from error
    cleanup = report.get("cleanup")
    try:
        assert_cleanup_receipt(cleanup, report)
    except AssertionError as error:
        raise ValueError("Guidance recovery lacks verified owned-fixture cleanup") from error
    if (type(cleanup.get("cleanup_revision")) is not int or cleanup["cleanup_revision"] < 1
            or report.get("browser_status") != "pass" or report.get("cli_events", 0) < 2
            or report.get("api_reads", 0) < 3):
        raise ValueError("Guidance recovery needs observed browser, CLI, API and durable cleanup")
    return report


def verify_guidance_effects_report(report):
    if (not isinstance(report, dict) or not isinstance(report.get("run_id"), str)
            or not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", report["run_id"])
            or any(not isinstance(report.get(name), str) or not re.fullmatch(r"[a-f0-9]{40}", report[name])
                   for name in ("source_revision", "requirements_revision"))
            or not isinstance(report.get("turns"), dict)
            or set(report["turns"]) != set(GUIDANCE_TURN_ORDER)):
        raise ValueError("Guidance effects need a pinned run and all three independently observed turns")
    request_ids = []
    scoped_actor = None
    previous = None
    for mode, count in (("model", 1), ("deterministic", 0), ("direct", 0)):
        turn = report["turns"][mode]
        scope = tuple(turn.get(field) for field in ("tenant_id", "actor_id", "model_id")) if isinstance(turn, dict) else ()
        if (not isinstance(turn, dict) or set(turn) != GUIDANCE_TURN_FIELDS or turn.get("mode") != mode
                or not all(isinstance(value, str) and value for value in scope)
                or (scoped_actor is not None and scope != scoped_actor)
                or type(turn.get("metered_requests")) is not int or turn["metered_requests"] != count
                or not isinstance(turn.get("request_ids"), list) or len(turn["request_ids"]) != count
                or not all(isinstance(request_id, str) and request_id for request_id in turn["request_ids"])
                or not isinstance(turn.get("usage_row_ids_before"), list)
                or not isinstance(turn.get("usage_row_ids_after"), list)
                or any(not isinstance(row_id, str) or not row_id for row_id in
                       turn["usage_row_ids_before"] + turn["usage_row_ids_after"])
                or len(set(turn["usage_row_ids_before"])) != len(turn["usage_row_ids_before"])
                or len(set(turn["usage_row_ids_after"])) != len(turn["usage_row_ids_after"])
                or len(set(turn["usage_row_ids_after"]) - set(turn["usage_row_ids_before"])) != count
                or not set(turn["usage_row_ids_before"]) <= set(turn["usage_row_ids_after"])
                or not isinstance(turn.get("provider_before"), dict)
                or turn.get("provider_before") != turn.get("provider_after")
                or not isinstance(turn.get("proposal_ids"), list)
                or len(turn["proposal_ids"]) > 10
                or any(not isinstance(proposal_id, str) or not proposal_id for proposal_id in turn["proposal_ids"])
                or len(set(turn["proposal_ids"])) != len(turn["proposal_ids"])
                or (mode != "model" and turn.get("proposal_ids") != [])
                or type(turn.get("profile_revision_before")) is not int
                or type(turn.get("profile_revision_after")) is not int
                or (mode == "direct" and turn["profile_revision_after"] != turn["profile_revision_before"] + 1)
                or (mode != "direct" and turn.get("profile_revision_after") != turn.get("profile_revision_before"))):
            raise ValueError("Guidance effects differ from bounded model and direct-save measurements")
        scoped_actor = scope
        request_ids.extend(turn["request_ids"])
    for mode in GUIDANCE_TURN_ORDER:
        turn = report["turns"][mode]
        if previous is not None and (turn["usage_row_ids_before"] != previous["usage_row_ids_after"]
                                     or turn["profile_revision_before"] != previous["profile_revision_after"]
                                     or turn["provider_before"] != previous["provider_after"]):
            raise ValueError("Guidance effects contain a missing or crossed observation interval")
        previous = turn
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Guidance effects repeated a metered request")
    return report


@pytest.mark.parametrize("starting_point", ("idea", "existing_assets", "running_product"))
def test_guided_journey_fixture(starting_point):
    checks = [{"id": name, "status": "complete" if name in ("information", "project_scope") else "missing",
               "required": True} for name in REQUIRED_CHECKS[starting_point]]
    profile = {"product": {"starting_point": starting_point, "name": "Owned Product",
                           "purpose": "Saved information", "intended_users": ["Maintainers"]},
               "progress": {"checks": checks}}
    session = {"id": "session-1", "revision": 2, "progress": profile["progress"]}
    message = ("No missing steps in the current scope." if starting_point == "idea"
               else "Review a repository from your connected sources." +
                    ("\nVerify your environment connection." if starting_point == "running_product" else ""))
    cli = {"revision": 2, "message": message, "proposals": [], "mode": "deterministic"}
    verify_guided_journey(starting_point, profile, session, message, cli, deepcopy(session))
    if starting_point != "idea":
        source_profile, source_session = deepcopy(profile), deepcopy(session)
        for check in source_session["progress"]["checks"]:
            if check["id"] == "repository":
                check["status"] = "complete"
        source_profile["progress"] = deepcopy(source_session["progress"])
        reused_message = ("No missing steps in the current scope." if starting_point == "existing_assets"
                          else "Verify your environment connection.\nVerify your pipeline connection.")
        verify_guided_journey(starting_point, source_profile, source_session, reused_message,
                              {**cli, "message": reused_message}, deepcopy(source_session))
    for change in ("wrong_scope", "missing_required_check", "stale_readback", "repeated_known_field"):
        changed_profile, changed_session, changed_readback, changed_message = (
            deepcopy(profile), deepcopy(session), deepcopy(session), message)
        if change == "wrong_scope":
            changed_profile["product"]["starting_point"] = "another"
        elif change == "missing_required_check":
            changed_session["progress"]["checks"] = changed_session["progress"]["checks"][:-1]
        elif change == "stale_readback":
            changed_readback["revision"] = 1
        else:
            changed_message = "Add your Product purpose."
        with pytest.raises(AssertionError):
            verify_guided_journey(starting_point, changed_profile, changed_session,
                                  changed_message, cli, changed_readback)


def test_direct_save_preserves_known_fields_and_checks():
    before = {"revision": 1, "product": {"name": "Saved name", "purpose": "Previous purpose",
                                          "starting_point": "existing_assets"}, "progress": {"checks": [{"id": "repository", "status": "complete"}]}}
    after = deepcopy(before)
    after["revision"] = 2
    after["product"]["purpose"] = "Confirmed by direct edit"
    old_session = {"revision": 1}
    saved_session = {"revision": 2}
    verify_direct_save(before, old_session, saved_session, after, deepcopy(saved_session))
    for changed in ({"product": {**after["product"], "name": "Overwritten"}},
                    {"progress": {"checks": []}}, {"revision": 1}):
        with pytest.raises(AssertionError):
            verify_direct_save(before, old_session, saved_session, {**after, **changed}, deepcopy(saved_session))


@pytest.mark.parametrize("starting_point", ("idea", "existing_assets", "running_product"))
def test_guided_report_requires_distinct_client_observations_and_cleanup(starting_point):
    report = {"starting_point": starting_point, "run_id": "example-run", "source_revision": "a" * 40,
              "browser_status": "pass", "cli_events": 2, "api_reads": 5,
              "cleanup": {"run_id": "example-run", "fixture_removed": True, "shared_unchanged": True}}
    assert verify_guided_case_report(report) == report
    for changed in ({"browser_status": "not_run"}, {"cli_events": 0}, {"api_reads": 0},
                    {"source_revision": "stale"}, {"cleanup": {**report["cleanup"], "fixture_removed": False}}):
        with pytest.raises(ValueError):
            verify_guided_case_report({**report, **changed})


def test_guided_selection_pins_run_and_reviewed_client_case():
    case = pinned_guided_journeys("a" * 40, "b" * 40, "product-42-1")
    assert case["selector"] == GUIDED_SELECTOR and case["scenario_id"] == "SCN-01"
    assert set(case["starting_points"]) == set(REQUIRED_CHECKS)
    for source, requirements, run_id, selector in (
        ("short", "b" * 40, "product-42-1", GUIDED_SELECTOR),
        ("a" * 40, "invalid", "product-42-1", GUIDED_SELECTOR),
        ("a" * 40, "b" * 40, "product-other-1", GUIDED_SELECTOR),
        ("a" * 40, "b" * 40, "product-42-1", "wrong::test"),
    ):
        with pytest.raises(ValueError):
            pinned_guided_journeys(source, requirements, run_id, selector)


def guided_surface_fixture(directory):
    from tests.e2e.product_onboarding.product_v02_record import private_json

    case = pinned_guided_journeys("a" * 40, "b" * 40, "product-42-1")
    reports = {}
    for starting_point in case["starting_points"]:
        cleanup = {"run_id": case["run_id"], "fixture_removed": True, "shared_unchanged": True}
        report = {"starting_point": starting_point, "source_revision": case["source_revision"],
                  "requirements_revision": case["requirements_revision"], "run_id": case["run_id"],
                  "browser_status": "pass", "cli_events": 2, "api_reads": 4, "cleanup": cleanup}
        surfaces = {
            "browser": {"status": "pass", "run_id": case["run_id"], "source_revision": case["source_revision"],
                        "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}},
            "installed_cli": [{"command": ["adp", "onboard", "guide"], "exit_code": 0},
                              {"command": ["adp", "onboard", "step"], "exit_code": 0}],
            "api": [{"status": 200} for _ in range(4)], "cleanup": [cleanup],
        }
        report["artifacts"] = {surface: private_json(directory / (
            f"{case['run_id']}-guidance_{starting_point}-{surface.replace('installed_cli', 'cli')}.json"), value)
            for surface, value in surfaces.items()}
        reports[starting_point] = report
    return case, reports


def test_guided_selector_requires_private_separate_surface_reports(tmp_path):
    case, reports = guided_surface_fixture(tmp_path)
    assert verify_live_guided_journeys(case, reports, tmp_path)
    for starting_point in case["starting_points"]:
        with pytest.raises(ValueError, match="three guided"):
            verify_live_guided_journeys(case, {name: value for name, value in reports.items()
                                               if name != starting_point}, tmp_path)
    first = case["starting_points"][0]
    for change in ({"run_id": "product-43-1"}, {"source_revision": "c" * 40},
                   {"requirements_revision": "d" * 40}, {"browser_status": "not_run"},
                   {"artifacts": {**reports[first]["artifacts"], "api": reports[first]["artifacts"]["browser"]}}):
        with pytest.raises(ValueError):
            verify_live_guided_journeys(case, {**reports, first: {**reports[first], **change}}, tmp_path)
    cli_ref = Path(reports[first]["artifacts"]["installed_cli"])
    cli_ref.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        verify_live_guided_journeys(case, reports, tmp_path)


def test_live_guided_selector_fails_closed_without_protected_reports(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCT_GUIDANCE_LIVE_ENABLED", raising=False)
    with pytest.raises(pytest.skip.Exception, match="deployed browser"):
        live_guided_journeys()
    monkeypatch.setenv("PRODUCT_GUIDANCE_LIVE_ENABLED", "1")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("PRODUCT_EXPECTED_REVISION", "a" * 40)
    monkeypatch.setenv("PRODUCT_REQUIREMENTS_REVISION", "b" * 40)
    monkeypatch.setenv("PRODUCT_GUIDANCE_REPORT_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        live_guided_journeys()


class GuidanceEffectFixture:
    def __init__(self):
        self.now = 0
        self.rows = []
        self.profile = {"revision": 1, "tenant_id": "tenant", "product_id": "product-1", "project_id": "project-1",
                        "product": {"name": "Saved name", "purpose": "Old",
                                    "owner_ref": {"kind": "user", "id": "owner"}}}
        self.session = {"revision": 1, "tenant_id": "tenant", "product_id": "product-1", "project_id": "project-1",
                        "proposals": []}
        self.provider = {"repository_ids": ["existing-provider-1"]}

    def usage(self):
        return {"items": deepcopy(self.rows), "total": len(self.rows)}

    def tick(self, seconds):
        self.now += seconds

    def turn(self, mode):
        if mode == "model":
            self.rows.append({"id": "metered-1", "request_id": "request-1", "org_id": "tenant",
                              "user_id": "owner", "model": "fixture-model", "status_code": 200})
            proposal = {"id": "proposal-1", "base_revision": 1, "configuration": {},
                        "product": {"purpose": "Reviewable"}, "remaining_paths": ["/product/purpose"],
                        "provenance": [{"paths": ["/product/purpose"], "state": "missing",
                                        "method": "extracted", "source_refs": [],
                                        "observed_at": "2026-10-20T00:00:00Z"}],
                        "expires_at": "2026-10-21T00:00:00Z"}
            self.session["proposals"].append(proposal)
            return {"mode": "model", "proposals": [proposal], "message": "Variable model prose"}
        if mode == "direct":
            self.profile["product"]["purpose"] = "Confirmed by direct edit"
            self.profile["revision"] = self.session["revision"] = 2
            return deepcopy(self.session)
        return {"mode": "deterministic", "proposals": [], "message": "A deterministic gap"}


def captured_effects_fixture():
    fixture = GuidanceEffectFixture()
    observations = {}
    for mode in ("deterministic", "model", "direct"):
        observations[mode] = capture_guidance_effects(
            "tenant", "owner", "fixture-model", mode, fixture.usage,
            lambda: deepcopy(fixture.profile), lambda: deepcopy(fixture.session),
            lambda: deepcopy(fixture.provider), lambda mode=mode: fixture.turn(mode),
            clock=lambda: fixture.now, pause=fixture.tick, deadline_seconds=4)
    return {"run_id": "product-42-1", "source_revision": "a" * 40,
            "requirements_revision": "b" * 40, "turns": observations}


@pytest.mark.parametrize("first_client,other_client", (("browser", "installed_cli"), ("installed_cli", "browser")))
def test_interrupted_cross_client_guidance_resume_fixture(first_client, other_client):
    fixture = GuidanceEffectFixture()
    fixture.session["id"] = "saved-session"
    fixture.turn("model")
    client_sessions = {"browser": deepcopy(fixture.session), "installed_cli": deepcopy(fixture.session)}
    independent = deepcopy(fixture.session)
    observed = verify_pending_guidance_resume(
        client_sessions[first_client], client_sessions[other_client], independent,
        tenant_id="tenant", product_id="product-1", project_id="project-1")
    assert observed == {"session_id": "saved-session", "revision": 1, "proposal_ids": ["proposal-1"],
                        "remaining_paths": {"proposal-1": ["/product/purpose"]}}
    assert fixture.turn("deterministic")["proposals"] == []
    assert fixture.session == independent
    assert len(fixture.rows) == 1


@pytest.mark.parametrize("fault", ("different_id", "lost_path", "new_path", "changed_value", "wrong_scope", "stale_api", "no_pending"))
def test_pending_guidance_resume_rejects_changed_or_missing_proposals(fault):
    fixture = GuidanceEffectFixture()
    fixture.session["id"] = "saved-session"
    fixture.turn("model")
    first = deepcopy(fixture.session)
    resumed = deepcopy(first)
    independent = deepcopy(first)
    if fault == "different_id":
        resumed["proposals"][0]["id"] = "different-proposal"
    elif fault == "lost_path":
        resumed["proposals"][0]["remaining_paths"] = []
    elif fault == "new_path":
        resumed["proposals"][0]["remaining_paths"].append("/product/goals")
    elif fault == "changed_value":
        resumed["proposals"][0]["product"]["purpose"] = "Changed"
    elif fault == "wrong_scope":
        resumed["tenant_id"] = "another-tenant"
    elif fault == "stale_api":
        independent["proposals"] = []
    else:
        first["proposals"] = []
    with pytest.raises(AssertionError):
        verify_pending_guidance_resume(first, resumed, independent,
                                       tenant_id="tenant", product_id="product-1", project_id="project-1")


def recovery_report_fixture():
    run_id = "product-42-1"
    source_ref = {"tenant_id": "tenant", "project_id": "project-1", "source_connection_id": "fixture-connection",
                  "record_type": "repository", "record_id": "fixture-repository", "record_revision": "source-revision-1"}
    bound = {"id": "bound-1", "remaining_paths": ["/product/purpose"],
             "provenance": [{"source_refs": [deepcopy(source_ref)]}]}
    safe = {"id": "safe-1", "remaining_paths": ["/product/goals"],
            "provenance": [{"source_refs": []}]}
    before_session = {"id": "session-1", "revision": 1, "tenant_id": "tenant",
                      "product_id": "product-1", "project_id": "project-1", "proposals": [bound, safe]}
    after_session = {**before_session, "proposals": [safe]}
    pending = {"session_id": "session-1", "revision": 1, "proposal_ids": ["bound-1", "safe-1"],
               "remaining_paths": {"bound-1": ["/product/purpose"], "safe-1": ["/product/goals"]}}
    artifacts = {surface: f"/private/{run_id}-{surface}.json" for surface in GUIDED_SURFACES}
    stage_surfaces = {"proposal": "installed_cli", "interruption": "browser", "resume": "api",
                      "outage": "installed_cli", "revocation": "api", "cleanup": "cleanup"}
    return {
        "run_id": run_id, "source_revision": "a" * 40, "requirements_revision": "b" * 40,
        "tenant_id": "tenant", "actor_id": "owner", "product_id": "product-1", "project_id": "project-1",
        "pending": pending,
        "outage": {"mode": "deterministic", "metered_requests": 0, "proposal_ids": [], "pending_after": deepcopy(pending)},
        "revocation": {"source_ref": source_ref, "before_session": before_session,
                       "cli_session": deepcopy(after_session), "api_session": deepcopy(after_session),
                       "browser_visible_proposal_ids": ["safe-1"]},
        "attempts": [{"attempt_id": f"{run_id}/{index}", "stage": stage, "result": "pass",
                      "evidence_ref": artifacts[stage_surfaces[stage]]}
                     for index, stage in enumerate(stage_surfaces, start=1)],
        "browser_status": "pass", "cli_events": 2, "api_reads": 3,
        "cleanup": {"run_id": run_id, "product_id": "product-1", "project_id": "project-1",
                    "fixture_removed": True, "owned_absent": True, "shared_unchanged": True, "cleanup_revision": 2},
        "artifacts": artifacts,
    }


def recovery_selector_fixture(directory):
    from tests.e2e.product_onboarding.product_v02_record import private_json

    case = pinned_guidance_resume("a" * 40, "b" * 40, "product-42-1")
    report = recovery_report_fixture()
    name = case["run_id"] + "-guidance-resume"
    screenshot = directory / f"{name}.png"
    screenshot.write_bytes(b"fixture browser image")
    screenshot.chmod(0o600)
    data = {
        "browser": {"status": "pass", "run_id": case["run_id"], "source_revision": case["source_revision"],
                    "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}, "screenshot": str(screenshot)},
        "installed_cli": [{"command": ["adp", "onboard", verb, "--product", report["product_id"],
                                       "--project", report["project_id"], "--session", report["pending"]["session_id"]],
                           "exit_code": 0} for verb in ("guide", "resume")],
        "api": [{"status": 200, "body": deepcopy(report["revocation"]["api_session"])},
                {"status": 200}, {"status": 200}],
        "cleanup": [report["cleanup"]],
    }
    old_references = report["artifacts"]
    report["artifacts"] = {surface: private_json(directory / f"{name}-{surface.replace('installed_cli', 'cli')}.json", body)
                           for surface, body in data.items()}
    for attempt in report["attempts"]:
        surface = next(surface for surface, reference in old_references.items()
                       if reference == attempt["evidence_ref"])
        attempt["evidence_ref"] = report["artifacts"][surface]
    return case, report, data


def test_guidance_resume_selector_pins_run_and_rejects_missing_private_report(tmp_path, monkeypatch):
    from tests.e2e.new_ui.product_release import private_capture_directory
    from tests.e2e.product_onboarding.product_v02_record import private_json

    case = pinned_guidance_resume("a" * 40, "b" * 40, "product-42-1")
    assert case["selector"] == GUIDANCE_RESUME_SELECTOR and case["scenario_id"] == "SCN-03"
    for source, requirements, run_id, selector in (
        ("short", "b" * 40, "product-42-1", GUIDANCE_RESUME_SELECTOR),
        ("a" * 40, "wrong", "product-42-1", GUIDANCE_RESUME_SELECTOR),
        ("a" * 40, "b" * 40, "product-0-1", GUIDANCE_RESUME_SELECTOR),
        ("a" * 40, "b" * 40, "product-42-1", "other::selector"),
    ):
        with pytest.raises(ValueError):
            pinned_guidance_resume(source, requirements, run_id, selector)
    monkeypatch.delenv("PRODUCT_GUIDANCE_RESUME_LIVE_ENABLED", raising=False)
    with pytest.raises(pytest.skip.Exception, match="protected deployed cross-client"):
        live_guidance_resume()
    directory = private_capture_directory(tmp_path / "run")
    monkeypatch.setenv("PRODUCT_GUIDANCE_RESUME_LIVE_ENABLED", "1")
    for field, value in {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "PRODUCT_EXPECTED_REVISION": "a" * 40,
                         "PRODUCT_REQUIREMENTS_REVISION": "b" * 40,
                         "PRODUCT_GUIDANCE_REPORT_DIR": str(directory)}.items():
        monkeypatch.setenv(field, value)
    with pytest.raises(FileNotFoundError):
        live_guidance_resume()
    case, report, _ = recovery_selector_fixture(directory)
    path = directory / f"{case['run_id']}-guidance-resume-report.json"
    private_json(path, report)
    assert verify_live_guidance_resume(case, report, directory)
    assert live_guidance_resume() is None
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        live_guidance_resume()


@pytest.mark.parametrize("fault", ("wrong_run", "stale_revision", "missing_attempt", "failed_cleanup",
                                     "missing_surface", "aliased_surface", "wrong_artifact_name", "public_artifact",
                                     "wrong_browser", "public_screenshot", "wrong_screenshot", "cli_failed",
                                     "cli_missing_resume", "cli_wrong_scope", "stale_api", "missing_readback",
                                     "crossed_attempt", "cleanup_artifact_mismatch", "public_directory"))
def test_guidance_resume_selector_rejects_failed_or_crossed_surfaces(tmp_path, fault):
    from tests.e2e.new_ui.product_release import private_capture_directory

    directory = private_capture_directory(tmp_path / "run")
    case, report, data = recovery_selector_fixture(directory)
    if fault == "wrong_run":
        report["run_id"] = "product-43-1"
    elif fault == "stale_revision":
        report["requirements_revision"] = "c" * 40
    elif fault == "missing_attempt":
        report["attempts"].pop()
    elif fault == "failed_cleanup":
        report["cleanup"]["owned_absent"] = False
    elif fault == "missing_surface":
        del report["artifacts"]["api"]
    elif fault == "aliased_surface":
        report["artifacts"]["api"] = report["artifacts"]["browser"]
    elif fault == "wrong_artifact_name":
        report["artifacts"]["api"] = str(directory / "different-api.json")
    elif fault == "public_artifact":
        Path(report["artifacts"]["installed_cli"]).chmod(0o644)
    elif fault == "wrong_browser":
        data["browser"]["status"] = "skip"
        Path(report["artifacts"]["browser"]).write_text(json.dumps(data["browser"]))
    elif fault == "public_screenshot":
        (directory / f"{case['run_id']}-guidance-resume.png").chmod(0o644)
    elif fault == "wrong_screenshot":
        data["browser"]["screenshot"] = str(directory / "other.png")
        Path(report["artifacts"]["browser"]).write_text(json.dumps(data["browser"]))
    elif fault == "cli_failed":
        data["installed_cli"][0]["exit_code"] = 1
        Path(report["artifacts"]["installed_cli"]).write_text(json.dumps(data["installed_cli"]))
    elif fault == "cli_missing_resume":
        data["installed_cli"][1]["command"][2] = "guide"
        Path(report["artifacts"]["installed_cli"]).write_text(json.dumps(data["installed_cli"]))
    elif fault == "cli_wrong_scope":
        data["installed_cli"][1]["command"][6] = "different-project"
        Path(report["artifacts"]["installed_cli"]).write_text(json.dumps(data["installed_cli"]))
    elif fault == "stale_api":
        data["api"][0]["body"] = report["revocation"]["before_session"]
        Path(report["artifacts"]["api"]).write_text(json.dumps(data["api"]))
    elif fault == "missing_readback":
        data["api"].pop()
        Path(report["artifacts"]["api"]).write_text(json.dumps(data["api"]))
    elif fault == "crossed_attempt":
        report["attempts"][0]["evidence_ref"] = report["artifacts"]["api"]
    elif fault == "cleanup_artifact_mismatch":
        Path(report["artifacts"]["cleanup"]).write_text("[]")
    else:
        directory.chmod(0o755)
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_live_guidance_resume(case, report, directory)


def test_guidance_recovery_report_writes_separate_private_artifacts_after_cleanup(tmp_path):
    from tests.e2e.new_ui.product_release import private_capture_directory
    from tests.e2e.new_ui.test_product_guidance import write_guidance_resume_report
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence

    report = recovery_report_fixture()
    directory = private_capture_directory(tmp_path / "recovery")
    screenshot = directory / f"{report['run_id']}-guidance-resume.png"
    screenshot.write_bytes(b"fixture screenshot")
    screenshot.chmod(0o600)
    record = CaseEvidence()
    record.phases = {"setup": "passed", "call": "passed", "teardown": "passed"}
    record.cli_events = [{"exit_code": 0}, {"exit_code": 0}]
    record.reads = [{"status": 200, "body": deepcopy(report["revocation"]["api_session"])},
                    {"status": 200}, {"status": 200}]
    record.cleanup = [report["cleanup"]]
    fixture = {key: report[key] for key in ("run_id", "source_revision", "tenant_id", "actor_id", "product_id", "project_id")}
    fixture["evidence_context"] = {"identities": {"requirements_revision": report["requirements_revision"]}}
    attempts = [{key: attempt[key] for key in ("attempt_id", "stage", "result")} for attempt in report["attempts"]]
    path = write_guidance_resume_report(fixture, report["pending"], report["outage"], report["revocation"],
                                        attempts, record, directory, screenshot)
    saved = protected_report(path, directory)
    assert verify_guidance_resume_report(saved) == saved
    assert len(set(saved["artifacts"].values())) == len(GUIDED_SURFACES)
    record.cleanup = []
    other = private_capture_directory(tmp_path / "incomplete")
    other_screenshot = other / f"{report['run_id']}-guidance-resume.png"
    other_screenshot.write_bytes(b"fixture screenshot")
    other_screenshot.chmod(0o600)
    with pytest.raises(ValueError, match="cleanup"):
        write_guidance_resume_report(fixture, report["pending"], report["outage"], report["revocation"],
                                     attempts, record, other, other_screenshot)
    assert protected_report(other / f"{fixture['run_id']}-guidance-resume-report.json", other)["cleanup"] is None
    record.cleanup = [report["cleanup"]]
    missing_attempt = private_capture_directory(tmp_path / "missing-attempt")
    missing_screenshot = missing_attempt / f"{report['run_id']}-guidance-resume.png"
    missing_screenshot.write_bytes(b"fixture screenshot")
    missing_screenshot.chmod(0o600)
    with pytest.raises(ValueError, match="attempts"):
        write_guidance_resume_report(fixture, report["pending"], report["outage"], report["revocation"],
                                     attempts[:-1], record, missing_attempt, missing_screenshot)
    assert len(protected_report(missing_attempt / f"{fixture['run_id']}-guidance-resume-report.json",
                                missing_attempt)["attempts"]) == 5
    record.reads[0]["body"] = deepcopy(report["revocation"]["before_session"])
    stale = private_capture_directory(tmp_path / "stale-readback")
    stale_screenshot = stale / f"{report['run_id']}-guidance-resume.png"
    stale_screenshot.write_bytes(b"fixture screenshot")
    stale_screenshot.chmod(0o600)
    with pytest.raises(ValueError, match="independently recorded API"):
        write_guidance_resume_report(fixture, report["pending"], report["outage"], report["revocation"],
                                     attempts, record, stale, stale_screenshot)
    assert protected_report(stale / f"{fixture['run_id']}-guidance-resume-report.json", stale)["revocation"] == report["revocation"]


@pytest.mark.parametrize("fault", ("missing_attempt", "failed_attempt", "repeated_attempt", "missing_cleanup",
                                     "wrong_cleanup", "missing_artifact", "missing_revision", "missing_api",
                                     "lost_pending", "wrong_outage", "unredacted_revocation"))
def test_guidance_recovery_report_rejects_incomplete_attempts_and_cleanup(fault):
    report = recovery_report_fixture()
    if fault == "missing_attempt":
        report["attempts"].pop()
    elif fault == "failed_attempt":
        report["attempts"][3]["result"] = "fail"
    elif fault == "repeated_attempt":
        report["attempts"][3]["attempt_id"] = report["attempts"][2]["attempt_id"]
    elif fault == "missing_cleanup":
        report["cleanup"] = None
    elif fault == "wrong_cleanup":
        report["cleanup"]["shared_unchanged"] = False
    elif fault == "missing_artifact":
        del report["artifacts"]["api"]
    elif fault == "missing_revision":
        report["requirements_revision"] = "unknown"
    elif fault == "missing_api":
        report["api_reads"] = 0
    elif fault == "lost_pending":
        report["outage"]["pending_after"]["proposal_ids"] = []
    elif fault == "wrong_outage":
        report["outage"]["metered_requests"] = 1
    else:
        report["revocation"]["cli_session"]["proposals"].append(report["revocation"]["before_session"]["proposals"][0])
    with pytest.raises(ValueError):
        verify_guidance_resume_report(report)


@pytest.mark.parametrize("fault", ("revoked_id", "renamed_revoked_source", "stale_source_revision",
                                     "wrong_tenant", "wrong_project", "api_leaks", "browser_leaks",
                                     "safe_lost", "unconfirmed_source", "wrong_session"))
def test_revoked_guidance_readback_rejects_stale_or_wrong_scope_source(fault):
    report = recovery_report_fixture()
    revocation = report["revocation"]
    if fault == "revoked_id":
        revocation["cli_session"]["proposals"].append(revocation["before_session"]["proposals"][0])
    elif fault == "renamed_revoked_source":
        leaked = deepcopy(revocation["before_session"]["proposals"][0])
        leaked["id"] = "renamed-bound"
        revocation["api_session"]["proposals"].append(leaked)
    elif fault == "stale_source_revision":
        revocation["source_ref"]["record_revision"] = "source-revision-2"
    elif fault == "wrong_tenant":
        revocation["api_session"]["tenant_id"] = "other-tenant"
    elif fault == "wrong_project":
        revocation["cli_session"]["project_id"] = "other-project"
    elif fault == "api_leaks":
        revocation["api_session"]["proposals"] = revocation["before_session"]["proposals"]
    elif fault == "browser_leaks":
        revocation["browser_visible_proposal_ids"] = ["bound-1", "safe-1"]
    elif fault == "safe_lost":
        revocation["cli_session"]["proposals"] = []
    elif fault == "unconfirmed_source":
        revocation["source_ref"] = {**revocation["source_ref"], "project_id": "other-project"}
    else:
        revocation["api_session"]["id"] = "other-session"
    with pytest.raises(ValueError, match="stale or revoked source"):
        verify_guidance_resume_report(report)


def test_guidance_effects_capture_metering_and_direct_save_without_model_text_oracle():
    report = captured_effects_fixture()
    observations = report["turns"]
    assert observations["deterministic"]["metered_requests"] == 0
    assert observations["model"]["request_ids"] == ["request-1"]
    assert observations["model"]["actor_id"] == "owner"
    assert observations["model"]["proposal_ids"] == ["proposal-1"]
    assert observations["direct"]["metered_requests"] == 0
    assert verify_guidance_effects_report(report) == report
    for changed in ({"metered_requests": 2}, {"request_ids": ["request-1", "request-2"]},
                    {"provider_after": {"repository_ids": ["changed-provider"]}}):
        with pytest.raises(ValueError):
            verify_guidance_effects_report({**report, "turns": {
                **observations, "model": {**observations["model"], **changed}}})


def test_guidance_effects_selector_requires_private_pinned_attempt(tmp_path, monkeypatch):
    from tests.e2e.product_onboarding.product_v02_record import private_json

    case = pinned_guidance_effects("a" * 40, "b" * 40, "product-42-1")
    assert case["selector"] == GUIDANCE_EFFECTS_SELECTOR and case["scenario_id"] == "SCN-02"
    with pytest.raises(ValueError, match="reviewed live selector"):
        pinned_guidance_effects("a" * 40, "b" * 40, "product-42-1", "wrong::test")
    monkeypatch.delenv("PRODUCT_GUIDANCE_EFFECTS_LIVE_ENABLED", raising=False)
    with pytest.raises(pytest.skip.Exception, match="protected deployed model"):
        live_guidance_effects()
    monkeypatch.setenv("PRODUCT_GUIDANCE_EFFECTS_LIVE_ENABLED", "1")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("PRODUCT_EXPECTED_REVISION", "a" * 40)
    monkeypatch.setenv("PRODUCT_REQUIREMENTS_REVISION", "b" * 40)
    monkeypatch.setenv("PRODUCT_GUIDANCE_REPORT_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        live_guidance_effects()
    path = Path(private_json(tmp_path / "product-42-1-guidance-effects.json", captured_effects_fixture()))
    assert live_guidance_effects() is None
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        live_guidance_effects()


@pytest.mark.parametrize("fault", (
    "missing_turn", "missing_model_meter", "extra_model_meter", "skipped_turn", "wrong_run",
    "stale_revision", "wrong_actor", "crossed_usage", "unselected_provider_write",
    "direct_meter", "missing_readback",
))
def test_guidance_effects_selector_rejects_failed_or_crossed_metering(fault):
    case = pinned_guidance_effects("a" * 40, "b" * 40, "product-42-1")
    report = captured_effects_fixture()
    turns = report["turns"]
    if fault == "missing_turn":
        del turns["model"]
    elif fault == "missing_model_meter":
        turns["model"]["metered_requests"] = 0
    elif fault == "extra_model_meter":
        turns["model"]["usage_row_ids_after"].append("usage-2")
    elif fault == "skipped_turn":
        turns["model"]["status"] = "skip"
    elif fault == "wrong_run":
        report["run_id"] = "product-43-1"
    elif fault == "stale_revision":
        report["source_revision"] = "c" * 40
    elif fault == "wrong_actor":
        turns["model"]["actor_id"] = "foreign-actor"
    elif fault == "crossed_usage":
        turns["direct"]["usage_row_ids_before"] = []
    elif fault == "unselected_provider_write":
        turns["model"]["provider_after"]["repository_ids"].append("unselected-created")
    elif fault == "direct_meter":
        turns["direct"]["metered_requests"] = 1
    else:
        del turns["model"]["usage_row_ids_before"]
    with pytest.raises(ValueError):
        verify_live_guidance_effects(case, report)


@pytest.mark.parametrize("fault", (
    "foreign_tenant_proposal", "foreign_source_reference", "duplicate_proposal_id",
    "unselected_provider_create", "duplicate_model_delivery", "foreign_metering",
    "unexpected_profile_write", "unselected_direct_effect", "unselected_direct_charge",
    "unmetered_deterministic_call",
))
def test_guidance_effects_reject_wrong_scope_and_duplicate_effects(fault):
    affected = GuidanceEffectFixture()
    mode = ("direct" if fault in ("unselected_direct_effect", "unselected_direct_charge")
            else "deterministic" if fault == "unmetered_deterministic_call" else "model")

    def wrong_turn():
        reply = affected.turn(mode)
        if fault == "foreign_tenant_proposal":
            reply["proposals"][0]["tenant_id"] = "foreign-tenant"
        elif fault == "foreign_source_reference":
            reply["proposals"][0]["provenance"][0]["source_refs"] = [{"tenant_id": "foreign-tenant"}]
        elif fault == "duplicate_proposal_id":
            reply["proposals"].append(deepcopy(reply["proposals"][0]))
        elif fault in ("unselected_provider_create", "unselected_direct_effect"):
            affected.provider["repository_ids"].append("unselected-created")
        elif fault == "duplicate_model_delivery":
            affected.rows.append({**affected.rows[0], "id": "metered-2", "request_id": "request-2"})
        elif fault == "foreign_metering":
            affected.rows[0]["org_id"] = "foreign-tenant"
        elif fault == "unexpected_profile_write":
            affected.profile["product"]["name"] = "Overwritten by model"
        elif fault in ("unselected_direct_charge", "unmetered_deterministic_call"):
            affected.rows.append({"id": "metered-1", "request_id": "request-1", "org_id": "tenant",
                                  "user_id": "owner", "model": "fixture-model", "status_code": 200})
        return reply

    with pytest.raises((AssertionError, ValueError)):
        capture_guidance_effects(
            "tenant", "owner", "fixture-model", mode,
            affected.usage, lambda: deepcopy(affected.profile), lambda: deepcopy(affected.session),
            lambda: deepcopy(affected.provider), wrong_turn,
            clock=lambda: affected.now, pause=affected.tick, deadline_seconds=4)

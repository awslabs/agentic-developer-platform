"""Offline contracts for served-browser V11 observations; not live acceptance."""

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.new_ui.test_product_profile import assert_denied_read, assert_journey_readback, assert_offline_unchanged, assert_revoked_read
from tests.e2e.new_ui.test_product_resume import assert_deferred_profile


def verify_full_journey_report(report, directory):
    if (not isinstance(report, dict)
            or not isinstance(report.get("run_id"), str)
            or any(not isinstance(report.get(name), str) for name in ("source_revision", "requirements_revision"))
            or report.get("scenario_id") != "SCN-01" or report.get("ac_id") != "AC-01"
            or report.get("starting_point") not in ("idea", "existing_assets", "running_product")
            or report.get("browser_status") != "pass"
            or not re.fullmatch(r"product-[1-9][0-9]*-[1-9][0-9]*", report.get("run_id", ""))
            or any(not re.fullmatch(r"[a-f0-9]{40}", report.get(name, ""))
                   for name in ("source_revision", "requirements_revision"))):
        raise ValueError("Journey requires executed browser checks and pinned run revisions")
    observed = report.get("observed", {})
    if not isinstance(observed, dict):
        raise ValueError("Journey needs independently observed deployment details")
    if (observed.get("source_revision") != report["source_revision"]
            or observed.get("requirements_revision") != report["requirements_revision"]
            or observed.get("product_onboarding_enabled") is not True
            or not report.get("frontend_release_ref") or not report.get("api_image_digest")
            or not report.get("schema_head") or not report.get("product_id") or not report.get("project_id")):
        raise ValueError("Journey source, release, schema and scope must match the observed deployment")
    viewports = report.get("viewports")
    if (not isinstance(viewports, list) or any(not isinstance(entry, dict) for entry in viewports)
            or [entry.get("width") for entry in viewports] != [390, 1440]):
        raise ValueError("Journey needs distinct narrow and wide browser observations")
    reads = report.get("api_reads")
    if not isinstance(reads, list):
        raise ValueError("Journey needs independently observed API readback")
    previous_revision = None
    for entry in viewports:
        if entry.get("keyboard") != "pass" or entry.get("offline_retry") != "pass":
            raise ValueError("Journey requires keyboard navigation and verified offline retry")
        screenshot = Path(entry.get("screenshot", ""))
        expected = directory / f"{report['run_id']}-v11_{report['starting_point']}-{entry['width']}.png"
        if (screenshot != expected or screenshot.is_symlink() or not screenshot.is_file()
                or screenshot.stat().st_uid != os.getuid() or screenshot.stat().st_mode & 0o077
                or screenshot.stat().st_size == 0):
            raise ValueError("Journey screenshot must be private, owned and bound to its viewport")
        revision = entry.get("revision")
        if (type(revision) is not int or revision < 2
                or previous_revision is not None and revision != previous_revision + 1):
            raise ValueError("Journey edits must advance the same saved revision once per viewport")
        previous_revision = revision
        if not any(isinstance(read, dict) and read.get("status") == 200
                   and isinstance(read.get("body"), dict)
                   and read["body"].get("product_id") == report["product_id"]
                   and read["body"].get("project_id") == report["project_id"]
                   and read["body"].get("revision") == revision
                   and read["body"].get("product", {}).get("starting_point") == report["starting_point"]
                   and read["body"]["product"].get("purpose") == entry.get("purpose")
                   for read in reads):
            raise ValueError("Journey viewport has no matching independent API observation")
    cleanup = report.get("cleanup")
    if (not isinstance(cleanup, dict)
            or any(cleanup.get(name) != report[name] for name in ("run_id", "product_id", "project_id"))
            or any(cleanup.get(name) is not True for name in ("fixture_removed", "owned_absent", "shared_unchanged"))
            or type(cleanup.get("cleanup_revision")) is not int or cleanup["cleanup_revision"] < 1):
        raise ValueError("Journey requires independently verified owned-fixture cleanup")
    return report


def verify_cross_client_report(report, directory):
    if (not isinstance(report, dict) or report.get("scenario_id") != "SCN-02"
            or report.get("ac_id") != "AC-02" or report.get("browser_status") != "pass"
            or not isinstance(report.get("run_id"), str)
            or not re.fullmatch(r"product-[1-9][0-9]*-[1-9][0-9]*", report["run_id"])
            or any(not isinstance(report.get(name), str)
                   or not re.fullmatch(r"[a-f0-9]{40}", report[name])
                   for name in ("source_revision", "requirements_revision"))):
        raise ValueError("Cross-client scenario requires executed checks and pinned revisions")
    observed = report.get("observed")
    if (not isinstance(observed, dict) or observed.get("product_onboarding_enabled") is not True
            or any(observed.get(name) != report.get(name) for name in ("source_revision", "requirements_revision",
                                                                       "frontend_release_ref", "api_image_digest", "schema_head"))
            or not all(report.get(name) for name in ("tenant_id", "product_id", "project_id"))):
        raise ValueError("Cross-client scenario differs from observed deployment or scope")
    started, first, final = (report.get(name) for name in ("session_started", "session_before", "session_after"))
    before, after = report.get("profile_before"), report.get("profile_after")
    if any(not isinstance(value, dict) for value in (started, first, final, before, after)):
        raise ValueError("Cross-client scenario needs separate CLI, session and Profile observations")
    for value in (started, first, final):
        if (not value.get("id") or value.get("id") != started["id"]
                or any(value.get(name) != report[name] for name in ("tenant_id", "product_id", "project_id"))
                or value.get("resume_path") != started.get("resume_path")
                or value.get("operation_ids") != []):
            raise ValueError("Cross-client session identity, scope, path or peer operations changed")
    if (type(first.get("revision")) is not int or first["revision"] != started.get("revision")
            or type(final.get("revision")) is not int or final["revision"] < first["revision"]
            or first.get("steps") != started.get("steps")):
        raise ValueError("Cross-client resume lost its CLI-created session revision or steps")
    try:
        availability = assert_deferred_profile(before, report)
        if (assert_deferred_profile(after, report) != availability
                or after["revision"] != before["revision"] + 1
                or before["product"]["purpose"] == after["product"]["purpose"]
                or any(after["product"].get(name) != before["product"].get(name)
                       for name in ("name", "intended_users", "starting_point"))):
            raise ValueError("Cross-client Profile edit or deferred peer state changed unexpectedly")
    except (AssertionError, KeyError) as failure:
        raise ValueError("Cross-client Profile or peer readback differs from the owned session") from failure
    events = report.get("cli_events")
    if not isinstance(events, list) or len(events) != 1:
        raise ValueError("Cross-client scenario needs one verified installed CLI start")
    event = events[0]
    command = event.get("command") if isinstance(event, dict) else None
    if (not isinstance(command, list) or command[:3] != ["adp", "onboard", "start"]
            or event.get("exit_code") != 0
            or not re.fullmatch(r"[a-f0-9]{64}", event.get("installed_cli_sha256", ""))):
        raise ValueError("Cross-client start did not use the pinned installed CLI")
    for flag, value in (("--product", report["product_id"]), ("--project", report["project_id"]),
                        ("--revision", str(before["revision"]))):
        if command.count(flag) != 1 or command.index(flag) + 1 >= len(command) or command[command.index(flag) + 1] != value:
            raise ValueError("Cross-client CLI start differs from the owned Profile scope")
    try:
        client_result = json.loads(event["stdout"].strip().splitlines()[-1])
    except (KeyError, IndexError, ValueError) as failure:
        raise ValueError("Cross-client CLI response is not a captured JSON result") from failure
    if client_result.get("status") != "ok" or client_result.get("detail") != started:
        raise ValueError("Cross-client CLI result differs from the recorded session")
    reads = report.get("api_reads")
    if (not isinstance(reads, list) or any(not any(isinstance(read, dict) and read.get("status") == 200
                                          and read.get("body") == observation for read in reads)
                                      for observation in (first, final, before, after))):
        raise ValueError("Cross-client scenario lacks independent API session and Profile readback")
    viewports = report.get("viewports")
    if not isinstance(viewports, list) or len(viewports) != 1 or not isinstance(viewports[0], dict):
        raise ValueError("Cross-client scenario needs a served-browser observation")
    screenshot = Path(viewports[0].get("screenshot", ""))
    if (screenshot != directory / f"{report['run_id']}-v11_resume.png" or screenshot.is_symlink()
            or not screenshot.is_file() or screenshot.stat().st_uid != os.getuid()
            or screenshot.stat().st_mode & 0o077 or not screenshot.stat().st_size):
        raise ValueError("Cross-client screenshot must be private and bound to its owned run")
    cleanup = report.get("cleanup")
    if (not isinstance(cleanup, dict)
            or any(cleanup.get(name) != report[name] for name in ("run_id", "product_id", "project_id"))
            or any(cleanup.get(name) is not True for name in ("fixture_removed", "owned_absent", "shared_unchanged"))
            or type(cleanup.get("cleanup_revision")) is not int or cleanup["cleanup_revision"] < 1):
        raise ValueError("Cross-client owned fixture must be independently retired")
    return report


def verify_release_report(report, directory):
    if (not isinstance(report, dict) or report.get("scenario_id") != "SCN-03"
            or report.get("ac_id") != "AC-03" or report.get("browser_status") != "pass"
            or not isinstance(report.get("run_id"), str)
            or not re.fullmatch(r"product-[1-9][0-9]*-[1-9][0-9]*", report["run_id"])
            or any(not isinstance(report.get(key), str) or not re.fullmatch(r"[a-f0-9]{40}", report[key])
                   for key in ("source_revision", "requirements_revision"))):
        raise ValueError("Release requires executed browser checks and pinned revisions")
    observed = report.get("observed")
    if (not isinstance(observed, dict) or observed.get("product_onboarding_enabled") is not True
            or any(not report.get(key) or observed.get(key) != report[key]
                   for key in ("source_revision", "requirements_revision", "frontend_release_ref",
                               "api_image_digest", "schema_head"))
            or not all(report.get(key) for key in ("tenant_id", "product_id", "project_id"))):
        raise ValueError("Release bundle, API, requirements or owned scope differs from deployment")
    baseline, concurrent, saved = (report.get(key) for key in ("baseline", "concurrent", "saved"))
    if any(not isinstance(value, dict) for value in (baseline, concurrent, saved)):
        raise ValueError("Release needs baseline, concurrent and saved Profile observations")
    scope = ("product_id", "project_id")
    if (any(value.get(key) != report[key] for value in (baseline, concurrent, saved) for key in scope)
            or any(type(value.get("revision")) is not int for value in (baseline, concurrent, saved))
            or baseline["revision"] < 1 or concurrent["revision"] != baseline["revision"] + 1
            or saved["revision"] != concurrent["revision"] + 1
            or not all(isinstance(value.get("product"), dict) for value in (baseline, concurrent, saved))):
        raise ValueError("Release Profile scope or revision progression is inconsistent")
    original, changed, final = (value["product"] for value in (baseline, concurrent, saved))
    if (any(changed.get(key) != original.get(key) or final.get(key) != original.get(key)
            for key in ("name", "intended_users", "starting_point"))
            or not isinstance(original.get("purpose"), str) or not original["purpose"]
            or changed.get("purpose") != original["purpose"] + " from another client"
            or final.get("purpose") != original["purpose"] + " from V11 browser"):
        raise ValueError("Release stale edit, offline retry or saved fields differ")
    reads = report.get("api_reads")
    if not isinstance(reads, list) or any(not any(
            isinstance(read, dict) and read.get("status") == 200 and read.get("body") == value
            for read in reads) for value in (baseline, concurrent, saved)):
        raise ValueError("Release lacks independent Profile readback")
    for status, code in ((404, "not_found"), (403, "tenant_membership_revoked")):
        denials = [read["body"] for read in reads if isinstance(read, dict) and read.get("status") == status
                   and isinstance(read.get("body"), dict)]
        if (not denials or not any(code in json.dumps(body) for body in denials)
                or any(field in json.dumps(body) for body in denials
                       for field in (original["name"], original["purpose"], final["purpose"]))):
            raise ValueError("Release lacks private foreign and revoked denial evidence")
    viewports = report.get("viewports")
    if (not isinstance(viewports, list) or any(not isinstance(entry, dict) for entry in viewports)
            or [entry.get("width") for entry in viewports] != [390, 1440]):
        raise ValueError("Release requires two served-browser viewports")
    for entry in viewports:
        screenshot = Path(entry.get("screenshot", ""))
        expected = directory / f"{report['run_id']}-v11-release-{entry['width']}.png"
        if (entry.get("keyboard") != "pass" or entry.get("overflow") is not False
                or screenshot != expected or screenshot.is_symlink() or not screenshot.is_file()
                or screenshot.stat().st_uid != os.getuid() or screenshot.stat().st_mode & 0o077
                or not screenshot.stat().st_size):
            raise ValueError("Release screenshot must be private and bound to the viewport")
    cleanup = report.get("cleanup")
    if (not isinstance(cleanup, dict)
            or any(cleanup.get(key) != report[key] for key in ("run_id", "product_id", "project_id"))
            or any(cleanup.get(key) is not True for key in ("fixture_removed", "owned_absent", "shared_unchanged"))
            or type(cleanup.get("cleanup_revision")) is not int or cleanup["cleanup_revision"] < 1):
        raise ValueError("Release owned fixture was not independently retired")
    return report

@pytest.mark.parametrize("starting_point", ("idea", "existing_assets", "running_product"))
def test_full_journey_readback_rejects_unsaved_or_other_scope(starting_point):
    fixture = {"product_id": "owned-product", "project_id": "owned-project"}
    profile = {"product_id": fixture["product_id"], "project_id": fixture["project_id"],
               "revision": 3, "product": {"name": "Example", "purpose": "At 390px",
                                           "intended_users": ["Maintainers"], "starting_point": starting_point}}

    def response(value):
        return SimpleNamespace(status=200, json=lambda: value)

    assert assert_journey_readback(response(profile), fixture, starting_point,
                                   "Example", "At 390px", ["Maintainers"], 2) == profile
    for path, value in (("revision", 2), ("project_id", "other-project"),
                        ("product_id", "other-product")):
        changed = deepcopy(profile)
        changed[path] = value
        with pytest.raises(AssertionError):
            assert_journey_readback(response(changed), fixture, starting_point,
                                    "Example", "At 390px", ["Maintainers"], 2)
    for field, value in (("name", "Other"), ("purpose", "Before save"),
                         ("intended_users", ["Other"]), ("starting_point", "other")):
        changed = deepcopy(profile)
        changed["product"][field] = value
        with pytest.raises(AssertionError):
            assert_journey_readback(response(changed), fixture, starting_point,
                                    "Example", "At 390px", ["Maintainers"], 2)


@pytest.mark.parametrize("starting_point", ("idea", "existing_assets", "running_product"))
def test_offline_retry_requires_unmodified_independent_readback(starting_point):
    fixture = {"product_id": "owned-product", "project_id": "owned-project"}
    baseline = {**fixture, "revision": 2,
                "product": {"name": "Example", "purpose": "Before offline save",
                            "intended_users": ["Maintainers"], "starting_point": starting_point}}

    def response(value):
        return SimpleNamespace(status=200, json=lambda: value)

    assert assert_offline_unchanged(response(baseline), fixture, baseline) == baseline
    for key, value in (("revision", 3), ("product_id", "other-product"), ("project_id", "other-project")):
        changed = deepcopy(baseline)
        changed[key] = value
        with pytest.raises(AssertionError):
            assert_offline_unchanged(response(changed), fixture, baseline)
    for key, value in (("purpose", "Early commit"), ("starting_point", "other")):
        changed = deepcopy(baseline)
        changed["product"][key] = value
        with pytest.raises(AssertionError):
            assert_offline_unchanged(response(changed), fixture, baseline)


def test_release_negative_readback_rejects_duplicate_revision_and_disclosure():
    fixture = {"product_id": "owned-product", "project_id": "owned-project"}
    saved = {**fixture, "revision": 3,
             "product": {"name": "Private Example", "purpose": "Private purpose",
                         "intended_users": ["Maintainers"], "starting_point": "idea"}}
    duplicate = SimpleNamespace(status=200, json=lambda: saved)
    with pytest.raises(AssertionError, match="revision once"):
        assert_journey_readback(duplicate, fixture, "idea", "Private Example", "Private purpose", ["Maintainers"], 3)

    def denial(status, body):
        return SimpleNamespace(status=status, text=lambda: str(body), json=lambda: body)

    assert_denied_read(denial(404, {"detail": {"code": "not_found"}}), ("Private Example", "Private purpose"))
    with pytest.raises(AssertionError, match="disclosed"):
        assert_denied_read(denial(404, {"detail": "Private purpose"}), ("Private Example", "Private purpose"))
    assert_revoked_read(denial(403, {"detail": {"error": "tenant_membership_revoked"}}),
                        ("Private Example", "Private purpose"))
    with pytest.raises(AssertionError, match="disclosed"):
        assert_revoked_read(denial(403, {"detail": {"error": "tenant_membership_revoked",
                                                     "message": "Private Example"}}),
                            ("Private Example", "Private purpose"))


@pytest.mark.parametrize("starting_point", ("idea", "existing_assets", "running_product"))
def test_full_journey_report_rejects_false_green(tmp_path, starting_point):
    run_id = "product-42-1"
    source = "a" * 40
    requirements = "b" * 40
    viewports = []
    reads = []
    for width, revision in ((390, 3), (1440, 4)):
        screenshot = tmp_path / f"{run_id}-v11_{starting_point}-{width}.png"
        screenshot.write_bytes(b"private browser capture")
        screenshot.chmod(0o600)
        purpose = f"Example saved at {width}px"
        viewports.append({"width": width, "screenshot": str(screenshot),
                          "revision": revision, "purpose": purpose,
                          "keyboard": "pass", "offline_retry": "pass"})
        reads.append({"status": 200, "body": {"product_id": "owned-product", "project_id": "owned-project",
                 "revision": revision, "product": {"starting_point": starting_point, "purpose": purpose}}})
    report = {
        "scenario_id": "SCN-01", "ac_id": "AC-01", "starting_point": starting_point,
        "run_id": run_id, "source_revision": source, "requirements_revision": requirements,
        "observed": {"source_revision": source, "requirements_revision": requirements,
                     "product_onboarding_enabled": True},
        "frontend_release_ref": "verified-receipt", "api_image_digest": "sha256:example",
        "schema_head": "example-head", "product_id": "owned-product", "project_id": "owned-project",
        "browser_status": "pass", "viewports": viewports, "api_reads": reads,
        "cleanup": {"run_id": run_id, "product_id": "owned-product", "project_id": "owned-project",
                    "fixture_removed": True, "owned_absent": True, "shared_unchanged": True,
                    "cleanup_revision": 1},
    }
    assert verify_full_journey_report(report, tmp_path) == report
    for field, value in (("browser_status", "skip"), ("source_revision", "c" * 40),
                         ("requirements_revision", "unknown"), ("scenario_id", "SCN-02"),
                         ("product_id", "other-product"), ("api_reads", []), ("viewports", [])):
        invalid = deepcopy(report)
        invalid[field] = value
        with pytest.raises(ValueError):
            verify_full_journey_report(invalid, tmp_path)
    for change in (lambda value: value["viewports"][1].update(revision=3),
                   lambda value: value["viewports"][0].update(keyboard="failed"),
                   lambda value: value["viewports"][0].update(offline_retry="not_run"),
                   lambda value: value["api_reads"][1]["body"].update(project_id="other-project"),
                   lambda value: value["observed"].update(product_onboarding_enabled=False),
                   lambda value: value["cleanup"].update(owned_absent=False),
                   lambda value: value["viewports"][0].update(screenshot="/tmp/another-run.png")):
        invalid = deepcopy(report)
        change(invalid)
        with pytest.raises(ValueError):
            verify_full_journey_report(invalid, tmp_path)
    screenshot = tmp_path / f"{run_id}-v11_{starting_point}-390.png"
    screenshot.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        verify_full_journey_report(report, tmp_path)


def test_deferred_profile_rejects_peer_success_or_new_required_steps():
    fixture = {"product_id": "owned-product", "project_id": "owned-project", "tenant_id": "owned-tenant"}
    profile = {**fixture, "revision": 2, "context_status": "unavailable",
               "progress": {"checks": [{"id": "information", "required": True},
                                       {"id": "project_scope", "required": True}]},
               "peer_availability": {
                   "context": {"status": "unavailable", "reason": "integration_deferred",
                               "message": "Product Context integration is unavailable. Your saved Product remains available."},
                   "l5": {"status": "unavailable", "reason": "integration_deferred",
                          "message": "L5 services integration is unavailable. Your saved Product remains available."},
               }}
    assert assert_deferred_profile(profile, fixture) == profile["peer_availability"]
    for key, value in (("product_id", "another-product"), ("project_id", "another-project"),
                       ("tenant_id", "another-tenant"), ("context_status", "complete"), ("revision", 0)):
        changed = deepcopy(profile)
        changed[key] = value
        with pytest.raises(AssertionError):
            assert_deferred_profile(changed, fixture)
    for peer in ("context", "l5"):
        for key, value in (("status", "complete"), ("reason", "connected"),
                           ("message", "Successfully dispatched to the provider")):
            changed = deepcopy(profile)
            changed["peer_availability"][peer][key] = value
            with pytest.raises(AssertionError):
                assert_deferred_profile(changed, fixture)
        for key in ("receipt", "operation_id", "result_ref"):
            changed = deepcopy(profile)
            changed["peer_availability"][peer][key] = "fabricated-peer-result"
            with pytest.raises(AssertionError, match="receipt or result"):
                assert_deferred_profile(changed, fixture)
    changed = deepcopy(profile)
    changed["progress"]["checks"].append({"id": "context", "required": True})
    with pytest.raises(AssertionError):
        assert_deferred_profile(changed, fixture)


def test_cross_client_report_requires_cli_browser_api_and_retirement(tmp_path):
    run_id, source, requirements = "product-42-1", "a" * 40, "b" * 40
    scope = {"tenant_id": "example-tenant", "product_id": "example-product", "project_id": "example-project"}
    session = {**scope, "id": "example-session", "revision": 1,
               "resume_path": "/products/example-product/projects/example-project/onboarding-sessions/example-session",
               "operation_ids": [], "steps": {"information": {"status": "complete"}}}
    availability = {
        peer: {"status": "unavailable", "reason": "integration_deferred",
               "message": f"{name} integration is unavailable. Your saved Product remains available."}
        for peer, name in (("context", "Product Context"), ("l5", "L5 services"))
    }
    before = {**scope, "revision": 1, "context_status": "unavailable", "peer_availability": availability,
              "progress": {"checks": [{"id": "information", "required": True}]},
              "product": {"name": "Example", "purpose": "Before edit", "intended_users": ["Maintainers"],
                          "starting_point": "idea"}}
    after = deepcopy(before)
    after["revision"] = 2
    after["product"]["purpose"] = "After browser edit"
    screenshot = tmp_path / f"{run_id}-v11_resume.png"
    screenshot.write_bytes(b"private browser capture")
    screenshot.chmod(0o600)
    command = ["adp", "onboard", "start", "--product", scope["product_id"],
               "--project", scope["project_id"], "--revision", "1", "--json"]
    report = {
        **scope, "scenario_id": "SCN-02", "ac_id": "AC-02", "run_id": run_id,
        "source_revision": source, "requirements_revision": requirements,
        "frontend_release_ref": "example-release", "api_image_digest": "sha256:example",
        "schema_head": "example-head", "browser_status": "pass",
        "observed": {"source_revision": source, "requirements_revision": requirements,
                     "frontend_release_ref": "example-release", "api_image_digest": "sha256:example",
                     "schema_head": "example-head", "product_onboarding_enabled": True},
        "viewports": [{"screenshot": str(screenshot)}],
        "cli_events": [{"command": command, "exit_code": 0, "installed_cli_sha256": "c" * 64,
                        "stdout": json.dumps({"status": "ok", "detail": session})}],
        "session_started": session, "session_before": deepcopy(session), "session_after": deepcopy(session),
        "profile_before": before, "profile_after": after,
        "api_reads": [{"status": 200, "body": value} for value in (session, before, after)],
        "cleanup": {**scope, "run_id": run_id, "fixture_removed": True, "owned_absent": True,
                    "shared_unchanged": True, "cleanup_revision": 1},
    }
    assert verify_cross_client_report(report, tmp_path) == report
    for field, value in (("browser_status", "skip"), ("requirements_revision", "missing"),
                         ("cli_events", []), ("api_reads", []), ("cleanup", None)):
        changed = deepcopy(report)
        changed[field] = value
        with pytest.raises(ValueError):
            verify_cross_client_report(changed, tmp_path)
    for change in (lambda item: item["session_after"]["operation_ids"].append("queued-peer"),
                   lambda item: item["session_before"].update(project_id="foreign-project"),
                   lambda item: item["profile_after"]["peer_availability"]["context"].update(status="complete"),
                   lambda item: item["profile_after"]["peer_availability"]["l5"].update(receipt="invented"),
                   lambda item: item["cli_events"][0].update(exit_code=1),
                   lambda item: item["cli_events"][0].update(stdout='{"status":"ok","detail":{}}'),
                   lambda item: item["api_reads"].pop(),
                   lambda item: item["cleanup"].update(shared_unchanged=False)):
        changed = deepcopy(report)
        change(changed)
        with pytest.raises(ValueError):
            verify_cross_client_report(changed, tmp_path)
    screenshot.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        verify_cross_client_report(report, tmp_path)


def test_release_report_requires_observed_revisions_reads_and_cleanup(tmp_path):
    run_id, source, requirements = "product-42-1", "a" * 40, "b" * 40
    scope = {"tenant_id": "example-tenant", "product_id": "example-product", "project_id": "example-project"}
    original = {"name": "Private Example", "purpose": "Private purpose", "intended_users": ["Maintainers"],
                "starting_point": "idea"}
    baseline = {**scope, "revision": 1, "product": original}
    concurrent = deepcopy(baseline)
    concurrent["revision"] = 2
    concurrent["product"]["purpose"] = original["purpose"] + " from another client"
    saved = deepcopy(concurrent)
    saved["revision"] = 3
    saved["product"]["purpose"] = original["purpose"] + " from V11 browser"
    viewports = []
    for width in (390, 1440):
        screenshot = tmp_path / f"{run_id}-v11-release-{width}.png"
        screenshot.write_bytes(b"private browser capture")
        screenshot.chmod(0o600)
        viewports.append({"width": width, "screenshot": str(screenshot), "overflow": False, "keyboard": "pass"})
    report = {
        **scope, "scenario_id": "SCN-03", "ac_id": "AC-03", "run_id": run_id,
        "source_revision": source, "requirements_revision": requirements,
        "frontend_release_ref": "example-release", "api_image_digest": "sha256:example", "schema_head": "example-head",
        "observed": {"source_revision": source, "requirements_revision": requirements,
                     "frontend_release_ref": "example-release", "api_image_digest": "sha256:example",
                     "schema_head": "example-head", "product_onboarding_enabled": True},
        "baseline": baseline, "concurrent": concurrent, "saved": saved,
        "browser_status": "pass", "viewports": viewports,
        "api_reads": [{"status": 200, "body": value} for value in (baseline, concurrent, saved)] + [
            {"status": 404, "body": {"detail": {"code": "not_found"}}},
            {"status": 403, "body": {"detail": {"error": "tenant_membership_revoked"}}}],
        "cleanup": {**scope, "run_id": run_id, "fixture_removed": True, "owned_absent": True,
                    "shared_unchanged": True, "cleanup_revision": 1},
    }
    assert verify_release_report(report, tmp_path) == report
    for change in (lambda item: item.update(browser_status="skip"),
                   lambda item: item["observed"].update(source_revision="c" * 40),
                   lambda item: item["observed"].update(api_image_digest="other"),
                   lambda item: item["observed"].update(requirements_revision="other"),
                   lambda item: item["saved"].update(revision=2),
                   lambda item: item["concurrent"].update(project_id="foreign-project"),
                   lambda item: item["api_reads"].pop(2),
                   lambda item: item["api_reads"][3]["body"]["detail"].update(name="Private Example"),
                   lambda item: item["api_reads"].pop(),
                   lambda item: item["viewports"].pop(),
                   lambda item: item["cleanup"].update(shared_unchanged=False)):
        changed = deepcopy(report)
        change(changed)
        with pytest.raises(ValueError):
            verify_release_report(changed, tmp_path)
    (tmp_path / f"{run_id}-v11-release-390.png").chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        verify_release_report(report, tmp_path)

"""V06 source-reuse selection and owned cleanup are not live acceptance evidence."""

import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from modules.gateway.tests.products.source_reuse import assert_source_reuse
from modules.gateway.tests.products.source_status import assert_status_surfaces

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS = "docs/product-onboarding/delivery/verification-contract.md"
SOURCE_REUSE_SELECTORS = {
    "api": "modules/gateway/tests/products/test_i13.py::running_product_adoption",
    "cli": "modules/gateway/tests/products/test_i10.py::source_picker_coverage",
    "browser_mock": "modules/gateway/frontend/src/__tests__/pages/products/ProductSources.test.tsx",
}
LIVE_SURFACES = ("api", "installed_cli", "browser", "independent_provider")


def pinned_source_reuse_case(source_revision, requirements_revision, run_id, owned_binding_ids=(), selectors=None):
    if not all(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
               for revision in (source_revision, requirements_revision)):
        raise ValueError("Source and requirements revisions must be full commit SHAs")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{4,64}", run_id):
        raise ValueError("A bounded owned fixture run ID is required")
    if selectors is not None and selectors != SOURCE_REUSE_SELECTORS:
        raise ValueError("Source reuse must select the reviewed local API, CLI and mocked browser cases")
    if (not isinstance(owned_binding_ids, (list, tuple)) or len(owned_binding_ids) > 20
            or any(not isinstance(binding_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", binding_id)
                   for binding_id in owned_binding_ids)
            or len(set(owned_binding_ids)) != len(owned_binding_ids)):
        raise ValueError("Owned binding IDs must be unique, bounded and run-owned")
    return {
        "scenario_id": "SCN-01", "source_revision": source_revision,
        "requirements_revision": requirements_revision, "run_id": run_id,
        "owned_binding_ids": list(owned_binding_ids), "selectors": dict(SOURCE_REUSE_SELECTORS),
        "selector_fixture_type": {"api": "local_fixture", "cli": "local_fixture", "browser_mock": "mocked_ui"},
        "live_surfaces": {surface: "not_run" for surface in LIVE_SURFACES},
        "cleanup": {"status": "pending"}, "status": "not_run",
    }


def record_source_cleanup(case, receipt):
    if not isinstance(receipt, dict):
        raise ValueError("Cleanup needs an owned readback receipt")
    if case["cleanup"]["status"] != "pending":
        raise ValueError("Cleanup has already been recorded")
    if (receipt.get("run_id") != case["run_id"]
            or receipt.get("source_revision") != case["source_revision"]
            or receipt.get("unlinked_binding_ids") != case["owned_binding_ids"]
            or receipt.get("remaining_owned_binding_ids") != []
            or receipt.get("independent_provider_unchanged") is not True):
        raise ValueError("Cleanup needs exact run ownership, independent readback and unchanged providers")
    return {**case, "cleanup": {"status": "verified", "receipt": receipt}}


def collect_pinned_source_reuse(tmp_path, run_id="example-run", owned_binding_ids=()):
    source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requirements_revision = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", REQUIREMENTS], cwd=ROOT, text=True,
    ).strip()
    case = pinned_source_reuse_case(source_revision, requirements_revision, run_id, owned_binding_ids)
    for selector in case["selectors"].values():
        path, _, _ = selector.partition("::")
        assert (ROOT / path).is_file(), "Reviewed source-reuse case is missing"
    output = tmp_path / "v06-source-reuse-case.json"
    output.write_text(json.dumps(case, sort_keys=True) + "\n")
    assert json.loads(output.read_text()) == case
    return case


@pytest.mark.live_only
def live_sources_reuse(tmp_path):
    """Validate protected deployed-client reports and independent provider observation."""
    if os.environ.get("PRODUCT_SOURCES_LIVE_ENABLED") != "1":
        pytest.skip("SCN-01 needs authorized deployed-source verification")
    run_id = os.environ.get("PRODUCT_SOURCES_RUN_ID")
    report_path = os.environ.get("PRODUCT_SOURCES_REPORT_PATH")
    if not run_id or not report_path:
        raise ValueError("SCN-01 needs a protected run and deployed attempt report")
    path = Path(report_path)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Source report must not be group or world readable")
    report = json.loads(path.read_text())
    case = collect_pinned_source_reuse(tmp_path, run_id, report.get("owned_binding_ids", ()))
    assert verify_live_source_reuse(case, report)


def test_source_reuse_scenario_pins_revisions_and_distinguishes_local_from_live(tmp_path):
    case = collect_pinned_source_reuse(tmp_path)
    assert case["scenario_id"] == "SCN-01" and case["selectors"] == SOURCE_REUSE_SELECTORS
    assert case["live_surfaces"] == dict.fromkeys(LIVE_SURFACES, "not_run")
    assert case["selector_fixture_type"]["browser_mock"] == "mocked_ui"
    for source, requirements, run_id, selectors in (
        ("a" * 39, "b" * 40, "example-run", None),
        ("a" * 40, "b" * 39, "example-run", None),
        ("a" * 40, "b" * 40, "bad/run", None),
        ("a" * 40, "b" * 40, "example-run", {**SOURCE_REUSE_SELECTORS, "api": "wrong"}),
    ):
        with pytest.raises(ValueError):
            pinned_source_reuse_case(source, requirements, run_id, selectors=selectors)


def test_source_reuse_cleanup_requires_independent_owned_readback():
    case = pinned_source_reuse_case("a" * 40, "b" * 40, "example-run", ["binding-1"])
    receipt = {"run_id": "example-run", "source_revision": "a" * 40,
               "unlinked_binding_ids": ["binding-1"], "remaining_owned_binding_ids": [],
               "independent_provider_unchanged": True}
    for change in ({"unlinked_binding_ids": []}, {"remaining_owned_binding_ids": ["binding-1"]},
                   {"run_id": "other-run"}, {"independent_provider_unchanged": False}):
        with pytest.raises(ValueError, match="Cleanup needs exact run ownership"):
            record_source_cleanup(case, {**receipt, **change})
    recorded = record_source_cleanup(case, receipt)
    assert recorded["cleanup"]["status"] == "verified" and case["cleanup"]["status"] == "pending"
    with pytest.raises(ValueError, match="already been recorded"):
        record_source_cleanup(recorded, receipt)


def test_source_reuse_live_selector_never_reports_collection_as_success(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_SOURCES_RUN_ID", "example-run")
    with pytest.raises(pytest.skip.Exception, match="authorized deployed-source verification"):
        live_sources_reuse(tmp_path)
    assert not (tmp_path / "v06-source-reuse-case.json").exists()
    with pytest.raises(ValueError, match="unique, bounded"):
        pinned_source_reuse_case("a" * 40, "b" * 40, "example-run", [["unhashable"]])


def verify_live_source_reuse(case, report):
    if (report.get("run_id") != case["run_id"]
            or report.get("source_revision") != case["source_revision"]
            or report.get("requirements_revision") != case["requirements_revision"]
            or report.get("owned_binding_ids") != case["owned_binding_ids"]):
        raise ValueError("Deployed source report differs from the pinned owned run")
    observations = report.get("observations")
    if not isinstance(observations, dict) or set(observations) != set(LIVE_SURFACES):
        raise ValueError("Deployed source report needs every independent surface")
    for surface, observation in observations.items():
        if (not isinstance(observation, dict) or observation.get("status") != "passed"
                or not isinstance(observation.get("artifact_ref"), str) or not observation["artifact_ref"]
                or observation.get("run_id") != case["run_id"]
                or observation.get("source_revision") != case["source_revision"]):
            raise ValueError(f"Missing or mismatched deployed {surface} observation")
    provider = observations["independent_provider"]
    calls = provider.get("calls")
    if (not isinstance(calls, list) or not 2 <= len(calls) <= 100
            or not any(call.startswith("GET ") for call in calls if isinstance(call, str))
            or "sts:GetCallerIdentity" not in calls
            or any(not isinstance(call, str) or not (call.startswith("GET ") or call == "sts:GetCallerIdentity")
                   for call in calls)
            or provider.get("before") != provider.get("after")):
        raise ValueError("Independent provider readback must remain read-only and unchanged")
    before = provider["before"]
    for surface in ("api", "installed_cli", "browser"):
        observation = observations[surface]
        assert_source_reuse(observation["repository"], observation["environment"],
                            before["repository"], before["environment"])
    return record_source_cleanup(case, report.get("cleanup"))["cleanup"]["status"] == "verified"


def test_source_reuse_live_report_requires_every_surface_and_owned_cleanup(tmp_path, monkeypatch):
    case = collect_pinned_source_reuse(tmp_path)
    repository = {"kind": "repository", "namespace": "27", "native_id": "35", "source_revision": "repo-rev"}
    environment = {"kind": "environment", "namespace": "000000000000", "native_id": "us-east-1",
                   "source_revision": "secret-rev", "observed_at": "2026-10-20T00:00:00Z",
                   "verified_environment": {"provider": "aws", "account_id": "000000000000",
                                            "regions": ["us-east-1"], "role_ref": "role/example",
                                            "source_revision": "secret-rev", "verified_at": "2026-10-20T00:00:00Z"}}
    provider = {"repository": {"id": 35, "owner": {"id": 27}, "updated_at": "repo-rev"},
                "environment": {"account_id": "000000000000", "region": "us-east-1", "role_ref": "role/example"}}
    reports = {surface: {"status": "passed", "artifact_ref": surface + ".json",
                         "run_id": case["run_id"], "source_revision": case["source_revision"],
                         "repository": repository, "environment": environment}
               for surface in ("api", "installed_cli", "browser")}
    reports["independent_provider"] = {"status": "passed", "artifact_ref": "provider.json",
                                       "run_id": case["run_id"], "source_revision": case["source_revision"],
                                       "calls": ["GET repository", "sts:GetCallerIdentity"],
                                       "before": provider, "after": provider}
    receipt = {"run_id": case["run_id"], "source_revision": case["source_revision"],
               "unlinked_binding_ids": [], "remaining_owned_binding_ids": [],
               "independent_provider_unchanged": True}
    report = {"run_id": case["run_id"], "source_revision": case["source_revision"],
              "requirements_revision": case["requirements_revision"], "owned_binding_ids": [],
              "observations": reports, "cleanup": receipt}
    assert verify_live_source_reuse(case, report)
    for surface, change in (("browser", {"status": "not_run"}),
                            ("installed_cli", {"source_revision": "stale"}),
                            ("independent_provider", {"calls": ["POST repository"]})):
        changed = json.loads(json.dumps(report))
        changed["observations"][surface].update(change)
        with pytest.raises((ValueError, AssertionError)):
            verify_live_source_reuse(case, changed)
    for change in ({"source_revision": "stale"}, {"cleanup": {**receipt, "remaining_owned_binding_ids": ["binding-1"]}}):
        with pytest.raises(ValueError):
            verify_live_source_reuse(case, {**report, **change})

    path = tmp_path / "protected-source-report.json"
    path.write_text(json.dumps(report))
    path.chmod(0o600)
    monkeypatch.setenv("PRODUCT_SOURCES_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_SOURCES_REPORT_PATH", str(path))
    monkeypatch.setenv("PRODUCT_SOURCES_RUN_ID", case["run_id"])
    live_sources_reuse(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="group or world readable"):
        live_sources_reuse(tmp_path)


SOURCE_STATUS_SELECTORS = {
    "api": "modules/gateway/tests/products/test_v06_source_surfaces.py::test_source_expiry_and_revocation_recheck_every_project_surface",
    "cli": "modules/gateway/tests/products/test_i10.py::source_picker_coverage",
    "browser_mock": "modules/gateway/frontend/src/__tests__/pages/products/ProductSources.test.tsx",
}
STATUS_PHASES = {
    "before_expiry": "verified", "partial": "verified", "at_expiry": "stale",
    "unavailable": "restricted", "revoked": "restricted",
}
STATUS_ARTIFACTS = ("api", "installed_cli", "browser", "independent_provider")


def source_status_clock(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Source status needs a pinned UTC clock")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Source status needs a pinned UTC clock") from error
    if parsed.tzinfo != UTC:
        raise ValueError("Source status needs a pinned UTC clock")
    return parsed


def pinned_source_status_case(source_revision, requirements_revision, run_id, observed_at, stale_after, selectors=None,
                              owned_binding_ids=()):
    case = pinned_source_reuse_case(source_revision, requirements_revision, run_id, owned_binding_ids)
    if selectors is not None and selectors != SOURCE_STATUS_SELECTORS:
        raise ValueError("Source status must select reviewed local API, CLI and mocked browser cases")
    if source_status_clock(stale_after) <= source_status_clock(observed_at):
        raise ValueError("Status expiry must follow the original observation")
    for selector in SOURCE_STATUS_SELECTORS.values():
        if not (ROOT / selector.partition("::")[0]).is_file():
            raise ValueError("Reviewed status selector is missing")
    return {**case, "scenario_id": "SCN-02", "selectors": dict(SOURCE_STATUS_SELECTORS),
            "selector_fixture_type": {"api": "local_fixture", "cli": "local_fixture", "browser_mock": "mocked_ui"},
            "observed_at": observed_at, "stale_after": stale_after,
            "attempts": dict.fromkeys(STATUS_PHASES, "not_run")}


def verify_live_source_status(case, report):
    for field in ("run_id", "source_revision", "requirements_revision", "owned_binding_ids", "observed_at", "stale_after"):
        if report.get(field) != case[field]:
            raise ValueError("Status report differs from the pinned run, source or clock")
    identity = report.get("provider_identity")
    if not isinstance(identity, dict) or any(not isinstance(identity.get(field), str) or not identity[field]
                                              for field in ("account_id", "role_ref")):
        raise ValueError("Independent provider identity is required to check revoked details")
    attempts = report.get("attempts")
    if not isinstance(attempts, dict) or set(attempts) != set(STATUS_PHASES):
        raise ValueError("Source status needs every scoped attempt")
    for name, phase in STATUS_PHASES.items():
        attempt = attempts[name]
        if (not isinstance(attempt, dict) or attempt.get("status") != "passed"
                or attempt.get("run_id") != case["run_id"]
                or attempt.get("source_revision") != case["source_revision"]):
            raise ValueError("Missing or failed source status attempt")
        artifacts = attempt.get("artifacts")
        if (not isinstance(artifacts, dict) or set(artifacts) != set(STATUS_ARTIFACTS)
                or any(not isinstance(ref, str) or not ref for ref in artifacts.values())):
            raise ValueError("Source status needs independent API, CLI, browser and provider artifacts")
        clock = source_status_clock(attempt.get("clock"))
        if (name in ("before_expiry", "partial") and not source_status_clock(case["observed_at"]) <= clock < source_status_clock(case["stale_after"])):
            raise ValueError("Pre-expiry attempt is outside the pinned observation window")
        if name == "at_expiry" and clock != source_status_clock(case["stale_after"]):
            raise ValueError("Expiry attempt must use the pinned boundary")
        view = attempt["surfaces"]
        assert_status_surfaces(view["source"], view["binding"], view["profile"], view["revision"], view["session"],
                               phase=phase, observed_at=case["observed_at"] if phase != "restricted" else None,
                               native_id=view["native_id"], private_values=(identity["account_id"], identity["role_ref"]))
    return record_source_cleanup(case, report.get("cleanup"))["cleanup"]["status"] == "verified"


@pytest.mark.live_only
def live_source_status(tmp_path):
    """Validate protected attempt reports, never the existence of a selected local case alone."""
    if os.environ.get("PRODUCT_SOURCE_STATUS_LIVE_ENABLED") != "1":
        pytest.skip("SCN-02 requires deployed status attempts and protected fixtures")
    run_id = os.environ.get("PRODUCT_SOURCES_RUN_ID")
    report_path = os.environ.get("PRODUCT_SOURCE_STATUS_REPORT_PATH")
    if not run_id or not report_path:
        raise ValueError("SCN-02 needs a run-owned protected status report")
    path = Path(report_path)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Status report must not be group or world readable")
    report = json.loads(path.read_text())
    source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requirements_revision = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", REQUIREMENTS], cwd=ROOT, text=True,
    ).strip()
    case = pinned_source_status_case(source_revision, requirements_revision, run_id, report.get("observed_at"),
                                    report.get("stale_after"), owned_binding_ids=report.get("owned_binding_ids", ()))
    output = tmp_path / "v06-source-status-case.json"
    output.write_text(json.dumps(case, sort_keys=True) + "\n")
    assert verify_live_source_status(case, report)


RECOVERY_SELECTORS = {
    "api": "modules/gateway/tests/products/test_v06_recovery.py::test_interrupted_cursor_and_verification_replay_keep_one_native_binding",
    "cli": "modules/gateway/tests/products/test_v06_recovery.py::test_cli_resumes_source_cursor_and_reads_only_owned_cleanup",
    "browser_mock": "modules/gateway/frontend/src/__tests__/pages/products/ProductSources.test.tsx",
}
RECOVERY_PHASES = ("interrupted", "resumed", "reverified", "unlinked")


def pinned_source_recovery_case(source_revision, requirements_revision, run_id, started_at, owned_binding_ids,
                                preserved_binding_id, selectors=None):
    case = pinned_source_reuse_case(source_revision, requirements_revision, run_id, owned_binding_ids)
    source_status_clock(started_at)
    if (not owned_binding_ids or len(owned_binding_ids) > 3 or not isinstance(preserved_binding_id, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", preserved_binding_id)
            or preserved_binding_id in owned_binding_ids):
        raise ValueError("Recovery needs bounded owned bindings and a separate preserved binding")
    if selectors is not None and selectors != RECOVERY_SELECTORS:
        raise ValueError("Recovery must select the reviewed local API, CLI and mocked browser cases")
    for selector in RECOVERY_SELECTORS.values():
        if not (ROOT / selector.partition("::")[0]).is_file():
            raise ValueError("Reviewed recovery selector is missing")
    return {**case, "scenario_id": "SCN-03", "selectors": dict(RECOVERY_SELECTORS),
            "selector_fixture_type": {"api": "local_fixture", "cli": "local_fixture", "browser_mock": "mocked_ui"},
            "started_at": started_at, "preserved_binding_id": preserved_binding_id,
            "max_pages": 2, "max_attempts": 3, "deadline_seconds": 300,
            "attempts": dict.fromkeys(RECOVERY_PHASES, "not_run")}


def verify_live_source_recovery(case, report):
    for field in ("run_id", "source_revision", "requirements_revision", "owned_binding_ids", "preserved_binding_id",
                  "started_at"):
        if report.get(field) != case[field]:
            raise ValueError("Recovery report differs from the pinned owned run")
    finish = source_status_clock(report.get("finished_at"))
    start = source_status_clock(case["started_at"])
    if not start <= finish <= start + timedelta(seconds=case["deadline_seconds"]):
        raise ValueError("Recovery exceeded the pinned deadline")
    attempts = report.get("attempts")
    if not isinstance(attempts, dict) or set(attempts) != set(RECOVERY_PHASES):
        raise ValueError("Recovery needs every scoped attempt")
    previous = start
    for phase in RECOVERY_PHASES:
        attempt = attempts[phase]
        if (not isinstance(attempt, dict) or attempt.get("status") != "passed"
                or attempt.get("run_id") != case["run_id"]
                or attempt.get("source_revision") != case["source_revision"]):
            raise ValueError("Missing or failed recovery attempt")
        artifacts = attempt.get("artifacts")
        if (not isinstance(artifacts, dict) or set(artifacts) != set(LIVE_SURFACES)
                or any(not isinstance(ref, str) or not ref for ref in artifacts.values())
                or len(set(artifacts.values())) != len(LIVE_SURFACES)):
            raise ValueError("Recovery needs independent API, CLI, browser and provider artifacts for every attempt")
        clock = source_status_clock(attempt.get("observed_at"))
        if not previous <= clock <= finish:
            raise ValueError("Recovery attempt is outside the bounded ordered window")
        previous = clock
    first, resumed, verified, unlinked = (attempts[phase] for phase in RECOVERY_PHASES)
    cursor = first.get("resource_cursor")
    first_ids, next_ids = first.get("resource_ids"), resumed.get("resource_ids")
    if (not isinstance(cursor, str) or not cursor
            or resumed.get("requested_cursor") != cursor or resumed.get("resource_cursor") is not None
            or not isinstance(first_ids, list) or not first_ids or not isinstance(next_ids, list) or not next_ids
            or len(first_ids) != 1 or len(next_ids) != 1
            or any(not isinstance(native_id, str) or not native_id for native_id in first_ids + next_ids)
            or len(set(first_ids + next_ids)) != len(first_ids + next_ids)
            or first.get("page_count") != 1 or resumed.get("page_count") != case["max_pages"]):
        raise ValueError("Recovery must resume a bounded cursor without duplicate resources")
    binding_ids = verified.get("binding_ids")
    receipt_ids = verified.get("verify_receipt_ids")
    if (verified.get("binding_id") not in case["owned_binding_ids"]
            or not isinstance(binding_ids, list)
            or any(not isinstance(binding_id, str) for binding_id in binding_ids)
            or sorted(binding_ids) != sorted([*case["owned_binding_ids"], case["preserved_binding_id"]])
            or verified.get("selected_native_id") not in first_ids + next_ids
            or verified.get("binding_native_id") != verified.get("selected_native_id")
            or not isinstance(receipt_ids, list) or len(receipt_ids) != 2
            or not isinstance(receipt_ids[0], str) or not receipt_ids[0]
            or receipt_ids[0] != receipt_ids[1] or verified.get("verify_attempts") != 2):
        raise ValueError("Repeated verification must reuse one binding and receipt")
    confirmed = unlinked.get("confirmed_values_before")
    if (unlinked.get("unlinked_binding_ids") != case["owned_binding_ids"]
            or unlinked.get("remaining_binding_ids") != [case["preserved_binding_id"]]
            or not isinstance(confirmed, dict) or not confirmed or confirmed != unlinked.get("confirmed_values_after")
            or type(unlinked.get("cleanup_attempts")) is not int
            or not 1 <= unlinked["cleanup_attempts"] <= case["max_attempts"]):
        raise ValueError("Cleanup must remove only run-owned bindings and preserve confirmed values")
    provider = report.get("independent_provider")
    if (not isinstance(provider, dict) or not isinstance(provider.get("artifact_ref"), str)
            or not provider["artifact_ref"] or not isinstance(provider.get("before"), dict) or not provider["before"]
            or provider.get("after") != provider["before"] or not isinstance(provider.get("calls"), list)
            or not provider["calls"] or any(not isinstance(call, str) or not call.startswith("GET ")
                                              for call in provider["calls"])):
        raise ValueError("Independent provider must report unchanged state and GET-only access")
    cleanup = report.get("cleanup")
    if (not isinstance(cleanup, dict) or any(not isinstance(cleanup.get(ref), str) or not cleanup[ref]
                                            for ref in ("recovery_ref", "owned_readback_ref"))):
        raise ValueError("Recovery needs a durable cleanup receipt and owned readback")
    return record_source_cleanup(case, cleanup)["cleanup"]["status"] == "verified"


@pytest.mark.live_only
def live_source_recovery(tmp_path):
    """Validate protected deployed recovery reports; local cases are not live observations."""
    if os.environ.get("PRODUCT_SOURCE_RECOVERY_LIVE_ENABLED") != "1":
        pytest.skip("SCN-03 requires deployed recovery attempts and run-owned fixtures")
    run_id = os.environ.get("PRODUCT_SOURCES_RUN_ID")
    report_path = os.environ.get("PRODUCT_SOURCE_RECOVERY_REPORT_PATH")
    if not run_id or not report_path:
        raise ValueError("SCN-03 needs a protected run-owned recovery report")
    path = Path(report_path)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Recovery report must not be group or world readable")
    report = json.loads(path.read_text())
    source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requirements_revision = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", REQUIREMENTS], cwd=ROOT, text=True,
    ).strip()
    case = pinned_source_recovery_case(source_revision, requirements_revision, run_id, report.get("started_at"),
                                       report.get("owned_binding_ids", ()), report.get("preserved_binding_id"))
    output = tmp_path / "v06-source-recovery-case.json"
    output.write_text(json.dumps(case, sort_keys=True) + "\n")
    assert verify_live_source_recovery(case, report)

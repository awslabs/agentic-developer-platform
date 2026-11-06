"""Local V04 checks are not evidence of a deployed browser or installed CLI run."""

import json
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.product_onboarding.product_v04_evidence import (
    DENIAL_SELECTORS, HISTORY_SELECTORS, ISOLATION_SELECTORS,
)
from tests.e2e.new_ui.test_product_scopes import (
    assert_scope_denial_actors, assert_scope_fixture_inputs, assert_scope_profile,
)

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS = "docs/product-onboarding/delivery/verification-contract.md"


def pinned_scope_case(source_revision, requirements_revision, scenario_id, expected, selectors=None):
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
               for value in (source_revision, requirements_revision)):
        raise ValueError("Source and requirements revisions must be full commit SHAs")
    if selectors is not None and selectors != expected:
        raise ValueError("Scope scenario must select its reviewed API, browser and installed CLI cases")
    return {"scenario_id": scenario_id, "source_revision": source_revision,
            "requirements_revision": requirements_revision, "selectors": dict(expected),
            "status": "not_run"}


def pinned_scope_isolation(source_revision, requirements_revision, selectors=None):
    return pinned_scope_case(source_revision, requirements_revision, "SCN-01", ISOLATION_SELECTORS, selectors)


def pinned_scope_denials(source_revision, requirements_revision, selectors=None):
    return pinned_scope_case(source_revision, requirements_revision, "SCN-02", DENIAL_SELECTORS, selectors)


def pinned_scope_history(source_revision, requirements_revision, selectors=None):
    return pinned_scope_case(source_revision, requirements_revision, "SCN-03", HISTORY_SELECTORS, selectors)


def collect_pinned_scope_case(tmp_path, pin, case_id, driver):
    from tests.e2e.cli_uplift import cases, stages

    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requirements = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", REQUIREMENTS], cwd=ROOT, text=True,
    ).strip()
    record = pin(source, requirements)
    for selector in record["selectors"].values():
        if not selector.startswith("tests/"):
            assert selector == record["selectors"]["cli"]
            continue
        collection = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        assert collection.returncode == 0, collection.stdout + collection.stderr
        assert collection.stdout.splitlines().count(selector) == 1, collection.stdout
    selected = cases.suite_cases(record["selectors"]["cli"])
    assert selected[-1].id == case_id
    assert sum(case.id == case_id for case in selected) == 1
    assert stages.JOURNEY_DRIVERS[case_id] == driver
    output = tmp_path / (record["scenario_id"] + "-case.json")
    output.write_text(json.dumps(record, sort_keys=True) + "\n")
    assert json.loads(output.read_text()) == record
    return record


def collect_pinned_scope_isolation(tmp_path):
    return collect_pinned_scope_case(tmp_path, pinned_scope_isolation, "PR04-C01", "product_scope_isolation")


def collect_pinned_scope_denials(tmp_path):
    return collect_pinned_scope_case(tmp_path, pinned_scope_denials, "PR04-C02", "product_scope_denials")


def collect_pinned_scope_history(tmp_path):
    return collect_pinned_scope_case(tmp_path, pinned_scope_history, "PR04-C03", "product_scope_history")


def live_scope_isolation(tmp_path):
    """Pin exact SCN-01 selections; collection is not deployed execution."""
    assert collect_pinned_scope_isolation(tmp_path)["status"] == "not_run"


def live_scope_denials(tmp_path):
    """Pin exact SCN-02 selections; collection is not deployed execution."""
    assert collect_pinned_scope_denials(tmp_path)["status"] == "not_run"


def live_scope_history_cleanup(tmp_path):
    """Pin SCN-03 CLI and independent API readback; no mutation runs at collection."""
    assert collect_pinned_scope_history(tmp_path)["status"] == "not_run"


def test_isolation_scenario_pins_exact_surface_selection(tmp_path):
    record = collect_pinned_scope_isolation(tmp_path)
    assert record["status"] == "not_run"
    assert record["selectors"] == ISOLATION_SELECTORS
    for surface in ISOLATION_SELECTORS:
        wrong = dict(ISOLATION_SELECTORS, **{surface: "case:unknown"})
        with pytest.raises(ValueError, match="reviewed API, browser and installed CLI"):
            pinned_scope_isolation(record["source_revision"], record["requirements_revision"], wrong)
    with pytest.raises(ValueError, match="full commit SHAs"):
        pinned_scope_isolation(record["source_revision"][:-1], record["requirements_revision"])


def test_denial_scenario_pins_exact_selectors_and_actors(tmp_path):
    record = collect_pinned_scope_denials(tmp_path)
    assert record["status"] == "not_run" and record["selectors"] == DENIAL_SELECTORS
    for surface in DENIAL_SELECTORS:
        wrong = dict(DENIAL_SELECTORS, **{surface: ISOLATION_SELECTORS[surface]})
        with pytest.raises(ValueError, match="reviewed API, browser and installed CLI"):
            pinned_scope_denials(record["source_revision"], record["requirements_revision"], wrong)
    with pytest.raises(ValueError, match="full commit SHAs"):
        pinned_scope_denials(record["source_revision"], "stale")
    fixture = {"tenant_id": "owned-tenant", "actor_id": "owner"}
    owner = {"tenant_id": "owned-tenant", "actor_id": "owner", "access_token": "owner-token"}
    foreign = {"tenant_id": "foreign-tenant", "actor_id": "outsider", "access_token": "foreign-token"}
    assert_scope_denial_actors(fixture, owner, foreign)
    for changed in ({"tenant_id": fixture["tenant_id"]}, {"actor_id": fixture["actor_id"]},
                    {"access_token": owner["access_token"]}):
        with pytest.raises(AssertionError):
            assert_scope_denial_actors(fixture, owner, {**foreign, **changed})
    with pytest.raises(AssertionError):
        assert_scope_denial_actors(fixture, {**owner, "actor_id": "different"}, foreign)


def test_history_scenario_pins_exact_cli_and_api_readback(tmp_path):
    record = collect_pinned_scope_history(tmp_path)
    assert record["status"] == "not_run" and record["selectors"] == HISTORY_SELECTORS
    for surface in HISTORY_SELECTORS:
        wrong = dict(HISTORY_SELECTORS, **{surface: "case:PR04-C02"})
        with pytest.raises(ValueError, match="reviewed API, browser and installed CLI"):
            pinned_scope_history(record["source_revision"], record["requirements_revision"], wrong)
    with pytest.raises(ValueError, match="full commit SHAs"):
        pinned_scope_history("stale", record["requirements_revision"])
def scoped_fixture():
    fixture = {"product_id": "product-one", "tenant_id": "owned-tenant", "actor_id": "owner"}
    scopes = [
        {
            "project_id": project_id, "name": name, "progress": {"completed": completed},
            "configuration": {
                "basic": {"repositories": [{
                    "tenant_id": "owned-tenant", "project_id": project_id,
                    "source_connection_id": connection_id, "record_type": "repository",
                    "record_id": "repository-" + project_id, "record_revision": "source-etag:1",
                }]},
                "financials": {"release_target_date": plan_date},
            },
        }
        for project_id, name, connection_id, plan_date, completed in (
            ("first", "First scope", "connection-first", "2026-12-01", 0),
            ("second", "Second scope", "connection-second", "2027-12-01", 1),
        )
    ]
    profiles = [
        {
            "product_id": fixture["product_id"], "project_id": scope["project_id"],
            "tenant_id": fixture["tenant_id"], "project": {"id": scope["project_id"], "name": scope["name"]},
            "product": {"name": "Planner", "owner_ref": {"kind": "user", "id": "owner"}},
            "configuration": deepcopy(scope["configuration"]), "progress": deepcopy(scope["progress"]),
        }
        for scope in scopes
    ]
    return fixture, scopes, profiles


def test_distinct_scope_readbacks_share_product_identity():
    fixture, scopes, profiles = scoped_fixture()
    assert scopes[0]["project_id"] != scopes[1]["project_id"]
    assert scopes[0]["configuration"] != scopes[1]["configuration"]
    shared = [assert_scope_profile(profile, fixture, scope) for profile, scope in zip(profiles, scopes)]
    assert shared[0] == shared[1]
    assert shared[0]["name"] == "Planner"


@pytest.mark.parametrize("changed_field,new_value", [
    ("product_id", "foreign-product"), ("project_id", "first"),
    ("configuration", {}), ("progress", {"completed": 0}),
    ("product", {"name": "Foreign", "owner_ref": {"kind": "user", "id": "other"}}),
])
def test_wrong_scope_or_changed_identity_is_rejected(changed_field, new_value):
    fixture, scopes, profiles = scoped_fixture()
    profiles[1][changed_field] = new_value
    with pytest.raises(AssertionError):
        assert_scope_profile(profiles[1], fixture, scopes[1])


@pytest.mark.parametrize("field", ["connection", "plan", "progress"])
def test_other_scope_connection_plan_or_progress_never_matches_expected_scope(field):
    fixture, scopes, profiles = scoped_fixture()
    if field == "connection":
        profiles[1]["configuration"]["basic"]["repositories"] = deepcopy(
            profiles[0]["configuration"]["basic"]["repositories"]
        )
    elif field == "plan":
        profiles[1]["configuration"]["financials"] = deepcopy(profiles[0]["configuration"]["financials"])
    else:
        profiles[1]["progress"] = deepcopy(profiles[0]["progress"])
    with pytest.raises(AssertionError):
        assert_scope_profile(profiles[1], fixture, scopes[1])


@pytest.mark.parametrize("field", ["connection_project", "same_repository", "same_plan", "same_progress"])
def test_incomplete_or_crossed_scope_fixtures_cannot_start_browser_case(field):
    fixture, scopes, _ = scoped_fixture()
    fixture["scopes"] = scopes
    if field == "connection_project":
        scopes[1]["configuration"]["basic"]["repositories"][0]["project_id"] = scopes[0]["project_id"]
    elif field == "same_repository":
        scopes[1]["configuration"]["basic"]["repositories"][0].update(
            {key: scopes[0]["configuration"]["basic"]["repositories"][0][key]
             for key in ("source_connection_id", "record_id")}
        )
    elif field == "same_plan":
        scopes[1]["configuration"]["financials"]["release_target_date"] = (
            scopes[0]["configuration"]["financials"]["release_target_date"]
        )
    else:
        scopes[1]["progress"] = deepcopy(scopes[0]["progress"])
    with pytest.raises(AssertionError):
        assert_scope_fixture_inputs(fixture)


def test_distinct_repositories_on_one_connection_are_valid_browser_inputs():
    fixture, scopes, _ = scoped_fixture()
    fixture["scopes"] = scopes
    scopes[1]["configuration"]["basic"]["repositories"][0]["source_connection_id"] = (
        scopes[0]["configuration"]["basic"]["repositories"][0]["source_connection_id"]
    )
    assert_scope_fixture_inputs(fixture)

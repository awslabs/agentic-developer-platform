"""Exercise verified selector output through the real qualification emitter."""

import importlib.util
import json
from functools import partial
from pathlib import Path
from datetime import UTC, datetime

import pytest
import test_context_selector as context_fixtures
import test_installed_journeys as journey_fixtures
import test_portability_selector as portability_fixtures
import test_qualification_driver as driver_fixtures
import test_recovery_selector as recovery_fixtures

from selector import qualify_journeys
from test_selector import FakeReader as FakeReleaseReader, offline_policy

selector_case = journey_fixtures.selector_case
installed_journeys = journey_fixtures.installed_journeys
live_context = context_fixtures.live_context
live_acceptance = context_fixtures.live_acceptance
portable_selection = portability_fixtures.portable_selection
recovery_selection = recovery_fixtures.recovery_selection
driver_case = driver_fixtures.driver_case


def emitter():
    path = Path(__file__).resolve().parents[3] / "scripts/qualification_devops.py"
    specification = importlib.util.spec_from_file_location("devops_evidence_emitter", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_successful_installed_journeys_feed_the_real_emitter(installed_journeys):
    manifest, path, directory, _, _, _, _ = installed_journeys
    qualify_journeys(manifest["release"]["source_sha"], path, directory, policy_reader=offline_policy)
    module = emitter()
    blockers = []
    summary = module.selector_summary(directory, "journeys", manifest["release"],
                                     module.moment(manifest["release"]["deployed_at"]), blockers)
    assert blockers == []
    assert summary["status"] == "passed"
    assert summary["readback_count"] >= 4
    document = module.selector_document(directory, "journeys")
    assert {item["capability"] for item in document["native_results"]} == set(manifest["limits"])


@pytest.mark.parametrize("scenario,fixture_name", [
    ("context", "live_context"), ("acceptance", "live_acceptance"),
    ("portability", "portable_selection"), ("recovery", "recovery_selection")])
def test_other_real_selectors_preserve_the_fields_consumed_by_the_emitter(request, scenario, fixture_name):
    selected = request.getfixturevalue(fixture_name)
    manifest, path, directory, prior_reader = selected[:4]
    if scenario == "context":
        _, driver, reader = selected[4:]
        def target_factory(fixture, destination, name, **kwargs):
            return context_fixtures.ContextAdapter(fixture, destination, name, driver, reader, **kwargs)
        selector = context_fixtures.qualify_context
    elif scenario == "acceptance":
        evidence, reader = selected[4:]
        def target_factory(fixture, destination, name, **kwargs):
            return context_fixtures.AcceptanceAdapter(fixture, destination, name, evidence, reader, **kwargs)
        selector = context_fixtures.qualify_acceptance
    elif scenario == "portability":
        target_factory = portability_fixtures.PortableAdapter
        selector = portability_fixtures.qualify_portability
    else:
        target_factory = recovery_fixtures.FakeAdapter
        selector = recovery_fixtures.qualify_recovery
    selector(manifest["release"]["source_sha"], path, directory, target_factory=target_factory,
             release_reader_factory=lambda fixture, **kwargs: FakeReleaseReader(fixture, prior_reader, **kwargs),
             policy_reader=offline_policy, now=datetime(2026, 10, 28, tzinfo=UTC))
    module = emitter()
    blockers = []
    summary = module.selector_summary(directory, scenario, manifest["release"],
                                     module.moment(manifest["release"]["deployed_at"]), blockers)
    assert summary["status"] == "passed"
    if scenario == "context":
        assert len(module.context_reuse(directory, blockers)) == 5
        assert module.selector_document(directory, scenario)["native_results"][0]["native_id"]
    elif scenario == "portability":
        assert module.selected_scope(directory, blockers)["project_count"] == 2
        assert len(module.selector_document(directory, scenario)["native_results"]) == 2
    elif scenario == "recovery":
        assert module.two_real_users(directory, blockers)["denied_provider_calls"] == 6
    else:
        pairs, epic = module.accepted_contract()
        missing, reported, cleanup = module.covered_cases(directory, pairs, epic, blockers)
        assert missing == [] and len(reported) == 63 and cleanup["owned_absent"]
        assert len(module.live_checks(directory, manifest["release"], blockers)) == 2
    assert blockers == []
    assert all(item.stat().st_mode & 0o077 == 0 for item in directory.glob("*.json"))


@pytest.mark.parametrize("mutation", ["raw", "digest", "readbacks", "run", "release", "status", "missing",
                                      "source", "symlink", "public", "traversal", "observations"])
def test_emitter_rejects_changed_or_missing_verified_selector_evidence(installed_journeys, mutation):
    manifest, path, directory, _, _, _, _ = installed_journeys
    run_id = qualify_journeys(manifest["release"]["source_sha"], path, directory, policy_reader=offline_policy)
    raw = directory / f"devops-{run_id}-journeys.json"
    summary_path = directory / "journeys.json"
    summary = json.loads(summary_path.read_text())
    if mutation == "raw":
        raw.write_bytes(raw.read_bytes() + b"\n")
    elif mutation == "digest":
        summary["evidence_sha256"] = "0" * 64
    elif mutation == "readbacks":
        summary["readback_refs"] = []
    elif mutation == "run":
        summary["run_id"] = "another-qualification-run"
    elif mutation == "release":
        summary["artifacts"]["worker"] = "0" * 64
    elif mutation == "status":
        summary["status"] = "failed"
    elif mutation == "missing":
        raw.unlink()
    elif mutation == "source":
        document = json.loads(raw.read_text())
        document["selector"] = "context"
        raw.write_text(json.dumps(document))
    elif mutation == "symlink":
        target = directory / "other.json"
        raw.rename(target)
        raw.symlink_to(target)
    elif mutation == "public":
        raw.chmod(0o644)
    elif mutation == "traversal":
        summary["evidence_ref"] = "../" + raw.name
    else:
        document = json.loads(raw.read_text())
        document["observations"] = []
        raw.write_text(json.dumps(document))
    summary_path.write_text(json.dumps(summary))
    module = emitter()
    blockers = []
    assert module.selector_summary(directory, "journeys", manifest["release"],
                                   module.moment(manifest["release"]["deployed_at"]), blockers) is None
    assert blockers == ["journeys selector produced no run-bound summary"]
    assert module.selector_document(directory, "journeys") == {}


def test_real_driver_consumes_successful_selector_summary_without_manufacturing_other_evidence(
        driver_case, installed_journeys, monkeypatch):
    driver, manifest, path, directory = driver_case
    run_id = manifest["authorization"]["run_id"]

    def incomplete(*args):
        raise ValueError("external scenario observation unavailable")

    monkeypatch.setattr(driver, "SELECTORS", {
        "journeys": lambda source, selected, destination: qualify_journeys(
            source, selected, destination, policy_reader=offline_policy),
        **{name: incomplete for name in ("recovery", "context", "portability", "acceptance")}})
    assert driver.run_all(manifest["release"]["source_sha"], path, directory) == 1
    attempt = directory / run_id
    report = json.loads((attempt / "l5-devops-qualification/qualification.json").read_text())
    assert report["unresolved"]
    assert not any("journeys selector" in reason for reason in report["unresolved"])
    assert len(report["story_criteria"]) == len(report["scenarios"]) == 63
    receipt = json.loads((attempt / "driver.json").read_text())
    assert {failure["selector"] for failure in receipt["failures"]} == {"recovery", "context", "portability", "acceptance"}
    assert driver.qualification_devops.selector_document(attempt, "journeys")["status"] == "passed"


def test_real_five_selector_outputs_reach_native_evaluator_with_synthetic_external_readbacks(
        driver_case, installed_journeys, live_context, live_acceptance, monkeypatch):
    driver, manifest, path, directory = driver_case
    context_driver, context_reader = live_context[-2:]
    acceptance_evidence, acceptance_reader = live_acceptance[-2:]
    selectors = {
        "journeys": partial(qualify_journeys, policy_reader=offline_policy),
        "context": partial(context_fixtures.qualify_context, policy_reader=offline_policy,
            target_factory=partial(context_fixtures.ContextAdapter, driver=context_driver, reader=context_reader)),
        "acceptance": partial(context_fixtures.qualify_acceptance, policy_reader=offline_policy,
            target_factory=partial(context_fixtures.AcceptanceAdapter, evidence=acceptance_evidence, reader=acceptance_reader)),
        "portability": partial(portability_fixtures.qualify_portability, policy_reader=offline_policy,
            target_factory=portability_fixtures.PortableAdapter),
        "recovery": partial(recovery_fixtures.qualify_recovery, policy_reader=offline_policy,
            target_factory=recovery_fixtures.FakeAdapter)}
    monkeypatch.setattr(driver, "SELECTORS", {name: selectors[name] for name in driver.SELECTORS})
    fixture_path = Path(__file__).resolve().parents[2] / "deployment/test_devops_qualification_rollup.py"
    specification = importlib.util.spec_from_file_location("devops_native_fixtures", fixture_path)
    fixtures = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(fixtures)
    fixtures.DEPLOYMENT = manifest["release"]["deployment_id"]
    fixtures.SOURCE_SHA = manifest["release"]["source_sha"]
    fixtures.RUN_ID = manifest["authorization"]["run_id"]
    observations = path.parent / "synthetic-independent-observations.json"
    observations.write_text(json.dumps(fixtures.independent_observations(
        driver.qualification_devops, driver.qualification_devops.accepted_contract())))
    observations.chmod(0o600)
    assert driver.run_all(manifest["release"]["source_sha"], path, directory, observations=observations) == 0
    attempt = directory / fixtures.RUN_ID
    report = json.loads((attempt / "l5-devops-qualification/qualification.json").read_text())
    delivery = json.loads((attempt / "delivery-evidence.json").read_text())
    assert report["unresolved"] == delivery["unresolved"] == []
    assert len(report["epic_criteria"]) == 10
    assert len(report["story_criteria"]) == len(report["scenarios"]) == 63
    assert len(report["live_checks"]) == 2
    assert all(json.loads((attempt / f"{name}.json").read_text())["selector_evidence_version"] == 1 for name in selectors)

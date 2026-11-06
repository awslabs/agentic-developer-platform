"""Run authorized Product bootstrap or native readback through the shared release."""

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import subprocess

import httpx

import qualification_well_architected as driver
import qualification_well_architected_bootstrap as bootstrap
import qualification_well_architected_acceptance as acceptance
import qualification_well_architected_cleanup as cleanup
import qualification_well_architected_integrations as integrations
import qualification_installed_artifacts as artifacts
import qualification_well_architected_revocation as revocation
import qualification_well_architected_reuse_intervals as reuse_intervals
from qualification_well_architected_journal import Journal
from release_context import release_identity


WORKFLOW = ".github/workflows/l5-services-release.yml"
SCOPE = "selected_product_native_readback"
LIMITATIONS = ["provider_source_coverage_unverified", "full_live_acceptance_not_evaluated"]
FAILURE_ACTIONS = {
    "selection": "Verify the private fixture digest and selected workflow identity before retrying.",
    "integration": "Inspect private integration-output.txt and integration-results.xml; fix the failed original criterion at the reviewed source before native submission.",
    "installed-artifacts": "Restore the exact selected modular release and protected runtime reader. Retain original installed checkpoints; never create a retrospective baseline for completed native work.",
    "compatibility": "Read the retained before/after/recovery checkpoints through their original scoped readers. Restore changed authority or installed pins; never backfill historical records or label a pod replacement as verified rollback.",
    "prerequisites": "Restore the selected Product configuration, source authority and provider delegation; no setup was submitted.",
    "bootstrap": "Inspect the private bootstrap-journal and original Product work. Preserve its request and idempotency key; do not submit a new generation.",
    "cleanup": "Inspect the private cleanup-journal and exact owned binding operation. Reconcile that operation; retain native observations and never replace its idempotency key.",
    "observations": "Read the selected run, source retention, publication debt and client scope. Collect readbacks again after resolving drift; do not rerun native work.",
    "source-revocation": "Retain the original source selection and same-release native evidence, then select the actual successful Product unlink operation. Never repeat native work or recreate source access to manufacture evidence.",
    "reuse-interval": "Select original before/after checkpoint IDs after the authorized publication replay and completed task use. Preserve native/cleanup evidence and the frozen fixture; this read-only stage cannot execute or repeat those actions.",
    "retained-evidence": "Verify the private observation belongs to this exact release and is still fresh; recollect readbacks without repeating setup.",
    "acceptance": "Resolve the original criterion's missing installed evidence before issuing delivery evidence.",
    "output": "Use a private evidence directory and retain previous artifacts; do not overwrite accepted observations.",
}


def failure(stage, error):
    code = ("transport_response_unknown" if isinstance(error, httpx.TransportError)
            else "authority_denied" if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403, 404}
            else "observation_unavailable_or_changed")
    print(f"Well-Architected qualification failed at {stage} ({code}). {FAILURE_ACTIONS[stage]}", file=sys.stderr)
    return 1


def stable_cleanup(document):
    return {**document, "evaluations": revocation.stable_evaluations(document["evaluations"])}


def arguments(argv, *, emitter=False):
    parser = argparse.ArgumentParser(description=__doc__)
    if not emitter:
        parser.add_argument("action", choices=("preflight", "run", "observe-source-revocation", "observe-reuse-interval"))
        parser.add_argument("--source-revocation-operations", help="private original run to existing Product unlink operation map")
        parser.add_argument("--reuse-intervals", help="private original run to existing before/after checkpoint IDs")
    parser.add_argument("source_sha")
    parser.add_argument("fixture_path")
    parser.add_argument("evidence_dir")
    return parser.parse_args(argv)


def selected_release(options):
    selected, fixture = driver.load_selection(options.fixture_path, options.fixture_path, options.source_sha)
    identity = release_identity(os.environ)
    if identity["service"] != "well_architected" or identity["source_revision"] != options.source_sha:
        raise ValueError("qualification differs from selected workflow")
    fixture_digest = sha256(Path(options.fixture_path).read_bytes()).hexdigest()
    if os.environ.get("L5_FIXTURE_DIGEST") != fixture_digest:
        raise ValueError("qualification fixture differs from materialized selection")
    directory = Path(options.evidence_dir)
    driver.private_output(directory / "qualification.json")
    context = {key: identity[key] for key in (
        "service", "environment", "repository_id", "run_id", "run_attempt",
        "source_revision", "definition_revision", "correlation", "contract_digest")}
    context.update(workflow_path=WORKFLOW, fixture_sha256=fixture_digest)
    return selected, fixture, directory, context


def main(argv=None):
    options = arguments(argv)
    stage = "selection"
    try:
        selected, fixture, directory, context = selected_release(options)
        if options.action == "observe-reuse-interval":
            stage = "reuse-interval"
            reuse_interval_observations(options, selected, directory, context)
            print("Well-Architected original replay/use interval observed; missing fault history and acceptance remain incomplete")
            return 0
        if options.reuse_intervals is not None:
            raise ValueError("reuse interval selection requires the read-only reuse observation action")
        if options.action == "observe-source-revocation":
            stage = "source-revocation"
            source_revocation_observations(options, selected, directory, context)
            print("Well-Architected source unlink and denied reads observed; remaining acceptance evidence is still required")
            return 0
        if options.source_revocation_operations is not None:
            raise ValueError("source unlink operation selection requires the read-only source revocation action")
        series = (artifacts.SnapshotSeries(directory, selected["qualification_installed_artifacts"], fixture, context, driver)
                  if "qualification_installed_artifacts" in selected else None)
        wants_cleanup = any("qualification_cleanup" in case for case in selected["cases"])
        for case in selected["cases"]:
            cleanup.validate(case)
        destination = directory / "native-observations.json"
        resume_cleanup = wants_cleanup and options.action == "run" and destination.exists()
        if not resume_cleanup:
            driver.private_output(destination)
        driver.private_output(directory / "delivery-evidence.json")
        if options.action == "run" and selected.get("qualification_source_checks") is True:
            stage = "integration"
            integrations.collect(directory, context, driver)
        if resume_cleanup:
            observation = read_observation(destination, context, historical=True)
            resolved = (bootstrap.validate_resolved(selected["cases"], observation.get("resolved_selection"))
                        if any("bootstrap" in case for case in selected["cases"]) else selected["cases"])
            selection = {**selected, "cases": resolved}
            cases, evaluations = observation["cases"], observation["evaluations"]
        elif any("bootstrap" in case for case in selected["cases"]):
            if options.action == "preflight":
                stage = "prerequisites"
                bootstrap.preflight(selected["cases"], driver.product_connection)
                print("Well-Architected Product prerequisites checked; setup and generation wait for resumed consumers")
                return 0
            stage = "bootstrap"
            extra = {}
            if series is not None:
                stage = "installed-artifacts"
                series.checkpoint("before-native-cases")
                def after_case(index, case):
                    nonlocal stage
                    stage = "installed-artifacts"
                    series.checkpoint("after-native-case-" + str(index), completed_case=artifacts.case_identity(case))
                    stage = "bootstrap"
                extra["after_case"] = after_case
            stage = "bootstrap"
            resolved = bootstrap.run(selected["cases"], driver.product_connection,
                                     journal=Journal(directory / "bootstrap-journal", context), **extra)
            selection = {**selected, "cases": resolved}
        else:
            resolved = None
            selection = selected
        if not resume_cleanup:
            stage = "observations"
            cases, evaluations = driver.observe_release(selection, fixture, read_only=options.action == "preflight",
                                                        preflight=options.action == "preflight")
        if options.action == "run" and not resume_cleanup:
            if series is not None:
                stage = "installed-artifacts"
                series.verify(cases)
            stage = "output"
            driver.write_private(destination, {
                "schema_version": 1, "context": context, "observed_at": datetime.now(UTC).isoformat(),
                "qualification_scope": SCOPE, "provider_coverage": "provider_coverage_unverified",
                "limitations": LIMITATIONS, "cases": cases,
                "resolved_selection": resolved,
                "evaluations": evaluations,
            })
        if options.action == "run" and wants_cleanup:
            stage = "cleanup"
            cleanup.execute(selection["cases"], directory, context, driver.product_connection)
            fresh = cleanup.observe(selection["cases"], cases, evaluations, driver)
            cleanup_path = directory / "cleanup-observations.json"
            cleanup_document = {"schema_version": 1, "context": context,
                "native_observation_sha256": sha256(destination.read_bytes()).hexdigest(), "evaluations": fresh}
            if cleanup_path.exists():
                if stable_cleanup(driver.private_json(cleanup_path)) != stable_cleanup(cleanup_document):
                    raise ValueError("cleanup observations changed since the retained readback")
            else:
                driver.write_private(cleanup_path, cleanup_document)
        print("Well-Architected qualification " + options.action + " observations collected; acceptance is evaluated separately")
        return 0
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, subprocess.SubprocessError) as error:
        return failure(stage, error)


def resolved_cases(selected, observation):
    return (bootstrap.validate_resolved(selected["cases"], observation.get("resolved_selection"))
            if any("bootstrap" in case for case in selected["cases"]) else selected["cases"])


def staged_prior(selected_cases, directory, context, baseline, baseline_path, *, include_reuse=True):
    prior, cleanup_digest = baseline["evaluations"], None
    if any("qualification_cleanup" in case for case in selected_cases):
        path = directory / "cleanup-observations.json"
        retained = driver.private_json(path)
        if (retained.get("schema_version") != 1 or retained.get("context") != context
                or retained.get("native_observation_sha256") != sha256(baseline_path.read_bytes()).hexdigest()):
            raise ValueError("the other case's cleanup observations differ from this release")
        prior, cleanup_digest = retained["evaluations"], sha256(path.read_bytes()).hexdigest()
    path = directory / "reuse-interval-observations.json"
    if include_reuse and path.exists():
        retained = driver.private_json(path)
        if (retained.get("schema_version") != 1 or retained.get("context") != context
                or retained.get("native_observation_sha256") != sha256(baseline_path.read_bytes()).hexdigest()
                or retained.get("cleanup_observation_sha256") != cleanup_digest):
            raise ValueError("reuse observations differ from the original native/cleanup evidence")
        prior = retained["evaluations"]
    return prior, cleanup_digest


def reuse_digest(directory):
    path = directory / "reuse-interval-observations.json"
    return sha256(path.read_bytes()).hexdigest() if path.exists() else None


def reuse_interval_observations(options, selected, directory, context):
    if options.reuse_intervals is None or options.source_revocation_operations is not None:
        raise ValueError("select only a private map of original runs to existing replay/use checkpoint IDs")
    destination = driver.private_output(directory / "reuse-interval-observations.json")
    baseline_path = directory / "native-observations.json"
    baseline = read_observation(baseline_path, context, historical=True)
    cases = resolved_cases(selected, baseline)
    intervals = reuse_intervals.selected_intervals(driver.private_json(options.reuse_intervals), cases)
    prior, cleanup_digest = staged_prior(cases, directory, context, baseline, baseline_path, include_reuse=False)
    evaluations = reuse_intervals.observe(cases, baseline["cases"], prior, intervals, driver)
    driver.write_private(destination, {"schema_version": 1, "context": context,
        "native_observation_sha256": sha256(baseline_path.read_bytes()).hexdigest(),
        "cleanup_observation_sha256": cleanup_digest, "intervals": intervals,
        "evaluations": evaluations, "observed_at": datetime.now(UTC).isoformat()})


def reuse_interval_readback(selected, directory, context, baseline, baseline_path):
    retained = driver.private_json(directory / "reuse-interval-observations.json")
    expected, _ = staged_prior(selected["cases"], directory, context, baseline, baseline_path)
    prior, _ = staged_prior(selected["cases"], directory, context, baseline, baseline_path, include_reuse=False)
    evaluations = reuse_intervals.observe(selected["cases"], baseline["cases"], prior, retained["intervals"], driver)
    if revocation.stable_evaluations(evaluations) != revocation.stable_evaluations(expected):
        raise ValueError("the original replay/use interval or task evidence changed")
    return baseline["cases"], evaluations


def source_revocation_observations(options, selected, directory, context):
    if options.source_revocation_operations is None:
        raise ValueError("select a private map of original runs to already completed Product unlink operations")
    destination = driver.private_output(directory / "source-revocation-observations.json")
    baseline_path = directory / "native-observations.json"
    baseline = read_observation(baseline_path, context, historical=True)
    cases = resolved_cases(selected, baseline)
    operations = revocation.selected_operations(driver.private_json(options.source_revocation_operations), cases)
    prior, cleanup_digest = staged_prior(cases, directory, context, baseline, baseline_path)
    evaluations = revocation.observe(cases, baseline["cases"], prior, operations,
                                     datetime.fromisoformat(baseline["observed_at"].replace("Z", "+00:00")), driver)
    driver.write_private(destination, {"schema_version": 1, "context": context,
        "native_observation_sha256": sha256(baseline_path.read_bytes()).hexdigest(),
        "cleanup_observation_sha256": cleanup_digest,
        "reuse_observation_sha256": reuse_digest(directory),
        "operations": operations, "evaluations": evaluations, "observed_at": datetime.now(UTC).isoformat()})


def source_revocation_readback(selected, directory, context, baseline, baseline_path):
    retained = driver.private_json(directory / "source-revocation-observations.json")
    if (retained.get("schema_version") != 1 or retained.get("context") != context
            or retained.get("native_observation_sha256") != sha256(baseline_path.read_bytes()).hexdigest()):
        raise ValueError("staged source observations differ from this release's retained native evidence")
    prior, cleanup_digest = staged_prior(selected["cases"], directory, context, baseline, baseline_path)
    if retained.get("cleanup_observation_sha256") != cleanup_digest:
        raise ValueError("the other case's retained cleanup evidence changed")
    if retained.get("reuse_observation_sha256") != reuse_digest(directory):
        raise ValueError("the original replay/use evidence changed after source revocation")
    evaluations = revocation.observe(selected["cases"], baseline["cases"], prior, retained["operations"],
                                     datetime.fromisoformat(baseline["observed_at"].replace("Z", "+00:00")), driver)
    if revocation.stable_evaluations(evaluations) != revocation.stable_evaluations(retained["evaluations"]):
        raise ValueError("the retained source transition or denied-content facts changed")
    return baseline["cases"], evaluations


def read_observation(path, context, *, historical=False):
    observation = driver.private_json(path)
    now = datetime.now(UTC)
    observed_at = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
    # Owned Profiles are intentionally absent after cleanup. Their historical
    # native snapshot is bound to this release; current source/result/absence
    # authority is still read again during emission.
    age = timedelta(hours=24) if historical else timedelta(minutes=15)
    if (observation.get("schema_version") != 1 or observation.get("context") != context
            or observation.get("qualification_scope") != SCOPE
            or observation.get("provider_coverage") != "provider_coverage_unverified"
            or observation.get("limitations") != LIMITATIONS or observed_at.tzinfo is None
            or not now - age <= observed_at <= now + timedelta(minutes=1)):
        raise ValueError("native observations are stale or belong to another release")
    return observation


def emit(argv=None):
    options = arguments(argv, emitter=True)
    stage = "selection"
    try:
        selected, fixture, directory, context = selected_release(options)
        qualification_path = driver.private_output(directory / "qualification.json")
        driver.private_output(directory / "delivery-evidence.json")
        observation_path = directory / "native-observations.json"
        stage = "retained-evidence"
        wants_cleanup = any("qualification_cleanup" in case for case in selected["cases"])
        wants_revocation = (directory / "source-revocation-observations.json").exists()
        wants_reuse = (directory / "reuse-interval-observations.json").exists()
        observation = read_observation(observation_path, context, historical=wants_cleanup or wants_revocation or wants_reuse)
        if any("bootstrap" in case for case in selected["cases"]):
            resolved = bootstrap.validate_resolved(selected["cases"], observation.get("resolved_selection"))
            selected = {**selected, "cases": resolved}
        stage = "observations"
        if wants_revocation:
            stage = "source-revocation"
            current_cases, evaluations = source_revocation_readback(selected, directory, context, observation, observation_path)
        elif wants_reuse:
            stage = "reuse-interval"
            current_cases, evaluations = reuse_interval_readback(selected, directory, context, observation, observation_path)
        elif wants_cleanup:
            current_cases = observation["cases"]
            evaluations = cleanup.observe(selected["cases"], current_cases, observation["evaluations"], driver)
            retained = driver.private_json(directory / "cleanup-observations.json")
            if stable_cleanup(retained) != stable_cleanup({"schema_version": 1, "context": context,
                    "native_observation_sha256": sha256(observation_path.read_bytes()).hexdigest(), "evaluations": evaluations}):
                raise ValueError("cleanup readback changed or belongs to another release")
        else:
            current_cases, evaluations = driver.observe_release(selected, fixture, read_only=True)
            if observation.get("cases") != json.loads(json.dumps(current_cases, default=str)):
                raise ValueError("native observations differ from current Product and provider readbacks")
            if revocation.stable_evaluations(observation.get("evaluations")) != revocation.stable_evaluations(json.loads(json.dumps(evaluations, default=str))):
                raise ValueError("runtime/client observations changed; collect them again without repeating native work")
        stage = "acceptance"
        integration = integrations.read(directory, context, driver) if selected.get("qualification_source_checks") is True else None
        installed = None
        if "qualification_installed_artifacts" in selected:
            stage = "installed-artifacts"
            installed = artifacts.SnapshotSeries(directory, selected["qualification_installed_artifacts"],
                                                  fixture, context, driver).verify(current_cases)
        compatibility = None
        if (directory / "compatibility-checkpoints").exists():
            stage = "compatibility"
            from qualification_well_architected_compatibility import read_progress
            compatibility = read_progress(directory, context, installed)
        stage = "acceptance"
        report = acceptance.evaluate(current_cases, evaluations, context, integration, installed, compatibility)
        report["native_observation_sha256"] = sha256(observation_path.read_bytes()).hexdigest()
        report["cases"] = current_cases
        stage = "output"
        driver.write_private(qualification_path, report)
        # The shared publisher alone creates delivery/v2 after every original
        # criterion passes and the installed source is independently verified.
        print("Well-Architected original acceptance catalog evaluated privately; incomplete cases block delivery")
        return 0
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as error:
        return failure(stage, error)

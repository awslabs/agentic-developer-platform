"""V10 installed-CLI selectors require deployed clients and run-owned fixtures."""

import os
import re
from urllib.parse import quote

import pytest

from tests.e2e.cli_uplift.product_guidance import REQUIRED_CHECKS
from tests.e2e.cli_uplift.product_onboarding_full import (
    REGRESSION_CASES, run_full_journey, verify_peer_unavailable, verify_release_evidence,
)
from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.new_ui.test_product_resume import installed_cli, read_profile, read_session


def run_cli_peer_unavailable(run, source_revision, requirements_revision):
    fixture = run.get("fixture")
    checksums = run.get("checksums")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
                       for value in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("deployment_source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or not isinstance(checksums, dict) or checksums != run.get("expected_cli_hashes")
            or any(not re.fullmatch(r"[a-f0-9]{64}", checksums.get(name, ""))
                   for name in ("adp", "adp-onboard.py", "adp-product.py"))
            or not all(run.get(name) for name in ("provider", "base_url", "binary", "record", "page"))
            or not callable(run.get("create_ui_session"))
            or not callable(run.get("observe_effects")) or not callable(run.get("cleanup"))
            or not fixture.get("frontend_release")
            or not all(fixture.get(name) for name in ("run_id", "tenant_id", "product_id", "project_id"))
            or not all(isinstance(fixture.get(actor), dict) and fixture[actor].get("access_token")
                       for actor in ("owner", "observer", "foreign", "revoked"))
            or not all(fixture["owner"].get(name) for name in ("id_token", "expires_in", "tenant_id"))
            or fixture["owner"]["tenant_id"] != fixture["tenant_id"]
            or fixture.get("evidence_context", {}).get("deployment", {}).get("observed", {}).get("product_onboarding_enabled") is not True
            or len({fixture[actor]["access_token"] for actor in ("owner", "observer", "foreign", "revoked")}) != 4
            or not isinstance(getattr(run.get("record"), "cli_events", None), list)):
        raise ValueError("V10 peer selector needs pinned installed clients, distinct actors and owned cleanup")

    provider = run["provider"]
    base_url = run["base_url"]
    root = (base_url + "/api/v1/products/" + quote(fixture["product_id"], safe="")
            + "/projects/" + quote(fixture["project_id"], safe=""))
    observer = {"Authorization": "Bearer " + fixture["observer"]["access_token"]}

    def independent_read(path):
        response = provider.request.get(path, headers=observer, timeout=30_000)
        assert response.status == 200, "Independent Profile history readback is unavailable"
        return response.json()

    def changes():
        document = independent_read(root + "/changes?limit=100")
        assert isinstance(document.get("items"), list) and not document.get("next_cursor")
        return document["items"]

    def attempt(peer, actor, product_id):
        path = (base_url + "/api/v1/products/" + quote(product_id, safe="")
                + "/projects/" + quote(fixture["project_id"], safe="")
                + "/peer-requests/" + peer)
        response = provider.request.post(path, headers={
            "Authorization": "Bearer " + fixture[actor]["access_token"],
            "If-Profile-Revision": str(profile_before["revision"]),
            "Idempotency-Key": "deferred_" + peer + "_" + fixture["run_id"],
        }, timeout=30_000)
        return {"status": response.status, "body": response.json()}

    try:
        check_served_assets(provider.request, base_url, fixture["frontend_release"], source_revision)
        ui_session = run["create_ui_session"]()
        assert isinstance(ui_session, dict) and ui_session.get("id")
        profile_before = read_profile(provider, base_url, fixture)
        session_before = read_session(provider, base_url, fixture, ui_session["id"])
        lineage_before = changes()
        assert lineage_before
        original_revision = lineage_before[0]["revision"]
        historical_before = independent_read(root + "/revisions/" + str(original_revision))
        effects_before = run["observe_effects"]()

        initial_events = len(run["record"].cli_events)
        scope = ["--product", fixture["product_id"], "--project", fixture["project_id"]]
        cli_profile = installed_cli(run["binary"], checksums, fixture["owner"], base_url,
                                    ["show", *scope], record=run["record"], command="product")
        refusals = {peer: [attempt(peer, "owner", fixture["product_id"])
                           for _ in range(2 if peer == "context" else 1)]
                    for peer in ("context", "l5")}
        denials = {actor: {peer: attempt(peer, actor, fixture["product_id"])
                           for peer in ("context", "l5")}
                   for actor in ("foreign", "revoked")}
        unknowns = {actor: {peer: attempt(peer, actor, "no-such-product")
                            for peer in ("context", "l5")}
                    for actor in ("foreign", "revoked")}
        cli_resumed = installed_cli(run["binary"], checksums, fixture["owner"], base_url,
                                    ["resume", *scope, "--session", ui_session["id"]], record=run["record"])
        events = run["record"].cli_events[initial_events:]
        assert len(events) == 2 and [event["command"][1] for event in events] == ["product", "onboard"]
        assert all(event["exit_code"] == 0 and event["installed_cli_sha256"] == checksums["adp"]
                   for event in events)
        result = verify_peer_unavailable(
            fixture, profile_before=profile_before, cli_profile=cli_profile,
            profile_after=read_profile(provider, base_url, fixture),
            historical_before=historical_before,
            historical_after=independent_read(root + "/revisions/" + str(original_revision)),
            lineage_before=lineage_before, lineage_after=changes(),
            ui_session=ui_session, session_before=session_before, cli_resumed=cli_resumed,
            session_after=read_session(provider, base_url, fixture, ui_session["id"]),
            refusals=refusals, denials=denials, unknowns=unknowns,
            effects_before=effects_before, effects_after=run["observe_effects"]())
    finally:
        cleanup = run["cleanup"]()
        if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                or cleanup.get("source_revision") != source_revision
                or cleanup.get("status") != "verified" or cleanup.get("owned_absent") is not True
                or cleanup.get("shared_unchanged") is not True or not cleanup.get("receipt_ref")):
            raise AssertionError("V10 peer fixture cleanup was not independently verified")
    return {**result, "cleanup": cleanup}


@pytest.mark.live_only
def live_cli_peer_unavailable(request):
    if os.environ.get("PRODUCT_V10_LIVE_ENABLED") != "1":
        pytest.skip("Requires authorized deployed V10 peers and independent effect observation")
    run = request.getfixturevalue("product_v10_peer_fixture")
    assert run_cli_peer_unavailable(run, os.environ["PRODUCT_V10_SOURCE_REVISION"],
                                    os.environ["PRODUCT_V10_REQUIREMENTS_REVISION"])


def run_cli_release_evidence(run, source_revision, requirements_revision):
    fixture = run.get("fixture")
    artifacts = run.get("artifacts")
    if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
            or not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
                       for value in (source_revision, requirements_revision))
            or fixture.get("source_revision") != source_revision
            or run.get("deployment_source_revision") != source_revision
            or run.get("requirements_revision") != requirements_revision
            or not fixture.get("run_id")
            or not isinstance(artifacts, dict)
            or artifacts.get("cli_hashes") != run.get("expected_cli_hashes")
            or run.get("selected_regressions") != list(REGRESSION_CASES)
            or any(not callable(run.get(name)) for name in (
                "observe_release", "load_ac_attempts", "run_regression", "disable_admissions",
                "rollback_smoke", "cleanup", "capture_ac03_attempt", "record_failure"))):
        raise ValueError("V10 release selector needs pinned artifacts and selected recovery cases")
    try:
        try:
            observed = run["observe_release"]()
            prior_attempts = run["load_ac_attempts"]()
            if not isinstance(prior_attempts, dict) or set(prior_attempts) != {"AC-01", "AC-02"}:
                raise ValueError("V10 release needs separate prior live AC attempts")
            regressions = {}
            for case in REGRESSION_CASES:
                result = run["run_regression"](case)
                if (not isinstance(result, dict) or result.get("case_id") != case
                        or result.get("run_id") != fixture["run_id"]
                        or result.get("result") != "pass"):
                    raise AssertionError("V10 access/replay regression did not pass: " + case)
                regressions[case] = {key: value for key, value in result.items() if key != "case_id"}
            disabled = run["disable_admissions"]()
            if (not isinstance(disabled, dict) or disabled.get("status") != "disabled"
                    or not disabled.get("receipt_ref")):
                raise AssertionError("V10 admissions must be disabled before rollback")
            rollback = run["rollback_smoke"](disabled)
        finally:
            cleanup = run["cleanup"]()
            if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                    or cleanup.get("source_revision") != source_revision
                    or cleanup.get("result") != "pass"
                    or cleanup.get("owned_absent") is not True
                    or cleanup.get("shared_unchanged") is not True
                    or not cleanup.get("receipt_ref")):
                raise AssertionError("V10 owned fixture cleanup was not independently verified")

        ac03_attempt = run["capture_ac03_attempt"](regressions, rollback, cleanup)
        document = {"run_id": fixture["run_id"], "source_revision": source_revision,
                    "requirements_revision": requirements_revision,
                    "artifacts": artifacts, "observed_release": observed,
                    "attempts": {**prior_attempts, "AC-03": ac03_attempt},
                    "regressions": regressions, "rollback": rollback, "cleanup": cleanup}
        result = verify_release_evidence(document)
    except Exception as error:
        run["record_failure"]("SCN-03", type(error).__name__)
        raise
    return {**result, "regression_cases": list(regressions), "cleanup": cleanup}


@pytest.mark.live_only
def live_cli_release_evidence(request):
    if os.environ.get("PRODUCT_V10_LIVE_ENABLED") != "1":
        pytest.skip("Requires authorized deployed V10 release and run-owned recovery fixtures")
    run = request.getfixturevalue("product_v10_release_fixture")
    assert run_cli_release_evidence(run, os.environ["PRODUCT_V10_SOURCE_REVISION"],
                                    os.environ["PRODUCT_V10_REQUIREMENTS_REVISION"])


def run_cli_full_journeys(run, source_revision, requirements_revision):
    journeys = run.get("journeys")
    if (not isinstance(journeys, dict) or set(journeys) != set(REQUIRED_CHECKS)
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value)
                   for value in (source_revision, requirements_revision))):
        raise ValueError("V10 needs every starting point and pinned release revisions")
    run_ids = []
    for starting_point in REQUIRED_CHECKS:
        journey = journeys[starting_point]
        if not isinstance(journey, dict):
            raise ValueError("V10 needs independently owned journey fixtures")
        fixture = journey.get("fixture")
        if (not isinstance(fixture, dict) or fixture.get("owned_fixture") is not True
                or not isinstance(fixture.get("run_id"), str) or not fixture["run_id"]
                or fixture.get("source_revision") != source_revision
                or journey.get("deployment_source_revision") != source_revision
                or journey.get("requirements_revision") != requirements_revision
                or not isinstance(journey.get("checksums"), dict)
                or any(not re.fullmatch(r"[a-f0-9]{64}", journey["checksums"].get(name, ""))
                       for name in ("adp", "adp-onboard.py"))
                or journey["checksums"] != run.get("expected_cli_hashes")
                or not all(journey.get(name) for name in ("provider", "base_url", "binary", "record"))
                or not callable(journey.get("create_ui_session"))
                or not callable(journey.get("observe_provider"))
                or not fixture.get("frontend_release")
                or not callable(journey.get("cleanup"))):
            raise ValueError("V10 needs owned fixtures, pinned clients and durable cleanup")
        run_ids.append(fixture["run_id"])
    if len(set(run_ids)) != len(REQUIRED_CHECKS):
        raise ValueError("V10 starting points must use separate owned fixtures")

    results = {}
    try:
        for starting_point in REQUIRED_CHECKS:
            journey = journeys[starting_point]
            fixture = journey["fixture"]
            check_served_assets(journey["provider"].request, journey["base_url"],
                                fixture["frontend_release"], source_revision)
            results[starting_point] = run_full_journey(journey, starting_point,
                                                       source_revision, requirements_revision)
    finally:
        cleanup_errors = []
        for starting_point in REQUIRED_CHECKS:
            journey = journeys[starting_point]
            fixture = journey["fixture"]
            try:
                cleanup = journey["cleanup"]()
            except Exception:
                cleanup_errors.append(starting_point)
                continue
            if (not isinstance(cleanup, dict) or cleanup.get("run_id") != fixture["run_id"]
                    or cleanup.get("source_revision") != source_revision
                    or cleanup.get("status") != "verified"
                    or cleanup.get("owned_absent") is not True
                    or cleanup.get("shared_unchanged") is not True
                    or not cleanup.get("receipt_ref")):
                cleanup_errors.append(starting_point)
            elif starting_point in results:
                results[starting_point]["cleanup"] = cleanup
        if cleanup_errors:
            raise AssertionError("V10 journey cleanup was not independently verified: "
                                 + ", ".join(cleanup_errors))
    return results


@pytest.mark.live_only
def live_cli_full_journeys(request):
    if os.environ.get("PRODUCT_V10_LIVE_ENABLED") != "1":
        pytest.skip("Requires authorized deployed V10 UI, CLI and independent observations")
    run = request.getfixturevalue("product_v10_journeys_fixture")
    assert run_cli_full_journeys(run, os.environ["PRODUCT_V10_SOURCE_REVISION"],
                                 os.environ["PRODUCT_V10_REQUIREMENTS_REVISION"])

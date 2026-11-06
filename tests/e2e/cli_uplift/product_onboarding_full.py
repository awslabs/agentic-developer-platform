"""Installed onboarding journeys checked against independently observed state."""

import re

from tests.e2e.cli_uplift.product_guidance import (
    REQUIRED_CHECKS,
    guide_with_installed_cli,
    verify_direct_save,
    verify_guided_journey,
)
from tests.e2e.new_ui.test_product_guidance import save_direct_with_installed_cli
from tests.e2e.new_ui.test_product_resume import (
    installed_cli,
    read_profile,
    read_session,
)
from tests.e2e.product_onboarding.product_v06_evidence import (
    DIGEST,
    REFERENCE,
    REVISION,
    SUMMARY,
)

RELEASE_CASES = {
    "AC-01": ("SCN-01", "live_cli_full_journeys"),
    "AC-02": ("SCN-02", "live_cli_peer_unavailable"),
    "AC-03": ("SCN-03", "live_cli_release_evidence"),
}
REGRESSION_CASES = (
    "foreign_access",
    "revoked_access",
    "stale_revision",
    "replayed_request",
)


def run_full_journey(run, starting_point, source_revision, requirements_revision):
    """Resume a UI-created session with the installed CLI, then save directly."""
    fixture = run.get("fixture")
    if (
        starting_point not in REQUIRED_CHECKS
        or not isinstance(fixture, dict)
        or fixture.get("owned_fixture") is not True
        or any(
            not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision)
            for revision in (source_revision, requirements_revision)
        )
        or fixture.get("source_revision") != source_revision
        or run.get("deployment_source_revision") != source_revision
        or run.get("requirements_revision") != requirements_revision
        or fixture.get("evidence_context", {})
        .get("deployment", {})
        .get("observed", {})
        .get("product_onboarding_enabled")
        is not True
        or not all(
            run.get(name)
            for name in ("provider", "base_url", "binary", "checksums", "record")
        )
        or not callable(run.get("create_ui_session"))
        or not callable(run.get("observe_provider"))
    ):
        raise ValueError(
            "Full journey needs pinned installed clients and an owned UI session"
        )

    provider = run["provider"]
    base_url = run["base_url"]
    record = run["record"]
    profile = read_profile(provider, base_url, fixture)
    assert profile["product"]["starting_point"] == starting_point
    provider_before = run["observe_provider"]()
    ui_session, browser_message = run["create_ui_session"]()
    assert ui_session["id"] and ui_session["revision"]
    session = read_session(provider, base_url, fixture, ui_session["id"])
    assert session == ui_session
    scope = ["--product", fixture["product_id"], "--project", fixture["project_id"]]
    resumed = installed_cli(
        run["binary"],
        run["checksums"],
        fixture["owner"],
        base_url,
        ["resume", *scope, "--session", session["id"]],
        record=record,
    )
    assert (
        resumed == session == read_session(provider, base_url, fixture, session["id"])
    )
    guidance = guide_with_installed_cli(
        run["binary"], run["checksums"], fixture, base_url, session, record
    )
    independent_session = read_session(provider, base_url, fixture, session["id"])
    verify_guided_journey(
        starting_point, profile, session, browser_message, guidance, independent_session
    )
    saved = save_direct_with_installed_cli(
        run["binary"], run["checksums"], fixture, base_url, session, record
    )
    after_profile = read_profile(provider, base_url, fixture)
    after_session = read_session(provider, base_url, fixture, session["id"])
    verify_direct_save(profile, session, saved, after_profile, after_session)
    assert all(
        after_session[key] == session[key]
        for key in ("id", "tenant_id", "product_id", "project_id")
    )
    assert after_profile["product"]["name"] == profile["product"]["name"]
    assert after_profile.get("sources") == profile.get("sources")
    assert after_profile.get("configuration") == profile.get("configuration")
    assert after_profile.get("provenance") == profile.get("provenance")
    assert run["observe_provider"]() == provider_before, (
        "Unselected provider effects changed"
    )
    return {
        "starting_point": starting_point,
        "session_id": session["id"],
        "revision": after_session["revision"],
        "source_revision": source_revision,
        "requirements_revision": requirements_revision,
    }


def verify_peer_unavailable(
    fixture,
    *,
    profile_before,
    cli_profile,
    profile_after,
    historical_before,
    historical_after,
    lineage_before,
    lineage_after,
    ui_session,
    session_before,
    cli_resumed,
    session_after,
    refusals,
    denials,
    unknowns,
    effects_before,
    effects_after,
):
    """Require independent readback of inert peer requests and preserved scope."""
    scope = ("tenant_id", "product_id", "project_id")
    if not all(fixture.get(key) for key in scope) or not all(
        isinstance(value, dict)
        for value in (
            profile_before,
            cli_profile,
            profile_after,
            historical_before,
            historical_after,
            ui_session,
            session_before,
            cli_resumed,
            session_after,
        )
    ):
        raise ValueError(
            "Deferred peer proof needs an owned Profile and independent reads"
        )
    assert profile_before == cli_profile == profile_after, (
        "CLI and API Profile readbacks differ"
    )
    assert historical_before == historical_after, "Original Profile history changed"
    assert profile_before["context_status"] == "unavailable"
    availability = profile_before["peer_availability"]
    assert set(availability) == {"context", "l5"}
    assert all(
        value["status"] == "unavailable"
        and value["reason"] == "integration_deferred"
        and "saved Product remains available" in value["message"]
        for value in availability.values()
    )
    assert all(
        profile_before[key] == historical_before[key] == fixture[key] for key in scope
    )
    assert historical_before["revision"] <= profile_before["revision"]
    assert session_before == ui_session == cli_resumed == session_after
    assert session_before["id"] and session_before["revision"]
    assert all(session_before[key] == fixture[key] for key in scope)

    assert (
        isinstance(lineage_before, list)
        and lineage_before == lineage_after
        and lineage_before
    )
    original = lineage_before[0]
    assert original["revision"] == historical_before["revision"]
    versions = original["relationship_versions"]
    assert versions and versions[0]["source_refs"]
    assert {ref["record_id"] for ref in versions[0]["source_refs"]} == {
        fixture["product_id"],
        fixture["project_id"],
    }
    assert all(
        ref["tenant_id"] == fixture["tenant_id"]
        and ref["project_id"] == fixture["project_id"]
        and ref["record_revision"]
        for ref in versions[0]["source_refs"]
    )

    assert set(refusals) == {"context", "l5"}
    for peer, attempts in refusals.items():
        assert isinstance(attempts, list) and len(attempts) >= (
            2 if peer == "context" else 1
        )
        for attempt in attempts:
            assert attempt == {
                "status": 503,
                "body": {
                    "detail": {
                        "code": "peer_integration_deferred",
                        "peer": peer,
                        **availability[peer],
                    }
                },
            }
    assert set(denials) == set(unknowns) == {"foreign", "revoked"}
    for actor in denials:
        assert set(denials[actor]) == set(unknowns[actor]) == {"context", "l5"}
        for peer in ("context", "l5"):
            denied = denials[actor][peer]
            assert denied == unknowns[actor][peer], (
                "Denied peer response disclosed Profile scope"
            )
            assert denied["status"] in (403, 404)

    fields = {
        "peer_requests",
        "queued_activations",
        "receipts",
        "results",
        "provider_calls",
        "operation_ids",
    }
    assert set(effects_before) == set(effects_after) == fields | {"observation_ref"}
    assert effects_before["observation_ref"] and effects_after["observation_ref"]
    assert all(effects_before[field] == effects_after[field] for field in fields)
    assert all(effects_after[field] == [] for field in fields - {"operation_ids"})
    return {
        "status": "unavailable",
        "peers": tuple(availability),
        "session_id": session_before["id"],
        "original_revision": historical_before["revision"],
        "current_revision": profile_after["revision"],
        "observation_refs": (
            effects_before["observation_ref"],
            effects_after["observation_ref"],
        ),
    }


def verify_release_evidence(document):
    """Refuse release acceptance unless every independent live check is linked."""
    if not isinstance(document, dict):
        raise ValueError("Release evidence needs a bounded attempt record")
    run_id = document.get("run_id")
    source = document.get("source_revision")
    requirements = document.get("requirements_revision")
    if (
        not isinstance(run_id, str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]{4,64}", run_id)
        or re.search(r"[0-9]{12}", run_id)
        or not isinstance(source, str)
        or not REVISION.fullmatch(source)
        or not isinstance(requirements, str)
        or not REVISION.fullmatch(requirements)
    ):
        raise ValueError(
            "Release evidence needs an owned run and source/requirements revisions"
        )

    def reference(value):
        return (
            isinstance(value, str)
            and REFERENCE.fullmatch(value)
            and ".." not in value
            and not re.search(r"[0-9]{12}", value)
        )

    def summary(value):
        return (
            isinstance(value, str)
            and 0 < len(value) <= 500
            and SUMMARY.fullmatch(value)
            and not re.search(
                r"https?://|arn:aws|[0-9]{7,}|[A-Za-z0-9_-]{24,}", value, re.I
            )
        )

    def separate_refs(entry):
        return (
            isinstance(entry, dict)
            and reference(entry.get("artifact_ref"))
            and reference(entry.get("readback_ref"))
            and entry["artifact_ref"] != entry["readback_ref"]
        )

    artifacts = document.get("artifacts")
    observed = document.get("observed_release")
    if not isinstance(artifacts, dict) or not isinstance(observed, dict):
        raise ValueError(
            "Release artifact and independent deployment observations are required"
        )
    peer_state = {"context": "deferred", "l5": "deferred"}
    if (
        artifacts.get("peer_state") != peer_state
        or not isinstance(artifacts.get("workflow_revision"), str)
        or not REVISION.fullmatch(artifacts["workflow_revision"])
        or not isinstance(artifacts.get("schema_head"), str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", artifacts["schema_head"])
        or any(
            not isinstance(artifacts.get(name), str)
            or not DIGEST.fullmatch(artifacts[name])
            for name in ("api_image_digest", "ui_manifest_sha256")
        )
        or not isinstance(artifacts.get("cli_hashes"), dict)
        or set(artifacts["cli_hashes"]) != {"adp", "adp-onboard.py", "adp-product.py"}
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
            for value in artifacts["cli_hashes"].values()
        )
        or observed.get("source_revision") != source
        or any(
            observed.get(name) != artifacts[name]
            for name in (
                "workflow_revision",
                "schema_head",
                "api_image_digest",
                "ui_manifest_sha256",
                "cli_hashes",
                "peer_state",
            )
        )
        or not separate_refs(observed)
    ):
        raise ValueError(
            "Deployed API, schema, UI, CLI or deferred peers differ from pinned release"
        )

    attempts = document.get("attempts")
    regressions = document.get("regressions")
    if not isinstance(attempts, dict) or set(attempts) != set(RELEASE_CASES):
        raise ValueError("Every AC needs its own live CLI attempt and readback")
    if not isinstance(regressions, dict) or set(regressions) != set(REGRESSION_CASES):
        raise ValueError("Prior access and replay cases must be selected explicitly")
    attempt_ids = set()
    for criterion, (scenario, selector) in RELEASE_CASES.items():
        entry = attempts[criterion]
        if (
            not separate_refs(entry)
            or entry.get("scenario_id") != scenario
            or entry.get("selector")
            != "tests/e2e/product_onboarding/test_v10.py::" + selector
            or entry.get("run_id") != run_id
            or entry.get("source_revision") != source
            or entry.get("requirements_revision") != requirements
            or entry.get("result") != "pass"
            or not summary(entry.get("expected"))
            or not summary(entry.get("actual"))
        ):
            raise ValueError(f"{criterion} has missing, failed or mismatched evidence")
        history = entry.get("attempt_history")
        if (
            not isinstance(history, list)
            or not 1 <= len(history) <= 20
            or any(
                not isinstance(attempt, dict)
                or attempt.get("result") != "pass"
                or not separate_refs(attempt)
                or attempt.get("run_id") != run_id
                or not isinstance(attempt.get("attempt_id"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", attempt["attempt_id"])
                for attempt in history
            )
        ):
            raise ValueError(f"{criterion} has a failed, skipped or unlinked attempt")
        if (
            entry["artifact_ref"] != history[-1]["artifact_ref"]
            or entry["readback_ref"] != history[-1]["readback_ref"]
        ):
            raise ValueError(
                f"{criterion} current result is not linked to its latest attempt"
            )
        for attempt in history:
            if attempt["attempt_id"] in attempt_ids:
                raise ValueError("Release attempts must have distinct identities")
            attempt_ids.add(attempt["attempt_id"])
    for case in REGRESSION_CASES:
        entry = regressions[case]
        if (
            not separate_refs(entry)
            or entry.get("run_id") != run_id
            or entry.get("result") != "pass"
            or not summary(entry.get("expected"))
            or not summary(entry.get("actual"))
            or not isinstance(entry.get("attempt_id"), str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", entry["attempt_id"])
            or entry["attempt_id"] in attempt_ids
        ):
            raise ValueError(f"Prior {case} case is failed, skipped or unlinked")
        attempt_ids.add(entry["attempt_id"])

    rollback = document.get("rollback")
    cleanup = document.get("cleanup")
    if (
        not separate_refs(rollback)
        or rollback.get("run_id") != run_id
        or rollback.get("result") != "pass"
        or any(
            rollback.get(name) is not True
            for name in (
                "admissions_disabled_first",
                "history_retained",
                "receipts_retained",
                "schema_additive",
            )
        )
    ):
        raise ValueError("Rollback smoke must preserve existing Profile evidence")
    if (
        not separate_refs(cleanup)
        or cleanup.get("run_id") != run_id
        or cleanup.get("source_revision") != source
        or cleanup.get("result") != "pass"
        or cleanup.get("owned_absent") is not True
        or cleanup.get("shared_unchanged") is not True
        or not reference(cleanup.get("receipt_ref"))
    ):
        raise ValueError(
            "Run-owned cleanup is incomplete or lacks independent absence proof"
        )
    return {
        "run_id": run_id,
        "source_revision": source,
        "requirements_revision": requirements,
        "status": "passed",
        "attempt_ids": sorted(attempt_ids),
        "deployment_readback_ref": observed["readback_ref"],
        "cleanup_readback_ref": cleanup["readback_ref"],
    }

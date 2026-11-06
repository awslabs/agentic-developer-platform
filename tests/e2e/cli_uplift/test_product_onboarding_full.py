"""Offline journeys cannot substitute for deployed installed-CLI evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift import product_onboarding_full as full
from tests.e2e.cli_uplift.product_guidance import REQUIRED_CHECKS

SOURCE = "a" * 40
REQUIREMENTS = "b" * 40


def journey(monkeypatch, starting_point, *, fault=None):
    checks = [
        {
            "id": name,
            "status": "missing"
            if name in ("environment", "pipeline", "operations")
            else "complete",
            "required": True,
        }
        for name in sorted(REQUIRED_CHECKS[starting_point])
    ]
    profile = {
        "revision": 1,
        "product": {
            "name": "Owned product",
            "purpose": "Starting purpose",
            "intended_users": ["Maintainers"],
            "starting_point": starting_point,
        },
        "progress": {"checks": checks},
        "sources": [{"id": "owned-source"}],
        "configuration": {"repository": {"id": "owned-repository"}},
        "provenance": [{"paths": ["/product/name"], "source_refs": ["owned-source"]}],
    }
    session = {
        "id": "owned-session",
        "revision": 1,
        "tenant_id": "owned-tenant",
        "product_id": "owned-product",
        "project_id": "owned-project",
        "progress": deepcopy(profile["progress"]),
        "proposals": [],
    }
    state = {"profile": profile, "session": session, "provider": {"operations": []}}
    commands = []
    browser_message = (
        "Verify your environment connection."
        if starting_point == "running_product"
        else "No missing steps in the current scope."
    )
    fixture = {
        "owned_fixture": True,
        "source_revision": SOURCE,
        "product_id": "owned-product",
        "project_id": "owned-project",
        "owner": {"access_token": "synthetic"},
        "evidence_context": {
            "deployment": {"observed": {"product_onboarding_enabled": True}}
        },
    }
    run = {
        "fixture": fixture,
        "deployment_source_revision": SOURCE,
        "requirements_revision": REQUIREMENTS,
        "provider": object(),
        "base_url": "https://example.invalid",
        "binary": "adp",
        "checksums": {"adp": "hash"},
        "record": SimpleNamespace(cli_events=[]),
        "create_ui_session": lambda: (deepcopy(state["session"]), browser_message),
        "observe_provider": lambda: deepcopy(state["provider"]),
    }

    monkeypatch.setattr(full, "read_profile", lambda *_args: deepcopy(state["profile"]))
    monkeypatch.setattr(full, "read_session", lambda *_args: deepcopy(state["session"]))

    def installed(_binary, _checksums, _owner, _origin, arguments, *, record):
        commands.append(arguments[0])
        assert arguments == [
            "resume",
            "--product",
            "owned-product",
            "--project",
            "owned-project",
            "--session",
            "owned-session",
        ]
        resumed = deepcopy(state["session"])
        if fault == "wrong_scope":
            resumed["tenant_id"] = "other-tenant"
        return resumed

    def guide(_binary, _checksums, _fixture, _origin, original, _record):
        commands.append("guide")
        assert original == state["session"]
        return {
            "revision": 1,
            "message": browser_message,
            "proposals": [],
            "mode": "deterministic",
        }

    def save(_binary, _checksums, _fixture, _origin, original, _record):
        commands.append("step")
        assert original == state["session"]
        state["session"]["revision"] = 2
        state["profile"]["revision"] = 2
        state["profile"]["product"]["purpose"] = "Confirmed by direct edit"
        if fault == "unselected_effect":
            state["provider"]["operations"].append("unselected-operation")
        if fault == "source_changed":
            state["profile"]["sources"].append({"id": "different-source"})
        return deepcopy(state["session"])

    monkeypatch.setattr(full, "installed_cli", installed)
    monkeypatch.setattr(full, "guide_with_installed_cli", guide)
    monkeypatch.setattr(full, "save_direct_with_installed_cli", save)
    return run, commands


@pytest.mark.parametrize("starting_point", tuple(REQUIRED_CHECKS))
def test_installed_cli_resumes_ui_session_and_saves_with_independent_readback(
    monkeypatch, starting_point
):
    run, commands = journey(monkeypatch, starting_point)
    result = full.run_full_journey(run, starting_point, SOURCE, REQUIREMENTS)
    assert commands == ["resume", "guide", "step"]
    assert result == {
        "starting_point": starting_point,
        "session_id": "owned-session",
        "revision": 2,
        "source_revision": SOURCE,
        "requirements_revision": REQUIREMENTS,
    }


@pytest.mark.parametrize(
    "fault", ("wrong_scope", "unselected_effect", "source_changed")
)
def test_rejects_wrong_scope_or_unobserved_effect(monkeypatch, fault):
    run, _commands = journey(monkeypatch, "idea", fault=fault)
    with pytest.raises(AssertionError):
        full.run_full_journey(run, "idea", SOURCE, REQUIREMENTS)


@pytest.mark.parametrize(
    "change",
    (
        "owned_fixture",
        "deployment_source_revision",
        "requirements_revision",
        "observe_provider",
        "create_ui_session",
    ),
)
def test_rejects_unpinned_or_unobservable_journeys(monkeypatch, change):
    run, commands = journey(monkeypatch, "idea")
    if change == "owned_fixture":
        run["fixture"][change] = False
    else:
        run[change] = None
    with pytest.raises(ValueError, match="pinned installed clients"):
        full.run_full_journey(run, "idea", SOURCE, REQUIREMENTS)
    assert commands == []


def unavailable_case():
    fixture = {
        "tenant_id": "owned-tenant",
        "product_id": "owned-product",
        "project_id": "owned-project",
    }
    availability = {
        peer: {
            "status": "unavailable",
            "reason": "integration_deferred",
            "message": f"{peer} is unavailable; your saved Product remains available.",
        }
        for peer in ("context", "l5")
    }
    original = {
        **fixture,
        "revision": 1,
        "context_status": "unavailable",
        "product": {"purpose": "Original purpose"},
        "peer_availability": availability,
    }
    current = {
        **deepcopy(original),
        "revision": 2,
        "product": {"purpose": "Saved direct edit"},
    }
    session = {**fixture, "id": "owned-session", "revision": 2, "proposals": []}
    lineage = [
        {
            "revision": 1,
            "relationship_versions": [
                {
                    "source_refs": [
                        {
                            **fixture,
                            "record_id": fixture["product_id"],
                            "record_revision": "event-1",
                        },
                        {
                            **fixture,
                            "record_id": fixture["project_id"],
                            "record_revision": "event-1",
                        },
                    ]
                }
            ],
        },
        {"revision": 2, "relationship_versions": []},
    ]
    refusals = {
        peer: [
            {
                "status": 503,
                "body": {
                    "detail": {
                        "code": "peer_integration_deferred",
                        "peer": peer,
                        **availability[peer],
                    }
                },
            }
        ]
        for peer in availability
    }
    refusals["context"].append(deepcopy(refusals["context"][0]))
    denied = {
        actor: {
            peer: {"status": status, "body": {"detail": {"code": "not_found"}}}
            for peer in availability
        }
        for actor, status in (("foreign", 404), ("revoked", 403))
    }
    effects = {
        "peer_requests": [],
        "queued_activations": [],
        "receipts": [],
        "results": [],
        "provider_calls": [],
        "operation_ids": ["prior-profile-save"],
        "observation_ref": "private:before",
    }
    return fixture, {
        "profile_before": current,
        "cli_profile": deepcopy(current),
        "profile_after": deepcopy(current),
        "historical_before": original,
        "historical_after": deepcopy(original),
        "lineage_before": lineage,
        "lineage_after": deepcopy(lineage),
        "ui_session": session,
        "session_before": deepcopy(session),
        "cli_resumed": deepcopy(session),
        "session_after": deepcopy(session),
        "refusals": refusals,
        "denials": denied,
        "unknowns": deepcopy(denied),
        "effects_before": effects,
        "effects_after": {**deepcopy(effects), "observation_ref": "private:after"},
    }


def test_deferred_peer_readback_retains_saved_session_and_original_scope():
    fixture, reads = unavailable_case()
    assert full.verify_peer_unavailable(fixture, **reads) == {
        "status": "unavailable",
        "peers": ("context", "l5"),
        "session_id": "owned-session",
        "original_revision": 1,
        "current_revision": 2,
        "observation_refs": ("private:before", "private:after"),
    }


@pytest.mark.parametrize(
    "fault",
    (
        "available",
        "fabricated_receipt",
        "queued_activation",
        "request_created",
        "result_invented",
        "provider_dispatched",
        "changed_operation",
        "stale_session",
        "changed_history",
        "changed_lineage",
        "cross_scope_reference",
        "leaked_denial",
        "false_503",
        "missing_replay",
        "cli_disagrees",
        "missing_observation",
    ),
)
def test_deferred_peer_proof_rejects_missing_or_fabricated_evidence(fault):
    fixture, reads = unavailable_case()
    if fault == "available":
        reads["profile_before"]["peer_availability"]["l5"]["status"] = "available"
    elif fault in (
        "fabricated_receipt",
        "queued_activation",
        "request_created",
        "result_invented",
        "provider_dispatched",
        "changed_operation",
    ):
        key = {
            "fabricated_receipt": "receipts",
            "queued_activation": "queued_activations",
            "request_created": "peer_requests",
            "result_invented": "results",
            "provider_dispatched": "provider_calls",
            "changed_operation": "operation_ids",
        }[fault]
        reads["effects_after"][key].append("unexpected-peer-effect")
    elif fault == "stale_session":
        reads["cli_resumed"]["revision"] = 1
    elif fault == "changed_history":
        reads["historical_after"]["product"]["purpose"] = "Rewritten"
    elif fault == "changed_lineage":
        reads["lineage_after"][0]["revision"] = 5
    elif fault == "cross_scope_reference":
        reads["lineage_before"][0]["relationship_versions"][0]["source_refs"][0][
            "tenant_id"
        ] = "other"
    elif fault == "leaked_denial":
        reads["denials"]["foreign"]["context"]["body"] = {"secret": "other-tenant"}
    elif fault == "false_503":
        reads["refusals"]["l5"][0]["body"]["detail"]["code"] = "accepted"
    elif fault == "missing_replay":
        reads["refusals"]["context"].pop()
    elif fault == "cli_disagrees":
        reads["cli_profile"]["revision"] = 1
    else:
        reads["effects_after"]["observation_ref"] = ""
    with pytest.raises(AssertionError):
        full.verify_peer_unavailable(fixture, **reads)


def test_deferred_peer_proof_requires_independent_readbacks():
    fixture, reads = unavailable_case()
    reads["cli_profile"] = None
    with pytest.raises(ValueError, match="independent reads"):
        full.verify_peer_unavailable(fixture, **reads)


def release_record():
    run_id = "owned-v10-run"
    artifacts = {
        "workflow_revision": "c" * 40,
        "schema_head": "profile_2026_10",
        "api_image_digest": "sha256:" + "d" * 64,
        "ui_manifest_sha256": "e" * 64,
        "cli_hashes": {
            "adp": "f" * 64,
            "adp-onboard.py": "0" * 64,
            "adp-product.py": "1" * 64,
        },
        "peer_state": {"context": "deferred", "l5": "deferred"},
    }
    attempts = {}
    for criterion, (scenario, selector) in full.RELEASE_CASES.items():
        label = criterion.lower().replace("-", "")
        latest = {
            "run_id": run_id,
            "attempt_id": label + "-attempt-one",
            "result": "pass",
            "artifact_ref": "evidence/" + label + "-cli",
            "readback_ref": "evidence/" + label + "-api",
        }
        attempts[criterion] = {
            "scenario_id": scenario,
            "selector": "tests/e2e/product_onboarding/test_v10.py::" + selector,
            "run_id": run_id,
            "source_revision": SOURCE,
            "requirements_revision": REQUIREMENTS,
            "expected": "Owned behavior matches independent readback",
            "actual": "All required observations agree",
            **{key: latest[key] for key in ("artifact_ref", "readback_ref")},
            "result": "pass",
            "attempt_history": [latest],
        }
    regressions = {
        case: {
            "run_id": run_id,
            "attempt_id": case + "-one",
            "result": "pass",
            "expected": "Denied or repeated writes cannot change saved scope",
            "actual": "Independent observations show no new effects",
            "artifact_ref": "evidence/" + case + "-case",
            "readback_ref": "evidence/" + case + "-api",
        }
        for case in full.REGRESSION_CASES
    }
    return {
        "run_id": run_id,
        "source_revision": SOURCE,
        "requirements_revision": REQUIREMENTS,
        "artifacts": artifacts,
        "observed_release": {
            **deepcopy(artifacts),
            "source_revision": SOURCE,
            "artifact_ref": "receipts/deployment",
            "readback_ref": "evidence/deployment-readback",
        },
        "attempts": attempts,
        "regressions": regressions,
        "rollback": {
            "run_id": run_id,
            "result": "pass",
            "admissions_disabled_first": True,
            "history_retained": True,
            "receipts_retained": True,
            "schema_additive": True,
            "artifact_ref": "evidence/rollback-action",
            "readback_ref": "evidence/rollback-api",
        },
        "cleanup": {
            "run_id": run_id,
            "source_revision": SOURCE,
            "result": "pass",
            "owned_absent": True,
            "shared_unchanged": True,
            "artifact_ref": "evidence/cleanup-action",
            "readback_ref": "evidence/cleanup-api",
            "receipt_ref": "receipts/owned-cleanup",
        },
    }


def test_release_evidence_links_each_ac_attempt_regressions_rollback_and_cleanup():
    record = release_record()
    checked = full.verify_release_evidence(record)
    assert checked == {
        "run_id": record["run_id"],
        "source_revision": SOURCE,
        "requirements_revision": REQUIREMENTS,
        "status": "passed",
        "attempt_ids": sorted(
            [
                entry["attempt_history"][0]["attempt_id"]
                for entry in record["attempts"].values()
            ]
            + [entry["attempt_id"] for entry in record["regressions"].values()]
        ),
        "deployment_readback_ref": "evidence/deployment-readback",
        "cleanup_readback_ref": "evidence/cleanup-api",
    }


@pytest.mark.parametrize(
    "fault",
    (
        "missing_ac",
        "missing_regression",
        "skipped_regression",
        "failed_attempt",
        "skipped_attempt",
        "duplicate_attempt",
        "wrong_attempt_link",
        "stale_source",
        "stale_requirements",
        "wrong_image",
        "wrong_schema",
        "wrong_cli",
        "peer_activated",
        "identical_deployment_proof",
        "disabled_admissions_late",
        "lost_history",
        "lost_receipts",
        "destructive_schema",
        "cleanup_failed",
        "cleanup_shared",
        "unsafe_receipt",
        "unsafe_actual",
    ),
)
def test_release_evidence_refuses_incomplete_or_false_green_cases(fault):
    record = release_record()
    if fault == "missing_ac":
        record["attempts"].pop("AC-02")
    elif fault == "missing_regression":
        record["regressions"].pop("revoked_access")
    elif fault == "skipped_regression":
        record["regressions"]["foreign_access"]["result"] = "skip"
    elif fault in ("failed_attempt", "skipped_attempt"):
        record["attempts"]["AC-01"]["attempt_history"].insert(
            0,
            {
                "run_id": record["run_id"],
                "attempt_id": "ac01-original",
                "result": fault.split("_")[0],
                "artifact_ref": "evidence/first-attempt",
                "readback_ref": "evidence/first-readback",
            },
        )
    elif fault == "duplicate_attempt":
        record["attempts"]["AC-02"]["attempt_history"][0]["attempt_id"] = record[
            "attempts"
        ]["AC-01"]["attempt_history"][0]["attempt_id"]
    elif fault == "wrong_attempt_link":
        record["attempts"]["AC-01"]["readback_ref"] = "evidence/unrelated-api"
    elif fault == "stale_source":
        record["observed_release"]["source_revision"] = "f" * 40
    elif fault == "stale_requirements":
        record["attempts"]["AC-03"]["requirements_revision"] = "f" * 40
    elif fault in ("wrong_image", "wrong_schema", "wrong_cli"):
        field = {
            "wrong_image": "api_image_digest",
            "wrong_schema": "schema_head",
            "wrong_cli": "cli_hashes",
        }[fault]
        record["observed_release"][field] = "other"
    elif fault == "peer_activated":
        record["observed_release"]["peer_state"]["context"] = "available"
    elif fault == "identical_deployment_proof":
        record["observed_release"]["readback_ref"] = "receipts/deployment"
    elif fault in (
        "disabled_admissions_late",
        "lost_history",
        "lost_receipts",
        "destructive_schema",
    ):
        field = {
            "disabled_admissions_late": "admissions_disabled_first",
            "lost_history": "history_retained",
            "lost_receipts": "receipts_retained",
            "destructive_schema": "schema_additive",
        }[fault]
        record["rollback"][field] = False
    elif fault == "cleanup_failed":
        record["cleanup"]["result"] = "fail"
    elif fault == "cleanup_shared":
        record["cleanup"]["shared_unchanged"] = False
    elif fault == "unsafe_receipt":
        record["cleanup"]["receipt_ref"] = "receipts/123456789012"
    else:
        record["attempts"]["AC-02"]["actual"] = "https://internal.example.invalid"
    with pytest.raises(ValueError):
        full.verify_release_evidence(record)

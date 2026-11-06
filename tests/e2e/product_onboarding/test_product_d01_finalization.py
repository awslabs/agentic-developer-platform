"""Compose preliminary release checks, independent recovery and final grading."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import (
    product_automatic_identity,
    product_d01_cases,
    product_d01_finalization,
    product_execution,
    product_recovery_observation,
)
from tests.e2e.product_onboarding.product_qualification_state import (
    RetainedQualification,
    held_evaluation,
    held_release,
)
from tests.e2e.product_onboarding.product_rollback_artifacts import qualification
from tests.e2e.product_onboarding.test_product_automatic_identity import stage

pytest_plugins = ("tests.e2e.product_onboarding.test_product_automatic_identity",)


@pytest.fixture
def recovery(selected, monkeypatch):
    _, documents, store = selected
    identity = stage(selected)
    source = identity["source_revision"]
    product_automatic_identity.complete(store, "product-42-1", source)
    native = documents["api"]["native"]
    candidate = native["release_artifact"]
    baseline = {**candidate, "source_revision": "e" * 40, "image_digest": "sha256:" + "e" * 64, "run_id": 41}
    cfg = {
        "expected_revision": source,
        "product_cases": "api_product_lifecycle",
        "hold_for_rollback": True,
        "platform_account": identity["account_id"],
        "region": identity["region"],
    }
    evaluation_id = "adp-e2e-20261021-000000-abcdef"
    owned = {
        "run_id": "product-42-1",
        "source_revision": source,
        "product_id": "owned-product",
        "project_id": "owned-project",
        "tenant_id": "owned-tenant",
        "actor_id": "owned-actor",
        "idempotency_key": "original-key",
        "history_ref": "sha256:" + "a" * 64,
        "idempotency_receipt": "sha256:" + "b" * 64,
    }
    document = {
        "evaluation_id": evaluation_id,
        "product_run_id": owned["run_id"],
        "cleanup_ok": False,
        "product_evidence": {"run_id": owned["run_id"], "held": owned},
    }
    manifest = {
        "prefix": evaluation_id,
        "resources": [
            {
                "kind": "product_fixture",
                "id": owned["run_id"],
                "status": "pending",
                "prefix": evaluation_id,
                "account": identity["account_id"],
                "region": identity["region"],
                "detail": {field: owned[field] for field in ("tenant_id", "actor_id", "source_revision", "idempotency_key")},
            }
        ],
    }
    store.load = lambda selected_id: (deepcopy(document), deepcopy(manifest)) if selected_id == evaluation_id else (None, None)
    held_evaluation(store, owned["run_id"], product_execution.recovery_binding(cfg, owned["run_id"], evaluation_id))
    held_release(store, owned["run_id"], native)
    state = RetainedQualification(store, candidate, baseline)
    state.retain(
        {
            "status": "incomplete",
            "candidate": qualification.release_identity(candidate),
            "baseline": qualification.release_identity(baseline),
            "owned": None,
            "attempts": [],
            "candidate_ref": identity["release_ref"],
            "baseline_ref": "s3://private/baseline?versionId=one",
        }
    )
    state.begin_hold()
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repository")
    aws = SimpleNamespace(
        call=lambda *_: {
            "Account": identity["account_id"],
            "Arn": f"arn:aws:sts::{identity['account_id']}:assumed-role/adp-cli-uplift-eval-orchestrator/fixture",
        }
    )

    def observe(choice):
        return product_recovery_observation.observe(
            cfg,
            owned["run_id"],
            aws,
            store,
            choice=choice,
            admissions=choice == "candidate",
            baseline_reader=lambda *_args, **_kwargs: (baseline, "s3://private/baseline?versionId=one"),
            runtime_reader=lambda *_: {"pod_uids": ["observed-pod"], "migration_head": candidate["migration_head"]},
            session_reader=lambda *_args, **_kwargs: {"access_token": "fixture-session"},
            product_reader=lambda *_args, admissions: {
                **{field: owned[field] for field in ("run_id", "product_id", "history_ref", "idempotency_key", "idempotency_receipt")},
                "supported_routes_ok": True,
                "non_product_available": True,
                "replay_verified": admissions,
            },
        )

    def cleanup():
        document["cleanup_ok"] = True
        document["product_cleanup_receipt"] = {
            **{field: owned[field] for field in ("run_id", "product_id", "project_id")},
            "fixture_removed": True,
        }
        manifest["resources"][0]["status"] = "deleted"

    return SimpleNamespace(store=store, source=source, state=state, document=document, manifest=manifest, observe=observe, cleanup=cleanup)


def test_preliminary_cli_succeeds_without_claiming_final_acceptance(selected, tmp_path):
    paths, _, _ = selected
    output = tmp_path / "preliminary.json"
    args = [argument for name in ("inputs", "dispatch", "packet", "release") for argument in ("--" + name, str(paths[name]))]
    args += ["--output", str(output)]
    assert product_d01_cases.main(args) == 1
    assert product_d01_cases.main([*args, "--preliminary"]) == 0
    report = json.loads(output.read_text())
    assert report["status"] == "incomplete"
    assert report["cases"]["PR01-C03"]["status"] == report["cases"]["PR01-C04"]["status"] == "NOT_RUN"
    paths["packet"].unlink()
    assert product_d01_cases.main([*args, "--preliminary"]) == 1


def test_composed_release_passes_only_after_observed_recovery_and_owned_cleanup(recovery):
    for choice in ("quiesced", "baseline", "candidate"):
        recovery.observe(choice)
        with pytest.raises(ValueError):
            product_d01_finalization.finalize(recovery.store, "product-42-1", recovery.source)
    recovery.cleanup()
    report = product_d01_finalization.finalize(recovery.store, "product-42-1", recovery.source)
    assert report["status"] == "pass"
    assert all(case["status"] == "PASSED" for case in report["cases"].values())
    assert recovery.state.load()["status"] == "passed"
    assert recovery.state.load()["attempts"][-1] == {"stage": "cleaned", "receipt": recovery.document["product_cleanup_receipt"]}
    assert recovery.store._get(product_d01_finalization.final_key("product-42-1"))["report"] == report
    assert product_d01_finalization.finalize(recovery.store, "product-42-1", recovery.source) == report


@pytest.mark.parametrize("interrupted_after", [None, "quiesced", "baseline"])
def test_interrupted_recovery_or_failed_restoration_cannot_pass_even_after_cleanup(recovery, interrupted_after):
    if interrupted_after:
        recovery.observe("quiesced")
    if interrupted_after == "baseline":
        recovery.observe("baseline")
    recovery.cleanup()
    with pytest.raises(ValueError, match="Interrupted"):
        product_d01_finalization.finalize(recovery.store, "product-42-1", recovery.source)
    assert recovery.state.load()["status"] == "incomplete"


def test_compensating_candidate_restoration_cannot_replace_baseline_readback(recovery):
    recovery.observe("quiesced")
    product_recovery_observation.record_stage(
        recovery.state, "baseline_restore_started", "candidate_restore_started", recovery.state.load()["candidate"]
    )
    recovery.observe("candidate")
    recovery.cleanup()
    with pytest.raises(ValueError, match="Interrupted"):
        product_d01_finalization.finalize(recovery.store, "product-42-1", recovery.source)
    assert recovery.state.load()["status"] == "incomplete"


@pytest.mark.parametrize("change", ["target", "schema", "history", "replay", "pods", "cleanup", "receipt", "owner", "attempt", "preliminary"])
def test_stale_or_incomplete_recovery_evidence_refuses_final_grades(recovery, monkeypatch, change):
    for choice in ("quiesced", "baseline", "candidate"):
        recovery.observe(choice)
    recovery.cleanup()
    stored = recovery.store.values[recovery.state.key]["report"]
    if change in ("target", "schema", "history", "pods"):
        field = {"target": "resource_id", "schema": "migration_head", "history": "history_ref", "pods": "pod_uids"}[change]
        stored["attempts"][4]["receipt"][field] = [] if change == "pods" else "wrong"
    elif change == "replay":
        stored["attempts"][6]["receipt"]["replay_verified"] = False
    elif change == "cleanup":
        recovery.document["cleanup_ok"] = False
    elif change == "receipt":
        recovery.document["product_cleanup_receipt"]["product_id"] = "foreign"
    elif change == "owner":
        recovery.manifest["resources"][0]["detail"]["tenant_id"] = "foreign"
    elif change == "attempt":
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    else:
        recovery.store.values[product_automatic_identity.preliminary_key("product-42-1")]["report"]["cases"]["PR01-C02"]["status"] = "FAILED"
    with pytest.raises((ValueError, product_automatic_identity.ports.PortError)):
        product_d01_finalization.finalize(recovery.store, "product-42-1", recovery.source)
    assert recovery.store._get(product_d01_finalization.final_key("product-42-1")) is None


@pytest.mark.parametrize("completed", [False, True])
def test_final_cli_retains_result_and_fails_unfinished_recovery(recovery, monkeypatch, tmp_path, completed):
    if completed:
        for choice in ("quiesced", "baseline", "candidate"):
            recovery.observe(choice)
        recovery.cleanup()
    monkeypatch.setattr(product_d01_finalization.live, "resolved_config", lambda _: {})
    monkeypatch.setattr(product_d01_finalization.ports, "default_ports", lambda _: {"aws": object()})
    monkeypatch.setattr(product_d01_finalization.statestore, "default_store", lambda *_: recovery.store)
    output = tmp_path / "final.json"
    assert product_d01_finalization.main(["--expected-revision", recovery.source, "--output", str(output)]) == (0 if completed else 1)
    expected_status = "pass" if completed else "incomplete"
    assert json.loads(output.read_text())["status"] == expected_status
    assert output.stat().st_mode & 0o777 == 0o600
    assert recovery.store._get(product_d01_finalization.final_key("product-42-1"))["report"]["status"] == expected_status

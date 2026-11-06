"""Rollback readback binds signed pods to the original owner and history."""

import json
import os
import hashlib
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from tests.e2e.cli_uplift import ports, statestore
from tests.e2e.product_onboarding import product_recovery_observation
from tests.e2e.product_onboarding.product_qualification_state import RetainedQualification

pytest_plugins = ("tests.e2e.product_onboarding.test_product_recovery_handoff",)


def observe(handoff, choice="baseline", *, seed=True, **changes):
    cfg, _, native, _, baseline, document, _, store, aws = handoff
    state = RetainedQualification(store, native["release_artifact"], baseline)
    if seed:
        state.begin_hold()
    if seed and choice in ("baseline", "candidate"):
        product_recovery_observation.record_stage(state, "quiesce_started", "quiesced", {"fixture": "quiesced"})
        product_recovery_observation.record_stage(state, "quiesced", "baseline_restore_started", state.load()["baseline"])
    if seed and choice == "candidate":
        product_recovery_observation.record_stage(state, "baseline_restore_started", "baseline_verified", {"fixture": "baseline"})
        product_recovery_observation.record_stage(state, "baseline_verified", "candidate_restore_started", state.load()["candidate"])
    owned = document["product_evidence"]["held"]

    def owner_reader(configuration, ctx, state, *, owner):
        assert configuration["product_run_id"] == owned["run_id"]
        assert ctx["document"] == document
        assert any(entry["id"] == owned["run_id"] for entry in ctx["manifest"].outstanding())
        assert state is store and owner is True
        return {"access_token": "private-owner-session"}

    def product_reader(configuration, original, token, *, admissions):
        assert original == owned and token == "private-owner-session"
        return {
            **{key: owned[key] for key in ("run_id", "product_id", "history_ref", "idempotency_key", "idempotency_receipt")},
            "supported_routes_ok": True,
            "non_product_available": True,
            "replay_verified": admissions,
        }

    params = {
        "choice": choice,
        "admissions": choice == "candidate",
        "baseline_reader": lambda *_args, **_kwargs: (baseline, "s3://private/baseline?versionId=one"),
        "runtime_reader": lambda _cfg, _aws, artifact, _admissions: {"pod_uids": ["fresh-serving-pod"], "migration_head": artifact["migration_head"]},
        "session_reader": owner_reader,
        "product_reader": product_reader,
    }
    params.update(changes)
    return product_recovery_observation.observe(cfg, "product-42-1", aws, store, **params)


def test_rollback_runtime_observes_signed_pods_without_relabeling_the_release(handoff, monkeypatch):
    cfg, _, native, _, _, _, _, _, aws = handoff
    cfg["internal_invoke_url"] = "https://private.example.invalid/dev"
    artifact = {
        **native["release_artifact"],
        "schema_version": 1,
        "product_profile_schema_version": "1.0",
        "produced_at": datetime.now(UTC),
    }
    cluster = artifact["resource_id"].split("/")[0]
    selected = {"cluster": cluster, "namespace": "adp-gateway", "cluster_arn": "arn:example:cluster"}
    monkeypatch.setattr(product_recovery_observation.deployment_provenance, "binding", lambda _: selected)

    class FakeKubernetes:
        def __init__(self, *_args, **_kwargs):
            self.closed = False

        def gateway(self, *, digest, source_revision, account, region, product):
            assert digest == artifact["image_digest"]
            assert source_revision == artifact["source_revision"]
            assert product.product_profile_enabled is False
            assert artifact["product_profile_enabled"] is True
            return ["observed-pod"], artifact["migration_head"]

        def close(self):
            self.closed = True

    monkeypatch.setattr(product_recovery_observation, "KubernetesRuntime", FakeKubernetes)
    aws.call = lambda *_args, **_kwargs: {"cluster": {"name": cluster, "status": "ACTIVE", "arn": selected["cluster_arn"]}}
    aws.session = lambda: object()
    assert product_recovery_observation.serving(cfg, aws, artifact, False) == {
        "pod_uids": ["observed-pod"],
        "migration_head": artifact["migration_head"],
    }


def test_recovery_observation_entrypoint_writes_private_receipt(handoff, monkeypatch, tmp_path):
    cfg, _, _, _, _, _, _, store, aws = handoff
    source = cfg["expected_revision"]
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setattr(product_recovery_observation.live, "resolved_config", lambda _: cfg)
    monkeypatch.setattr(product_recovery_observation.live, "workflow_run_id", lambda: "product-42-1")
    monkeypatch.setattr(product_recovery_observation.ports, "default_ports", lambda _: {"aws": aws})
    monkeypatch.setattr(product_recovery_observation.statestore, "default_store", lambda *_: store)
    monkeypatch.setattr(product_recovery_observation, "observe", lambda *_args, **_kwargs: {"run_id": "product-42-1"})
    path = tmp_path / "private.json"
    args = ["--expected-revision", source, "--choice", "baseline", "--receipt", str(path)]
    assert product_recovery_observation.main(args) == json.loads(path.read_text())
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        product_recovery_observation.main(args)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/foreign")
    with pytest.raises(ports.PortError):
        product_recovery_observation.main(args)


def test_restore_intent_survives_retry_after_interrupted_switch(handoff, monkeypatch):
    cfg, _, native, _, baseline, _, _, store, aws = handoff
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/adp")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", native["release_artifact"]["workflow_revision"])
    state = RetainedQualification(store, native["release_artifact"], baseline)
    state.begin_hold()
    def reader(*_args, **_kwargs):
        return baseline, "s3://private/baseline?versionId=one"
    intent = product_recovery_observation.prepare_restore(cfg, "product-42-1", aws, store, baseline_reader=reader)
    assert intent == state.load()["candidate"]
    assert state.load()["attempts"][-1] == {"stage": "candidate_restore_started", "receipt": intent}
    assert product_recovery_observation.prepare_restore(cfg, "product-42-1", aws, store, baseline_reader=reader) == intent
    assert len(state.load()["attempts"]) == 3


@pytest.mark.parametrize("change", ["manifest", "resource", "release", "baseline", "role", "stage"])
def test_restore_intent_rejects_changed_owner_or_release(handoff, monkeypatch, change):
    cfg, _, native, _, baseline, document, manifest, store, aws = handoff
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/adp")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", native["release_artifact"]["workflow_revision"])
    state = RetainedQualification(store, native["release_artifact"], baseline)
    state.begin_hold()
    def reader(*_args, **_kwargs):
        version = "changed" if change == "baseline" else "one"
        return baseline, f"s3://private/baseline?versionId={version}"
    if change == "manifest":
        store.load = lambda *_: (document, None)
    elif change == "resource":
        manifest["resources"].append(deepcopy(manifest["resources"][0]))
    elif change == "release":
        monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "0" * 40)
    elif change == "role":
        aws.call = lambda *_args, **_kwargs: {"Account": str(cfg["platform_account"]), "Arn": "foreign"}
    else:
        product_recovery_observation.record_stage(state, "quiesce_started", "quiesced", {"receipt": "quiesced"})
        product_recovery_observation.record_stage(state, "quiesced", "baseline_restore_started", state.load()["baseline"])
        product_recovery_observation.record_stage(state, "baseline_restore_started", "baseline_verified", {"receipt": "baseline"})
        product_recovery_observation.record_stage(state, "baseline_verified", "candidate_restore_started", state.load()["candidate"])
        product_recovery_observation.record_stage(state, "candidate_restore_started", "candidate_verified", {"receipt": "candidate"})
    with pytest.raises((ports.PortError, statestore.StateStoreError)):
        product_recovery_observation.prepare_restore(cfg, "product-42-1", aws, store, baseline_reader=reader)


@pytest.mark.parametrize("choice", ["quiesced", "baseline", "candidate"])
def test_signed_recovery_readback_uses_original_owner_and_schema(handoff, monkeypatch, choice):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/adp")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", handoff[2]["release_artifact"]["workflow_revision"])
    result = observe(handoff, choice)
    assert result["source_revision"] == (handoff[4] if choice == "baseline" else handoff[2]["release_artifact"])["source_revision"]
    assert result["pod_uids"] == ["fresh-serving-pod"]
    assert result["replay_verified"] is (choice == "candidate")
    state = RetainedQualification(handoff[7], handoff[2]["release_artifact"], handoff[4])
    last = {"quiesced": "baseline_restore_started", "baseline": "candidate_restore_started", "candidate": "candidate_verified"}[choice]
    assert state.load()["attempts"][-1]["stage"] == last


def test_cross_job_readbacks_advance_one_retained_attempt(handoff, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/adp")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", handoff[2]["release_artifact"]["workflow_revision"])
    RetainedQualification(handoff[7], handoff[2]["release_artifact"], handoff[4]).begin_hold()
    receipts = [observe(handoff, choice, seed=False) for choice in ("quiesced", "baseline", "candidate")]
    assert [receipt["product_id"] for receipt in receipts] == ["owned-product"] * 3
    state = RetainedQualification(handoff[7], handoff[2]["release_artifact"], handoff[4])
    assert [entry["stage"] for entry in state.load()["attempts"]] == [
        "created",
        "quiesce_started",
        "quiesced",
        "baseline_restore_started",
        "baseline_verified",
        "candidate_restore_started",
        "candidate_verified",
    ]
    with pytest.raises(ports.PortError, match="protected stage"):
        observe(handoff, "baseline", seed=False)


def test_failed_owned_cleanup_cannot_claim_restored_product_is_retired(handoff, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/adp")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", handoff[2]["release_artifact"]["workflow_revision"])
    cfg, _, native, _, baseline, _, manifest, store, aws = handoff
    document = handoff[5]
    evaluation_id = document["evaluation_id"]
    document["instance_id"] = "i-00000001"
    held = document["product_evidence"]["held"]
    held["tenant_id"] = evaluation_id + "-product-" + hashlib.sha256((evaluation_id + ":product-workspace").encode()).hexdigest()[:24]
    held["idempotency_key"] = "product-create-" + hashlib.sha256(("product-42-1:" + evaluation_id).encode()).hexdigest()
    manifest["resources"][0]["detail"].update(
        tenant_id=held["tenant_id"], idempotency_key=held["idempotency_key"], instance_id=document["instance_id"]
    )
    state = RetainedQualification(store, native["release_artifact"], baseline)
    state.begin_hold()
    for choice in ("quiesced", "baseline", "candidate"):
        observe(handoff, choice, seed=False)
    monkeypatch.setattr(product_recovery_observation.product_execution.runner, "main", lambda *_args, **_kwargs: 1)
    with pytest.raises(ports.PortError, match="independent recovery is incomplete"):
        product_recovery_observation.product_execution.recover(cfg, "product-42-1", aws, store=store)
    assert state.load()["attempts"][-1]["stage"] == "candidate_verified"
    assert any(entry["status"] == "pending" for entry in manifest["resources"] if entry["kind"] == "product_fixture")


@pytest.mark.parametrize(
    "change",
    ["baseline_ref", "schema", "manifest_missing", "duplicate", "wrong_role", "wrong_workflow", "wrong_phase", "wrong_history"],
)
def test_rollback_readback_rejects_changed_proof_or_owner(handoff, monkeypatch, change):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/adp")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", handoff[2]["release_artifact"]["workflow_revision"])
    cfg, _, _, _, baseline, _, manifest, store, aws = handoff
    options = {}
    if change == "baseline_ref":
        options["baseline_reader"] = lambda *_args, **_kwargs: (baseline, "s3://private/baseline?versionId=changed")
    elif change == "schema":
        options["runtime_reader"] = lambda *_args: {"pod_uids": ["pod"], "migration_head": "wrong"}
    elif change == "manifest_missing":
        manifest["resources"].clear()
    elif change == "duplicate":
        manifest["resources"].append(deepcopy(manifest["resources"][0]))
    elif change == "wrong_role":
        aws.call = lambda *_args, **_kwargs: {"Account": str(cfg["platform_account"]), "Arn": "foreign"}
    elif change == "wrong_workflow":
        monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "0" * 40)
    elif change == "wrong_phase":
        options["admissions"] = True
    else:

        def wrong_history(_cfg, owned, _token, *, admissions):
            return {"run_id": owned["run_id"], "product_id": owned["product_id"], "history_ref": "sha256:" + "e" * 64}

        options["product_reader"] = wrong_history
    with pytest.raises((ValueError, ports.PortError, statestore.StateStoreError)):
        observe(handoff, **options)

"""Synthetic release and schema receipts for offline catalog recovery checks."""

import hashlib
import json
from copy import deepcopy

import pytest

from src.orchestration.deployment_runtime_contract import RuntimeComponent
from tests.e2e.product_onboarding.product_catalog_release import WORKFLOW, pin_catalog_release


class LocalState:
    def __init__(self, source="a" * 40):
        self.binding = {"run_id": "example-run", "source_revision": source,
                        "tenant_id": "example-tenant", "actor_id": "example-editor"}
        self.document = {"catalog": {"plan": {"source_revision": source}}}
        self.saved = 0

    def save(self, value):
        self.saved += 1
        self.document = deepcopy(value)


def catalog_release_fixture(state):
    selection = {"repository": "aws-e/adp", "repository_id": 17, "run_id": 42,
                 "attempt": 1, "account_id": "000000000000", "region": "us-east-1",
                 "environment": "dev", "workflow_path": WORKFLOW, "workflow_revision": "b" * 40,
                 "source_revision": state.binding["source_revision"], "image_digest": "sha256:" + "c" * 64,
                 "schema_head": "example_catalog_head", "resource_id": "example-cluster/dev"}
    artifact = {"schema_version": 1, "repository_id": selection["repository_id"],
                "run_id": selection["run_id"], "run_attempt": selection["attempt"],
                "source_revision": selection["source_revision"], "workflow_revision": selection["workflow_revision"],
                "workflow_path": WORKFLOW, "account_id": selection["account_id"],
                "region": selection["region"], "resource_id": selection["resource_id"],
                "component": "gateway-backend", "image_digest": selection["image_digest"],
                "migration_head": selection["schema_head"], "produced_at": "2026-10-20T00:00:00Z"}
    reference = "s3://private-fixture/example?versionId=one"

    class FixtureStore:
        def read(self, **kwargs):
            assert kwargs == {"account": selection["account_id"], "environment": "dev", "region": selection["region"],
                              "repository": selection["repository"], "repository_id": selection["repository_id"],
                              "run_id": selection["run_id"], "attempt": selection["attempt"], "kind": "release",
                              "name": "gateway-backend", "workflow_path": WORKFLOW,
                              "workflow_revision": selection["workflow_revision"]}
            payload = json.dumps(artifact, sort_keys=True).encode()
            return payload, hashlib.sha256(payload).hexdigest(), reference

    runtime = {"component": "gateway-backend", "actual_revision": selection["source_revision"],
               "image_digest": selection["image_digest"], "healthy": True, "evidence_ref": reference,
               "observed_at": "2026-10-20T00:01:00Z", "migration_head": selection["schema_head"],
               "tick_digest": selection["image_digest"], "pod_uids": ["example-pod"]}
    payload = json.dumps(artifact, sort_keys=True).encode()
    receipt = RuntimeComponent.model_validate({**runtime, "artifact_hash": hashlib.sha256(payload).hexdigest()})
    observation_digest = hashlib.sha256(json.dumps(receipt.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    observation = {"source_revision": selection["source_revision"],
                   "pod_uids": ["example-pod"], "tick_verified": True,
                   "tick_digest": selection["image_digest"],
                   "database_heads": [selection["schema_head"]],
                   "image_heads": [selection["schema_head"]],
                   "observation_ref": "runtime:sha256:" + observation_digest}

    def observe(release, digest, signed_reference):
        assert release.source_revision == selection["source_revision"]
        return {**observation, "runtime_receipt": {**runtime, "artifact_hash": digest}}

    return selection, FixtureStore(), observe, artifact, runtime, observation


def test_selected_release_pins_signed_artifact_and_independent_serving_schema():
    state = LocalState()
    selection, store, observe, _, _, observation = catalog_release_fixture(state)
    binding = pin_catalog_release(state, selection, store, observe)
    assert binding == state.document["catalog_release"]
    assert binding["schema_head"] == selection["schema_head"]
    assert binding["image_digest"] == selection["image_digest"]
    assert binding["observation_ref"] == observation["observation_ref"]
    assert binding["release_ref"] != binding["observation_ref"] and state.saved == 1
    assert pin_catalog_release(state, selection, store, observe) == binding


@pytest.mark.parametrize("mismatch", [
    "unselected_source", "unselected_schema", "wrong_workflow", "wrong_image", "missing_release",
    "wrong_artifact_hash", "wrong_runtime_schema", "wrong_database_head", "wrong_image_head",
    "wrong_tick", "missing_pods", "missing_observation", "wrong_observation_digest", "different_saved_artifact",
    "wrong_attempt", "wrong_release_schema", "missing_runtime", "wrong_reference", "wrong_release_digest",
])
def test_catalog_recovery_refuses_missing_stale_or_mismatched_release_receipts(mismatch):
    state = LocalState()
    selection, store, observe, artifact, runtime, observation = catalog_release_fixture(state)
    if mismatch == "unselected_source":
        selection["source_revision"] = "e" * 40
    elif mismatch == "unselected_schema":
        selection["schema_head"] = "other_head"
    elif mismatch == "wrong_workflow":
        selection["workflow_path"] = "unreviewed.yml"
    elif mismatch == "wrong_image":
        selection["image_digest"] = "sha256:" + "f" * 64
    elif mismatch == "missing_release":
        store.read = lambda **kwargs: None
    elif mismatch == "wrong_release_digest":
        original_read = store.read
        def corrupted(**kwargs):
            payload, _, reference = original_read(**kwargs)
            return payload, "f" * 64, reference
        store.read = corrupted
    elif mismatch == "wrong_attempt":
        artifact["run_attempt"] += 1
    elif mismatch == "wrong_release_schema":
        artifact["migration_head"] = "other_head"
    elif mismatch == "wrong_artifact_hash":
        runtime["artifact_hash"] = "f" * 64
    elif mismatch == "wrong_runtime_schema":
        runtime["migration_head"] = "other_head"
    elif mismatch == "wrong_database_head":
        observation["database_heads"] = ["other_head"]
    elif mismatch == "wrong_image_head":
        observation["image_heads"] = ["other_head"]
    elif mismatch == "wrong_tick":
        observation["tick_digest"] = "sha256:" + "f" * 64
    elif mismatch == "missing_pods":
        observation["pod_uids"] = []
    elif mismatch == "missing_observation":
        observation["observation_ref"] = ""
    elif mismatch == "wrong_observation_digest":
        observation["observation_ref"] = "runtime:sha256:" + "f" * 64
    elif mismatch == "wrong_reference":
        runtime["evidence_ref"] = "s3://private-fixture/different?versionId=one"
    elif mismatch == "different_saved_artifact":
        initial = pin_catalog_release(state, selection, store, observe)
        state.document["catalog_release"] = {**initial, "artifact_hash": "f" * 64}
    if mismatch == "missing_runtime":
        def missing_receipt(release, digest, reference):
            return {}
        observer = missing_receipt
    elif mismatch == "wrong_artifact_hash":
        def changed(release, digest, reference):
            value = observe(release, digest, reference)
            value["runtime_receipt"]["artifact_hash"] = "f" * 64
            return value
        observer = changed
    elif mismatch in ("wrong_runtime_schema", "wrong_reference"):
        def changed(release, digest, reference):
            value = observe(release, digest, reference)
            value["runtime_receipt"].update(runtime)
            return value
        observer = changed
    else:
        observer = observe
    with pytest.raises(ValueError):
        pin_catalog_release(state, selection, store, observer)
    assert state.saved == (1 if mismatch == "different_saved_artifact" else 0)

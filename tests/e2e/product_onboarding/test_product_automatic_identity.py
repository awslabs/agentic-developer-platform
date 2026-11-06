"""Offline signed-release correlation between selected checks and recovery."""

import json
from copy import deepcopy

import pytest

from tests.e2e.cli_uplift import ports
from tests.e2e.product_onboarding import product_automatic_identity, product_d01_cases
from tests.e2e.product_onboarding.test_product_inputs import (
    produce_and_publish,
    signed_release_reports,
)

pytest_plugins = ("tests.e2e.product_onboarding.test_product_inputs",)


class Store:
    def __init__(self):
        self.values = {}

    def _get(self, key):
        return deepcopy(self.values.get(key))

    def _put(self, key, value):
        self.values[key] = deepcopy(value)


@pytest.fixture
def selected(tmp_path, reports, monkeypatch):
    signed_release_reports(reports)
    paths, documents = reports
    for name in ("dispatch", "api"):
        paths[name].write_text(json.dumps(documents[name]))
    from tests.e2e.product_onboarding.product_d01_release import materialize

    documents["release"] = materialize(paths["dispatch"], paths["api"], paths["release"])
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "pass"
    paths["inputs"] = tmp_path / "product-b01-inputs.json"
    paths["packet"] = tmp_path / "product-b01.json"
    grades = product_d01_cases.evaluate(inputs, documents["dispatch"], packet, documents["release"])
    grades["input_issues"] = []
    paths["grades"] = tmp_path / "grades.json"
    paths["grades"].write_text(json.dumps(grades))
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", documents["dispatch"]["workflow_revision"])
    return paths, documents, Store()


def stage(selected):
    paths, _, store = selected
    return product_automatic_identity.stage(
        store, *(paths[name] for name in ("inputs", "dispatch", "packet", "release", "api", "cli", "grades"))
    )


def test_verified_cleanup_qualifies_original_release_for_held_attempt(selected):
    _, documents, store = selected
    identity = stage(selected)
    assert identity["run_id"] == 42 and identity["run_attempt"] == 1
    assert identity["resource_id"] == documents["api"]["native"]["target"]["resource_id"]
    with pytest.raises(ports.PortError, match="completed original"):
        product_automatic_identity.require_qualified(store, "product-42-1", identity["source_revision"])
    assert stage(selected) == identity
    assert product_automatic_identity.complete(store, "product-42-1", identity["source_revision"]) == identity
    assert product_automatic_identity.complete(store, "product-42-1", identity["source_revision"]) == identity
    assert product_automatic_identity.require_qualified(
        store, "product-42-1", identity["source_revision"], documents["api"]["native"]
    ) == identity
    new_observation = deepcopy(documents["api"]["native"])
    new_observation["observation_ref"] = "runtime:sha256:" + "b" * 64
    assert product_automatic_identity.require_qualified(
        store, "product-42-1", identity["source_revision"], new_observation
    ) == identity


@pytest.mark.parametrize("change", ["run", "attempt", "workflow", "source", "target", "receipt", "cli", "grades", "cleanup"])
def test_stale_wrong_target_or_incomplete_automatic_attempt_refuses(selected, monkeypatch, change):
    paths, documents, store = selected
    if change == "run":
        monkeypatch.setenv("GITHUB_RUN_ID", "43")
    elif change == "attempt":
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    elif change == "workflow":
        monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "0" * 40)
    elif change == "source":
        documents["api"]["native"]["release_artifact"]["source_revision"] = "0" * 40
        paths["api"].write_text(json.dumps(documents["api"]))
    elif change == "target":
        documents["api"]["native"]["target"]["resource_id"] = "other-cluster/adp-gateway"
        paths["api"].write_text(json.dumps(documents["api"]))
    elif change == "receipt":
        documents["release"]["deployment"]["receipt_ref"] = "s3://private/foreign?versionId=one"
        paths["release"].write_text(json.dumps(documents["release"]))
    elif change == "cli":
        documents["cli"]["workflow_run_id"] = "product-43-1"
        paths["cli"].write_text(json.dumps(documents["cli"]))
    elif change == "grades":
        grades = json.loads(paths["grades"].read_text())
        grades["cases"]["PR01-C02"]["status"] = "FAILED"
        paths["grades"].write_text(json.dumps(grades))
    elif change == "cleanup":
        identity = stage(selected)
        with pytest.raises(ports.PortError, match="completed original"):
            product_automatic_identity.require_qualified(store, "product-42-1", identity["source_revision"])
        return
    with pytest.raises((ports.PortError, ValueError)):
        stage(selected)
    assert store.values == {}


@pytest.mark.parametrize("change", ["run", "attempt", "workflow", "source", "target", "receipt", "digest"])
def test_held_handoff_rejects_changed_automatic_release(selected, monkeypatch, change):
    _, documents, store = selected
    identity = stage(selected)
    product_automatic_identity.complete(store, "product-42-1", identity["source_revision"])
    current = deepcopy(documents["api"]["native"])
    if change == "run":
        monkeypatch.setenv("GITHUB_RUN_ID", "43")
    elif change == "attempt":
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    elif change == "workflow":
        monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "0" * 40)
    elif change == "source":
        current["release_artifact"]["source_revision"] = "0" * 40
    elif change == "target":
        current["release_artifact"]["resource_id"] = "other-cluster/adp-gateway"
        current["target"]["resource_id"] = "other-cluster/adp-gateway"
    elif change == "receipt":
        current["release_ref"] = "s3://private/foreign?versionId=one"
    else:
        current["release_artifact"]["image_digest"] = "sha256:" + "0" * 64
    with pytest.raises((ports.PortError, ValueError)):
        product_automatic_identity.require_qualified(store, "product-42-1", identity["source_revision"], current)


@pytest.mark.parametrize("field", ["run_id", "run_attempt", "source_revision", "workflow_revision", "resource_id", "repository_id", "image_digest", "release_ref", "release_hash"])
def test_recovery_preflight_refuses_corrupted_private_binding_before_new_fixture(selected, field):
    _, _, store = selected
    identity = stage(selected)
    key = product_automatic_identity.identity_key("product-42-1")
    store.values[key]["identity"].pop(field)
    with pytest.raises(ports.PortError):
        product_automatic_identity.complete(store, "product-42-1", identity["source_revision"])
    with pytest.raises(ports.PortError):
        product_automatic_identity.require_qualified(store, "product-42-1", identity["source_revision"])

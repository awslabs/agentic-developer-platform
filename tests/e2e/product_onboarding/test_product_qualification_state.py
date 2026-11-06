"""A retried rollback cannot substitute a release or discard recovery attempts."""

import copy

import pytest

from tests.e2e.cli_uplift.statestore import StateStoreError
from tests.e2e.product_onboarding.product_qualification_state import (
    RetainedQualification,
)


@pytest.fixture
def attempt():
    candidate = {
        "run_id": 42,
        "run_attempt": 1,
        "source_revision": "a" * 40,
        "workflow_revision": "b" * 40,
        "image_digest": "sha256:" + "c" * 64,
        "account_id": "000000000000",
        "region": "us-east-1",
        "resource_id": "example-cluster/adp-gateway",
    }
    baseline = {
        **candidate,
        "source_revision": "d" * 40,
        "image_digest": "sha256:" + "e" * 64,
    }

    class Store:
        def __init__(self):
            self.values = {}

        def _get(self, key):
            return copy.deepcopy(self.values.get(key))

        def _put(self, key, value):
            self.values[key] = copy.deepcopy(value)

    store = Store()
    binding = RetainedQualification(store, candidate, baseline)
    report = {
        "candidate": candidate,
        "baseline": baseline,
        "owned": None,
        "attempts": [],
    }
    return binding, report, store, candidate, baseline


def test_same_run_retains_owned_fixture_and_retries_without_erasing_history(attempt):
    binding, report, store, candidate, baseline = attempt
    assert binding.load() is None
    binding.retain(report)
    report["owned"] = {"run_id": "product-42-1", "product_id": "owned-product"}
    report["attempts"].append({"stage": "created"})
    binding.retain(report)
    assert RetainedQualification(store, candidate, baseline).load() == report
    report["attempts"].append({"stage": "candidate_restore_started"})
    binding.retain(report)
    assert binding.load() == report


@pytest.mark.parametrize(
    "change", ("run", "attempt", "source", "workflow", "target", "baseline")
)
def test_foreign_release_cannot_adopt_owned_fixture(attempt, change):
    binding, report, store, candidate, baseline = attempt
    binding.retain(report)
    if change == "run":
        candidate["run_id"] = 43
    elif change == "attempt":
        candidate["run_attempt"] = 2
    elif change == "source":
        candidate["source_revision"] = "0" * 40
    elif change == "workflow":
        candidate["workflow_revision"] = "0" * 40
    elif change == "target":
        candidate["resource_id"] = "other-cluster/adp-gateway"
    elif change == "baseline":
        baseline["image_digest"] = "sha256:" + "0" * 64
    foreign = RetainedQualification(store, candidate, baseline)
    if change in ("run", "attempt"):
        assert foreign.load() is None
    else:
        with pytest.raises(StateStoreError, match="another release"):
            foreign.load()


def test_retry_cannot_discard_attempts_or_change_fixture(attempt):
    binding, report, _, _, _ = attempt
    report["owned"] = {"run_id": "product-42-1", "product_id": "owned-product"}
    report["attempts"] = [{"stage": "created"}, {"stage": "quiesced"}]
    binding.retain(report)
    with pytest.raises(StateStoreError, match="earlier attempt"):
        binding.retain({**report, "attempts": [{"stage": "created"}]})
    with pytest.raises(StateStoreError, match="fixture ownership"):
        binding.retain(
            {**report, "owned": {"run_id": "product-42-1", "product_id": "other"}}
        )


def test_held_evaluator_has_separate_original_run_binding(attempt):
    from tests.e2e.product_onboarding.product_qualification_state import held_evaluation

    binding, _, store, _, _ = attempt
    original = {
        "workflow_run_id": binding.binding["run_id"],
        "evaluation_id": "adp-e2e-20261021-000000-abcdef",
    }
    assert held_evaluation(store, original["workflow_run_id"], original) == original
    assert held_evaluation(store, original["workflow_run_id"]) == original
    with pytest.raises(StateStoreError, match="another evaluation"):
        held_evaluation(
            store,
            original["workflow_run_id"],
            {**original, "evaluation_id": "adp-e2e-20261021-000001-abcdef"},
        )
    assert held_evaluation(store, original["workflow_run_id"]) == original


def test_held_candidate_release_is_private_immutable_and_owned(attempt):
    from tests.e2e.product_onboarding.product_qualification_state import (
        held_evaluation,
        held_release,
    )

    binding, _, store, candidate, _ = attempt
    run_id = binding.binding["run_id"]
    original = {
        "workflow_run_id": run_id,
        "evaluation_id": "adp-e2e-20261021-000000-abcdef",
        "expected_revision": candidate["source_revision"],
    }
    held_evaluation(store, run_id, original)
    release = {
        **candidate,
        "repository_id": 42,
        "migration_head": "schema-head",
    }
    runtime = {
        "actual_revision": candidate["source_revision"],
        "image_digest": candidate["image_digest"],
        "migration_head": "schema-head",
    }
    target = {
        key: release[key]
        for key in ("account_id", "region", "resource_id", "repository_id")
    }
    native = {
        "source_revision": candidate["source_revision"],
        "release_artifact": release,
        "runtime_receipt": runtime,
        "target": target,
        "release_ref": "s3://private/immutable?versionId=one",
        "observation_ref": "runtime:sha256:" + "a" * 64,
    }
    stored = held_release(store, run_id, native)
    assert stored["evaluation"] == original
    assert stored["runtime_receipt"]["migration_head"] == "schema-head"
    assert held_release(store, run_id) == stored
    assert held_release(store, run_id, native) == stored
    with pytest.raises(StateStoreError, match="another observation"):
        held_release(
            store, run_id, {**native, "observation_ref": "runtime:sha256:" + "b" * 64}
        )
    assert held_release(store, run_id) == stored


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "source_revision",
        "image_digest",
        "migration_head",
        "account_id",
        "repository_id",
    ],
)
def test_foreign_or_incomplete_candidate_proof_cannot_bind_held_product(attempt, field):
    from tests.e2e.product_onboarding.product_qualification_state import (
        held_evaluation,
        held_release,
    )

    binding, _, store, candidate, _ = attempt
    run_id = binding.binding["run_id"]
    held_evaluation(
        store,
        run_id,
        {
            "workflow_run_id": run_id,
            "evaluation_id": "adp-e2e-20261021-000000-abcdef",
            "expected_revision": candidate["source_revision"],
        },
    )
    release = {**candidate, "repository_id": 42, "migration_head": "head"}
    runtime = {
        "actual_revision": candidate["source_revision"],
        "image_digest": candidate["image_digest"],
        "migration_head": "head",
    }
    target = {
        key: release[key]
        for key in ("account_id", "region", "resource_id", "repository_id")
    }
    release[field] = "foreign"
    with pytest.raises(StateStoreError, match="differs"):
        held_release(
            store,
            run_id,
            {
                "source_revision": candidate["source_revision"],
                "release_artifact": release,
                "runtime_receipt": runtime,
                "target": target,
                "release_ref": "s3://private/immutable?versionId=one",
                "observation_ref": "runtime:sha256:" + "a" * 64,
            },
        )
    assert held_release(store, run_id) is None

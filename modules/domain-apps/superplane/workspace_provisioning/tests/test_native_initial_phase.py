"""Native selection accepts the original recipe without rewriting its approval."""

import copy

import pytest

from workspace_provisioning.artifacts import (
    canonical,
    initial_execution_steps,
    native_lifecycle_phase,
)
from workspace_provisioning.runtime_config import LifecycleRefused


def initial_parameters():
    parameters = {
        "runtime_config_sha256": "a" * 64,
        "plan_revision": "b" * 64,
        "lifecycle_request": canonical({"mode": "existing-account-managed"}),
    }
    parameters["execution_steps"] = initial_execution_steps(parameters)
    return parameters


def test_initial_phase_preserves_exact_approved_parameters():
    parameters = initial_parameters()
    original = copy.deepcopy(parameters)
    assert native_lifecycle_phase(parameters) == "prepare-infrastructure"
    assert parameters == original


@pytest.mark.parametrize(
    "changed",
    [
        {"execution_steps": "[]"},
        {"execution_steps": "apply-infrastructure"},
        {"lifecycle_artifact_id": "artifact"},
        {"lifecycle_source_operation_id": "source"},
        {"lifecycle_request": "{}"},
        {"lifecycle_request": "[]"},
        {"lifecycle_request": "invalid"},
        {"lifecycle_request": canonical({"mode": "new-account-managed"})},
        {"lifecycle_request": canonical({"mode": "bring-existing-cluster"})},
        {"plan_revision": "changed"},
        {"runtime_config_sha256": "changed"},
    ],
)
def test_incomplete_or_continuation_recipe_cannot_inherit_initial_phase(changed):
    with pytest.raises(LifecycleRefused):
        native_lifecycle_phase({**initial_parameters(), **changed})


def test_controller_and_explicit_continuation_selection_are_unchanged():
    assert native_lifecycle_phase({}) is None
    assert (
        native_lifecycle_phase({"lifecycle_phase": "bootstrap-workspace"})
        == "bootstrap-workspace"
    )

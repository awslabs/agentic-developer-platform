from copy import deepcopy

import pytest

from src.agentauth.task_repository_policy import freeze_repository, require_current_repository

BINDING = {"provider": "github", "connection_id": "installation:123", "repository_id": "456", "repository": "org/repo", "base_branch": "main"}


def test_selection_is_explicit_and_snapshot_is_independent_of_policy():
    policy = {"repositories": {"application": deepcopy(BINDING)}}
    assert freeze_repository({}, policy) is None
    frozen = freeze_repository({"repository_binding": "application"}, policy)
    assert frozen == {"alias": "application", "binding": BINDING}
    require_current_repository(frozen, policy)
    policy["repositories"]["application"]["repository_id"] = "789"
    with pytest.raises(ValueError):
        require_current_repository(frozen, policy)
    assert frozen["binding"]["repository_id"] == "456"


@pytest.mark.parametrize("selector", ["unknown", "https://github.com/org/repo", BINDING, ["application"]])
def test_task_cannot_supply_repository_authority(selector):
    with pytest.raises(ValueError):
        freeze_repository({"repository_binding": selector}, {"repositories": {"application": BINDING}})


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_branch": "../main"},
        {"base_branch": "main.lock"},
        {"base_branch": "refs//main"},
        {"base_branch": "main@{1}"},
        {"repository": "org/../repo"},
        {"repository_id": "*"},
        {"token": "forbidden"},
        {"provider": "arbitrary"},
    ],
)
def test_invalid_repository_binding_cannot_be_frozen(overrides):
    with pytest.raises(ValueError):
        freeze_repository({"repository_binding": "application"}, {"repositories": {"application": {**BINDING, **overrides}}})


def test_validation_checks_are_frozen_from_policy_and_changes_revoke_them():
    check = {"name": "unit", "image": "sha256:" + "a" * 64, "argv": ["python", "-m", "pytest"]}
    policy = {"repositories": {"application": {**BINDING, "validation_checks": [check]}}}
    frozen = freeze_repository({"repository_binding": "application", "validation_checks": [{"argv": ["untrusted"]}]}, policy)
    admitted = frozen["binding"]["validation_checks"][0]
    assert admitted["argv"] == ["python", "-m", "pytest"]
    assert admitted["max_output_bytes"] == 16384
    require_current_repository(frozen, policy)
    check["argv"] = ["different"]
    with pytest.raises(ValueError):
        require_current_repository(frozen, policy)
    assert admitted["argv"] == ["python", "-m", "pytest"]


@pytest.mark.parametrize(
    "override",
    [
        {"image": "python:latest"},
        {"cpus": True},
        {"timeout_seconds": 0},
        {"max_output_bytes": 32768},
        {"argv": ["a\x00b"]},
        {"argv": []},
        {"argv": ["a" * 4096] * 5},
        {"volumes": ["/:/host"]},
    ],
)
def test_validation_policy_rejects_unbounded_or_untrusted_execution_fields(override):
    check = {"name": "unit", "image": "sha256:" + "a" * 64, "argv": ["true"], **override}
    with pytest.raises(ValueError):
        freeze_repository({"repository_binding": "application"}, {"repositories": {"application": {**BINDING, "validation_checks": [check]}}})


def test_duplicate_named_checks_are_rejected():
    check = {"name": "unit", "image": "sha256:" + "a" * 64, "argv": ["true"]}
    with pytest.raises(ValueError):
        freeze_repository({"repository_binding": "application"}, {"repositories": {"application": {**BINDING, "validation_checks": [check, check]}}})


@pytest.mark.parametrize("image", [
    "registry.example/checks@sha256:" + "a" * 64,
    "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp/checks@sha256:" + "b" * 64,
])
def test_registry_digest_checks_are_frozen_without_rewriting_identity(image):
    check = {"name": "unit", "image": image, "argv": ["/checks/unit"]}
    frozen = freeze_repository({"repository_binding": "application"}, {
        "repositories": {"application": {**BINDING, "validation_checks": [check]}},
    })
    assert frozen["binding"]["validation_checks"][0]["image"] == image


@pytest.mark.parametrize("image", [
    "registry.example/checks:latest", "registry.example/checks:tag@sha256:" + "a" * 64,
    "https://registry.example/checks@sha256:" + "a" * 64,
    "user:password@registry.example/checks@sha256:" + "a" * 64,
    "registry.example/../checks@sha256:" + "a" * 64,
])
def test_mutable_or_credential_bearing_check_images_are_refused(image):
    check = {"name": "unit", "image": image, "argv": ["/checks/unit"]}
    with pytest.raises(ValueError):
        freeze_repository({"repository_binding": "application"}, {
            "repositories": {"application": {**BINDING, "validation_checks": [check]}},
        })

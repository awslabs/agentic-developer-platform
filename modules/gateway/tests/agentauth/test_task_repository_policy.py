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

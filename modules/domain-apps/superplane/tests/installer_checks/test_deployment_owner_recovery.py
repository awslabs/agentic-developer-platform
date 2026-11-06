import copy

import pytest

from installation.config import Refusal
from installation.deployment_owner_recovery import check_receipt, prepare
from .test_deployment_recovery import recovery as historical_recovery

__all__ = ["historical_recovery"]


@pytest.fixture
def recovery(historical_recovery):
    installer, desired, old = historical_recovery
    old["metadata"]["managedFields"][0].update(
        manager="superplane-installer", operation="Apply"
    )
    return installer, desired, old


def test_current_review_pins_prior_spec_and_original_desired(recovery):
    installer, desired, old = recovery
    review, apply = prepare(installer.receipt, desired, old)
    assert review["changed_existing_images"] == [
        old["spec"]["template"]["spec"]["containers"][0]["name"]
    ]
    assert apply["metadata"]["uid"] == old["metadata"]["uid"]
    assert apply["metadata"]["resourceVersion"] == old["metadata"]["resourceVersion"]
    assert apply["spec"] == desired["spec"]


def test_new_listener_is_not_an_existing_image_conflict(recovery):
    installer, desired, old = recovery
    listener = {
        "name": "recovery-transport",
        "image": "example.test/api@sha256:" + "b" * 64,
    }
    desired["spec"]["template"]["spec"]["containers"].append(listener)
    review, apply = prepare(installer.receipt, desired, old)
    assert review["added_containers"] == ["recovery-transport"]
    assert "recovery-transport" not in review["changed_existing_images"]
    assert apply["spec"] == desired["spec"]


@pytest.mark.parametrize(
    "change", ["uid", "owner", "manager", "co-owner", "unselected", "remove-container"]
)
def test_recovery_rejects_unreviewable_target(recovery, change):
    installer, desired, old = recovery
    if change == "uid":
        old["metadata"]["uid"] = "replacement"
    elif change == "owner":
        old["metadata"]["labels"] = {}
    elif change == "manager":
        old["metadata"]["managedFields"][0]["manager"] = "another-owner"
    elif change == "co-owner":
        old["metadata"]["managedFields"].append(
            copy.deepcopy(old["metadata"]["managedFields"][0])
        )
    elif change == "unselected":
        installer.receipt["environment"]["deployment_image_recovery"] = []
    else:
        old["spec"]["template"]["spec"]["containers"].append(
            {"name": "other", "image": "other"}
        )
    with pytest.raises(Refusal):
        prepare(installer.receipt, desired, old)


@pytest.mark.parametrize(
    "change",
    [
        "running",
        "stage",
        "lock",
        "bootstrap",
        "token",
        "route",
        "release",
        "confirmation",
    ],
)
def test_recovery_requires_exact_stopped_stage(recovery, change):
    installer, _, _ = recovery
    receipt = installer.receipt
    receipt.update(
        status="recovery-required",
        stage="rollout",
        bootstrap={"token_secret_removed": True},
        public_route_enabled=False,
    )
    stopped = receipt["run_id"]
    check_receipt(receipt, stopped)
    if change == "running":
        receipt["status"] = "preflight-passed"
    elif change == "stage":
        receipt["stage"] = "migration"
    elif change == "lock":
        receipt.pop("remote_lock")
    elif change == "bootstrap":
        receipt["completed"].remove("bootstrap")
    elif change == "token":
        receipt["bootstrap"]["token_secret_removed"] = False
    elif change == "route":
        receipt["public_route_enabled"] = True
    elif change == "release":
        receipt["release_id"] = "different"
    else:
        stopped = "another-run"
    with pytest.raises(Refusal):
        check_receipt(receipt, stopped)

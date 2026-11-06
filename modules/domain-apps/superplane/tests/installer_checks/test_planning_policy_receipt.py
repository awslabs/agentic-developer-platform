import copy
import hashlib

import pytest

from installation.config import LABEL, digest
from installation.planning_policy_receipt import extension_settings


@pytest.fixture
def case():
    env = dict(
        account_id="111122223333",
        region="us-east-1",
        cluster="management",
        namespace="superplane",
        org_id="org",
        adp_org_id="tenant",
        paid_worker={
            "lifecycle_policy_configmap": "original",
            "lifecycle_policy_sha256": "a" * 64,
            "lifecycle_state_claim": "retained",
        },
    )
    original = {"installation_id": "installation", "environment": env}
    env["lifecycle_foundations"] = {"policy_json": "original"}
    after = copy.deepcopy(env)
    raw = '{"fixture":true}'
    after["lifecycle_foundations"]["policy_json"] = raw
    after["paid_worker"].update(
        lifecycle_policy_configmap="extension",
        lifecycle_policy_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    image = "registry/worker@sha256:" + "b" * 64
    release = {"images": {"superplane-paid-worker": image.rsplit("@", 1)[1]}}
    plan = {
        "installation_id": "installation",
        "kind": "workspace-policy-extension-v1",
        "workspace_id": "workspace",
        "release_sha256": digest(release),
        "environment_sha256": digest(env),
        "next_environment": after,
        "new_configmap": {
            "immutable": True,
            "metadata": {
                "name": "extension",
                "namespace": "superplane",
                "labels": {LABEL: "installation"},
            },
            "data": {"lifecycle.json": raw},
        },
    }
    receipt = {
        "status": "workspace-policy-extension-complete",
        "installation_id": "installation",
        "environment": env,
        "release_lock": release,
        "release_id": digest(release),
        "reviewed_plan_sha256": digest(plan),
        "workspace_policy_extension": {
            "state": "observed-paused",
            "plan_sha256": digest(plan),
            "configmap_uid": "policy-uid",
            "api": {"state": "observed"},
            "worker": {"state": "observed"},
        },
    }
    return {"receipt": receipt, "plan": plan}, original, image


def test_completed_extension_uses_saved_next_policy_not_old_environment(case):
    bundle, original, image = case
    settings, uid = extension_settings(bundle, original, image, "workspace")
    assert settings["lifecycle_policy_configmap"] == "extension" and uid == "policy-uid"
    assert (
        bundle["receipt"]["environment"]["paid_worker"]["lifecycle_policy_configmap"]
        == "original"
    )


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "locked",
        "wrong-plan",
        "wrong-worker",
        "foreign-workspace",
        "changed-storage",
        "foreign-owner",
        "mutable",
        "unobserved",
    ],
)
def test_incomplete_or_unrelated_extension_is_refused(case, change):
    bundle, original, image = case
    receipt, plan = bundle["receipt"], bundle["plan"]
    if change == "pending":
        receipt["status"] = "pending"
    elif change == "locked":
        receipt["remote_lock"] = {"holder": "other"}
    elif change == "wrong-plan":
        receipt["reviewed_plan_sha256"] = "c" * 64
    elif change == "wrong-worker":
        image = "registry/worker@sha256:" + "d" * 64
    elif change == "foreign-workspace":
        plan["workspace_id"] = "other"
    elif change == "changed-storage":
        plan["next_environment"]["paid_worker"]["lifecycle_state_claim"] = "other"
    elif change == "foreign-owner":
        plan["new_configmap"]["metadata"]["labels"][LABEL] = "other"
    elif change == "mutable":
        plan["new_configmap"]["immutable"] = False
    else:
        receipt["workspace_policy_extension"]["worker"]["state"] = "pending"
    if change in {"foreign-workspace", "changed-storage", "foreign-owner", "mutable"}:
        receipt["reviewed_plan_sha256"] = digest(plan)
        receipt["workspace_policy_extension"]["plan_sha256"] = digest(plan)
    with pytest.raises(ValueError):
        extension_settings(bundle, original, image, "workspace")

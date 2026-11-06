"""Bind a completed workspace policy extension to its saved plan and source pod."""

import copy
import hashlib

from .config import LABEL, digest
from .provider_authority import require


def extension_settings(bundle, original, image, workspace_id):
    receipt, plan = bundle["receipt"], bundle["plan"]
    extension = receipt.get("workspace_policy_extension", {})
    require(
        receipt.get("status") == "workspace-policy-extension-complete"
        and not receipt.get("remote_lock")
        and not receipt.get("lock_attempt")
        and receipt.get("installation_id") == original["installation_id"]
        and plan.get("installation_id") == original["installation_id"]
        and plan.get("kind") == "workspace-policy-extension-v1"
        and plan.get("workspace_id") == workspace_id
        and receipt.get("reviewed_plan_sha256") == digest(plan)
        and extension.get("plan_sha256") == digest(plan)
        and extension.get("state") == "observed-paused"
        and all(
            extension.get(k, {}).get("state") == "observed" for k in ("api", "worker")
        )
        and receipt.get("release_id") == digest(receipt.get("release_lock"))
        and plan.get("release_sha256") == receipt["release_id"]
        and receipt["release_lock"].get("images", {}).get("superplane-paid-worker")
        == image.rsplit("@", 1)[-1]
        and plan.get("environment_sha256") == digest(receipt.get("environment"))
    )
    before, after = receipt["environment"], plan["next_environment"]
    require(
        all(
            before.get(k) == original["environment"][k]
            for k in (
                "account_id",
                "region",
                "cluster",
                "namespace",
                "org_id",
                "adp_org_id",
            )
        )
    )
    expected = copy.deepcopy(before)
    selected = after["paid_worker"]
    for key in ("lifecycle_policy_configmap", "lifecycle_policy_sha256"):
        expected["paid_worker"][key] = selected[key]
    expected["lifecycle_foundations"]["policy_json"] = plan["new_configmap"]["data"][
        "lifecycle.json"
    ]
    require(expected == after)
    policy = plan["new_configmap"]
    require(
        policy.get("immutable") is True
        and policy["metadata"]["name"] == selected["lifecycle_policy_configmap"]
        and policy["metadata"]["namespace"] == after["namespace"]
        and policy["metadata"].get("labels", {}).get(LABEL)
        == original["installation_id"]
        and hashlib.sha256(policy["data"]["lifecycle.json"].encode()).hexdigest()
        == selected["lifecycle_policy_sha256"]
        and isinstance(extension.get("configmap_uid"), str)
        and bool(extension["configmap_uid"])
    )
    return selected, extension["configmap_uid"]

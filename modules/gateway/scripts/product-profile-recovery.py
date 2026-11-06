#!/usr/bin/env python3
"""Quiesce Product writes before an approved compatible Gateway rollback."""

import argparse
import copy
import json
import re
import subprocess


def command(args, payload=None):
    result = subprocess.run(
        ["kubectl", *args], input=payload, text=True, capture_output=True,
        timeout=1230, check=False,
    )
    if result.returncode:
        raise RuntimeError("Gateway recovery Kubernetes operation failed")
    return result.stdout


def require(condition, message):
    if not condition:
        raise ValueError(message)


def prepare(namespace, baseline_image, baseline_source, rollback_source, approved_source, candidate_manifest, *, run=command):
    require(re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace), "Invalid recovery namespace")
    require(all(re.fullmatch(r"[0-9a-f]{40}", str(sha)) for sha in
                (baseline_source, rollback_source, approved_source)) and rollback_source == approved_source,
            "Rollback requires an exact approved compatible source")
    require(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", baseline_image),
            "Rollback baseline image must be digest pinned")
    candidate = json.loads(run(["-n", namespace, "create", "--dry-run=client", "--validate=false",
                                "-f", candidate_manifest, "-o", "json"]))
    candidates = [item for item in candidate["spec"]["template"]["spec"]["containers"]
                  if item.get("name") == "bedrockgateway"]
    require(len(candidates) == 1, "Rollback candidate has ambiguous Gateway container")
    candidate_flags = {item.get("name"): item.get("value") for item in candidates[0].get("env", [])}
    candidate_env = candidates[0].get("env", [])
    require(candidate_flags.get("BG_PRODUCT_PROFILE_ENABLED") == "false"
            and candidate_flags.get("BG_PRODUCT_ONBOARDING_ENABLED") == "false"
            and sum(item.get("name") == "BG_PRODUCT_PROFILE_ENABLED" for item in candidate_env) == 1
            and sum(item.get("name") == "BG_PRODUCT_ONBOARDING_ENABLED" for item in candidate_env) == 1,
            "Rollback candidate must keep Product admissions disabled")
    query = ["-n", namespace, "get", "deployment", "bedrockgateway", "-o", "json"]
    current = json.loads(run(query))
    metadata = current.get("metadata", {})
    require(current.get("kind") == "Deployment" and metadata.get("name") == "bedrockgateway"
            and metadata.get("namespace") == namespace and not metadata.get("deletionTimestamp"),
            "Rollback Gateway identity differs")
    require(all(isinstance(metadata.get(field), str) and metadata[field] for field in ("uid", "resourceVersion")),
            "Rollback Gateway concurrency binding missing")
    containers = [container for container in current["spec"]["template"]["spec"]["containers"]
                  if container.get("name") == "bedrockgateway"]
    require(len(containers) == 1 and containers[0].get("image") == baseline_image,
            "Rollback current image differs from verified release")
    previous = {item["name"]: item["value"] for item in containers[0].get("env", [])
                if item.get("name") in {"BG_PRODUCT_PROFILE_ENABLED", "BG_PRODUCT_ONBOARDING_ENABLED"}}
    require(len(previous) == 2 and sum(item.get("name") == "BG_PRODUCT_PROFILE_ENABLED"
                                       for item in containers[0]["env"]) == 1
            and sum(item.get("name") == "BG_PRODUCT_ONBOARDING_ENABLED"
                    for item in containers[0]["env"]) == 1,
            "Rollback baseline lacks unambiguous Product admission controls")
    if previous != {"BG_PRODUCT_PROFILE_ENABLED": "false", "BG_PRODUCT_ONBOARDING_ENABLED": "false"}:
        desired = copy.deepcopy(current)
        desired.pop("status", None)
        gateway = next(item for item in desired["spec"]["template"]["spec"]["containers"]
                       if item.get("name") == "bedrockgateway")
        for item in gateway["env"]:
            if item.get("name") in previous:
                item["value"] = "false"
        endpoint = f"/apis/apps/v1/namespaces/{namespace}/deployments/bedrockgateway"
        run(["-n", namespace, "replace", "--raw", endpoint, "-f", "-"], json.dumps(desired))
    run(["-n", namespace, "rollout", "status", "deployment/bedrockgateway", "--timeout=1200s"])
    observed = json.loads(run(query))
    pod = next((item for item in observed["spec"]["template"]["spec"]["containers"]
                if item.get("name") == "bedrockgateway"), {})
    flags = {item.get("name"): item.get("value") for item in pod.get("env", [])}
    status = observed.get("status", {})
    replicas = observed["spec"].get("replicas")
    require(observed["metadata"].get("uid") == metadata["uid"] and pod.get("image") == baseline_image
            and flags.get("BG_PRODUCT_PROFILE_ENABLED") == "false"
            and flags.get("BG_PRODUCT_ONBOARDING_ENABLED") == "false"
            and type(replicas) is int and replicas > 0 and status.get("readyReplicas") == replicas
            and status.get("observedGeneration") == observed["metadata"].get("generation"),
            "Rollback admissions are not disabled on ready Gateway replicas")
    return {"baseline_source_revision": baseline_source,
            "baseline_image_digest": baseline_image.rsplit("@", 1)[1],
            "admissions_disabled_before_rollback": True,
            "migration_mode": "retained", "data_readback": "pending"}


def switch_compatible(namespace, current_image, desired_image, *, restore, quiesce=False, run=command):
    require(re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace),
            "Invalid recovery namespace")
    for image in (current_image, desired_image):
        require(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", str(image)),
                "Recovery requires digest-pinned images")
    require(current_image.split("@", 1)[0] == desired_image.split("@", 1)[0]
            and type(restore) is bool and type(quiesce) is bool
            and ((quiesce and not restore and current_image == desired_image)
                 or (not quiesce and current_image != desired_image)),
            "Recovery images must be qualified artifacts in one reviewed repository")
    query = ["-n", namespace, "get", "deployment", "bedrockgateway", "-o", "json"]
    current = json.loads(run(query))
    metadata = current.get("metadata", {})
    require(current.get("kind") == "Deployment" and metadata.get("name") == "bedrockgateway"
            and metadata.get("namespace") == namespace and not metadata.get("deletionTimestamp")
            and all(isinstance(metadata.get(field), str) and metadata[field]
                    for field in ("uid", "resourceVersion")),
            "Recovery Gateway or concurrency binding differs")
    containers = [container for container in current["spec"]["template"]["spec"]["containers"]
                  if container.get("name") == "bedrockgateway"]
    require(len(containers) == 1, "Recovery Gateway container is ambiguous")
    flags = [item for item in containers[0].get("env", [])
             if item.get("name") in ("BG_PRODUCT_PROFILE_ENABLED", "BG_PRODUCT_ONBOARDING_ENABLED")]
    flag_values = {item["name"]: item.get("value") for item in flags}
    require(len(flags) == 2 and set(flag_values) ==
            {"BG_PRODUCT_PROFILE_ENABLED", "BG_PRODUCT_ONBOARDING_ENABLED"}
            and flag_values["BG_PRODUCT_ONBOARDING_ENABLED"] == "false",
            "Product admissions must be unambiguous and onboarding disabled")
    require(containers[0].get("image") in (current_image, desired_image),
            "Recovery observed an unqualified Gateway image")
    desired_flag = "true" if restore else "false"
    expected_flag = ("true", "false") if quiesce or (restore and containers[0]["image"] == desired_image) else (
        desired_flag if containers[0]["image"] == desired_image else "false",)
    require(flag_values["BG_PRODUCT_PROFILE_ENABLED"] in expected_flag,
            "Product admissions differ from the qualified recovery stage")
    status, replicas = current.get("status", {}), current["spec"].get("replicas")
    require(type(replicas) is int and replicas > 0, "Recovery requires bounded replicas")
    if not restore and (containers[0]["image"] != desired_image or quiesce):
        require(status.get("readyReplicas") == replicas
                and status.get("observedGeneration") == metadata.get("generation"),
                "Recovery cannot switch to an unready baseline")
    if containers[0]["image"] != desired_image or flag_values["BG_PRODUCT_PROFILE_ENABLED"] != desired_flag:
        desired = copy.deepcopy(current)
        desired.pop("status", None)
        gateway = next(item for item in desired["spec"]["template"]["spec"]["containers"]
                       if item.get("name") == "bedrockgateway")
        gateway["image"] = desired_image
        for item in gateway["env"]:
            if item.get("name") == "BG_PRODUCT_PROFILE_ENABLED":
                item["value"] = desired_flag
        endpoint = f"/apis/apps/v1/namespaces/{namespace}/deployments/bedrockgateway"
        run(["-n", namespace, "replace", "--raw", endpoint, "-f", "-"], json.dumps(desired))
    run(["-n", namespace, "rollout", "status", "deployment/bedrockgateway", "--timeout=900s"])
    observed = json.loads(run(query))
    serving = [item for item in observed["spec"]["template"]["spec"]["containers"]
               if item.get("name") == "bedrockgateway"]
    actual_flags = {item.get("name"): item.get("value") for item in serving[0].get("env", [])} if len(serving) == 1 else {}
    actual = observed.get("status", {})
    require(observed.get("metadata", {}).get("uid") == metadata["uid"]
            and len(serving) == 1 and serving[0].get("image") == desired_image
            and actual_flags.get("BG_PRODUCT_PROFILE_ENABLED") == desired_flag
            and actual_flags.get("BG_PRODUCT_ONBOARDING_ENABLED") == "false"
            and observed["spec"].get("replicas") == replicas
            and actual.get("readyReplicas") == replicas
            and actual.get("observedGeneration") == observed["metadata"].get("generation"),
            "Recovery image or admissions were not ready after protected switch")
    return {"from_image_digest": current_image.rsplit("@", 1)[1],
            "to_image_digest": desired_image.rsplit("@", 1)[1],
            "admissions_enabled": restore, "gateway_uid": metadata["uid"],
            "ready_replicas": replicas, **({"admissions_disabled": True} if quiesce else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--baseline-image", required=True)
    parser.add_argument("--baseline-source", required=True)
    parser.add_argument("--rollback-source", required=True)
    parser.add_argument("--approved-source", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--candidate-manifest", required=True)
    args = parser.parse_args()
    result = prepare(args.namespace, args.baseline_image, args.baseline_source,
                     args.rollback_source, args.approved_source, args.candidate_manifest)
    with open(args.receipt, "w") as output:
        json.dump(result, output, sort_keys=True)


if __name__ == "__main__":
    main()

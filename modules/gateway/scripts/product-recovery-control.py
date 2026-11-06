#!/usr/bin/env python3
"""Execute a held Product switch under the reviewed dev deployment identity."""

import argparse
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("product_profile_recovery", Path(__file__).with_name("product-profile-recovery.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def execute(binding, phase, *, identity, run=None):
    if run is None:
        run = recovery.command
    source = re.compile(r"[a-f0-9]{40}\Z")
    digest = re.compile(r"sha256:[a-f0-9]{64}\Z")
    expected = {
        "run_id",
        "source_revision",
        "workflow_revision",
        "baseline_source_revision",
        "candidate_image_digest",
        "baseline_image_digest",
        "resource_id",
        "account_id",
    }
    if (
        phase not in ("quiesce", "baseline", "candidate")
        or set(binding) != expected
        or not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", binding["run_id"])
        or not all(source.fullmatch(binding[field]) for field in ("source_revision", "workflow_revision", "baseline_source_revision"))
        or not all(digest.fullmatch(binding[field]) for field in ("candidate_image_digest", "baseline_image_digest"))
        or binding["candidate_image_digest"] == binding["baseline_image_digest"]
        or binding["resource_id"] != "adp-dev-eks-cluster/adp-gateway"
        or not re.fullmatch(r"[0-9]{12}", binding["account_id"])
    ):
        raise ValueError("Protected Product switch has no exact qualified dev binding")
    run_id, attempt = binding["run_id"].split("-")[1:]
    role = os.environ.get("ADP_DEPLOY_ROLE_ARN", "")
    caller = identity()
    if (
        os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_RUN_ID") != run_id
        or os.environ.get("GITHUB_RUN_ATTEMPT") != attempt
        or os.environ.get("GITHUB_WORKFLOW_SHA") != binding["workflow_revision"]
        or not re.fullmatch(
            rf"arn:aws:iam::{binding['account_id']}:role/adp-[a-z0-9-]+-trusted-deployment",
            role,
        )
        or caller.get("Account") != binding["account_id"]
        or not str(caller.get("Arn", "")).startswith(f"arn:aws:sts::{binding['account_id']}:assumed-role/{role.rsplit('/', 1)[-1]}/")
    ):
        raise ValueError("Product switch requires the matching protected deployment run and role")
    cluster, namespace = binding["resource_id"].split("/", 1)
    current = json.loads(run(["-n", namespace, "get", "deployment", "bedrockgateway", "-o", "json"]))
    containers = [item for item in current["spec"]["template"]["spec"]["containers"] if item.get("name") == "bedrockgateway"]
    if len(containers) != 1:
        raise ValueError("Protected Product switch has no unambiguous Gateway image")
    image = containers[0].get("image", "")
    if not re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Protected Product switch has no digest-pinned Gateway image")
    repository = image.split("@", 1)[0]
    if repository != f"{binding['account_id']}.dkr.ecr.us-east-1.amazonaws.com/adp-gateway":
        raise ValueError("Protected Product switch differs from the reviewed Gateway repository")
    candidate_image = repository + "@" + binding["candidate_image_digest"]
    baseline_image = repository + "@" + binding["baseline_image_digest"]
    if phase == "quiesce":
        receipt = recovery.switch_compatible(namespace, candidate_image, candidate_image, restore=False, quiesce=True, run=run)
    elif phase == "baseline":
        receipt = recovery.switch_compatible(namespace, candidate_image, baseline_image, restore=False, run=run)
    else:
        receipt = recovery.switch_compatible(namespace, baseline_image, candidate_image, restore=True, run=run)
    return {
        "phase": phase,
        "run_id": binding["run_id"],
        "resource_id": cluster + "/" + namespace,
        "source_revision": binding["source_revision"],
        "workflow_revision": binding["workflow_revision"],
        "baseline_source_revision": binding["baseline_source_revision"],
        **receipt,
    }


def caller_identity():
    result = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--phase", required=True, choices=("quiesce", "baseline", "candidate"))
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    with open(args.binding, encoding="utf-8") as handle:
        binding = json.load(handle)
    receipt = execute(binding, args.phase, identity=caller_identity)
    with open(args.receipt, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, sort_keys=True)
    return receipt


if __name__ == "__main__":
    main()

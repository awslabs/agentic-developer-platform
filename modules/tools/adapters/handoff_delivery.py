"""Bind completed connector readback to generic workflow delivery evidence.

The protected switcher supplies this private result after fresh API/core reads.
This reporter neither mutates the target nor certifies provider acceptance. Its
three checks describe only the selected source, routes and durable transition.
"""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

WORKFLOW = ".github/workflows/tools-platform-deploy.yml"
CONNECTORS = {"atlassian", "github", "jira", "slack"}
CHECKS = ("handoff-source", "handoff-four-routes", "handoff-core-transition")
MAX_BYTES = 1024 * 1024


def require(condition):
    if not condition:
        raise ValueError("handoff_delivery_unverified")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value)
            value[key] = item
        return value

    def invalid(_):
        require(False)

    require(len(raw) <= MAX_BYTES)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def same(left, right):
    return encoded(left) == encoded(right)


def release(value):
    require(
        type(value) is dict
        and set(value)
        == {
            "contract_version",
            "source_revision",
            "image_digest",
            "group",
            "group_generation",
            "backend_generation",
        }
    )
    require(value["contract_version"] == "1.0" and value["group"] == "tool-connectors")
    require(matches(value["source_revision"], r"[0-9a-f]{40}"))
    require(matches(value["image_digest"], r"sha256:[0-9a-f]{64}"))
    require(all(type(value[key]) is int and value[key] >= 1 for key in ("group_generation", "backend_generation")))


def snapshot(value):
    require(type(value) is dict and set(value) == {"status", "release", "routes", "deployment_id"})
    require(value["status"] in {"legacy", "active"} and matches(value["deployment_id"], r"[a-z0-9]{1,32}"))
    if value["release"] is not None:
        release(value["release"])
    require(value["status"] != "active" or value["release"] is not None)
    require(type(value["routes"]) is dict and set(value["routes"]) == CONNECTORS)
    targets = set()
    for name, route in value["routes"].items():
        require(type(route) is dict and set(route) == {"backend", "target_id", "digest"})
        mode = "adapter" if value["status"] == "active" else "disabled" if name == "atlassian" else "legacy"
        require(route["backend"] == mode and matches(route["target_id"], r"[A-Za-z0-9_-]{1,128}"))
        require(matches(route["digest"], r"sha256:[0-9a-f]{64}"))
        targets.add(route["target_id"])
    require(len(targets) == 4)


def identity(env, config, inputs):
    require(type(inputs) is dict and all(type(k) is str and type(v) is str for k, v in inputs.items()))
    required = {
        "mode",
        "stage",
        "environment",
        "adapter_action",
        "adp_contract_digest",
        "adp_source_revision",
        "adp_definition_revision",
        "adp_correlation",
    }
    require(required <= set(inputs) <= required | {"manual_source_revision"})
    require(inputs["mode"] == "release" and inputs["stage"] == "tool-adapters" and inputs["environment"] == "dev")
    require(inputs["adapter_action"] in {"handoff", "rollback"} and inputs.get("manual_source_revision", "") == "")
    require(env["ADP_TOOLS_ENVIRONMENT"] == "dev")
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch")
    require(env.get("GITHUB_REF") == "refs/heads/main" and env.get("GITHUB_RUN_ATTEMPT") == "1")
    require(env.get("GITHUB_WORKFLOW_REF") == env["GITHUB_REPOSITORY"] + "/" + WORKFLOW + "@refs/heads/main")
    require(env["GITHUB_SHA"] == env["GITHUB_WORKFLOW_SHA"] == env["ADP_DEFINITION_REVISION"])
    for field in ("ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION"):
        require(matches(env[field], r"[0-9a-f]{40}"))
    require(matches(inputs["adp_definition_revision"], r"[0-9a-f]{40}"))
    for key, field in (
        ("adp_source_revision", "ADP_SOURCE_REVISION"),
        ("adp_correlation", "ADP_CORRELATION"),
        ("adp_contract_digest", "ADP_IDENTITY_CONTRACT_DIGEST"),
    ):
        require(inputs[key] == env[field])
    require(matches(env["ADP_CORRELATION"], r"orch:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"))
    require(matches(env["ADP_IDENTITY_CONTRACT_DIGEST"], r"[0-9a-f]{64}"))
    account, region = env["ADP_TOOLS_ACCOUNT_ID"], env["ADP_TOOLS_AWS_REGION"]
    require(matches(account, r"[0-9]{12}") and matches(region, r"[a-z]{2}-[a-z]+-[0-9]+"))
    role = env["ADP_TOOLS_DEPLOY_ROLE_ARN"]
    require(role == f"arn:aws:iam::{account}:role/adp-dev-tools-handoff-trusted-deployment")
    require(matches(env["ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN"], rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+"))
    require(role != env["ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN"])
    selectors = config["selectors"]
    require(selectors["account_id"] == account and selectors["region"] == region)
    gateway = env["ADP_TOOLS_GATEWAY_ID"]
    require(matches(gateway, r"[A-Za-z0-9_-]{1,128}") and selectors["gateway_id"] == gateway)
    require(config["plan"]["stage"] == inputs["environment"])
    require(all(matches(env[field], r"[1-9][0-9]{0,19}") for field in ("GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID")))
    return {
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": 1,
        "workflow_path": WORKFLOW,
        "workflow_revision": env["ADP_DEFINITION_REVISION"],
        "source_revision": env["ADP_SOURCE_REVISION"],
        "account_id": account,
        "region": region,
        "resource_kind": "agentcore-gateway",
        "resource_id": f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/{gateway}",
        "correlation": env["ADP_CORRELATION"],
    }


def documents(result, env, inputs, *, now=None):
    """Validate the switcher's result before rendering any publishable output."""
    now = time.time() if now is None else now
    require(type(result) is dict and set(result) == {"config", "state", "observed"})
    config, state, observed = result["config"], result["state"], result["observed"]
    require(type(config) is dict and set(config) == {"plan", "selectors"})
    plan = config["plan"]
    require(type(plan) is dict and set(plan) == {"api_id", "stage", "before", "after"})
    require(matches(plan["api_id"], r"[a-z0-9]{1,32}") and matches(plan["stage"], r"[A-Za-z0-9_-]{1,128}"))
    before, after = plan["before"], plan["after"]
    snapshot(before)
    snapshot(after)
    snapshot(observed)
    require(after["status"] == "active" and before["deployment_id"] != after["deployment_id"])
    require(all(before["routes"][name]["target_id"] == after["routes"][name]["target_id"] for name in CONNECTORS))
    for key in ("group_generation", "backend_generation"):
        require(after["release"][key] == (before["release"][key] if before["release"] else 0) + 1)
    if before["release"] is not None and before["status"] == "active":
        require(all(before["release"][key] == after["release"][key] for key in ("source_revision", "image_digest")))
    common = identity(env, config, inputs)
    require(after["release"]["source_revision"] == common["source_revision"])
    expected = copy.deepcopy(after)
    phase = "active"
    if inputs["adapter_action"] == "rollback":
        expected = copy.deepcopy(before)
        expected["release"] = copy.deepcopy(before["release"] or after["release"])
        for key in ("group_generation", "backend_generation"):
            expected["release"][key] = after["release"][key] + 1
        phase = "restored"
    require(same(observed, expected))
    require(type(state) is dict and set(state) == {"status", "release", "transition"})
    require(state["status"] == expected["status"] and same(state["release"], expected["release"]))
    transition = state["transition"]
    require(type(transition) is dict and transition.get("phase") == phase)
    require(same(transition["plan"], plan) and transition["plan_sha256"] == fingerprint(plan))
    require(same(transition["verified_routes"], observed["routes"]))
    require(transition["verified_deployment_id"] == observed["deployment_id"])
    require(
        same(
            transition["owner"],
            {
                "correlation": common["correlation"],
                "source_revision": common["source_revision"],
                "workflow": WORKFLOW.rsplit("/", 1)[1],
                "role_arn": env["ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN"],
                "mutation_role_arn": env["ADP_TOOLS_DEPLOY_ROLE_ARN"],
            },
        )
    )
    drain = transition["drain_until"]
    require(type(drain) in (int, float) and math.isfinite(drain) and 0 < drain <= now)
    # Only selected, validated fields leave the private result. No exported API
    # specifications, provenance headers, credentials or arbitrary metadata.
    readback = {
        "schema_version": 1,
        **common,
        "component": "tools-platform",
        "plan_sha256": fingerprint(plan),
        "api_id": plan["api_id"],
        "stage": plan["stage"],
        "phase": phase,
        "observed": observed,
    }
    reference = "handoff-readback.json#sha256:" + hashlib.sha256(encoded(readback) + b"\n").hexdigest()
    delivery = {
        "schema_version": 2,
        **common,
        "component": "tools-platform",
        "contract_digest": env["ADP_IDENTITY_CONTRACT_DIGEST"],
        "actual_revision": after["release"]["source_revision"],
        "checks": [{"id": check, "outcome": "passed", "evidence_ref": reference} for check in CHECKS],
        "cleanup": "not_required",
        "produced_at": datetime.fromtimestamp(now, UTC).isoformat(),
    }
    context = {
        "schema_version": 1,
        **common,
        "inputs": {
            key: value for key, value in inputs.items() if not key.startswith("adp_") or key == "adp_contract_digest"
        },
    }
    return {"deployment-context.json": context, "delivery.json": delivery, "handoff-readback.json": readback}


def verify_checkout(env, inputs):
    def git(*args):
        return subprocess.check_output(["git", *args], timeout=30, stderr=subprocess.DEVNULL)

    source, approved, actual = (
        env["ADP_SOURCE_REVISION"],
        inputs["adp_definition_revision"],
        env["ADP_DEFINITION_REVISION"],
    )
    require(git("rev-parse", "HEAD").decode().strip() == actual)
    git("merge-base", "--is-ancestor", source, "refs/remotes/origin/main")
    git("merge-base", "--is-ancestor", source, actual)
    git("merge-base", "--is-ancestor", approved, actual)
    require(git("show", approved + ":" + WORKFLOW) == git("show", actual + ":" + WORKFLOW))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fd = os.open(args.result, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_uid == os.getuid())
            require(info.st_nlink == 1 and 0 < info.st_size <= MAX_BYTES)
            result = decode(stream.read(MAX_BYTES + 1))
        inputs = decode(os.environ["ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON"])
        output = documents(result, os.environ, inputs)
        verify_checkout(os.environ, inputs)
        require(all(len(encoded(value)) <= 65536 for value in output.values()))
        args.output.mkdir(mode=0o700)
        for name, value in output.items():
            fd = os.open(args.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded(value) + b"\n")
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        raise SystemExit("Completed handoff evidence is unavailable or does not match this workflow") from None
    print("Completed connector transition bound to generic delivery evidence; provider acceptance remains separate")


if __name__ == "__main__":
    main()

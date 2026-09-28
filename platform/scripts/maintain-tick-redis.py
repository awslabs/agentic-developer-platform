#!/usr/bin/env python3
"""Saved-plan maintenance for only the tick's existing gateway Redis access.

No image rollout, flow acceptance, worker change, budget write or pause toggle.
Diagnosis runs again immediately before planning; all raw inputs/plans remain
private. Only a validated saved plan can be applied on the gateway release lane.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from shared_runtime_plan_guard import ACCOUNT, REGION, Refused, preserved_tick_inputs, require
import tick_redis
from tick_redis_plan_guard import TARGETS, LAMBDA, SG, INGRESS, IAM, check_plan, plan_shape, existing_policy_facts

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "modules/gateway/infra"
MODULE = GATEWAY / "modules/orchestration-tick"
TICK = tick_redis.TICK


def command(args, *, cwd=None):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("Redis maintenance command unavailable or timed out") from None
    require(result.returncode == 0, "Redis maintenance command failed; raw output suppressed")
    return result.stdout


def aws(*args):
    require(args[:2] in {("lambda", "get-function"), ("iam", "get-role-policy"), ("ssm", "get-parameter")}, "unapproved maintenance read")
    return tick_redis.diag.decode(tick_redis.diag.run(["aws", *args, "--region", REGION, "--output", "json"]))


def write_private(path, value):
    # Exclusive create: never overwrite an operator's override or stale evidence.
    with path.open("x") as stream:
        json.dump(value, stream)
    path.chmod(0o600)


def snapshot_identity(context, function):
    old, now = context["configuration"], function["Configuration"]
    require(now.get("RevisionId") == old.get("RevisionId") and bool(old.get("RevisionId")), "tick changed after Redis diagnosis")
    require(now.get("Environment") == old.get("Environment") and now.get("Role") == old.get("Role") and now.get("VpcConfig") == old.get("VpcConfig"), "tick configuration changed after Redis diagnosis")
    require(function["Code"]["ResolvedImageUri"] == context["image_uri"], "tick image changed after Redis diagnosis")


def ready(evidence):
    require(evidence.get("complete") is True, "Redis diagnosis is incomplete")
    checks = evidence["checks"]
    return (all(row["matches_gateway_store"] for row in checks["tick"]["redis_settings"].values())
            and checks["network"]["tick_to_redis_egress"] and checks["network"]["redis_from_tick_ingress"]
            and checks["iam"]["elasticache_connect_allowed"])


def run_stage(account, directory, execute):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="adp-tick-redis-") as temporary:
        scratch = Path(temporary)
        tick_redis.diag.identity(account, scratch)
        context, evidence = tick_redis.collect()
        write_private(directory / "redis-before.json", evidence)
        require(evidence.get("complete"), "Redis live diagnosis is incomplete; no plan or mutation permitted")
        if ready(evidence):
            print(json.dumps({"stage": "tick-redis", "already_configured": True, "executed": False}))
            return
        checks = evidence["checks"]
        context["required_changes"] = set()
        if not all(row["matches_gateway_store"] for row in checks["tick"]["redis_settings"].values()):
            context["required_changes"].add(LAMBDA)
        for address, present in ((SG, checks["network"]["tick_to_redis_egress"]),
                                 (INGRESS, checks["network"]["redis_from_tick_ingress"]),
                                 (IAM, checks["iam"]["elasticache_connect_allowed"])):
            if not present:
                context["required_changes"].add(address)
        config = context["configuration"]
        require(config["Role"] == f"arn:aws:iam::{ACCOUNT}:role/{TICK}-role", "tick role differs from managed identity")
        function = aws("lambda", "get-function", "--function-name", TICK)
        context["image_uri"] = function["Code"]["ResolvedImageUri"]
        require(context["image_uri"].startswith(f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-gateway@sha256:"), "tick image is not a gateway digest")
        snapshot_identity(context, function)
        role_name = config["Role"].rsplit("/", 1)[1]
        policy = aws("iam", "get-role-policy", "--role-name", role_name, "--policy-name", TICK + "-policy")["PolicyDocument"]
        wiring = json.loads(aws("ssm", "get-parameter", "--name", "/adp/dev/webhook-ingress/worker-runtime/wiring")["Parameter"]["Value"])
        overlay = preserved_tick_inputs(config, policy, wiring, revision="latest")
        variables = scratch / "retained.tfvars.json"
        write_private(variables, overlay)
        # Override only the maintenance snapshot, not permanent source defaults.
        # The normal module now derives Redis from the existing gateway module.
        # Keeping the actual running image/environment here makes concurrent
        # backend releases safe: neither a stale SHA nor absent tfvars may roll it back.
        root_override = GATEWAY / "zz_tick_redis_maintenance_override.tf.json"
        module_override = MODULE / "zz_tick_redis_maintenance_override.tf.json"
        created = []
        try:
            write_private(root_override, {"module": {"orchestration_tick": {"image_uri": context["image_uri"]}}})
            created.append(root_override)
            write_private(module_override, {"resource": {
                "aws_lambda_function": {"tick": {"environment": {"variables": config["Environment"]["Variables"] | context["expected_env"]}}},
                # The existing policy is outside this maintenance operation.
                # Even provider/document normalization must not schedule a rewrite.
                # The guard still refuses any change to this resource, and the
                # live document is compared exactly before and after apply.
                "aws_iam_role_policy": {"tick": {"policy": json.dumps(policy), "lifecycle": {"ignore_changes": ["policy"]}}}}})
            created.append(module_override)
            command(["terraform", "init", "-input=false", "-reconfigure", f"-backend-config={ROOT}/environments/dev/modules/gateway-backend.tfvars", f"-backend-config=bucket=adp-terraform-state-{ACCOUNT}"], cwd=GATEWAY)
            saved = scratch / "redis.tfplan"
            command(["terraform", "plan", "-input=false", "-lock-timeout=30s", f"-var-file={ROOT}/environments/dev/modules/gateway.tfvars", f"-var-file={variables}", f"-out={saved}", *[f"-target={name}" for name in sorted(TARGETS)]], cwd=GATEWAY)
            saved.chmod(0o600)
            plan = tick_redis.diag.decode(command(["terraform", "show", "-json", str(saved)], cwd=GATEWAY))
            shape = plan_shape(plan)
            image_facts = []
            for resource in plan.get("resource_changes", []):
                if resource.get("address") == LAMBDA:
                    before_image = (resource["change"].get("before") or {}).get("image_uri")
                    image_facts.append({"before_matches_live_reference": before_image == function["Code"].get("ImageUri"),
                                        "before_matches_resolved_digest": before_image == context["image_uri"],
                                        "live_reference_is_digest": "@sha256:" in function["Code"].get("ImageUri", "")})
            policy_facts = existing_policy_facts(plan, policy)
            write_private(directory / "redis-plan-shape.json", {"resources": shape, "lambda_image_facts": image_facts, "existing_policy_facts": policy_facts})
            print(json.dumps({"redis_plan_shape": shape, "lambda_image_facts": image_facts, "existing_policy_facts": policy_facts}))
            changes = check_plan(plan, context)
            digest = hashlib.sha256(saved.read_bytes()).hexdigest()
            summary = {"stage": "tick-redis", "account_id": ACCOUNT, "plan_sha256": digest, "changes": changes, "executed": False}
            write_private(directory / "redis-plan.json", summary)
            print(json.dumps(summary))
            if execute:
                # A different backend release or a changed store invalidates this
                # plan. Compare private values; emit none of them on a mismatch.
                fresh, current = tick_redis.collect()
                require(current.get("complete") and fresh.get("expected_env") == context["expected_env"] and fresh.get("resource_arns") == context["resource_arns"] and fresh.get("redis_sg") == context["redis_sg"], "Redis identity changed before apply")
                require(fresh.get("security_groups") == context.get("security_groups"), "network configuration changed before apply")
                snapshot_identity(context, aws("lambda", "get-function", "--function-name", TICK))
                require(aws("iam", "get-role-policy", "--role-name", role_name, "--policy-name", TICK + "-policy")["PolicyDocument"] == policy, "existing tick permissions changed before apply")
                require(hashlib.sha256(saved.read_bytes()).hexdigest() == digest, "saved Redis plan changed")
                command(["terraform", "apply", "-input=false", "-lock-timeout=30s", str(saved)], cwd=GATEWAY)
                after = None
                for attempt in range(6):
                    _, after = tick_redis.collect()
                    if after.get("complete") and ready(after):
                        break
                    if attempt < 5:
                        time.sleep(5)
                write_private(directory / "redis-after.json", after)
                require(ready(after), "Redis configuration verification incomplete")
                latest = aws("lambda", "get-function", "--function-name", TICK)
                require(latest["Code"]["ResolvedImageUri"] == context["image_uri"] and latest["Configuration"]["Environment"]["Variables"] == config["Environment"]["Variables"] | context["expected_env"], "post-apply image or environment differs")
                require(aws("iam", "get-role-policy", "--role-name", role_name, "--policy-name", TICK + "-policy")["PolicyDocument"] == policy, "existing tick permissions changed after apply")
                summary["executed"] = True
                write_private(directory / "redis-verified.json", summary)
                print(json.dumps(summary))
        finally:
            for path in created:
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.update(AWS_PAGER="", TF_IN_AUTOMATION="true")
    run_stage(args.account_id, args.evidence_directory, args.execute)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Static type and code location only; exception values may contain secrets.
        spec = importlib.util.spec_from_file_location("maintenance", Path(__file__).with_name("maintain-shared-runtime.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print(json.dumps({"redis_maintenance_stopped": module.failure_location(error), "refusal": str(error) if isinstance(error, Refused) else None}))
        raise SystemExit(1) from None

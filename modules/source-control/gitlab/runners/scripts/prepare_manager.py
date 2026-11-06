#!/usr/bin/env python3
"""Prevent a restarted manager from assigning an instance from its prior lifetime."""

import argparse
import hmac
import json
import os
import re
import signal
import time
import tomllib
from pathlib import Path


class StartupError(Exception):
    pass


def stop(signum, frame):
    # Kubernetes sends SIGTERM to this PID 1 sidecar. An explicit handler is
    # required: the default action is ignored by a namespace's init process.
    raise SystemExit(0)


def owned_group(client, name, environment, group_id):
    groups = client.describe_auto_scaling_groups(AutoScalingGroupNames=[name])["AutoScalingGroups"]
    if len(groups) != 1 or groups[0]["AutoScalingGroupName"] != name:
        raise StartupError("manager_startup_not_verified")
    group = groups[0]
    tags = {tag["Key"]: tag["Value"] for tag in group["Tags"]}
    if any(tags.get(key) != value for key, value in (
        ("Component", "gitlab-job"), ("Environment", environment), ("GroupId", group_id)
    )):
        raise StartupError("manager_startup_not_verified")
    if group["MinSize"] != 0 or group["MaxSize"] > 2 or group["DesiredCapacity"] > 2:
        raise StartupError("manager_startup_not_verified")
    for instance in group["Instances"]:
        if not re.fullmatch(r"i-[0-9a-f]{8,17}", instance["InstanceId"]):
            raise StartupError("manager_startup_not_verified")
    return group


def drain(client, environment, group_id, *, clock=time.monotonic, pause=time.sleep):
    name = f"adp-{environment}-gitlab-job-{group_id}"
    deadline = clock() + 240
    while clock() < deadline:
        group = owned_group(client, name, environment, group_id)
        if not group["Instances"] and group["DesiredCapacity"] == 0:
            return
        for instance in group["Instances"]:
            if instance.get("ProtectedFromScaleIn", False):
                client.set_instance_protection(
                    AutoScalingGroupName=name, InstanceIds=[instance["InstanceId"]],
                    ProtectedFromScaleIn=False,
                )
        if group["DesiredCapacity"]:
            client.set_desired_capacity(
                AutoScalingGroupName=name, DesiredCapacity=0, HonorCooldown=False
            )
        pause(1)
    raise StartupError("manager_startup_not_verified")


def verified_registration(config_dir, credentials_dir, group_id):
    try:
        receipt = json.loads((config_dir / ".adp-verified-runner.json").read_text())
        system_id = (config_dir / ".runner_system_id").read_text().strip()
        configuration = tomllib.loads((config_dir / "config.toml").read_text())
        runners = configuration["runners"]
        runner = runners[0]
        token = (credentials_dir / "runner-token").read_text()
        if (set(receipt) != {"runner_id", "system_id"}
                or type(receipt["runner_id"]) is not int or receipt["runner_id"] <= 0
                or not isinstance(receipt["system_id"], str)
                or not re.fullmatch(r"r_[0-9a-f]{12}", system_id)
                or receipt["system_id"] != system_id or len(runners) != 1
                or type(runner["id"]) is not int or runner["id"] != receipt["runner_id"]
                or runner["name"] != f"gitlab-group-{group_id}"
                or runner["tls-ca-file"] != "/secrets/ca.pem"
                or not isinstance(runner["token"], str)
                or not hmac.compare_digest(runner["token"], token)):
            raise StartupError("manager_startup_not_verified")
        return json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        raise StartupError("manager_startup_not_verified") from None


def acknowledge(directory, nonce, client, ssm, environment, group_id, config_dir, credentials_dir, *, drain_owned=drain):
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", nonce):
        raise StartupError("manager_startup_not_verified")
    receipt = verified_registration(config_dir, credentials_dir, group_id)
    drain_owned(client, environment, group_id)
    ssm.put_parameter(Name=f"/adp/{environment}/gitlab-runners/{group_id}/registration-receipt",
                      Value=receipt, Type="String", Overwrite=True)
    ssm.put_parameter(Name=f"/adp/{environment}/gitlab-runners/{group_id}/manager-alive",
                      Value="active", Type="String", Overwrite=True)
    (directory / "lease").touch()
    temporary = directory / "ack.next"
    temporary.write_text(nonce + "\n")
    os.replace(temporary, directory / "ack")


def serve(directory, client, ssm, environment, group_id, *, clock=time.monotonic, pause=time.sleep):
    last_nonce = None
    last_heartbeat = clock()
    while True:
        request = directory / "request"
        if request.exists():
            nonce = request.read_text().strip()
            if nonce != last_nonce:
                acknowledge(directory, nonce, client, ssm, environment, group_id,
                            Path("/runner-config"), Path("/runner-credentials"))
                last_nonce = nonce
                last_heartbeat = clock()
        alive = directory / "alive"
        if (last_nonce is not None and alive.exists() and
                time.time() - alive.stat().st_mtime <= 30 and clock() - last_heartbeat >= 20):
            ssm.put_parameter(Name=f"/adp/{environment}/gitlab-runners/{group_id}/manager-alive",
                              Value="active", Type="String", Overwrite=True)
            (directory / "lease").touch()
            last_heartbeat = clock()
        pause(1)


def main():
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--group-id", required=True)
    args = parser.parse_args()
    if not (re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args.environment)
            and re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-[1-9][0-9]*", args.region)
            and re.fullmatch(r"[1-9][0-9]*", args.group_id)):
        raise SystemExit("manager_startup_not_verified")
    try:
        import boto3
        client = boto3.client("autoscaling", region_name=args.region)
        ssm = boto3.client("ssm", region_name=args.region)
        serve(Path("/runner-guard"), client, ssm, args.environment, args.group_id)
    except Exception:
        raise SystemExit("manager_startup_not_verified") from None


if __name__ == "__main__":
    main()

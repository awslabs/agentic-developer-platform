#!/usr/bin/env python3
"""Drain runner-owned detached instances and groups with expired heartbeats."""

import argparse
import datetime
import json
import re
import subprocess
import time


class ReconcileError(ValueError):
    pass


def require(condition):
    if not condition:
        raise ReconcileError("runner_reconciliation_failed")


class AwsCli:
    def __init__(self, service, region, run=subprocess.run):
        self.service = service
        self.region = region
        self.run = run

    def __getattr__(self, operation):
        def call(**query):
            result = self.run(["aws", self.service, operation.replace("_", "-"),
                               "--region", self.region, "--cli-input-json", json.dumps(query),
                               "--output", "json"], capture_output=True, check=False)
            require(result.returncode == 0)
            if not result.stdout or not result.stdout.strip():
                require(operation in ("terminate_instances", "set_instance_protection",
                                      "set_desired_capacity"))
                return {}
            return json.loads(result.stdout)
        return call


def group_ids(raw):
    identifiers = raw.split(",")
    require(1 <= len(identifiers) <= 4 and len(identifiers) == len(set(identifiers)))
    require(all(re.fullmatch(r"[1-9][0-9]*", identifier) for identifier in identifiers))
    return identifiers


def owned_group(autoscaling, environment, group_id):
    name = f"adp-{environment}-gitlab-job-{group_id}"
    groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])["AutoScalingGroups"]
    require(len(groups) == 1 and groups[0]["AutoScalingGroupName"] == name)
    group = groups[0]
    tags = {tag["Key"]: tag["Value"] for tag in group["Tags"]}
    require(len(tags) == len(group["Tags"]))
    require(all(tags.get(key) == value for key, value in
                (("Component", "gitlab-job"), ("Environment", environment), ("GroupId", group_id))))
    require(group["MinSize"] == 0 and group["MaxSize"] == 2 and 0 <= group["DesiredCapacity"] <= 2)
    template = group["LaunchTemplate"]
    template_id = template["LaunchTemplateId"]
    version = template["Version"]
    require(re.fullmatch(r"lt-[0-9a-f]{8,17}", template_id) and re.fullmatch(r"[1-9][0-9]*", version))
    require(tags.get("LaunchTemplateId") == template_id)
    subnets = group["VPCZoneIdentifier"].split(",")
    require(subnets and len(subnets) == len(set(subnets)) and
            all(re.fullmatch(r"subnet-[0-9a-f]{8,17}", subnet) for subnet in subnets))
    members = {instance["InstanceId"]: instance for instance in group["Instances"]}
    require(len(members) <= 2 and all(re.fullmatch(r"i-[0-9a-f]{8,17}", identifier) for identifier in members))
    return group


def inventory(autoscaling, ec2, environment, group_id):
    group = owned_group(autoscaling, environment, group_id)
    name = group["AutoScalingGroupName"]
    template_id = group["LaunchTemplate"]["LaunchTemplateId"]
    version = group["LaunchTemplate"]["Version"]
    subnets = group["VPCZoneIdentifier"].split(",")
    versions = ec2.describe_launch_template_versions(LaunchTemplateId=template_id, Versions=[version])["LaunchTemplateVersions"]
    require(len(versions) == 1 and versions[0]["LaunchTemplateId"] == template_id and
            versions[0]["VersionNumber"] == int(version))
    spec = versions[0]["LaunchTemplateData"]
    interfaces = spec["NetworkInterfaces"]
    require(len(interfaces) == 1 and len(interfaces[0]["Groups"]) == 1)
    security_group = interfaces[0]["Groups"][0]
    require(re.fullmatch(r"sg-[0-9a-f]{8,17}", security_group) and
            re.fullmatch(r"ami-[0-9a-f]{8,17}", spec["ImageId"]))
    members = {instance["InstanceId"]: instance for instance in group["Instances"]}
    instances = {}
    all_found = set()
    filters = [{"Name": "tag:Component", "Values": ["gitlab-job"]},
               {"Name": "tag:Environment", "Values": [environment]},
               {"Name": "tag:GroupId", "Values": [group_id]}]
    token = None
    seen_tokens = set()
    while True:
        query = {"Filters": filters}
        if token:
            query["NextToken"] = token
        page = ec2.describe_instances(**query)
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                identifier = instance["InstanceId"]
                require(re.fullmatch(r"i-[0-9a-f]{8,17}", identifier) and identifier not in all_found)
                all_found.add(identifier)
                if instance["State"]["Name"] == "terminated":
                    continue
                found = {tag["Key"]: tag["Value"] for tag in instance["Tags"]}
                require(len(found) == len(instance["Tags"]))
                require(all(found.get(key) == value for key, value in (
                    ("Component", "gitlab-job"), ("Environment", environment), ("GroupId", group_id),
                    ("LaunchTemplateId", template_id))))
                require(found.get("aws:autoscaling:groupName", name) == name)
                instance_groups = [entry["GroupId"] for entry in instance["SecurityGroups"]]
                require(instance["ImageId"] == spec["ImageId"] and instance["SubnetId"] in subnets and
                        instance_groups == [security_group])
                instances[identifier] = instance
        token = page.get("NextToken")
        if not token:
            break
        require(token not in seen_tokens)
        seen_tokens.add(token)
    require(set(members).issubset(all_found))
    return name, group, members, instances


def terminate_detached(autoscaling, ec2, environment, group_id, group, candidates, healthy):
    if not candidates:
        return set()
    current = owned_group(autoscaling, environment, group_id)
    require(current["LaunchTemplate"] == group["LaunchTemplate"] and
            set(current["VPCZoneIdentifier"].split(",")) == set(group["VPCZoneIdentifier"].split(",")))
    members = {instance["InstanceId"] for instance in current["Instances"]}
    detached = set(candidates) - members
    if healthy and (current["DesiredCapacity"] > len(members) or any(
            instance.get("LifecycleState", "InService") != "InService" for instance in current["Instances"])):
        return set()
    if detached:
        ec2.terminate_instances(InstanceIds=sorted(detached))
    return detached


def manager_is_alive(ssm, environment, group_id, now):
    response = ssm.get_parameter(Name=f"/adp/{environment}/gitlab-runners/{group_id}/manager-alive",
                                 WithDecryption=False)["Parameter"]
    modified = response["LastModifiedDate"]
    if isinstance(modified, str):
        modified = datetime.datetime.fromisoformat(modified.replace("Z", "+00:00"))
    require(isinstance(modified, datetime.datetime) and modified.tzinfo is not None)
    age = (now - modified).total_seconds()
    require(age >= -5)
    return response["Value"] == "active" and age <= 120


def reconcile(autoscaling, ec2, ssm, environment, identifiers, *, now=None,
              clock=time.monotonic, pause=time.sleep):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    require(now.tzinfo is not None)
    snapshots = []
    for group_id in identifiers:
        require(re.fullmatch(r"[1-9][0-9]*", group_id))
        name, group, members, instances = inventory(autoscaling, ec2, environment, group_id)
        healthy = manager_is_alive(ssm, environment, group_id, now)
        snapshots.append((group_id, name, group, members, instances, healthy))
    pending = []
    deadline = clock() + 360
    for group_id, name, group, members, instances, healthy in snapshots:
        detached = set(instances) - set(members)
        terminating = terminate_detached(autoscaling, ec2, environment, group_id, group, detached, healthy)
        if detached or not healthy:
            pending.append((group_id, healthy, terminating))
        if healthy:
            continue
        for identifier, instance in members.items():
            if instance.get("ProtectedFromScaleIn", False):
                autoscaling.set_instance_protection(AutoScalingGroupName=name, InstanceIds=[identifier],
                                                    ProtectedFromScaleIn=False)
        if group["DesiredCapacity"]:
            autoscaling.set_desired_capacity(AutoScalingGroupName=name, DesiredCapacity=0, HonorCooldown=False)
    while pending:
        remaining = []
        for group_id, healthy, terminating in pending:
            _, current, current_members, current_instances = inventory(autoscaling, ec2, environment, group_id)
            detached = set(current_instances) - set(current_members)
            terminating.update(terminate_detached(autoscaling, ec2, environment, group_id,
                                                 current, detached - terminating, healthy))
            if healthy:
                if detached:
                    remaining.append((group_id, healthy, terminating))
            elif current_members or current_instances or current["DesiredCapacity"] != 0:
                remaining.append((group_id, healthy, terminating))
        pending = remaining
        if pending:
            remaining_time = deadline - clock()
            require(remaining_time > 0)
            pause(min(5, remaining_time))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--group-ids", required=True)
    args = parser.parse_args()
    try:
        require(re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args.environment))
        require(re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-[1-9][0-9]*", args.region))
        identifiers = group_ids(args.group_ids)
        identity = AwsCli("sts", args.region).get_caller_identity()
        account = identity["Account"]
        role = f"adp-{args.environment}-gitlab-runners-reconciler"
        require(re.fullmatch(r"[0-9]{12}", account) and re.fullmatch(
            rf"arn:aws:sts::{account}:assumed-role/{role}/[^/]+", identity["Arn"]))
        reconcile(AwsCli("autoscaling", args.region), AwsCli("ec2", args.region),
                  AwsCli("ssm", args.region), args.environment, identifiers)
        print("runner_reconciliation_verified")
    except Exception:
        raise SystemExit("runner_reconciliation_failed") from None


if __name__ == "__main__":
    main()

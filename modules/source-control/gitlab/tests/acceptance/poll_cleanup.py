#!/usr/bin/env python3
"""Retain independent, read-only cleanup observations for an owned qualification job."""
import argparse
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import verify


STATUSES = {"legal": "success", "failed": "failed", "cancelled": "canceled", "manager_loss": "canceled"}


def collect(fixtures, scenario, job_id, instance_id, *, aws, gitlab, manager,
            clock=time.monotonic, wait=time.sleep, now=lambda: datetime.now(timezone.utc), save=lambda row: None):
    verify.require(scenario in STATUSES and type(job_id) is int and job_id > 0
                   and isinstance(instance_id, str) and instance_id, "cleanup_case_invalid")
    project = gitlab(f"projects/{fixtures['project_a']}")
    verify.project_scope(fixtures, "a", project, gitlab, "cleanup_project_scope_wrong")
    job = gitlab(f"projects/{fixtures['project_a']}/jobs/{job_id}")
    verify.require(job.get("id") == job_id and job.get("status") == STATUSES[scenario]
                   and job.get("commit", {}).get("id") == fixtures["source_revision"]
                   and (job.get("runner") or {}).get("id") == fixtures["runner_a"]
                   and type(job.get("pipeline", {}).get("id")) is int
                   and job["pipeline"]["id"] > 0, "cleanup_job_scope_wrong")
    finished = verify.timestamp(job["finished_at"])
    verify.require(verify.timestamp(job["created_at"]) <= finished, "cleanup_job_scope_wrong")
    host = aws.client("ec2").describe_instances(InstanceIds=[instance_id])
    instances = [row for reservation in host["Reservations"] for row in reservation["Instances"]]
    verify.require(len(instances) == 1 and instances[0]["InstanceId"] == instance_id
                   and instances[0]["VpcId"] == fixtures["vpc_id"]
                   and {tag["Key"]: tag["Value"] for tag in instances[0]["Tags"]}.items()
                   >= {"Component": "gitlab-job", "Environment": "dev",
                       "GroupId": str(fixtures["group_a"])}.items(), "cleanup_host_scope_wrong")
    started = clock()
    manager_loss = None
    while True:
        asgs = aws.client("autoscaling").describe_auto_scaling_groups(AutoScalingGroupNames=fixtures["asg_names"])
        groups = asgs["AutoScalingGroups"]
        verify.require(len(groups) == len(fixtures["asg_names"])
                       and {group["AutoScalingGroupName"] for group in groups} == set(fixtures["asg_names"])
                       and all({tag["Key"]: tag["Value"] for tag in group["Tags"]}.items()
                               >= {"Component": "gitlab-job", "Environment": "dev"}.items()
                               and {tag["Value"] for tag in group["Tags"] if tag["Key"] == "GroupId"}
                               <= {str(fixtures["group_a"]), str(fixtures["group_b"])}
                               and len([tag for tag in group["Tags"] if tag["Key"] == "GroupId"]) == 1
                               for group in groups), "cleanup_asg_scope_wrong")
        pages = list(aws.client("ec2").get_paginator("describe_instances").paginate(Filters=[
            {"Name": "tag:Component", "Values": ["gitlab-job"]},
            {"Name": "tag:Environment", "Values": ["dev"]},
            {"Name": "tag:GroupId", "Values": [str(fixtures["group_a"]), str(fixtures["group_b"])]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
        ]))
        verify.require(isinstance(asgs.get("ResponseMetadata", {}).get("RequestId"), str)
                       and asgs["ResponseMetadata"]["RequestId"]
                       and pages and all(isinstance(page.get("ResponseMetadata", {}).get("RequestId"), str)
                                         and page["ResponseMetadata"]["RequestId"] for page in pages),
                       "cleanup_provider_receipts_missing")
        remaining = [instance for page in pages for reservation in page["Reservations"]
                     for instance in reservation["Instances"]]
        verify.require(all(instance["VpcId"] == fixtures["vpc_id"] for instance in remaining),
                       "cleanup_unowned_instance")
        deployment = manager()
        observed = now()
        elapsed = (observed - finished).total_seconds()
        row = {"name": scenario, "job_id": job_id, "pipeline_id": job["pipeline"]["id"],
               "job_status": job["status"], "group_id": fixtures["group_a"], "runner_id": fixtures["runner_a"],
               "instance_id": instance_id, "job_finished_at": job["finished_at"],
               "observed_at": observed.isoformat(), "elapsed_seconds": elapsed,
               "observed_asg_names": [group["AutoScalingGroupName"] for group in groups],
               "asg_desired": sum(group["DesiredCapacity"] for group in groups),
               "asg_instances": [instance for group in groups for instance in group["Instances"]],
               "ec2_nonterminated_instances": [instance["InstanceId"] for instance in remaining],
               "manager_desired": deployment["spec"]["replicas"] if deployment else None,
               "manager_ready": deployment["status"].get("readyReplicas", 0) if deployment else None,
               "manager_resource_version": deployment["metadata"].get("resourceVersion") if deployment else None,
               "aws_request_ids": {"autoscaling": asgs["ResponseMetadata"]["RequestId"],
                                   "ec2": [page["ResponseMetadata"]["RequestId"] for page in pages]}}
        if scenario == "manager_loss":
            if row["manager_ready"] == 0:
                verify.require(row["manager_desired"] == 1 and row["manager_resource_version"],
                               "manager_loss_receipt_missing")
                manager_loss = {"unready_at": row["observed_at"], "unready_ready_replicas": 0,
                                "unready_resource_version": row["manager_resource_version"]}
            if manager_loss:
                row["manager_loss_receipt"] = manager_loss
        save(row)
        verify.require(0 <= elapsed <= 900 and clock() - started <= 900, "cleanup_deadline_exceeded")
        if row["asg_desired"] == 0 and row["asg_instances"] == [] and not remaining and row["manager_ready"] == 1:
            verify.require(row["manager_desired"] == 1 and row["manager_resource_version"],
                           "cleanup_manager_not_ready")
            if scenario == "manager_loss":
                verify.require(manager_loss and manager_loss["unready_resource_version"] != row["manager_resource_version"],
                               "manager_loss_receipt_missing")
            return row
        verify.require(elapsed < 900, "cleanup_deadline_exceeded")
        wait(min(15, 900 - elapsed))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--scenario", choices=tuple(STATUSES), required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--gitlab-url", required=True)
    parser.add_argument("--admin-secret-arn", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--manager-deployment", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fixtures = verify.private_json(args.fixtures)
        from jsonschema import Draft202012Validator
        schema = json.loads(Path(verify.__file__).with_name("fixtures.schema.json").read_text())
        verify.require(Draft202012Validator(schema).is_valid(fixtures), "fixture_schema_invalid")
        aws, gitlab, manager = verify.live_clients(args, fixtures)
        directory = args.output.parent.lstat()
        verify.require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.getuid()
                       and stat.S_IMODE(directory.st_mode) == 0o700, "private_output_directory_required")
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            def save(row):
                output.write(json.dumps(row, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())

            receipt = collect(fixtures, args.scenario, args.job_id, args.instance_id,
                              aws=aws, gitlab=gitlab, manager=manager, save=save)
        print(json.dumps({"status": "cleanup_observed", "name": receipt["name"]}))
        return 0
    except Exception:
        print(json.dumps({"status": "incomplete", "reason": "private_cleanup_observation_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline provider doubles check bounded owned-ASG and detached-host observation."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cleanup_collector", ROOT / "poll_cleanup.py")
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def scenario(*, change=None, case="legal"):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fixtures = {"project_a": 101, "group_a": 10, "group_b": 20, "runner_a": 42,
                "project_namespace_a": 11,
                "source_revision": "a" * 40, "vpc_id": "vpc-a", "asg_names": ["owned-asg"]}
    job = {"id": 1001, "status": collector.STATUSES[case], "commit": {"id": fixtures["source_revision"]},
           "runner": {"id": 42}, "pipeline": {"id": 500},
           "created_at": (now - timedelta(minutes=1)).isoformat(), "finished_at": now.isoformat()}
    host = {"InstanceId": "i-owned", "VpcId": "vpc-a", "Tags": [
        {"Key": key, "Value": value} for key, value in
        (("Component", "gitlab-job"), ("Environment", "dev"), ("GroupId", "10"))]}
    asg = {"AutoScalingGroupName": "owned-asg", "DesiredCapacity": 0, "Instances": [],
           "Tags": [dict(tag) for tag in host["Tags"]]}
    if change == "wrong_host":
        host["VpcId"] = "vpc-unowned"
    if change == "wrong_asg":
        asg["Tags"][2] = {"Key": "GroupId", "Value": "99"}
    if change == "pending":
        asg["Instances"] = [{"InstanceId": "i-owned", "LifecycleState": "Terminating"}]
    ec2, autoscaling = Mock(), Mock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [host]}]}
    ec2.get_paginator.return_value.paginate.return_value = [{"Reservations": [{"Instances": []}],
                                                             "ResponseMetadata": {"RequestId": "ec2-request"}}]
    if change == "no_receipt":
        ec2.get_paginator.return_value.paginate.return_value = [{"Reservations": [{"Instances": []}]}]
    autoscaling.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [asg],
                                                              "ResponseMetadata": {"RequestId": "asg-request"}}
    clients = {"ec2": ec2, "autoscaling": autoscaling}
    aws = Mock()
    aws.client.side_effect = clients.__getitem__
    ticks = [600]
    rows = []

    def gitlab(path):
        if path == "projects/101":
            return {"id": 101, "namespace": {"id": 11, "kind": "group"}}
        if path == "groups/11":
            return {"id": 11, "parent_id": 99 if change == "unrelated_group" else 10}
        if path == "groups/99":
            return {"id": 99, "parent_id": None}
        return job

    def collect():
        def manager():
            missing = case == "manager_loss" and change != "no_manager_loss" and ticks[0] == 600
            return {"spec": {"replicas": 1}, "status": {"readyReplicas": 0 if missing else 1},
                    "metadata": {"resourceVersion": "rv-0" if missing else "rv-1"}}

        return collector.collect(fixtures, case, 1001, "i-owned", aws=aws, gitlab=gitlab,
                                 manager=manager,
                                 clock=lambda: ticks[0] - 600,
                                 wait=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
                                 now=lambda: now + timedelta(seconds=ticks[0]), save=rows.append)

    return collect, rows


def test_cleanup_poller_collects_independent_receipts_without_mutation():
    collect, rows = scenario()
    receipt = collect()
    assert rows == [receipt]
    assert receipt["aws_request_ids"] == {"autoscaling": "asg-request", "ec2": ["ec2-request"]}
    assert receipt["manager_ready"] == receipt["manager_desired"] == 1


def test_manager_loss_requires_observed_unready_deployment_then_recovery():
    collect, rows = scenario(case="manager_loss")
    receipt = collect()
    assert len(rows) == 2
    assert receipt["manager_loss_receipt"] == {"unready_at": rows[0]["observed_at"],
            "unready_ready_replicas": 0, "unready_resource_version": "rv-0"}
    collect, rows = scenario(case="manager_loss", change="no_manager_loss")
    with pytest.raises(collector.verify.IncompleteEvidence, match="manager_loss_receipt_missing"):
        collect()
    assert len(rows) == 1


@pytest.mark.parametrize("change,reason", [
    ("wrong_host", "cleanup_host_scope_wrong"), ("wrong_asg", "cleanup_asg_scope_wrong"),
    ("pending", "cleanup_deadline_exceeded"), ("no_receipt", "cleanup_provider_receipts_missing"),
    ("unrelated_group", "cleanup_project_scope_wrong"),
])
def test_cleanup_poller_refuses_unowned_or_nonempty_inventory(change, reason):
    collect, rows = scenario(change=change)
    with pytest.raises(collector.verify.IncompleteEvidence, match=reason):
        collect()
    assert (len(rows) > 0) is (change == "pending")

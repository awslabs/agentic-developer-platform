"""Explicit remote check against the pinned SkyPilot parser, without cloud I/O.

Kept outside default test collection: the ordinary domain environment does not
install SkyPilot. Domain CI runs this explicitly in a separate pinned venv.
"""

import copy
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sky


@pytest.mark.parametrize(
    "clouds", [[], ["aws"], ["nebius"], ["aws", "nebius", "lambda"]]
)
def test_actual_skypilot_parser_preserves_resource_choices(clouds):
    path = Path(__file__).parents[1] / "agent/skills/skypilot/scripts/capacity_task.py"
    spec = importlib.util.spec_from_file_location("capacity_task_native", path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    raw = builder.capacity_task(
        name="issue-123",
        gpus=["H100:1", "A100-80GB:1"],
        nodes=2,
        disk_gb=100,
        hold_seconds=900,
        clouds=clouds,
        cpus=4,
        memory_gb=32,
    )
    parsed = sky.Task.from_yaml_config(copy.deepcopy(raw))
    assert parsed.num_nodes == 2
    assert parsed.run == "sleep 900"
    assert len(parsed.resources) == 2 * max(1, len(clouds))
    for resource in parsed.resources:
        assert resource.instance_type is None
        assert resource.region is None
        assert resource.accelerators in ({"H100": 1}, {"A100-80GB": 1})
        assert resource.cpus == "4+"
        assert resource.memory == "32+"
        if clouds:
            assert str(resource.cloud).lower() in clouds
        else:
            assert resource.cloud is None


def test_actual_executor_task_preserves_choices_join_and_physical_limit(monkeypatch):
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/harness/jobs"))
    monkeypatch.syspath_prepend(str(root / "modules/domain-apps/superplane/executor"))
    from superplane_executor.plan import Plan

    plan = Plan(
        {
            "version": 3,
            "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
            "endpoint": "https://workspace.example.invalid",
            "certificate_authority": "public-test-ca",
            "service_cidr": "172.20.0.0/16",
            "node_count": 2,
            "region": "us-east-1",
            "image_id": "ami-0123456789abcdef0",
            "disk_size": 100,
            "accelerators": ["A10G:1", "L4:1"],
            "max_gpus_per_node": 4,
            "cpus": 4,
            "memory_gb": 32,
        },
        "sp-" + "a" * 32,
        (),
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                workspace_id="workspace",
                runtime_deadline=datetime.now(UTC) + timedelta(seconds=900),
            )
        )
    )
    raw = json.loads(plan.task(operation))
    parsed = sky.Task.from_yaml_config(copy.deepcopy(raw))
    assert parsed.num_nodes == 2
    assert "nodeadm init" in parsed.setup
    assert "remaining=" in parsed.run
    assert len(parsed.resources) == 2
    for resource in parsed.resources:
        assert resource.instance_type is None
        assert resource.accelerators in ({"A10G": 1}, {"L4": 1})
        assert str(resource.cloud).lower() == "aws"
        assert resource.region == "us-east-1"
        assert resource.labels["superplane-max-gpus-per-node"] == "4"
        assert resource.cpus == "4+"
        assert resource.memory == "32+"


def test_actual_executor_task_offers_multiple_regions_without_pinning_one(
    monkeypatch,
):
    """#5925: a plan with >=2 eligible regions reaches the real pinned parser

    without pinning a single region or instance type, each candidate retaining
    its own approved region's image, VPC, security group and node identity.
    """
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/harness/jobs"))
    monkeypatch.syspath_prepend(str(root / "modules/domain-apps/superplane/executor"))
    from superplane_executor.plan import Plan

    plan = Plan(
        {
            "version": 4,
            "provider_account_id": "123456789012",
            "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
            "endpoint": "https://workspace.example.invalid",
            "certificate_authority": "public-test-ca",
            "service_cidr": "172.20.0.0/16",
            "node_count": 2,
            "disk_size": 100,
            "regions": [
                {
                    "region": "us-east-1",
                    "image_id": "ami-0123456789abcdef0",
                    "vpc_name": "vpc-a",
                    "security_group": "sg-a",
                    "vpc_id": "vpc-0123456789abcdef0",
                    "security_group_id": "sg-0123456789abcdef0",
                    "subnet_ids": ["subnet-0123456789abcdef0"],
                    "instance_profile": "profile-a",
                },
                {
                    "region": "us-west-2",
                    "image_id": "ami-0123456789abcdef1",
                    "vpc_name": "vpc-b",
                    "security_group": "sg-b",
                    "vpc_id": "vpc-0123456789abcdef0",
                    "security_group_id": "sg-0123456789abcdef0",
                    "subnet_ids": ["subnet-0123456789abcdef0"],
                    "instance_profile": "profile-b",
                },
            ],
            "accelerators": ["A10G:1", "L4:1"],
            "max_gpus_per_node": 4,
            "cpus": 4,
            "memory_gb": 32,
        },
        "sp-" + "a" * 32,
        (),
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                workspace_id="workspace",
                runtime_deadline=datetime.now(UTC) + timedelta(seconds=900),
            )
        )
    )
    raw = json.loads(plan.task(operation))
    assert "region" not in raw["resources"]
    assert "image_id" not in raw["resources"]
    assert "instance_type" not in raw["resources"]
    parsed = sky.Task.from_yaml_config(copy.deepcopy(raw))
    assert parsed.num_nodes == 2
    # Two regions x two GPU choices = four independently selectable candidates.
    assert len(parsed.resources) == 4
    seen_regions = set()
    for resource in parsed.resources:
        assert resource.instance_type is None
        assert str(resource.cloud).lower() == "aws"
        assert resource.region in ("us-east-1", "us-west-2")
        assert resource.accelerators in ({"A10G": 1}, {"L4": 1})
        assert resource.cpus == "4+"
        assert resource.memory == "32+"
        assert resource.labels["superplane-max-gpus-per-node"] == "4"
        bound_image = {
            "us-east-1": "ami-0123456789abcdef0",
            "us-west-2": "ami-0123456789abcdef1",
        }
        assert resource.image_id == {resource.region: bound_image[resource.region]}
        bound_overrides = {
            "us-east-1": {
                "remote_identity": "profile-a",
                "vpc_name": "vpc-a",
                "security_group_name": "sg-a",
                "disk_encrypted": True,
            },
            "us-west-2": {
                "remote_identity": "profile-b",
                "vpc_name": "vpc-b",
                "security_group_name": "sg-b",
                "disk_encrypted": True,
            },
        }
        assert resource.cluster_config_overrides == {
            "aws": bound_overrides[resource.region]
        }
        seen_regions.add(resource.region)
    assert seen_regions == {"us-east-1", "us-west-2"}

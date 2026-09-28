"""Network policy is explicit, closed and bound to every approved compute region."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from harness_jobs.identity import OperationRefused
from network_support import ACCOUNT, HOME, REMOTE, policy
from superplane_executor.network_plan import read_network


def inputs():
    value = policy(str(uuid4()))
    side = value["regions"][REMOTE]["network"]
    data = {
        "version": 4,
        "cluster_arn": f"arn:aws:eks:{HOME}:{ACCOUNT}:cluster/test",
        "endpoint": "https://test.eks.amazonaws.com",
        "service_cidr": "172.20.0.0/16",
        "regions": [
            {
                "region": REMOTE,
                "vpc_id": side["vpc_id"],
                "security_group_id": side["security_group_id"],
                "subnet_ids": side["subnet_ids"],
            }
        ],
    }
    return value, data


def encoded(value):
    return {
        "controller_network_cluster": json.dumps(
            value["cluster"], separators=(",", ":")
        ),
        "controller_network_regions": json.dumps(
            value["regions"], separators=(",", ":")
        ),
    }


def test_old_admissions_do_not_inherit_installed_network_defaults():
    assert read_network({}, {"version": 1}) is None


def test_explicit_network_preserves_cluster_and_compute_identities():
    value, data = inputs()
    assert read_network(encoded(value), data) == value


@pytest.mark.parametrize(
    "change",
    [
        "vpc",
        "subnet",
        "group",
        "overlap",
        "service",
        "extra",
        "generation",
        "region",
        "version",
        "half",
        "oversized",
    ],
)
def test_wrong_or_unbounded_network_policy_is_refused(change):
    value, data = inputs()
    remote = value["regions"][REMOTE]["network"]
    if change == "vpc":
        remote["vpc_id"] = "vpc-ffffffff"
    if change == "subnet":
        remote["subnet_ids"] = ["subnet-ffffffff"]
    if change == "group":
        remote["security_group_id"] = "sg-ffffffff"
    if change == "overlap":
        remote["pod_cidr"] = "10.1.0.0/16"
    if change == "service":
        remote["pod_cidr"] = data["service_cidr"]
    if change == "extra":
        remote["caller_override"] = True
    if change == "generation":
        value["cluster"]["membership_generation"] = 0
    if change == "region":
        value["regions"]["eu-west-1"] = deepcopy(value["regions"][REMOTE])
    if change == "version":
        value["cluster"]["version"] = 2
    params = encoded(value)
    if change == "half":
        del params["controller_network_cluster"]
    if change == "oversized":
        params["controller_network_regions"] = " " * 2001
    with pytest.raises(OperationRefused):
        read_network(params, data)

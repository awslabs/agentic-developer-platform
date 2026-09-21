import copy
import sys
from pathlib import Path

import pytest
import yaml

MODULE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MODULE))

# Actual 44-char workspace cluster name produced by the workspace Terraform module.
# Used to verify that EKS_NAME accepts Terraform-generated suffix form without
# the IDENTIFIER 40-char ceiling blocking preflight.
ACTUAL_WORKSPACE_CLUSTER = "adp-dev-spw-f67f322455acd6f6df9cb4bec015ffce"


@pytest.fixture
def environment():
    return {
        "version": 1,
        "environment": "dev",
        "account_id": "879318057152",
        "region": "us-east-1",
        "namespace": "superplane",
        "skypilot_namespace": "skypilot",
        "cluster": "adp-dev-eks-cluster",
        "workspace_cluster": "research-workspace",
        "workspace_namespace": "superplane-controller",
        "origin": "https://adp.example.test",
        "org_id": "10000000-0000-0000-0000-000000000001",
        "adp_org_id": "aws-e",
        "workspace_id": "20000000-0000-0000-0000-000000000002",
        "cluster_id": "30000000-0000-0000-0000-000000000003",
        "auth": {
            "issuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example",
            "client_ids": ["adpclient"],
        },
        "database": {
            "identifier": "superplane-database",
            "database": "superplane",
            "schema": "superplane",
            "skypilot_schema": "skypilot",
            "backup_id": "superplane-backup",
            "migration_owner": "operator",
            "restore_owner": "operator",
            "backup_owner": "operator",
        },
        "secrets": {
            "database": "adp/dev/superplane/database",
            "observation": "adp/dev/superplane/observation",
            "workspace_access": "adp/dev/superplane/workspace",
        },
        "timeout_seconds": 30,
        "network_policy_enforced": True,
        "controller_ownership": "single-workspace-controller",
    }


@pytest.fixture
def actual_name_environment(environment):
    """Environment using the selected 44-char Terraform-generated workspace cluster name."""
    env = copy.deepcopy(environment)
    env["workspace_cluster"] = ACTUAL_WORKSPACE_CLUSTER
    return env


@pytest.fixture
def release(environment):
    lock = copy.deepcopy(
        yaml.safe_load((MODULE / "releases/superplane.lock.yaml").read_text())
    )
    lock["source_revision"] = "a" * 40
    lock["pending_images"] = {}
    for i, name in enumerate(
        ("superplane-api", "superplane-controller", "superplane-platform-monitor")
    ):
        lock["images"][name] = "sha256:" + str(i + 1) * 64
        lock["image_sources"][name] = {
            "registry": f"{environment['account_id']}.dkr.ecr.us-east-1.amazonaws.com",
            "repository": "adp-" + name,
            "source_revision": lock["source_revision"],
        }
    return lock

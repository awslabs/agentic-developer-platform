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
    env = {
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
        "execution": {
            "authority_endpoint": "https://authority.example.test",
            "role_arn": "arn:aws:iam::879318057152:role/adp-dev-superplane-control-plane",
            "provider_role_arn": "arn:aws:iam::879318057152:role/adp-dev-superplane-skypilot-api",
            "run_projection_secret": "selected-adp-run",
            "database_secret": "selected-execution-database",
            "workspace_credentials_secret": "selected-workspace-execution",
        },
    }
    env["controller_profiles"] = controller_policy(env)
    return env


def controller_policy(env):
    import base64
    import ssl

    # Public trust material only; choose a short real certificate so the fixture
    # exercises the production validator's unchanged per-parameter size bound.
    certificate = min(
        ssl.create_default_context().get_ca_certs(binary_form=True), key=len
    )
    ca = base64.b64encode(ssl.DER_cert_to_PEM_cert(certificate).encode()).decode()
    profile = {
        "cluster_id": env["cluster_id"],
        "cluster_arn": f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['workspace_cluster']}",
        "endpoint": "https://workspace.example.test",
        "namespace": env["workspace_namespace"],
        "provider_account_id": env["account_id"],
        "region": env["region"],
        "image_id": "ami-0123456789abcdef0",
        "instance_type": "g5.xlarge",
        "node_count": 1,
        "disk_size": 100,
        "instance_profile": "approved-nodes",
        "vpc_name": "workspace",
        "security_group": "approved-workers",
        "service_cidr": "172.20.0.0/16",
        "certificate_authority": ca,
        "physical_gpus": 1,
        "max_runtime_seconds": 900,
        "max_cost_micros": 1_000_000,
        "credential_reference": {
            "credential_id": "cred-fixture",
            "credential_service": "aws",
            "credential_label": "fixture",
        },
        "serving_auth_contract": "superplane-token-file-header-v1",
        "model_options": {
            "model_name": "fixture/model",
            "precision": "fp16",
            "serving_framework": "vllm",
            "replicas": 1,
            "gpu_per_replica": 1,
            "tensor_parallel_size": 1,
            "max_model_len": None,
        },
        "workload": {
            "kind": "serving",
            "image": "registry.example/verified-server@sha256:" + "a" * 64,
            "command": ["/app/serve"],
            "args": ["--model", "fixture/model"],
            "gpu_count": 1,
            "cpu": "2000m",
            "memory": "8Gi",
            "port": 8000,
            "auth_secret": "model-access",
        },
    }
    return {
        "version": 1,
        "tenants": {
            env["org_id"]: {
                "adp_org_id": env["adp_org_id"],
                "workspaces": {env["workspace_id"]: {"approved-model": profile}},
            }
        },
    }


@pytest.fixture
def actual_name_environment(environment):
    """Environment using the selected 44-char Terraform-generated workspace cluster name."""
    env = copy.deepcopy(environment)
    env["workspace_cluster"] = ACTUAL_WORKSPACE_CLUSTER
    env["controller_profiles"] = controller_policy(env)
    return env


@pytest.fixture
def release(environment):
    lock = copy.deepcopy(
        yaml.safe_load((MODULE / "releases/superplane.lock.yaml").read_text())
    )
    lock["source_revision"] = "a" * 40
    lock["pending_images"] = {}
    for i, name in enumerate(
        (
            "superplane-api",
            "superplane-controller",
            "superplane-platform-monitor",
            "superplane-executor",
        )
    ):
        lock["images"][name] = "sha256:" + str(i + 1) * 64
        lock["image_sources"][name] = {
            "registry": f"{environment['account_id']}.dkr.ecr.us-east-1.amazonaws.com",
            "repository": "adp-" + name,
            "source_revision": lock["source_revision"],
        }
    return lock

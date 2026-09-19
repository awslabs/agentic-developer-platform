import copy
import json
import subprocess
import sys

import pytest
import yaml

from installation.config import COMPONENTS, MODULE, Refusal, image, validate
from installation.manifests import migration_job, render


def test_supported_input_and_four_service_runtime(environment, release):
    validate(environment, release)
    docs = render(environment, release)
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    assert set(deployments) == set(COMPONENTS)
    for name, deployment in deployments.items():
        pod = deployment["spec"]["template"]["spec"]
        assert pod["containers"][0]["image"] == image(release, name)
        assert not pod.get("hostNetwork")
        assert pod["automountServiceAccountToken"] is False
        assert pod["containers"][0]["resources"]["limits"]
        assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    controller = deployments["superplane-controller"]["spec"]["template"]["spec"]
    env = {x["name"]: x.get("value") for x in controller["containers"][0]["env"]}
    assert env["KUBECONFIG"] == "/workspace/kubeconfig"
    assert env["EKS_CLUSTER_NAME"] == environment["workspace_cluster"]
    assert not any(
        d["kind"]
        in {"ClusterRole", "ClusterRoleBinding", "Ingress", "PersistentVolume"}
        for d in docs
    )
    monitor = str(deployments["superplane-platform-monitor"])
    assert "DATABASE_URL" not in monitor
    assert "OBSERVATION_CREDENTIAL" in monitor


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("namespace", "adp"),
        ("namespace", "kube-system"),
        ("namespace", "x;touch /tmp/pwn"),
        ("origin", "http://adp.example.test"),
        ("origin", "https://user:password@adp.example.test"),
        ("account_id", "wrong"),
        ("network_policy_enforced", False),
        ("controller_ownership", "unknown"),
        ("workspace_cluster", "adp-dev-eks-cluster"),
        ("timeout_seconds", True),
    ],
)
def test_reject_unsafe_environment(environment, release, key, value):
    environment[key] = value
    with pytest.raises(Refusal):
        validate(environment, release)


@pytest.mark.parametrize(
    "schema",
    [
        "public",
        "pg_catalog",
        "superplane,public",
        "x;DROP TABLE users",
        "information_schema",
    ],
)
def test_schema_boundary(environment, release, schema):
    environment["database"]["schema"] = schema
    with pytest.raises(Refusal):
        validate(environment, release)


def test_reject_stale_image_provenance_and_schema(environment, release):
    stale = copy.deepcopy(release)
    stale["image_sources"]["superplane-api"]["source_revision"] = "b" * 40
    with pytest.raises(Refusal, match="source"):
        validate(environment, stale)
    stale = copy.deepcopy(release)
    stale["schema"]["observed"]["head"] = "013_add_provider_operations"
    with pytest.raises(Refusal, match="014"):
        validate(environment, stale)


def test_migration_runs_maintained_image_and_actual_settings(environment, release):
    job = migration_job(environment, release, "test123")
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "app.installation", "migrate"]
    variables = {e["name"]: e for e in container["env"]}
    assert (
        variables["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["key"] == "migration-url"
    )
    assert variables["SUPERPLANE_DB_SCHEMA"]["value"] == "superplane"
    assert job["spec"]["backoffLimit"] == 0
    assert "PGOPTIONS" not in variables


def test_cli_plan_never_invokes_cloud_or_platform(tmp_path, environment, release):
    config, lock = tmp_path / "environment.yaml", tmp_path / "lock.yaml"
    config.write_text(yaml.safe_dump(environment))
    lock.write_text(yaml.safe_dump(release))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "installation",
            "--environment",
            str(config),
            "--release-lock",
            str(lock),
            "--output",
            str(tmp_path / "plan"),
        ],
        cwd=MODULE,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / "plan/receipt.json").read_text())
    assert receipt["status"] == "planned"
    assert receipt["completed"] == []
    assert "installed-and-verified" not in result.stdout
    assert (
        len(list(yaml.safe_load_all((tmp_path / "plan/manifests.yaml").read_text())))
        > 15
    )


def test_unknown_secret_reference_is_rejected(environment, release):
    environment["secrets"]["unexpected"] = "adp/dev/another-domain/database"
    with pytest.raises(Refusal, match="exactly the three"):
        validate(environment, release)


def test_runtime_role_annotations_match_maintained_terraform(environment, release):
    import re

    source = (MODULE / "infra/control-plane/irsa.tf").read_text()
    for doc in render(environment, release):
        if (
            doc["kind"] != "ServiceAccount"
            or doc["metadata"]["name"] == "superplane-platform-monitor"
        ):
            continue
        role = (
            "skypilot" if doc["metadata"]["name"] == "skypilot-api" else "control_plane"
        )
        name = re.search(
            r'resource "aws_iam_role" "' + role + r'"\s*\{.*?name\s*=\s*"([^"]+)"',
            source,
            re.S,
        ).group(1)
        name = name.replace(
            "${local.name_prefix}", "adp-" + environment["environment"] + "-superplane"
        )
        assert (
            doc["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
            == f"arn:aws:iam::{environment['account_id']}:role/{name}"
        )

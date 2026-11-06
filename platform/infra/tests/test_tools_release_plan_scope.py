"""Inspect Terraform's real scoped plan with provider operations mocked."""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scoped_rbac_plan_keeps_fresh_access_readiness_without_unrelated_eks_resources():
    assert shutil.which("terraform"), "Requires Terraform"
    assert (ROOT / ".terraform").is_dir(), (
        "Run terraform init -backend=false in platform/infra first"
    )
    result = subprocess.run(
        [
            "terraform",
            "test",
            "-no-color",
            "-json",
            "-verbose",
            "-filter=tests/tools_release_plan_scope.tftest.hcl",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    plans = [event["test_plan"] for event in events if event["type"] == "test_plan"]
    assert len(plans) == 1
    resources = {
        resource["address"]: resource
        for resource in plans[0]["resource_changes"]
        if resource["mode"] == "managed"
    }
    principal = '"arn:aws:iam::123456789012:user/test-deployer"'
    # Fresh installs must still create the cluster, operator entry/association,
    # and propagation wait before the namespace and RBAC can be installed.
    assert {address for address in resources if address.startswith("module.eks.")} == {
        "module.eks.aws_eks_cluster.main",
        "module.eks.aws_kms_key.eks_secrets",
        f"module.eks.aws_eks_access_entry.admins[{principal}]",
        f"module.eks.aws_eks_access_policy_association.admins[{principal}]",
        "module.eks.time_sleep.wait_for_access_entry",
    }
    expected_rbac = {
        f"{kind}.{name}[0]"
        for kind in ("kubernetes_role_v1", "kubernetes_role_binding_v1")
        for name in ("tools_release", "shared_tools_release", "tools_browser_observer")
    }
    assert expected_rbac <= resources.keys()
    assert resources["null_resource.tools_release_namespace[0]"]["change"][
        "actions"
    ] == ["create"]
    assert all(
        resources[address]["change"]["actions"] == ["create"]
        for address in expected_rbac
    )


if __name__ == "__main__":
    test_scoped_rbac_plan_keeps_fresh_access_readiness_without_unrelated_eks_resources()
    print(
        "Scoped RBAC plan preserves fresh access readiness and excludes unrelated EKS resources."
    )

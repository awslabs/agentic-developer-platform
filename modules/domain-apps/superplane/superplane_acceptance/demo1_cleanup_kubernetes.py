"""Read original cleanup RBAC and the active fence over a pinned observer transport."""

import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.target import VerifiedTarget

from workspace_provisioning.artifacts import digest
from workspace_provisioning.retirement_access_clients import require_cluster
from workspace_provisioning.retirement_fence import verify
from workspace_provisioning.retirement_inventory import OwnedGrant
from workspace_provisioning.runtime_config import LifecycleRefused

from .demo1_evidence import EvidenceError
from .demo1_report import reference

RESOURCES = {
    ("rbac.authorization.k8s.io/v1", "Role"): "roles",
    ("rbac.authorization.k8s.io/v1", "RoleBinding"): "rolebindings",
    ("rbac.authorization.k8s.io/v1", "ClusterRole"): "clusterroles",
    ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"): "clusterrolebindings",
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
    ): "validatingadmissionpolicies",
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
    ): "validatingadmissionpolicybindings",
}


def require(condition):
    if not condition:
        raise EvidenceError("cleanup Kubernetes: current grants or fence unverified")


def observe_kubernetes(reader, selected, workspace_id, material, expected_digest):
    try:
        return _observe(reader, selected, workspace_id, material, expected_digest)
    except (
        BootstrapRefused,
        LifecycleRefused,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ):
        raise EvidenceError(
            "cleanup Kubernetes: current grants or fence unverified"
        ) from None


def _observe(reader, selected, workspace_id, material, expected_digest):
    require(
        all(
            getattr(reader, actual) == getattr(selected, expected)
            for actual, expected in (
                ("connection_id", "connection_id"),
                ("account", "account"),
                ("region", "region"),
                ("role_name", "role"),
            )
        )
        and isinstance(material, dict)
        and set(material) == {"transport", "grants", "fence"}
        and digest(material) == expected_digest
    )
    outputs = material["transport"]
    require(
        set(outputs)
        == {
            "cluster_arn",
            "cluster_name",
            "cluster_endpoint",
            "cluster_certificate_authority_data",
        }
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", outputs["cluster_name"])
        and outputs["cluster_arn"]
        == f"arn:aws:eks:{selected.region}:{selected.account}:cluster/{outputs['cluster_name']}"
    )
    endpoint = urlsplit(outputs["cluster_endpoint"])
    require(
        endpoint.scheme == "https"
        and endpoint.hostname
        and not endpoint.username
        and not endpoint.password
        and endpoint.path in ("", "/")
        and not endpoint.query
        and not endpoint.fragment
    )
    certificate = base64.b64decode(
        outputs["cluster_certificate_authority_data"], validate=True
    )
    require(bool(certificate))
    rows = material["grants"]
    expected_keys = {
        f"cleanup-{scope}-{kind}"
        for scope in ("cluster", "namespace", "system")
        for kind in ("role", "binding")
    }
    expected_keys |= {"retirement-fence-policy", "retirement-fence-binding"}
    require(
        isinstance(rows, list)
        and len(rows) == 8
        and all(
            isinstance(row, dict) and set(row) == {"spec", "identity"} for row in rows
        )
        and {row["spec"]["key"] for row in rows} == expected_keys
        and all(
            row["spec"]["kind"] == "kubernetes"
            and row["spec"]["cluster_arn"] == outputs["cluster_arn"]
            for row in rows
        )
    )

    def guard():
        require(selected.authorized_at <= reader.clock() < selected.deadline)

    guard()
    require(reader._identity())
    guard()
    code, cluster, _ = reader._execute(
        "eks",
        "describe-cluster",
        "--name",
        outputs["cluster_name"],
        "--region",
        selected.region,
    )
    guard()
    require(code == 0)
    require_cluster(cluster["cluster"], outputs)
    target = VerifiedTarget(
        org_id=selected.org_id,
        workspace_id=workspace_id,
        account_id=selected.account,
        region=selected.region,
        cluster_name=outputs["cluster_name"],
        cluster_arn=outputs["cluster_arn"],
        endpoint=outputs["cluster_endpoint"],
        certificate_authority_data=outputs["cluster_certificate_authority_data"],
        principal_arn=f"arn:aws:iam::{selected.account}:role/{selected.role}",
        cluster_ownership="adp-created",
    )
    with tempfile.TemporaryDirectory(prefix="demo1-cleanup-kube-") as directory:
        ca = Path(directory) / "ca.pem"
        ca.touch(mode=0o600, exist_ok=False)
        ca.write_bytes(certificate)
        config = Path(directory) / "kubeconfig.json"
        config.touch(mode=0o600, exist_ok=False)
        config.write_text(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "current-context": "selected",
                    "clusters": [
                        {
                            "name": "selected",
                            "cluster": {
                                "server": target.endpoint,
                                "certificate-authority-data": target.certificate_authority_data,
                            },
                        }
                    ],
                    "contexts": [
                        {
                            "name": "selected",
                            "context": {"cluster": "selected", "user": "selected"},
                        }
                    ],
                    "users": [
                        {
                            "name": "selected",
                            "user": {
                                "exec": {
                                    "apiVersion": "client.authentication.k8s.io/v1beta1",
                                    "command": "aws",
                                    "args": [
                                        "eks",
                                        "get-token",
                                        "--cluster-name",
                                        target.cluster_name,
                                        "--region",
                                        target.region,
                                        "--output",
                                        "json",
                                    ],
                                    "interactiveMode": "Never",
                                }
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        class Resources:
            def get(self, *, api_version, kind):
                resource = RESOURCES[(api_version, kind)]

                def get(*, name, namespace=None):
                    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name))
                    require(
                        (namespace is not None) == (kind in {"Role", "RoleBinding"})
                    )
                    prefix = "/apis/" + api_version + "/"
                    if namespace is not None:
                        require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", namespace))
                        prefix += "namespaces/" + namespace + "/"
                    guard()
                    require(reader._identity())
                    guard()
                    response = reader.runner(
                        [
                            "adp-cred",
                            "assume",
                            "--service",
                            "aws",
                            "--label",
                            reader.broker_label,
                            "--exec",
                            "kubectl",
                            "--kubeconfig",
                            str(config),
                            "--request-timeout=30s",
                            "get",
                            "--raw",
                            prefix + resource + "/" + name,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    guard()
                    require(response.returncode == 0)
                    body = json.loads(response.stdout)
                    require(
                        body["apiVersion"] == api_version
                        and body["kind"] == kind
                        and body["metadata"]["name"] == name
                        and body["metadata"].get("namespace", "") == (namespace or "")
                        and not body["metadata"].get("deletionTimestamp")
                    )
                    return body

                return SimpleNamespace(get=get)

        client = SimpleNamespace(
            resources=Resources(),
            client=SimpleNamespace(
                configuration=SimpleNamespace(
                    host=target.endpoint,
                    ssl_ca_cert=str(ca),
                    verify_ssl=True,
                    assert_hostname=None,
                    tls_server_name=None,
                    proxy=None,
                )
            ),
        )
        grants = KubeGrants(client, target)
        inventory = SimpleNamespace(
            cluster_ownership="adp-created",
            remove_namespace=True,
            cluster_arn=target.cluster_arn,
            grants=tuple(OwnedGrant(**row) for row in rows),
        )
        verify(grants, inventory, material["fence"])
        for row in rows:
            if row["spec"]["key"].startswith("cleanup-"):
                actual = grants.observe(row["spec"])
                require(actual == row["identity"])
                grants.verify(row["spec"], actual)
        verify(grants, inventory, material["fence"])
    return {
        "status": "OBSERVED",
        "observed_at": reader.clock().isoformat(),
        "inventory_ref": reference(expected_digest),
        "cleanup_grant_count": 6,
        "fence_ref": reference(digest(material["fence"])),
        "scope": "current recorded Kubernetes grants and active fence only; resource coverage and ongoing deletion authority unverified",
    }

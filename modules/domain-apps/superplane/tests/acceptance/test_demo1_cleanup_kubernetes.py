"""Synthetic Kubernetes reads exercise real grant and fence validators offline."""

import base64
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_demo1_aws as aws_fixtures
from superplane_bootstrap.kube_grants import _digest
from superplane_bootstrap.retirement_fence import documents
from test_demo1_cleanup_grants import GrantCli

from superplane_acceptance.demo1_cleanup_kubernetes import observe_kubernetes
from superplane_acceptance.demo1_evidence import EvidenceError
from workspace_provisioning.artifacts import digest
from workspace_provisioning.retirement_fence import KEY, review_recipe
from workspace_provisioning.retirement_inventory import OwnedGrant

selection = aws_fixtures.selection


def material(selected):
    cluster = f"arn:aws:eks:{selected.region}:{selected.account}:cluster/example-owned"
    generation = "a" * 64
    rows = []
    for scope in ("cluster", "namespace", "system"):
        for kind in ("role", "binding"):
            name = f"cleanup-{scope}-{kind}"
            body = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": ("Cluster" if scope == "cluster" else "")
                + ("Role" if kind == "role" else "RoleBinding"),
                "metadata": {
                    "name": name,
                    "annotations": {
                        "superplane.aws-e/authority-generation": generation
                    },
                },
            }
            if scope != "cluster":
                body["metadata"]["namespace"] = (
                    "kube-system" if scope == "system" else "example-workspace"
                )
            if kind == "role":
                body["rules"] = [
                    {
                        "apiGroups": [""],
                        "resources": ["pods"],
                        "verbs": ["get", "delete"],
                    }
                ]
            else:
                body["roleRef"] = {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole" if scope == "cluster" else "Role",
                    "name": f"cleanup-{scope}-role",
                }
                body["subjects"] = [
                    {
                        "kind": "Group",
                        "apiGroup": "rbac.authorization.k8s.io",
                        "name": "example-cleanup",
                    }
                ]
            rows.append(
                {
                    "spec": {
                        "key": name,
                        "kind": "kubernetes",
                        "body": body,
                        "cluster_arn": cluster,
                        "generation": generation,
                    },
                    "identity": {
                        "uid": name + "-uid",
                        "generation": generation,
                        "digest": _digest(body),
                    },
                }
            )
    for key, body in zip(
        ("retirement-fence-policy", "retirement-fence-binding"),
        documents("sp-bootstrap-" + generation[:24] + "-retirement", generation),
        strict=True,
    ):
        rows.append(
            {
                "spec": {
                    "key": key,
                    "kind": "kubernetes",
                    "body": body,
                    "cluster_arn": cluster,
                    "generation": generation,
                },
                "identity": {
                    "uid": key + "-uid",
                    "generation": generation,
                    "digest": _digest(body),
                },
            }
        )
    inventory = SimpleNamespace(
        cluster_ownership="adp-created",
        remove_namespace=True,
        cluster_arn=cluster,
        grants=tuple(OwnedGrant(**row) for row in rows),
    )
    return {
        "transport": {
            "cluster_arn": cluster,
            "cluster_name": "example-owned",
            "cluster_endpoint": "https://example-cluster.example.invalid",
            "cluster_certificate_authority_data": base64.b64encode(
                b"fictional-test-ca"
            ).decode(),
        },
        "grants": rows,
        "fence": {
            "version": 1,
            "identity": review_recipe(inventory, {})[KEY]["arguments"],
            "managed_workload_inventory": [],
            "managed_workload_inventory_sha256": digest([]),
        },
    }


class KubernetesCli(GrantCli):
    def __init__(self, selected):
        super().__init__(selected)
        self.material = material(selected)
        self.kube_calls = []
        self.change_kube = lambda body: body
        self.change_cluster = lambda cluster: cluster
        self.config_paths = []

    def __call__(self, command, **options):
        if command[8:10] == ["eks", "describe-cluster"]:
            assert command[10:] == [
                "--name",
                "example-owned",
                "--region",
                self.selected.region,
                "--output",
                "json",
            ]
            assert self.calls[-1][8:10] == ["sts", "get-caller-identity"]
            self.calls.append(command)
            transport = self.material["transport"]
            cluster = {
                "arn": transport["cluster_arn"],
                "name": transport["cluster_name"],
                "endpoint": transport["cluster_endpoint"],
                "certificateAuthority": {
                    "data": transport["cluster_certificate_authority_data"]
                },
                "status": "ACTIVE",
                "accessConfig": {"authenticationMode": "API"},
            }
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"cluster": self.change_cluster(cluster)}), ""
            )
        if command[7] != "kubectl":
            return super().__call__(command, **options)
        assert command[:8] == [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            "example-connection",
            "--exec",
            "kubectl",
        ]
        assert command[8] == "--kubeconfig" and command[10:13] == [
            "--request-timeout=30s",
            "get",
            "--raw",
        ]
        assert self.calls[-1][8:10] == ["sts", "get-caller-identity"]
        assert 0 < options["timeout"] <= 30
        path = Path(command[9])
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        config = json.loads(path.read_text())
        assert config["clusters"][0]["cluster"] == {
            "server": self.material["transport"]["cluster_endpoint"],
            "certificate-authority-data": self.material["transport"][
                "cluster_certificate_authority_data"
            ],
        }
        assert config["users"][0]["user"]["exec"]["command"] == "aws"
        assert config["users"][0]["user"]["exec"]["args"] == [
            "eks",
            "get-token",
            "--cluster-name",
            "example-owned",
            "--region",
            self.selected.region,
            "--output",
            "json",
        ]
        self.config_paths.append(path)
        self.calls.append(command)
        self.kube_calls.append(command)
        for row in self.material["grants"]:
            body = copy.deepcopy(row["spec"]["body"])
            metadata = body["metadata"]
            prefix = "/apis/" + body["apiVersion"] + "/"
            if metadata.get("namespace"):
                prefix += "namespaces/" + metadata["namespace"] + "/"
            expected = (
                prefix
                + {
                    "Role": "roles",
                    "RoleBinding": "rolebindings",
                    "ClusterRole": "clusterroles",
                    "ClusterRoleBinding": "clusterrolebindings",
                    "ValidatingAdmissionPolicy": "validatingadmissionpolicies",
                    "ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings",
                }[body["kind"]]
                + "/"
                + metadata["name"]
            )
            if command[-1] != expected:
                continue
            metadata["uid"] = row["identity"]["uid"]
            if body["kind"] == "ValidatingAdmissionPolicy":
                body["spec"] = documents(
                    metadata["name"], row["spec"]["generation"], active=True
                )[0]["spec"]
                metadata["generation"] = 2
                body["status"] = {"observedGeneration": 2, "typeChecking": {}}
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.change_kube(body)), ""
            )
        pytest.fail("unexpected Kubernetes read")


def check(selected, executor):
    return observe_kubernetes(
        aws_fixtures.reader(selected, executor),
        selected,
        "example-workspace",
        executor.material,
        digest(executor.material),
    )


def test_current_cleanup_rbac_and_active_fence_are_observed_without_mutations(
    selection,
):
    executor = KubernetesCli(selection)
    result = check(selection, executor)
    assert result["status"] == "OBSERVED" and result["cleanup_grant_count"] == 6
    assert len(executor.kube_calls) == 10
    assert all(not path.exists() for path in executor.config_paths)
    assert selection.account not in json.dumps(result)
    assert "example-cluster" not in json.dumps(result)


@pytest.mark.parametrize(
    "case",
    [
        "uid",
        "permissions",
        "labels",
        "role-ref",
        "subjects",
        "generation",
        "deleting",
        "foreign-object",
        "inactive-fence",
        "stale-status",
        "type-warning",
        "binding",
        "fence-replaced-after-grants",
    ],
)
def test_replaced_or_broadened_kubernetes_authority_is_refused(selection, case):
    executor = KubernetesCli(selection)

    def changed(body):
        kind = body["kind"]
        if case == "uid":
            body["metadata"]["uid"] += "-replacement"
        elif case == "permissions" and kind == "ClusterRole":
            body["rules"][0]["verbs"] = ["*"]
        elif case == "labels" and kind == "ClusterRole":
            body["metadata"]["labels"] = {
                "rbac.authorization.k8s.io/aggregate-to-admin": "true"
            }
        elif case == "role-ref" and kind == "ClusterRoleBinding":
            body["roleRef"]["name"] = "cluster-admin"
        elif case == "subjects" and kind == "RoleBinding":
            body["subjects"].append({"kind": "Group", "name": "foreign-group"})
        elif case == "generation":
            body["metadata"]["annotations"]["superplane.aws-e/authority-generation"] = (
                "f" * 64
            )
        elif case == "deleting":
            body["metadata"]["deletionTimestamp"] = "2026-10-06T00:00:00Z"
        elif case == "foreign-object":
            body["metadata"]["name"] += "-foreign"
        elif kind == "ValidatingAdmissionPolicy":
            if case == "inactive-fence":
                body["spec"]["validations"][0]["expression"] = "true"
            elif case == "stale-status":
                body["status"]["observedGeneration"] = 1
            elif case == "type-warning":
                body["status"]["typeChecking"]["expressionWarnings"] = [
                    {"warning": "invalid expression"}
                ]
            elif case == "fence-replaced-after-grants" and len(executor.kube_calls) > 8:
                body["metadata"]["uid"] += "-replacement"
        elif kind == "ValidatingAdmissionPolicyBinding" and case == "binding":
            body["spec"]["validationActions"] = ["Warn"]
        return body

    executor.change_kube = changed
    with pytest.raises(EvidenceError, match="cleanup Kubernetes"):
        check(selection, executor)
    assert all(not path.exists() for path in executor.config_paths)


@pytest.mark.parametrize(
    "field",
    ["endpoint", "certificateAuthority", "arn", "name", "status", "accessConfig"],
)
def test_cluster_identity_changes_refuse_before_kubernetes(selection, field):
    executor = KubernetesCli(selection)
    executor.change_cluster = lambda cluster: {
        **cluster,
        field: {} if field in {"certificateAuthority", "accessConfig"} else "changed",
    }
    with pytest.raises(EvidenceError, match="cleanup Kubernetes"):
        check(selection, executor)
    assert executor.kube_calls == []


@pytest.mark.parametrize(
    "failure", ["denied", "missing", "malformed", "timeout", "wrong-role"]
)
def test_unavailable_kubernetes_observation_never_passes(selection, failure):
    executor = KubernetesCli(selection)

    def run(command, **options):
        if failure == "wrong-role" and len(executor.calls) >= 2:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"Account": selection.account, "Arn": "wrong-role"}),
                "",
            )
        if command[7] != "kubectl":
            return executor(command, **options)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output="private-provider-data")
        return subprocess.CompletedProcess(
            command,
            0 if failure == "malformed" else 1,
            "private-provider-data",
            failure,
        )

    with pytest.raises(EvidenceError, match="cleanup Kubernetes") as caught:
        observe_kubernetes(
            aws_fixtures.reader(selection, run),
            selection,
            "example-workspace",
            executor.material,
            digest(executor.material),
        )
    assert "private-provider-data" not in str(caught.value)

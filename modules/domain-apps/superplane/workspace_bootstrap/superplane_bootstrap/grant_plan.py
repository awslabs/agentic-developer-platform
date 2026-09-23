"""Compile service-owned bootstrap grants from the verified release and target.

The registrar's EKS administration is temporary and confined to one cluster.
The installer has named cluster-object permissions and namespaced installation
permissions. A separate workspace supervisor retains inventory/SAR reads and
the scheduling interlock; it cannot install workloads, read secrets or edit RBAC.
Requests never supply policy documents, groups or grant names.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .components import _namespace_labels
from .errors import BootstrapRefused
from .kube_grants import GENERATION_ANNOTATION

ADMIN_POLICY = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
RBAC = "rbac.authorization.k8s.io"


@dataclass(frozen=True)
class BootstrapRelease:
    namespace: str
    service_account: str
    controller: str
    enforce_version: str
    crds: tuple[str, ...]
    system_workloads: tuple[str, ...] = ("coredns",)

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", self.namespace):
            raise BootstrapRefused("workspace namespace must be a Kubernetes DNS label")
        if not re.fullmatch(r"v1\.[0-9]+", self.enforce_version):
            raise BootstrapRefused("bootstrap requires a pinned Pod Security version")
        if not self.system_workloads or len(set(self.system_workloads)) != len(
            self.system_workloads
        ):
            raise BootstrapRefused(
                "bootstrap requires a nonempty pinned system workload inventory"
            )
        for name in (
            self.namespace,
            self.service_account,
            self.controller,
            *self.crds,
            *self.system_workloads,
        ):
            if not isinstance(name, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]|[a-z0-9]", name
            ):
                raise BootstrapRefused(
                    "bootstrap release contains an invalid Kubernetes name"
                )
        if self.namespace in {
            "default",
            "kube-system",
            "kube-public",
            "kube-node-lease",
        }:
            raise BootstrapRefused("bootstrap requires a dedicated workspace namespace")
        if not self.crds or len(set(self.crds)) != len(self.crds):
            raise BootstrapRefused("bootstrap requires a pinned CRD inventory")


def rule(group, resources, verbs, names=()):
    value = {"apiGroups": [group], "resources": list(resources), "verbs": list(verbs)}
    if names:
        value["resourceNames"] = list(names)
    return value


def compile_grants(journal, release: BootstrapRelease, principals):
    """Names and usernames bind to this real reservation generation."""
    if (
        set(principals) != {"registrar", "installer", "supervisor"}
        or len(set(principals.values())) != 3
    ):
        raise BootstrapRefused(
            "registrar, installer and supervisor require distinct service roles"
        )
    target, generation = journal.target, journal.generation
    prefix = f"arn:aws:iam::{target.account_id}:role/"
    if any(
        not isinstance(p, str) or not p.startswith(prefix) or len(p) <= len(prefix)
        for p in principals.values()
    ):
        raise BootstrapRefused(
            "bootstrap roles must belong to the verified target account"
        )
    stem = "sp-bootstrap-" + generation[:24]
    grants = []

    def entry(actor, lifetime="temporary"):
        value = {
            "key": actor + "-entry",
            "kind": "eks-entry",
            "actor": actor,
            "cluster_arn": target.cluster_arn,
            "generation": generation,
            "principal_arn": principals[actor],
            "groups": [stem + ":" + actor],
            "username": stem + ":" + actor + ":{{SessionName}}",
            "client_token": sha256((generation + ":" + actor).encode()).hexdigest(),
            "lifetime": lifetime,
        }
        grants.append(value)
        return value

    def kube(
        key,
        kind,
        name,
        contents,
        *,
        namespace=None,
        lifetime="temporary",
        actor="installer",
    ):
        metadata = {"name": name, "annotations": {GENERATION_ANNOTATION: generation}}
        if namespace:
            metadata["namespace"] = namespace
        body = {
            "apiVersion": "v1" if kind == "Namespace" else RBAC + "/v1",
            "kind": kind,
            "metadata": metadata,
            **contents,
        }
        value = {
            "key": key,
            "kind": "kubernetes",
            "actor": actor,
            "cluster_arn": target.cluster_arn,
            "generation": generation,
            "body": body,
            "lifetime": lifetime,
        }
        grants.append(value)
        return value

    def role_pair(key, rules, actor, *, namespace=None, lifetime="temporary"):
        name = stem + "-" + key
        kind = "Role" if namespace else "ClusterRole"
        kube(
            key + "-role",
            kind,
            name,
            {"rules": rules},
            namespace=namespace,
            lifetime=lifetime,
            actor=actor,
        )
        kube(
            key + "-binding",
            kind + "Binding",
            name,
            {
                "roleRef": {"apiGroup": RBAC, "kind": kind, "name": name},
                "subjects": [
                    {"apiGroup": RBAC, "kind": "Group", "name": stem + ":" + actor}
                ],
            },
            namespace=namespace,
            lifetime=lifetime,
            actor=actor,
        )

    registrar = entry("registrar")
    grants.append(
        {
            **registrar,
            "key": "registrar-policy",
            "kind": "eks-policy",
            "policy_arn": ADMIN_POLICY,
            "scope": {"type": "cluster"},
        }
    )

    # The registrar establishes the namespace before namespaced RBAC can exist.
    # An existing namespace is only adopted after its admission labels are checked.
    namespace = kube(
        "workspace-namespace",
        "Namespace",
        release.namespace,
        {},
        lifetime="resource",
        actor="registrar",
    )
    namespace["body"]["metadata"]["labels"] = _namespace_labels(
        target, release.enforce_version
    )

    entry("supervisor", "workspace")
    read_cluster = [
        rule("", ["namespaces", "nodes"], ["get", "list"]),
        rule("", ["nodes"], ["patch"]),
        rule(RBAC, ["clusterroles", "clusterrolebindings"], ["get", "list"]),
        rule("apps", ["deployments"], ["get", "list"]),
        rule("apiextensions.k8s.io", ["customresourcedefinitions"], ["get", "list"]),
        rule("authorization.k8s.io", ["subjectaccessreviews"], ["create"]),
    ]
    role_pair("supervisor-cluster", read_cluster, "supervisor", lifetime="workspace")
    role_pair(
        "supervisor-namespace",
        [
            rule("", ["serviceaccounts"], ["get", "list"]),
            rule(RBAC, ["roles", "rolebindings"], ["get", "list"]),
            rule("coordination.k8s.io", ["leases"], ["get"], ["superplane-controller"]),
        ],
        "supervisor",
        namespace=release.namespace,
        lifetime="workspace",
    )

    role_pair(
        "supervisor-system",
        [rule(RBAC, ["roles", "rolebindings"], ["get", "list"])],
        "supervisor",
        namespace="kube-system",
        lifetime="workspace",
    )

    entry("installer")
    controller_role = release.service_account + "-workspace"
    controller_cluster_role = (
        release.service_account + "-" + release.namespace + "-cluster"
    )
    role_pair(
        "installer-cluster",
        [
            rule("", ["namespaces"], ["get"], [release.namespace]),
            rule("", ["nodes"], ["get", "list"]),
            rule("apps", ["deployments"], ["get", "list"]),
            rule(
                "apiextensions.k8s.io",
                ["customresourcedefinitions"],
                ["create", "list"],
            ),
            rule(
                "apiextensions.k8s.io",
                ["customresourcedefinitions"],
                ["get", "patch"],
                release.crds,
            ),
            rule(RBAC, ["clusterroles", "clusterrolebindings"], ["create", "list"]),
            rule(
                RBAC,
                ["clusterroles"],
                ["get", "patch", "bind", "escalate"],
                [controller_cluster_role],
            ),
            rule(
                RBAC,
                ["clusterrolebindings"],
                ["get", "patch"],
                [controller_cluster_role],
            ),
            rule("authorization.k8s.io", ["subjectaccessreviews"], ["create"]),
        ],
        "installer",
    )
    role_pair(
        "installer-namespace",
        [
            rule("", ["serviceaccounts"], ["create", "list"]),
            rule("", ["serviceaccounts"], ["get", "patch"], [release.service_account]),
            rule("", ["pods"], ["create", "get", "list", "watch"]),
            rule("", ["pods"], ["delete"], ["imds-probe-ipv4", "imds-probe-ipv6"]),
            rule(
                "",
                ["pods/attach"],
                ["create", "get"],
                ["imds-probe-ipv4", "imds-probe-ipv6"],
            ),
            rule("", ["pods/log"], ["get"], ["imds-probe-ipv4", "imds-probe-ipv6"]),
            rule("apps", ["deployments"], ["create"]),
            rule(
                "apps", ["deployments"], ["get", "patch", "watch"], [release.controller]
            ),
            rule(RBAC, ["roles", "rolebindings"], ["create", "list"]),
            rule(
                RBAC, ["roles"], ["get", "patch", "bind", "escalate"], [controller_role]
            ),
            rule(RBAC, ["rolebindings"], ["get", "patch"], [controller_role]),
            rule("coordination.k8s.io", ["leases"], ["get"], ["superplane-controller"]),
        ],
        "installer",
        namespace=release.namespace,
    )
    role_pair(
        "installer-system",
        [
            rule("apps", ["deployments"], ["get", "patch"], release.system_workloads),
        ],
        "installer",
        namespace="kube-system",
    )
    return {"version": 1, "grants": grants}

"""Ownership precedes retirement snapshots; fixed policy identities remain pinned."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.retirement_fence import documents
from superplane_bootstrap.workload_inventory import (
    capture_system_baseline,
    ownership_closure,
)

from workspace_provisioning.artifacts import digest
from workspace_provisioning.retirement_fence import (
    KEY,
    review_recipe,
    validate_fence_metadata,
    verify,
)
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_retirement_managed_access import inputs


def body(kind, name, uid, *, namespace="kube-system", owner=None):
    value = {
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace, "uid": uid},
    }
    if owner:
        value["metadata"]["ownerReferences"] = [
            dict(kind=owner[0], name=owner[1], uid=owner[2], controller=True)
        ]
    return value


def test_original_roots_authorize_only_exact_uid_descendants():
    root = body("Deployment", "coredns", "d1")
    replica = body(
        "ReplicaSet", "coredns-1", "r1", owner=("Deployment", "coredns", "d1")
    )
    pod = body("Pod", "coredns-1-x", "p1", owner=("ReplicaSet", "coredns-1", "r1"))
    assert (
        len(
            ownership_closure(
                [pod, replica, root], [("Deployment", "kube-system", "coredns", "d1")]
            )
        )
        == 3
    )
    for foreign in (
        body("Deployment", "coredns", "replaced"),
        body("Pod", "foreign", "f1"),
        body(
            "Pod",
            "foreign",
            "f1",
            namespace="foreign",
            owner=("Deployment", "coredns", "d1"),
        ),
    ):
        with pytest.raises(BootstrapRefused, match="ownership"):
            ownership_closure(
                [root, foreign], [("Deployment", "kube-system", "coredns", "d1")]
            )


def test_baseline_cannot_relabel_replaced_provider_root_as_owned(monkeypatch):
    from superplane_bootstrap import workload_inventory

    root = body("Deployment", "coredns", "original")
    monkeypatch.setattr(workload_inventory, "read", lambda grants: [root])
    journal = SimpleNamespace(
        target=SimpleNamespace(is_adopted=False, cluster_arn="arn"),
        original_allocation_id="allocation",
        binding=SimpleNamespace(operation_id="operation"),
        generation="generation",
    )
    first = capture_system_baseline(None, journal, {})
    assert first["operation_id"] == "operation"
    assert capture_system_baseline(None, journal, {}, previous=first) == first
    root["metadata"]["uid"] = "replacement"
    with pytest.raises(BootstrapRefused, match="ownership"):
        capture_system_baseline(None, journal, {}, previous=first)


def test_fence_uses_original_bootstrap_uids_and_exact_active_policy(runtime):
    arguments = inputs(runtime)
    inventory = arguments["inventory"]
    recipe = review_recipe(inventory, arguments["runtime"])
    metadata = dict(
        version=1,
        identity=recipe[KEY]["arguments"],
        managed_workload_inventory=[],
        managed_workload_inventory_sha256=digest([]),
    )
    grants = KubeGrants(runtime.clients.supervisor_kubernetes, runtime.target)
    with pytest.raises(LifecycleRefused, match="specification"):
        verify(grants, inventory, metadata)
    policy = next(
        g for g in inventory.grants if g.spec.get("key") == "retirement-fence-policy"
    )
    name = policy.spec["body"]["metadata"]["name"]
    live = runtime.cloud.objects[("ValidatingAdmissionPolicy", None, name)]
    live["spec"] = documents(name, policy.spec["generation"], active=True)[0]["spec"]
    live["metadata"]["generation"] = 2
    live["status"] = {
        "observedGeneration": 2,
        "typeChecking": {"expressionWarnings": []},
    }
    assert verify(grants, inventory, metadata)
    live["metadata"]["uid"] = "replaced"
    with pytest.raises(LifecycleRefused, match="identity"):
        verify(grants, inventory, metadata)
    with pytest.raises(LifecycleRefused, match="exclusive"):
        review_recipe(replace(inventory, cluster_ownership="adopted"), {})
    cleanup = next(
        g for g in inventory.grants if g.spec.get("key") == "cleanup-cluster-role"
    )
    admission = [
        rule
        for rule in cleanup.spec["body"]["rules"]
        if "admissionregistration.k8s.io" in rule["apiGroups"]
    ]
    assert admission == [
        {
            "apiGroups": ["admissionregistration.k8s.io"],
            "resources": [
                "validatingadmissionpolicies",
                "validatingadmissionpolicybindings",
            ],
            "verbs": ["get", "patch"],
            "resourceNames": [name],
        }
    ]


def test_metadata_rejects_changed_or_duplicate_workload_inventory():
    identity = dict(
        cluster_arn="arn",
        name="name",
        policy_uid="p",
        binding_uid="b",
        generation="a" * 64,
        active_spec_sha256="b" * 64,
    )
    rows = [["Pod", "ns", "p", "uid"]]
    value = dict(
        version=1,
        identity=identity,
        managed_workload_inventory=rows,
        managed_workload_inventory_sha256=digest(rows),
    )
    assert validate_fence_metadata(value) == value
    changed = deepcopy(value)
    changed["managed_workload_inventory"][0][3] = "other"
    with pytest.raises(LifecycleRefused, match="digest"):
        validate_fence_metadata(changed)
    changed["managed_workload_inventory"] = rows + rows
    changed["managed_workload_inventory_sha256"] = digest(rows + rows)
    with pytest.raises(LifecycleRefused, match="digest"):
        validate_fence_metadata(changed)

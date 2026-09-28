"""Temporary registrar scope, exact cleanup objects and a distinct allocation."""

from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

from workspace_provisioning.retirement_access_plan import (
    ADMIN_POLICY,
    compile_access_plan,
)
from workspace_provisioning.retirement_inventory import OwnedGrant
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_policy import policy
from .test_retirement_plan import component, inventory


def inputs():
    owned = inventory(
        cluster_ownership="adopted",
        remove_namespace=False,
        components=(component("ours"), component("foreign", owned=False)),
    )
    runtime = policy()["runtime"]
    runtime["namespace"] = owned.namespace
    return owned, runtime


def compile_plan(owned, runtime, request_id="22222222-2222-4222-8222-222222222222"):
    return compile_access_plan(
        owned,
        runtime,
        original_allocation_id="original-allocation",
        retirement_request_id=request_id,
    )


def test_same_request_keeps_distinct_allocation_and_changed_request_is_separate():
    owned, runtime = inputs()
    first = compile_plan(owned, runtime)
    assert first == compile_plan(owned, runtime)
    assert first.allocation_id != first.original_allocation_id
    assert first.allocation_id != first.request_id
    second = compile_plan(owned, runtime, str(uuid.uuid4()))
    assert second.allocation_id != first.allocation_id
    assert second.generation != first.generation


def test_real_bootstrap_supervisor_cluster_grants_block_namespaced_cleanup():
    from superplane_bootstrap.grant_plan import BootstrapRelease, compile_grants
    from workspace_provisioning.retirement_plan import compose_retirement_plan

    owned, runtime = inputs()
    account_id = owned.cluster_arn.split(":")[4]
    target = SimpleNamespace(
        account_id=account_id,
        org_id=owned.org_id,
        workspace_id=owned.workspace_id,
        cluster_arn=owned.cluster_arn,
    )
    original = compile_grants(
        SimpleNamespace(target=target, generation="a" * 64),
        BootstrapRelease(
            namespace=owned.namespace,
            service_account="superplane-controller",
            controller="superplane-controller",
            enforce_version="v1.31",
            crds=("nodepools.superplane.ai",),
        ),
        {
            actor: f"arn:aws:iam::{account_id}:role/{name}"
            for actor, name in runtime["actor_role_names"].items()
        },
    )
    retained = tuple(
        OwnedGrant(spec, {"uid": spec["key"], "generation": spec["generation"]})
        for spec in original["grants"]
        if spec.get("lifetime") == "workspace"
        and spec.get("body", {}).get("kind") in {"ClusterRole", "ClusterRoleBinding"}
    )
    assert {grant.spec["key"] for grant in retained} == {
        "supervisor-cluster-role",
        "supervisor-cluster-binding",
    }
    actual = replace(owned, grants=retained)
    assert not compose_retirement_plan(actual).completes_teardown
    with pytest.raises(LifecycleRefused, match="independently provisioned exact-name"):
        compile_plan(actual, runtime)


def test_registrar_scope_is_explicitly_broader_than_exact_cleaner_deletes():
    owned, runtime = inputs()
    plan = compile_plan(owned, runtime)
    policy_grant = next(g for g in plan.grants if g["kind"] == "eks-policy")
    assert policy_grant["policy_arn"] == ADMIN_POLICY
    assert policy_grant["scope"] == {
        "type": "namespace",
        "namespaces": [owned.namespace],
    }
    assert plan.owned_objects == (
        {
            "kind": "ServiceAccount",
            "namespace": owned.namespace,
            "name": "ours",
            "uid": "uid-ours",
        },
    )
    rules = [
        r
        for g in plan.grants
        if g["kind"] == "kubernetes" and g["body"]["kind"] == "Role"
        for r in g["body"]["rules"]
    ]
    deletions = [r for r in rules if "delete" in r["verbs"]]
    assert deletions == [
        {
            "apiGroups": [""],
            "resources": ["serviceaccounts"],
            "verbs": ["get", "delete"],
            "resourceNames": ["ours"],
        }
    ]
    assert all(
        "namespaces" not in r["resources"] and "*" not in r["resources"] for r in rules
    )
    assert all(
        g["body"]["kind"] in {"Role", "RoleBinding"}
        for g in plan.grants
        if g["kind"] == "kubernetes"
    )
    assert any("secrets" in r["resources"] and r["verbs"] == ["list"] for r in rules)
    assert plan.revocation_order[-3:] == (
        "cleaner-entry",
        "registrar-policy",
        "registrar-entry",
    )
    assert set(plan.recipe()) == {g["key"] for g in plan.grants}


def test_system_namespace_authority_is_present_only_for_recorded_owned_grant():
    owned, runtime = inputs()
    spec = {
        "kind": "kubernetes",
        "body": {
            "kind": "Role",
            "metadata": {"namespace": "kube-system", "name": "original-observer"},
        },
    }
    owned = replace(owned, grants=(OwnedGrant(spec, {"uid": "original-system-role"}),))
    plan = compile_plan(owned, runtime)
    assert plan.registrar_namespaces == tuple(sorted([owned.namespace, "kube-system"]))
    system = next(
        g
        for g in plan.grants
        if g["kind"] == "kubernetes"
        and g["body"]["kind"] == "Role"
        and g["body"]["metadata"]["namespace"] == "kube-system"
    )
    assert system["body"]["rules"] == [
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["roles"],
            "verbs": ["get", "delete"],
            "resourceNames": ["original-observer"],
        }
    ]
    assert "kube-system" not in compile_plan(*inputs()).registrar_namespaces


@pytest.mark.parametrize(
    "change",
    ["managed", "namespace-owned", "incomplete", "retargeted", "foreign-grant"],
)
def test_unbounded_or_unowned_cleanup_access_is_refused(change):
    owned, runtime = inputs()
    if change == "managed":
        owned = replace(owned, cluster_ownership="adp-created")
    elif change == "namespace-owned":
        owned = replace(owned, remove_namespace=True)
    elif change == "incomplete":
        owned = replace(owned, components_complete=False)
    elif change == "retargeted":
        runtime["namespace"] = "another"
    else:
        spec = {
            "kind": "kubernetes",
            "body": {
                "kind": "Role",
                "metadata": {"namespace": "foreign", "name": "role"},
            },
        }
        owned = replace(owned, grants=(OwnedGrant(spec, {"uid": "foreign-role"}),))
    with pytest.raises(LifecycleRefused):
        compile_plan(owned, runtime)


def test_changed_uid_or_runtime_changes_recipe_identity():
    owned, runtime = inputs()
    original = compile_plan(owned, runtime)
    changed = replace(owned, namespace_uid="replacement")
    assert compile_plan(changed, runtime).revision != original.revision
    runtime["actor_role_names"]["installer"] = "different-cleaner"
    assert compile_plan(owned, runtime).revision != original.revision

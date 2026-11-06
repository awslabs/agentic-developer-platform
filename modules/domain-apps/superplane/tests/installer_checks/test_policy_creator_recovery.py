import copy
import json

import pytest

from installation import policy_creator_recovery as recovery
from installation.config import LABEL, Refusal
from .test_private_service_egress import configured
from .test_paid_worker import native

__all__ = ["native"]


def policies(native, name):
    env, _ = configured(native)
    old = copy.deepcopy(env)
    old["paid_worker"]["egress"].pop("interface_endpoints")
    current = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": name,
            "namespace": env["namespace"],
            "uid": "original",
            "resourceVersion": "123",
            "labels": {LABEL: "owner"},
            "managedFields": [
                {
                    "manager": "superplane-installer",
                    "operation": operation,
                    "fieldsV1": {
                        "f:spec": {"f:egress": {}},
                        "f:metadata": {
                            "f:annotations": {"f:" + recovery.ANNOTATION: {}}
                        },
                    },
                }
                for operation in ["Apply", "Update"]
            ],
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": "worker"}},
            "policyTypes": ["Ingress", "Egress"],
            "egress": recovery.recipe(old, name),
        },
    }
    if name == "superplane-paid-worker-preparation-deny":
        current["spec"]["egress"] = []
        current["metadata"]["annotations"] = {
            recovery.ANNOTATION: json.dumps(recovery.recipe(old, name))
        }
    desired = copy.deepcopy(current)
    desired["metadata"].pop("managedFields")
    desired["spec"]["ingress"] = []
    if name == "superplane-paid-worker-preparation-deny":
        desired["metadata"]["annotations"][recovery.ANNOTATION] = json.dumps(
            recovery.recipe(env, name)
        )
    else:
        desired["spec"]["egress"] = recovery.recipe(env, name)
    return env, current, desired, {name: "original"}


@pytest.mark.parametrize("name", sorted(recovery.NAMES))
def test_only_installer_creator_conflict_gets_reviewed_transfer(native, name):
    env, current, desired, uids = policies(native, name)
    assert recovery.policy_args(env, "owner", desired, current, uids) == [
        "--force-conflicts"
    ]
    current["metadata"]["managedFields"] = current["metadata"]["managedFields"][:1]
    assert recovery.policy_args(env, "owner", desired, current, uids) == []


def test_unchanged_policy_and_other_resources_never_force(native):
    env, current, desired, uids = policies(native, "superplane-binding-check")
    desired["spec"]["egress"] = current["spec"]["egress"]
    assert recovery.policy_args(env, "owner", desired, current, uids) == []
    desired["kind"] = "Deployment"
    assert recovery.policy_args(env, "owner", desired, current, uids) == []


@pytest.mark.parametrize(
    "fault",
    [
        "foreign-manager",
        "missing-owner",
        "subresource",
        "uid",
        "resource-version",
        "reviewed-uid",
        "owner",
        "namespace",
        "selector",
        "ingress",
        "broad-egress",
        "unreviewed-old-egress",
        "types",
        "annotation",
        "labels",
        "deleting",
    ],
)
def test_unreviewed_changes_and_foreign_ownership_refuse(native, fault):
    env, current, desired, uids = policies(native, "superplane-binding-check")
    if fault == "foreign-manager":
        current["metadata"]["managedFields"][1]["manager"] = "another-controller"
    elif fault == "missing-owner":
        current["metadata"]["managedFields"] = []
    elif fault == "subresource":
        current["metadata"]["managedFields"][1]["subresource"] = "status"
    elif fault == "uid":
        desired["metadata"]["uid"] = "replaced"
    elif fault == "resource-version":
        desired["metadata"]["resourceVersion"] = "124"
    elif fault == "reviewed-uid":
        uids[desired["metadata"]["name"]] = "other"
    elif fault == "owner":
        current["metadata"]["labels"][LABEL] = "foreign"
    elif fault == "namespace":
        current["metadata"]["namespace"] = "foreign"
    elif fault == "selector":
        desired["spec"]["podSelector"] = {}
    elif fault == "ingress":
        desired["spec"]["ingress"] = [{}]
    elif fault == "broad-egress":
        desired["spec"]["egress"] = [{}]
    elif fault == "unreviewed-old-egress":
        current["spec"]["egress"] = [{}]
    elif fault == "types":
        desired["spec"]["policyTypes"] = ["Egress"]
    elif fault == "annotation":
        desired["metadata"]["annotations"] = {"other": "changed"}
    elif fault == "labels":
        desired["metadata"]["labels"]["new"] = "changed"
    elif fault == "deleting":
        current["metadata"]["deletionTimestamp"] = "now"
    with pytest.raises(Refusal):
        recovery.policy_args(env, "owner", desired, current, uids)


@pytest.mark.parametrize("active", [True, False])
def test_worker_activation_and_disabled_compensation_are_exact_recipes(native, active):
    name = "superplane-paid-worker-preparation-deny"
    env, current, desired, uids = policies(native, name)
    current["metadata"]["annotations"] = copy.deepcopy(
        desired["metadata"]["annotations"]
    )
    if active:
        desired["spec"]["egress"] = recovery.recipe(env, name)
    else:
        current["spec"]["egress"] = recovery.recipe(env, name)
    assert recovery.policy_args(env, "owner", desired, current, uids) == [
        "--force-conflicts"
    ]
    desired["metadata"]["annotations"][recovery.ANNOTATION] = json.dumps([{}])
    with pytest.raises(Refusal):
        recovery.policy_args(env, "owner", desired, current, uids)


def test_recipe_selection_matches_existing_worker_and_standalone_probe_renderers():
    from types import SimpleNamespace

    original = {"reviewed": "immutable"}
    discovered = {**original, "cluster_dns_ip": "172.20.0.10"}
    installer = SimpleNamespace(env=original, network_environment=discovered)
    assert (
        recovery.policy_environment(installer, "superplane-binding-check") is original
    )
    assert (
        recovery.policy_environment(installer, "superplane-network-probe") is original
    )
    assert (
        recovery.policy_environment(
            installer, "superplane-paid-worker-preparation-deny"
        )
        is discovered
    )


@pytest.mark.parametrize("active", [True, False])
def test_worker_runtime_rules_and_discovered_dns_annotation_keep_distinct_rendering(
    native, active
):
    name = "superplane-paid-worker-preparation-deny"
    env, current, desired, uids = policies(native, name)
    rendered = dict(env, cluster_dns_ip="172.20.0.10")
    annotation = json.dumps(recovery.recipe(rendered, name))
    current["metadata"]["annotations"][recovery.ANNOTATION] = annotation
    desired["metadata"]["annotations"][recovery.ANNOTATION] = annotation
    if active:
        desired["spec"]["egress"] = recovery.recipe(env, name)
    else:
        current["spec"]["egress"] = recovery.recipe(env, name)
    # Real worker egress has already passed from CREATE to Apply ownership.
    current["metadata"]["managedFields"] = current["metadata"]["managedFields"][:1]
    assert (
        recovery.policy_args(rendered, "owner", desired, current, uids, runtime_env=env)
        == []
    )
    desired["spec"]["egress"] = recovery.recipe(
        dict(env, cluster_dns_ip="172.20.0.99"), name
    )
    with pytest.raises(Refusal):
        recovery.policy_args(rendered, "owner", desired, current, uids, runtime_env=env)


def paused_job():
    from installation.config import digest

    current = {
        "kind": "ScaledJob",
        "metadata": {
            "name": "superplane-paid-worker",
            "namespace": "superplane",
            "uid": "worker-uid",
            "resourceVersion": "10",
            "labels": {LABEL: "owner"},
            "annotations": {recovery.PAUSED: "true", "other": "retained"},
            "managedFields": [
                {
                    "manager": "superplane-installer",
                    "operation": operation,
                    "fieldsV1": {
                        "f:metadata": {"f:annotations": {"f:" + recovery.PAUSED: {}}}
                    },
                }
                for operation in ["Apply", "Update"]
            ],
        },
        "spec": {
            "maxReplicaCount": 1,
            "jobTargetRef": {
                "template": {"spec": {"containers": [{"image": "reviewed"}]}}
            },
        },
    }
    desired = copy.deepcopy(current)
    desired["metadata"].pop("managedFields")
    desired["metadata"]["annotations"][recovery.PAUSED] = "false"
    reviewed = {
        "uid": "worker-uid",
        "spec_sha256": digest(current["spec"]),
        "desired_spec_sha256": digest(desired["spec"]),
    }
    return current, desired, reviewed


def test_pause_transfer_needs_verified_activation_and_preserves_entire_spec():
    current, desired, reviewed = paused_job()
    assert recovery.scaled_job_args(
        "owner", "superplane", desired, current, reviewed, "adapter-activation", True
    ) == ["--force-conflicts"]
    current["metadata"]["managedFields"] = current["metadata"]["managedFields"][:1]
    assert (
        recovery.scaled_job_args(
            "owner",
            "superplane",
            desired,
            current,
            reviewed,
            "adapter-activation",
            True,
        )
        == []
    )
    desired["metadata"]["annotations"][recovery.PAUSED] = "true"
    assert (
        recovery.scaled_job_args(
            "owner", "superplane", desired, current, reviewed, "foundations", False
        )
        == []
    )


@pytest.mark.parametrize(
    "fault",
    [
        "unprepared",
        "stage",
        "foreign-manager",
        "missing-owner",
        "uid",
        "version",
        "namespace",
        "owner",
        "replicas",
        "image",
        "annotation",
        "labels",
        "deleting",
    ],
)
def test_pause_recovery_cannot_escape_exact_activation_flag(fault):
    current, desired, reviewed = paused_job()
    stage, ready = "adapter-activation", True
    if fault == "unprepared":
        ready = False
    elif fault == "stage":
        stage = "foundations"
    elif fault == "foreign-manager":
        current["metadata"]["managedFields"][1]["manager"] = "another-operator"
    elif fault == "missing-owner":
        current["metadata"]["managedFields"] = []
    elif fault == "uid":
        current["metadata"]["uid"] = "replaced"
    elif fault == "version":
        desired["metadata"]["resourceVersion"] = "11"
    elif fault == "namespace":
        desired["metadata"]["namespace"] = "another"
    elif fault == "owner":
        current["metadata"]["labels"][LABEL] = "foreign"
    elif fault == "replicas":
        desired["spec"]["maxReplicaCount"] = 4
    elif fault == "image":
        current["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0][
            "image"
        ] = "foreign"
    elif fault == "annotation":
        desired["metadata"]["annotations"]["other"] = "changed"
    elif fault == "labels":
        desired["metadata"]["labels"]["new"] = "changed"
    elif fault == "deleting":
        current["metadata"]["deletionTimestamp"] = "now"
    with pytest.raises(Refusal):
        recovery.scaled_job_args(
            "owner", "superplane", desired, current, reviewed, stage, ready
        )


def test_disabled_compensation_may_restore_only_pause_flag():
    current, desired, reviewed = paused_job()
    current["metadata"]["annotations"][recovery.PAUSED] = "false"
    desired["metadata"]["annotations"][recovery.PAUSED] = "true"
    assert recovery.scaled_job_args(
        "owner",
        "superplane",
        desired,
        current,
        reviewed,
        "adapter-disabled-compensation",
        False,
    ) == ["--force-conflicts"]

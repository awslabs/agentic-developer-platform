"""Cleanup target observations never borrow namespace authority from a new grant."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from workspace_provisioning.retirement_access_clients import (
    AccessClients,
    GuardedClient,
    require_cluster,
)
from workspace_provisioning.retirement_access_plan import PHASE
from workspace_provisioning.retirement_access_runtime import check_access_call
from workspace_provisioning.runtime_config import LifecycleRefused


OUTPUTS = {
    "cluster_arn": "arn:aws:eks:us-east-1:111122223333:cluster/workspace",
    "cluster_name": "workspace",
    "cluster_endpoint": "https://verified.example.invalid",
    "cluster_certificate_authority_data": "verified-ca",
}


def cluster():
    return {
        "arn": OUTPUTS["cluster_arn"],
        "name": OUTPUTS["cluster_name"],
        "endpoint": OUTPUTS["cluster_endpoint"],
        "certificateAuthority": {"data": OUTPUTS["cluster_certificate_authority_data"]},
        "status": "ACTIVE",
        "accessConfig": {"authenticationMode": "API"},
    }


@pytest.mark.parametrize(
    "changed",
    [None, "arn", "name", "endpoint", "certificateAuthority", "status", "accessConfig"],
)
def test_cluster_replacement_or_different_authentication_mode_is_refused(changed):
    observed = cluster()
    if changed is None:
        require_cluster(observed, OUTPUTS)
        return
    observed[changed] = (
        {} if changed in {"certificateAuthority", "accessConfig"} else "changed"
    )
    with pytest.raises(LifecycleRefused, match="cluster differs"):
        require_cluster(observed, OUTPUTS)


def test_sdk_boundary_checks_authority_before_and_after_provider_call():
    order = []
    provider = SimpleNamespace(
        describe_cluster=lambda **kwargs: order.append(("provider", kwargs))
    )
    guarded = GuardedClient(provider, lambda: order.append("authority"))
    guarded.describe_cluster(name="workspace")
    assert order == ["authority", ("provider", {"name": "workspace"}), "authority"]


def test_revoked_authority_cannot_reach_sdk():
    provider = SimpleNamespace(create_access_entry=Mock())
    guarded = GuardedClient(provider, Mock(side_effect=LifecycleRefused("revoked")))
    with pytest.raises(LifecycleRefused, match="revoked"):
        guarded.create_access_entry(clusterName="workspace")
    provider.create_access_entry.assert_not_called()


@pytest.mark.parametrize(
    "changed",
    [None, "namespace-uid", "terminating", "retained-grant", "missing-supervisor"],
)
def test_namespace_uid_comes_from_retained_supervisor_and_changed_authority_refuses(
    changed,
):
    specs = [
        {"actor": "supervisor", "kind": "eks-entry", "key": "entry"},
        {
            "actor": "supervisor",
            "kind": "kubernetes",
            "key": "role",
            "body": {"kind": "ClusterRole"},
        },
        {
            "actor": "supervisor",
            "kind": "kubernetes",
            "key": "binding",
            "body": {"kind": "ClusterRoleBinding"},
        },
    ]
    identities = {spec["key"]: {"uid": spec["key"]} for spec in specs}
    grants = tuple(
        SimpleNamespace(spec=spec, identity=deepcopy(identities[spec["key"]]))
        for spec in specs
    )
    namespace = {"metadata": {"name": "workspace", "uid": "namespace-original"}}
    if changed == "namespace-uid":
        namespace["metadata"]["uid"] = "replacement"
    elif changed == "terminating":
        namespace["metadata"]["deletionTimestamp"] = "2026-09-24T09:00:00Z"
    elif changed == "retained-grant":
        identities["entry"] = {"uid": "replacement"}
    elif changed == "missing-supervisor":
        grants = ()
    resource = Mock()
    resource.get.return_value = namespace
    supervisor = Mock()
    supervisor.client.resources.get.return_value = resource
    supervisor.observe.side_effect = lambda spec: identities[spec["key"]]
    eks = Mock()
    eks.observe.side_effect = lambda spec: identities[spec["key"]]
    registrar = Mock()
    clients = AccessClients(
        eks=eks,
        kubernetes=registrar,
        supervisor=supervisor,
        target=SimpleNamespace(cluster_name="workspace"),
        outputs=OUTPUTS,
        inventory=SimpleNamespace(
            grants=grants, namespace="workspace", namespace_uid="namespace-original"
        ),
        verify=Mock(),
        dynamic_clients=(),
        provider_eks=Mock(),
    )
    clients.provider_eks.describe_cluster.return_value = {"cluster": cluster()}
    if changed is None:
        clients.observe_target()
        supervisor.client.resources.get.assert_called_once_with(
            api_version="v1", kind="Namespace"
        )
        resource.get.assert_called_once_with(name="workspace")
    else:
        with pytest.raises(LifecycleRefused):
            clients.observe_target()
    assert registrar.mock_calls == []
    eks.create.assert_not_called()


@pytest.mark.parametrize(
    "changed",
    [
        None,
        "operation_id",
        "org_id",
        "workspace_id",
        "job_id",
        "attempt_id",
        "fence_token",
        "provider",
        "operation_kind",
        "target",
    ],
)
def test_shared_execution_call_must_match_exact_control_descriptor(changed):
    lease = SimpleNamespace(
        operation_id="access",
        org_id="org",
        workspace_id="workspace",
        attempt_id="attempt",
        fence_token=7,
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease),
        job_id="job",
        request=SimpleNamespace(
            parameters={"retirement_access_recipe_sha256": "a" * 64}
        ),
    )
    call = SimpleNamespace(
        **vars(lease),
        job_id="job",
        provider="superplane-lifecycle",
        operation_kind=PHASE,
        target="a" * 64,
    )
    if changed is None:
        check_access_call(call, operation)
    else:
        setattr(call, changed, "different")
        with pytest.raises(LifecycleRefused, match="admitted cleanup"):
            check_access_call(call, operation)

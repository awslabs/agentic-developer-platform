"""Real removal hooks with provider-native UID/version conflict behavior."""

import base64
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from harness_jobs.effects import CallEffect, call_effect
from harness_jobs.execution import CallOutcome
from superplane_bootstrap.component_journal import ANNOTATION, component_identity
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION, KubeGrants
from superplane_bootstrap.target import verify_target

from workspace_provisioning.retirement_adapters import OwnedResourceRemover
from workspace_provisioning.retirement_inventory import ComponentOwnership, OwnedGrant
from workspace_provisioning.retirement_plan import compose_retirement_plan

from .test_retirement_plan import component, inventory


class ApiError(Exception):
    def __init__(self, status):
        self.status = status


class Resource:
    def __init__(self, body):
        self.body, self.error, self.before_delete = body, None, None
        self.deletes = []

    def get(self, **kwargs):
        if self.error:
            raise ApiError(self.error)
        if self.body is None:
            raise ApiError(404)
        return deepcopy(self.body)

    def delete(self, **kwargs):
        if self.before_delete:
            self.before_delete(self.body)
        if any(
            self.body["metadata"][key] != value
            for key, value in kwargs["body"]["preconditions"].items()
        ):
            raise ApiError(409)
        self.deletes.append(kwargs)
        self.body = None


@pytest.fixture
def owned(tmp_path, binding, provider_identity, observed_cluster, expected_target):
    target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )
    ca = tmp_path / "ca.pem"
    ca.write_bytes(base64.b64decode(target.certificate_authority_data))
    body = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "management",
            "namespace": "ws",
            "uid": "uid-1",
            "resourceVersion": "1",
            "annotations": {ANNOTATION: "created-by-operation"},
        },
    }
    resource = Resource(body)
    client = SimpleNamespace(
        client=SimpleNamespace(
            configuration=SimpleNamespace(
                host=target.endpoint,
                verify_ssl=True,
                assert_hostname=None,
                tls_server_name=None,
                proxy=None,
                ssl_ca_cert=str(ca),
            )
        ),
        resources=SimpleNamespace(get=lambda **_: resource),
    )
    adapter = KubeGrants(client, target)
    item = ComponentOwnership(deepcopy(body), component_identity(body), True)
    record = inventory(cluster_arn=target.cluster_arn, components=(item,))
    return OwnedResourceRemover(kubernetes=adapter, eks=None), resource, record


def component_step(record):
    return next(
        step
        for step in compose_retirement_plan(record).steps
        if step.operation_kind == "delete-controller-component"
    )


@pytest.mark.parametrize("presence", ["present", "absent", "replaced", "unreadable"])
def test_recovery_reads_original_component_without_mutation(owned, presence):
    remover, api, record = owned
    if presence == "absent":
        api.body = None
    elif presence == "replaced":
        api.body["metadata"]["uid"] = "replacement"
    elif presence == "unreadable":
        api.error = 403
    step = component_step(record)
    if presence == "unreadable":
        with pytest.raises(ApiError):
            remover.observe(step, record)
    else:
        observed, _, reference = remover.observe(step, record)
        assert observed is (
            CallOutcome.SUCCEEDED if presence == "absent" else CallOutcome.UNKNOWN
        )
        assert reference == record.components[0].identity["uid"]
    assert api.deletes == []


def test_recovery_refuses_changed_or_adopted_ownership(owned):
    remover, api, record = owned
    step = component_step(record)
    with pytest.raises(BootstrapRefused):
        remover.observe(step, replace(record, components=()))
    with pytest.raises(BootstrapRefused):
        remover.observe(
            step,
            replace(record, components=(replace(record.components[0], owned=False),)),
        )
    assert api.deletes == []


def test_component_removal_rechecks_identity_and_confirms_provider_absence(owned):
    remover, api, record = owned
    step = component_step(record)
    assert (
        call_effect(step.operation_kind, provider=step.provider) is CallEffect.REMOVES
    )
    assert remover.execute(step, record)[0] is CallOutcome.SUCCEEDED
    assert api.deletes[0]["body"]["preconditions"] == {
        "uid": "uid-1",
        "resourceVersion": "1",
    }
    assert api.deletes[0]["body"]["propagationPolicy"] == "Foreground"


@pytest.mark.parametrize("field", ["uid", "resourceVersion"])
def test_replacement_at_component_delete_is_atomically_refused(owned, field):
    remover, api, record = owned
    api.before_delete = lambda body: body["metadata"].update({field: "replacement"})
    with pytest.raises(ApiError) as exc:
        remover.execute(component_step(record), record)
    assert exc.value.status == 409
    assert not api.deletes


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_component_unanswered_read_never_becomes_success(owned, status):
    remover, api, record = owned
    api.error = status
    with pytest.raises(ApiError):
        remover.execute(component_step(record), record)
    assert not api.deletes


def test_changed_component_spec_and_adopted_objects_are_preserved(owned):
    remover, api, record = owned
    api.body["automountServiceAccountToken"] = True
    with pytest.raises(BootstrapRefused):
        remover.execute(component_step(record), record)
    adopted = replace(record, components=(replace(record.components[0], owned=False),))
    with pytest.raises(BootstrapRefused):
        remover.execute(component_step(record), adopted)
    assert not api.deletes


def test_shared_component_rbac_and_namespace_are_never_removal_candidates():
    plan = compose_retirement_plan(
        inventory(
            components=(
                component("shared", kind="ClusterRole", namespace=""),
                component("binding", kind="ClusterRoleBinding", namespace=""),
            )
        )
    )
    assert not plan.deletion_steps()
    assert len([item for item in plan.preserved if "cluster-scoped" in item]) == 2


def test_complete_grant_journal_drives_real_adapter(owned):
    remover, api, record = owned
    body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": "supervisor",
            "namespace": "ws",
            "uid": "rb-1",
            "resourceVersion": "3",
            "annotations": {GENERATION_ANNOTATION: "g1"},
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "supervisor",
        },
        "subjects": [],
    }
    api.body = body
    spec = {
        "kind": "kubernetes",
        "cluster_arn": record.cluster_arn,
        "generation": "g1",
        "body": body,
    }
    identity = remover.kubernetes.observe(spec)
    record = replace(record, components=(), grants=(OwnedGrant(spec, identity),))
    step = next(
        step
        for step in compose_retirement_plan(record).steps
        if step.operation_kind == "revoke-grant"
    )
    assert remover.observe(step, record)[0] is CallOutcome.UNKNOWN
    assert not api.deletes
    assert remover.execute(step, record)[0] is CallOutcome.SUCCEEDED
    assert remover.observe(step, record)[0] is CallOutcome.SUCCEEDED
    assert api.deletes[0]["body"]["preconditions"] == {
        "uid": "rb-1",
        "resourceVersion": "3",
    }


def test_unimplemented_actions_are_not_given_sealed_allocation_authority():
    for provider, action in (
        ("superplane-aws", "delete-arbitrary-resource"),
        ("superplane-kubernetes", "delete-namespace"),
    ):
        assert call_effect(action, provider=provider) is CallEffect.UNRECOGNIZED


def test_network_removal_uses_owned_rule_id_and_provider_absence(owned):
    from superplane_bootstrap.inventory import OwnedPrerequisite
    from superplane_bootstrap.prerequisites import ExpectedPrerequisites

    from workspace_provisioning.retirement_adapters import SecurityGroupRules

    remover, _, record = owned
    target = remover.kubernetes.target
    rule = {
        "SecurityGroupRuleId": "sgr-1",
        "GroupId": "sg-1",
        "IsEgress": False,
        "ReferencedGroupInfo": {"GroupId": "sg-management"},
        "IpProtocol": "tcp",
        "FromPort": 443,
        "ToPort": 443,
        "Tags": [
            {"Key": "OrgId", "Value": record.org_id},
            {"Key": "WorkspaceId", "Value": record.workspace_id},
        ],
    }
    calls = []

    class Absent(Exception):
        response = {"Error": {"Code": "InvalidSecurityGroupRuleId.NotFound"}}

    def read(**kwargs):
        assert kwargs == {"SecurityGroupRuleIds": ["sgr-1"]}
        if calls:
            raise Absent()
        return {"SecurityGroupRules": [rule]}

    ec2 = SimpleNamespace(
        describe_security_group_rules=read,
        describe_security_groups=lambda **_: {
            "SecurityGroups": [{"OwnerId": target.account_id, "VpcId": "vpc-1"}]
        },
        revoke_security_group_ingress=lambda **kwargs: calls.append(kwargs),
    )
    clients = {
        "ec2": ec2,
        "sts": SimpleNamespace(
            get_caller_identity=lambda: {"Account": target.account_id}
        ),
        "eks": SimpleNamespace(
            describe_cluster=lambda **_: {
                "cluster": {
                    "arn": record.cluster_arn,
                    "endpoint": target.endpoint,
                    "resourcesVpcConfig": {"vpcId": "vpc-1"},
                }
            }
        ),
    }
    network = SecurityGroupRules(
        session=SimpleNamespace(client=lambda name, **_: clients[name]),
        target=target,
        expected=ExpectedPrerequisites(
            target.account_id,
            "vpc-1",
            "sg-1",
            "sg-management",
            "sg-node",
            "sg-sts",
            "vpc-sts",
        ),
    )
    prerequisite = OwnedPrerequisite(
        "SecurityGroupRule/cluster-endpoint",
        "sgr-1",
        record.workspace_id,
        "adp-created",
        "created for workspace",
    )
    with pytest.raises(BootstrapRefused):
        network.revoke(record, replace(prerequisite, ownership="adopted"))
    assert not calls
    assert network.observe(record, prerequisite)[0] is CallOutcome.UNKNOWN
    assert not calls
    remover.network = network
    authorized = replace(record, prerequisites=(prerequisite,))
    step = next(
        item
        for item in compose_retirement_plan(authorized).steps
        if item.operation_kind == "revoke-network-prerequisite"
    )
    assert remover.observe(step, authorized)[0] is CallOutcome.UNKNOWN
    assert not calls
    assert network.revoke(record, prerequisite)[0] is CallOutcome.SUCCEEDED
    assert remover.observe(step, authorized)[0] is CallOutcome.SUCCEEDED
    assert calls == [{"GroupId": "sg-1", "SecurityGroupRuleIds": ["sgr-1"]}]

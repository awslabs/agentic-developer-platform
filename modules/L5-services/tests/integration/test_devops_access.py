"""Offline DevOps resource verification through a selected binding and broker."""

import json
import ssl
import base64
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from botocore.exceptions import ClientError

from l5_services.adapters.devops.aws_access import AWSAccessAdapter, AccessDenied, KubernetesReader
from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceSelection
from l5_services.devops.lifecycle import SpaceLifecycle
from l5_services.devops.resource_scope import ResourceSelection, policy_digest
from l5_services.models import DevopsSpaceIntent
from l5_services.models.base import Base
from test_devops_recovery import NativeSpaces


class NativeAssociations(NativeSpaces):
    def __init__(self):
        super().__init__()
        self.associations = []
        self.creates_for_association = 0
        self.lose_association_response = False
        self.fail_before_association = False

    def list_associations(self, *, agentSpaceId, **kwargs):
        return {"associations": list(self.associations)}

    def associate_service(self, *, agentSpaceId, serviceId, configuration):
        self.creates_for_association += 1
        if self.fail_before_association:
            raise ConnectionError("association request failed before provider effect")
        association = {"agentSpaceId": agentSpaceId, "associationId": "association-example",
                       "serviceId": serviceId, "configuration": configuration, "status": "valid"}
        self.associations.append(association)
        if self.lose_association_response:
            raise ConnectionError("lost association response")
        return {"association": association}

    def get_association(self, *, agentSpaceId, associationId):
        association = next(item for item in self.associations if item["associationId"] == associationId)
        return {"association": association.copy()}


class EKS:
    def __init__(self, account, region):
        self.account = account
        self.region = region
        self.calls = []
        self.observed_arn = None
        self.authentication_mode = "API"
        self.network_public = True
        self.network_private = False
        self.selected_role = None
        self.namespace = None
        self.access_entry_present = True
        self.extra_policy = False
        self.entry_groups = []
        self.entry_type = "STANDARD"
        self.entry_calls = []
        self.policy_calls = []

    def describe_cluster(self, *, name):
        self.calls.append(name)
        return {"cluster": {"name": name, "arn": self.observed_arn or
                            f"arn:aws:eks:{self.region}:{self.account}:cluster/{name}",
                            "endpoint": "https://cluster.example.invalid", "certificateAuthority": {"data": "example"},
                            "accessConfig": {"authenticationMode": self.authentication_mode},
                            "resourcesVpcConfig": {"endpointPublicAccess": self.network_public,
                                                   "endpointPrivateAccess": self.network_private}}}

    def describe_access_entry(self, *, clusterName, principalArn):
        self.entry_calls.append((clusterName, principalArn))
        if not self.access_entry_present:
            return {"accessEntry": {}}
        return {"accessEntry": {"clusterName": clusterName, "principalArn": self.selected_role,
                                "type": self.entry_type, "kubernetesGroups": self.entry_groups}}

    def list_associated_access_policies(self, *, clusterName, principalArn, **kwargs):
        self.policy_calls.append((clusterName, principalArn, kwargs.get("nextToken")))
        if kwargs.get("nextToken") == "page-2":
            policies = [{"policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                         "accessScope": {"type": "cluster"}}]
            return {"clusterName": clusterName, "principalArn": self.selected_role,
                    "associatedAccessPolicies": policies}
        policy = {"policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonAIOpsAssistantPolicy",
                  "accessScope": {"type": "namespace", "namespaces": [self.namespace]}}
        return {"clusterName": clusterName, "principalArn": self.selected_role,
                "associatedAccessPolicies": [policy],
                **({"nextToken": "page-2"} if self.extra_policy else {})}


class Session:
    def __init__(self, provider, iam, function=None, logs=None):
        self.provider = provider
        self.iam = iam
        self.function = function
        self.logs = logs
        self.calls = []

    def client(self, service):
        self.calls.append(service)
        assert service in {"eks", "iam", "lambda", "logs"}
        return {"eks": self.provider, "iam": self.iam, "lambda": self.function, "logs": self.logs}[service]


class Function:
    def __init__(self, resource_arn):
        self.resource_arn = resource_arn
        self.calls = []

    def get_function(self, *, FunctionName):
        self.calls.append(FunctionName)
        return {"Configuration": {"FunctionArn": self.resource_arn}, "Code": {"Location": "synthetic"}}


class Logs:
    def __init__(self):
        self.calls = []

    def filter_log_events(self, *, logGroupName, limit):
        self.calls.append((logGroupName, limit))
        return {"events": [{"message": "synthetic"}], "nextToken": "more-events"}


class IAM:
    def __init__(self, resource, space_arn, host_account):
        self.resource = resource
        self.space_arn = space_arn
        self.trust_space_arn = space_arn
        self.host_account = host_account
        self.trust_principal = "aidevops.amazonaws.com"
        self.scoped_policy = True
        self.broad_policy = False
        self.calls = []

    def get_role(self, *, RoleName):
        self.calls.append(("get_role", RoleName))
        return {"Role": {"Arn": self.resource.role_arn, "AssumeRolePolicyDocument": {
            "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                           "Principal": {"Service": self.trust_principal},
                           "Condition": {"StringEquals": {"aws:SourceAccount": self.host_account},
                                         "ArnEquals": {"aws:SourceArn": self.trust_space_arn}}}]}}}

    def list_attached_role_policies(self, *, RoleName, **kwargs):
        self.calls.append(("list_attached", kwargs.get("Marker")))
        if kwargs.get("Marker") == "next":
            return {"AttachedPolicies": [{"PolicyArn": "arn:aws:iam::210987654321:policy/extra"}]}
        policies = ([{"PolicyArn": "arn:aws:iam::210987654321:policy/scoped"}]
                    if self.scoped_policy else [])
        return {"AttachedPolicies": policies, "IsTruncated": self.broad_policy,
                **({"Marker": "next"} if self.broad_policy else {})}

    def list_role_policies(self, *, RoleName, **kwargs):
        self.calls.append(("list_inline", RoleName))
        return {"PolicyNames": []}

    def get_policy(self, *, PolicyArn):
        return {"Policy": {"DefaultVersionId": "v1"}}

    def get_policy_version(self, *, PolicyArn, VersionId):
        grants = getattr(self, "grants", {"eks:DescribeCluster": {
            f"arn:aws:eks:{self.resource.region}:{self.resource.account_id}:cluster/{self.resource.cluster_name}"}})
        statements = [{"Effect": "Allow", "Action": action,
                       "Resource": sorted(resources) if PolicyArn.endswith("/scoped") else ["*"]}
                      for action, resources in grants.items()]
        return {"PolicyVersion": {"Document": {"Statement": statements}}}

    def simulate_principal_policy(self, *, PolicySourceArn, ActionNames, ResourceArns):
        self.calls.append(("simulate", ResourceArns[0]))
        return {"EvaluationResults": [{"EvalDecision": "allowed"}]}


class NamespaceReader:
    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = []
        self.fail_network = False

    async def read(self, session, cluster, cluster_name, namespace):
        self.calls.append((cluster["arn"], cluster_name, namespace))
        if self.fail_network:
            raise AccessDenied("selected_cluster_network_unavailable")
        if namespace != self.allowed:
            raise AccessDenied("namespace not authorized")
        return {"pods": 2, "events": 1, "logs_read": 1}


@pytest.fixture
async def access_scenario(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    binding = SpaceSelection("tenant-example", "product-example", "project-example", "binding-example",
                             "123456789012", "example-region-1")
    fixed_now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    lifecycle = SpaceLifecycle(sessions, SpaceAdapter(NativeAssociations(), account_id=binding.account_id,
                                                      region=binding.region), clock=lambda: fixed_now)
    await lifecycle.prepare_create(binding, "operation-example", 1)
    await lifecycle.execute_create(binding.tenant_id, "operation-example")
    space_arn = f"arn:aws:aidevops:{binding.region}:{binding.account_id}:agentspace/space-example"
    reference = {"tenant_id": binding.tenant_id, "project_id": binding.project_id,
                 "source_connection_id": "credential-example", "record_type": "credential",
                 "record_id": "credential-example", "record_revision": "version-example"}
    resource = ResourceSelection("210987654321", "example-region-2",
                                 "arn:aws:iam::210987654321:role/resource-reader",
                                 "arn:aws:iam::210987654321:role/setup-reader", reference,
                                 "eks", "cluster-example", "team-example")
    broker_calls = []
    broker_state = {"revision": resource.connection_ref["record_revision"], "revoked": False,
                    "pending": []}

    async def broker(request):
        body = json.loads(request.content)
        broker_calls.append(body)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
            broker_state["pending"].append(receipt.resource_observation.copy())
        if broker_state["revoked"]:
            return httpx.Response(403, json={"detail": {"code": "delegation_denied"}})
        if body["connection_ref"]["record_revision"] != broker_state["revision"]:
            return httpx.Response(409, json={"detail": {"code": "selected_connection_changed"}})
        return httpx.Response(200, json={"access_key_id": "synthetic", "secret_access_key": "synthetic",
                                         "session_token": "synthetic", "expires_at": (
                                             fixed_now + timedelta(hours=1)).isoformat(),
                                         "provider_identity": {"account_id": resource.account_id,
                                                               "region": resource.region,
                                                               "role_arn": resource.setup_role_arn},
                                         "delegation_revision": 1, "operation_id": "operation-example",
                                         "attempt_id": "attempt-example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(broker), base_url="https://broker.example.invalid") as transport:
        provider = EKS(resource.account_id, resource.region)
        provider.selected_role = resource.role_arn
        provider.namespace = resource.namespace
        reader = NamespaceReader(resource.namespace)
        iam = IAM(resource, space_arn, binding.account_id)
        adapter = AWSAccessAdapter(transport, aws_session_factory=lambda credentials, region: Session(provider, iam),
                                   kubernetes_reader=reader, clock=lambda: fixed_now)
        adapter.broker_state = broker_state
        try:
            yield sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_resource_access(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    observation = await lifecycle.verify_resource_access(
        binding, "operation-example", resource, adapter, delegation_id="delegation-example",
        delegation_revision=1, attempt_id="attempt-example")
    assert observation["status"] == "verifying"
    assert observation["account_id"] == resource.account_id
    assert observation["role_arn"] == resource.role_arn
    effective = observation["effective_scope"]
    assert {key: effective[key] for key in ("trust", "policy_documents", "selected_actions")} == {
        "trust": "selected_space", "policy_documents": 1, "selected_actions": ["eks:DescribeCluster"]}
    assert effective["policy_sha256"] == policy_digest(effective["policy_snapshot"])
    assert effective["policy_snapshot"]["managed_policies"][0]["version_id"] == "v1"
    assert observation["resource_selection"] == asdict(resource)
    assert observation["association_status"] == "valid"
    assert observation["account_type"] == "source"
    assert observation["scope_fingerprint"] == resource.fingerprint()
    assert observation["observations"] == {"pods": 2, "events": 1, "logs_read": 1}
    assert observation["eks_access"] == {
        "principal_arn": resource.role_arn,
        "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonAIOpsAssistantPolicy",
        "namespace": resource.namespace, "authentication_mode": "API", "network_mode": "public"}
    assert provider.calls == [resource.cluster_name]
    assert provider.entry_calls == [(resource.cluster_name, resource.role_arn)]
    assert provider.policy_calls == [(resource.cluster_name, resource.role_arn, None)]
    assert ("simulate", observation["cluster_arn"]) in iam.calls
    assert reader.calls == [(observation["cluster_arn"], resource.cluster_name, resource.namespace)]
    assert broker_calls == [{"delegation_id": "delegation-example", "delegation_revision": 1,
                             "operation_id": "operation-example", "attempt_id": "attempt-example",
                             "connection_ref": resource.connection_ref}]
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == observation
        assert receipt.state == "succeeded"
    changed = replace(resource, namespace="other-team")
    denied = await _verify(lifecycle, binding, changed, adapter)
    assert denied["reason"] == "eks_access_policy_outside_namespace"
    assert denied["scope_fingerprint"] == changed.fingerprint()
    assert lifecycle.adapter.client.creates_for_association == 1
    assert lifecycle.adapter.client.associations[0]["configuration"]["sourceAws"]["accountId"] == resource.account_id
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == denied


@pytest.mark.asyncio
async def test_mismatched_cluster_arn_and_unsafe_namespace_are_rejected(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    with pytest.raises(ValueError, match="selected cluster and namespace"):
        replace(resource, namespace="..").fingerprint()
    assert broker_calls == [] and reader.calls == []
    provider.observed_arn = f"arn:aws:eks:{resource.region}:{resource.account_id}:cluster/other-cluster"
    assert (await _verify(lifecycle, binding, resource, adapter))["reason"] == (
        "cluster readback is outside selected account or Region")
    assert reader.calls == []
    assert lifecycle.adapter.client.associations == []


@pytest.mark.asyncio
async def test_unselected_namespace_does_not_replace_binding_readback(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    changed = ResourceSelection(resource.account_id, resource.region, resource.role_arn, resource.setup_role_arn,
                                resource.connection_ref, "eks", resource.cluster_name, "other-team")
    denied = await lifecycle.verify_resource_access(binding, "operation-example", changed, adapter,
                                                    delegation_id="delegation-example", delegation_revision=1,
                                                    attempt_id="attempt-example")
    assert (denied["status"], denied["reason"], denied["scope_fingerprint"]) == (
        "blocked", "eks_access_policy_outside_namespace", changed.fingerprint())
    assert reader.calls == []
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == denied


@pytest.mark.asyncio
async def test_native_eks_role_entry_and_namespace_policy_readback(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    provider.authentication_mode = "API_AND_CONFIG_MAP"
    provider.network_public = False
    provider.network_private = True
    result = await _verify(lifecycle, binding, resource, adapter)
    assert result["status"] == "verifying"
    assert result["eks_access"] == {
        "principal_arn": resource.role_arn,
        "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonAIOpsAssistantPolicy",
        "namespace": resource.namespace,
        "authentication_mode": "API_AND_CONFIG_MAP", "network_mode": "private"}
    assert provider.entry_calls == [(resource.cluster_name, resource.role_arn)]
    assert provider.policy_calls == [(resource.cluster_name, resource.role_arn, None)]
    assert reader.calls == [(result["cluster_arn"], resource.cluster_name, resource.namespace)]
    assert provider.authentication_mode == "API_AND_CONFIG_MAP"
    assert (provider.network_public, provider.network_private) == (False, True)
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == result


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"access_entry_present": False}, "eks_resource_role_access_entry_unverified"),
    ({"selected_role": "arn:aws:iam::210987654321:role/another-reader"},
     "eks_resource_role_access_entry_unverified"),
    ({"entry_groups": ["cluster-admins"]}, "eks_resource_role_access_entry_unverified"),
    ({"entry_type": "EC2_LINUX"}, "eks_resource_role_access_entry_unverified"),
    ({"extra_policy": True}, "eks_access_policy_outside_namespace"),
    ({"namespace": "another-team"}, "eks_access_policy_outside_namespace"),
])
async def test_native_eks_entry_missing_or_broad_policy_denied(access_scenario, change, reason):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    for key, value in change.items():
        setattr(provider, key, value)
    denied = await _verify(lifecycle, binding, resource, adapter)
    assert (denied["status"], denied["reason"]) == ("blocked", reason)
    assert denied["scope_fingerprint"] == resource.fingerprint()
    assert reader.calls == []
    assert lifecycle.adapter.client.associations == []
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == denied


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,public,private,network_failure,reason", [
    ("CONFIG_MAP", True, False, False, "eks_api_authentication_unavailable"),
    ("API", False, False, False, "eks_network_access_unverified"),
    ("API", False, True, True, "selected_cluster_network_unavailable"),
])
async def test_native_eks_authentication_and_network_prerequisites(
        access_scenario, mode, public, private, network_failure, reason):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    provider.authentication_mode = mode
    provider.network_public = public
    provider.network_private = private
    reader.fail_network = network_failure
    denied = await _verify(lifecycle, binding, resource, adapter)
    assert (denied["status"], denied["reason"]) == ("blocked", reason)
    assert provider.authentication_mode == mode
    assert (provider.network_public, provider.network_private) == (public, private)
    assert lifecycle.adapter.client.associations == []
    assert reader.calls == ([(f"arn:aws:eks:{resource.region}:{resource.account_id}:"
                             f"cluster/{resource.cluster_name}", resource.cluster_name, resource.namespace)]
                            if network_failure else [])
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == denied


@pytest.mark.asyncio
async def test_effective_scope(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    observation = await _verify(lifecycle, binding, resource, adapter)
    assert observation["effective_scope"]["trust"] == "selected_space"
    assert observation["effective_scope"]["policy_documents"] == 1
    assert {"get_role", "list_attached", "list_inline", "simulate"} == {
        call[0] for call in iam.calls}
    native = lifecycle.adapter.client
    original_associations = list(native.associations)
    iam.trust_space_arn = iam.space_arn.replace("space-example", "other-space")
    assert iam.get_role(RoleName="resource-reader")["Role"]["AssumeRolePolicyDocument"]["Statement"][0][
        "Condition"]["ArnEquals"]["aws:SourceArn"] == iam.trust_space_arn
    wrong_trust = await _verify(lifecycle, binding, resource, adapter)
    assert wrong_trust["reason"] == "resource_role_trust_outside_selected_space"
    iam.trust_space_arn = iam.space_arn
    iam.scoped_policy = False
    unknown = await _verify(lifecycle, binding, resource, adapter)
    assert unknown["reason"] == "role_permissions_unknown"
    iam.scoped_policy = True
    iam.broad_policy = True
    extra = iam.get_policy_version(PolicyArn="arn:aws:iam::210987654321:policy/extra", VersionId="v1")
    assert extra["PolicyVersion"]["Document"]["Statement"][0]["Resource"] == ["*"]
    too_wide = await _verify(lifecycle, binding, resource, adapter)
    assert too_wide["reason"] == "unselected_policy_resource"
    assert adapter.broker_state["pending"][-1]["status"] == "verifying"
    assert [call for call in iam.calls if call[0] == "simulate"] == [
        ("simulate", observation["cluster_arn"])]
    assert provider.calls == [resource.cluster_name]
    assert native.associations == original_associations
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == too_wide


async def _verify(lifecycle, binding, resource, adapter):
    return await lifecycle.verify_resource_access(binding, "operation-example", resource, adapter,
                                                  delegation_id="delegation-example", delegation_revision=1,
                                                  attempt_id="attempt-example")


@pytest.mark.asyncio
async def test_wrong_service_trust_blocks_readiness(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    iam.trust_principal = "another-service.amazonaws.com"
    assert (await _verify(lifecycle, binding, resource, adapter))["reason"] == "resource_role_trust_outside_selected_space"
    assert provider.calls == [] and reader.calls == []


@pytest.mark.asyncio
async def test_unknown_role_permissions_block_readiness(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    iam.scoped_policy = False
    assert (await _verify(lifecycle, binding, resource, adapter))["reason"] == "role_permissions_unknown"
    assert provider.calls == [] and reader.calls == []


@pytest.mark.asyncio
async def test_extra_broad_grant_blocks_despite_scoped_allow(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    assert (await _verify(lifecycle, binding, resource, adapter))["status"] == "verifying"
    iam.broad_policy = True
    assert (await _verify(lifecycle, binding, resource, adapter))["reason"] == "unselected_policy_resource"
    assert [call for call in iam.calls if call[0] == "list_attached"] == [
        ("list_attached", None), ("list_attached", None), ("list_attached", "next")]
    assert provider.calls == [resource.cluster_name]
    assert reader.calls == [(f"arn:aws:eks:{resource.region}:{resource.account_id}:"
                             f"cluster/{resource.cluster_name}", resource.cluster_name, resource.namespace)]
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation["status"] == "blocked"
        assert receipt.resource_observation["reason"] == "unselected_policy_resource"


@pytest.mark.asyncio
async def test_access_drift(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    first = await _verify(lifecycle, binding, resource, adapter)
    native = lifecycle.adapter.client
    original_space = deepcopy(native.spaces)
    original_associations = deepcopy(native.associations)
    assert adapter.broker_state["pending"][-1]["status"] == "verifying"
    assert adapter.broker_state["pending"][-1]["reason"] == "rechecking_current_access"
    rotated = replace(resource, connection_ref={**resource.connection_ref, "record_revision": "version-rotated"})
    adapter.broker_state["revision"] = "version-rotated"
    stale = await _verify(lifecycle, binding, resource, adapter)
    assert stale["reason"] == "selected_connection_changed"
    assert stale["scope_fingerprint"] == first["scope_fingerprint"]
    refreshed = await _verify(lifecycle, binding, rotated, adapter)
    assert refreshed["status"] == "verifying"
    assert refreshed["scope_fingerprint"] != first["scope_fingerprint"]
    assert broker_calls[-1]["connection_ref"]["record_revision"] == "version-rotated"
    assert provider.calls == [resource.cluster_name, resource.cluster_name]
    changed = replace(rotated, cluster_name="other-cluster")
    with pytest.raises(AccessDenied, match="resource_selection_changed"):
        await lifecycle.require_current_resource_access(binding, "operation-example", changed)
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation["scope_fingerprint"] == changed.fingerprint()
        assert receipt.resource_observation["status"] == "blocked"
    adapter.broker_state["revoked"] = True
    revoked = await _verify(lifecycle, binding, rotated, adapter)
    assert revoked["reason"] == "selected_connection_revoked"
    assert provider.calls == [resource.cluster_name, resource.cluster_name]
    adapter.broker_state["revoked"] = False
    restored = await _verify(lifecycle, binding, rotated, adapter)
    assert restored["status"] == "verifying"
    assert restored["scope_fingerprint"] == rotated.fingerprint()
    assert restored["checked_at"] == lifecycle.clock().isoformat()
    assert len(adapter.broker_state["pending"]) == 5
    assert len({entry["verification_id"] for entry in adapter.broker_state["pending"]}) == 5
    assert native.spaces == original_space and native.associations == original_associations
    assert native.creates_for_association == 1
    assert all(cluster == resource.cluster_name for cluster in provider.calls)
    assert all(namespace == resource.namespace for _, _, namespace in reader.calls)
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == restored


@pytest.mark.asyncio
async def test_revoked_connection_blocks_subsequent_operation(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    await _verify(lifecycle, binding, resource, adapter)
    adapter.broker_state["revoked"] = True
    blocked = await _verify(lifecycle, binding, resource, adapter)
    assert blocked["reason"] == "selected_connection_revoked"
    assert adapter.broker_state["pending"][-1]["status"] == "verifying"
    assert provider.calls == [resource.cluster_name]
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == blocked


@pytest.mark.asyncio
async def test_changed_cluster_selection_invalidates_cached_readiness(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    expired_at = (lifecycle.clock() + timedelta(minutes=10)).isoformat()
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        receipt.resource_observation = {"status": "ready", "scope_fingerprint": resource.fingerprint(),
                                        "expires_at": expired_at}
        await session.commit()
    changed = replace(resource, cluster_name="other-cluster")
    with pytest.raises(AccessDenied, match="resource_selection_changed"):
        await lifecycle.require_current_resource_access(binding, "operation-example", changed)
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation["status"] == "blocked"
        assert receipt.resource_observation["scope_fingerprint"] == changed.fingerprint()
        assert receipt.selection["account_id"] == binding.account_id
    assert provider.calls == [] and reader.calls == []


@pytest.mark.asyncio
async def test_expired_readiness_is_not_reused(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        receipt.resource_observation = {"status": "ready", "scope_fingerprint": resource.fingerprint(),
                                        "expires_at": (lifecycle.clock() - timedelta(seconds=1)).isoformat()}
        await session.commit()
    with pytest.raises(AccessDenied, match="resource_access_expired"):
        await lifecycle.require_current_resource_access(binding, "operation-example", resource)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_current_readiness_rechecks_broker_before_use(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        receipt.resource_observation = {
            "status": "ready", "scope_fingerprint": resource.fingerprint(),
            "expires_at": (lifecycle.clock() + timedelta(minutes=10)).isoformat(),
        }
        await session.commit()
    with pytest.raises(AccessDenied, match="current_broker_verification_required"):
        await lifecycle.require_current_resource_access(binding, "operation-example", resource)
    adapter.broker_state["revoked"] = True
    with pytest.raises(AccessDenied, match="selected_connection_revoked"):
        await lifecycle.require_current_resource_access(
            binding, "operation-example", resource, adapter,
            delegation_id="delegation-example", delegation_revision=1, attempt_id="attempt-example",
        )
    assert adapter.broker_state["pending"][-1]["status"] == "verifying"
    assert provider.calls == [] and reader.calls == []
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation["reason"] == "selected_connection_revoked"


def non_eks_scenario(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    function_arn = f"arn:aws:lambda:{resource.region}:{resource.account_id}:function:example-api"
    log_arn = f"arn:aws:logs:{resource.region}:{resource.account_id}:log-group:/aws/lambda/example-api:*"
    selected = replace(resource, workload_kind="lambda", cluster_name=None, namespace=None,
                       resource_arn=function_arn, telemetry_arn=log_arn)
    iam.resource = selected
    iam.grants = {"lambda:GetFunction": {function_arn}, "logs:FilterLogEvents": {log_arn}}
    function = Function(function_arn)
    logs = Logs()
    sessions_used = []

    def session_factory(credentials, region):
        session = Session(provider, iam, function, logs)
        sessions_used.append(session)
        return session

    adapter.aws_session_factory = session_factory
    return selected, function, logs, sessions_used


@pytest.mark.asyncio
async def test_non_eks_access(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    selected, function, logs, sessions_used = non_eks_scenario(access_scenario)
    retained = {"agentSpaceId": "space-example", "associationId": "existing-monitor",
                "serviceId": "aws", "status": "valid",
                "configuration": {"aws": {"accountId": binding.account_id, "accountType": "monitor",
                                          "assumableRoleArn": f"arn:aws:iam::{binding.account_id}:role/monitor"}}}
    lifecycle.adapter.client.associations.append(retained)
    observation = await _verify(lifecycle, binding, selected, adapter)
    assert observation["status"] == "verifying"
    assert observation["account_id"] == resource.account_id
    assert observation["resource_arn"] == selected.resource_arn
    assert observation["telemetry_arn"] == selected.telemetry_arn
    assert observation["observations"] == {"functions": 1, "log_events_sampled": 1}
    assert observation["effective_scope"]["selected_actions"] == ["lambda:GetFunction", "logs:FilterLogEvents"]
    assert observation["association_status"] == "valid" and observation["account_type"] == "source"
    assert lifecycle.adapter.client.associations == [retained, {
        "agentSpaceId": "space-example", "associationId": observation["association_id"],
        "serviceId": "aws", "status": "valid",
        "configuration": {"sourceAws": {"accountId": selected.account_id, "accountType": "source",
                                        "assumableRoleArn": selected.role_arn}},
    }]
    assert function.calls == [selected.resource_arn]
    assert logs.calls == [("/aws/lambda/example-api", 1)]
    assert "eks" not in sessions_used[0].calls and reader.calls == []
    assert set(iam.grants) == {"lambda:GetFunction", "logs:FilterLogEvents"}
    assert provider.calls == []
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == observation
    native = lifecycle.adapter.client
    original_associations = deepcopy(native.associations)
    function.resource_arn = selected.resource_arn.replace("example-api", "unselected-api")
    mismatched = await _verify(lifecycle, binding, selected, adapter)
    assert mismatched["reason"] == "resource_readback_outside_selection"
    assert logs.calls == [("/aws/lambda/example-api", 1)]
    function.resource_arn = selected.resource_arn
    foreign_account = "111111111111"
    foreign = replace(selected, account_id=foreign_account,
                      role_arn=selected.role_arn.replace(resource.account_id, foreign_account),
                      setup_role_arn=selected.setup_role_arn.replace(resource.account_id, foreign_account),
                      resource_arn=selected.resource_arn.replace(resource.account_id, foreign_account),
                      telemetry_arn=selected.telemetry_arn.replace(resource.account_id, foreign_account))
    wrong_account = await _verify(lifecycle, binding, foreign, adapter)
    assert wrong_account["reason"] == "selected setup role, account or Region does not match broker readback"
    unselected = replace(selected, resource_arn=selected.resource_arn.replace("example-api", "unselected-api"))
    denied = await _verify(lifecycle, binding, unselected, adapter)
    assert denied["reason"] == "unselected_policy_resource"
    assert native.associations == original_associations and native.creates_for_association == 1
    assert logs.calls == [("/aws/lambda/example-api", 1)]
    assert all("eks" not in session.calls for session in sessions_used) and reader.calls == []
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (binding.tenant_id, "operation-example"))
        assert receipt.resource_observation == denied


@pytest.mark.asyncio
async def test_non_eks_denies_wrong_account_and_unselected_function(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    selected, function, logs, sessions_used = non_eks_scenario(access_scenario)
    wrong_account = "111111111111"
    foreign = replace(selected, account_id=wrong_account,
                      role_arn=selected.role_arn.replace(resource.account_id, wrong_account),
                      setup_role_arn=selected.setup_role_arn.replace(resource.account_id, wrong_account),
                      resource_arn=selected.resource_arn.replace(resource.account_id, wrong_account),
                      telemetry_arn=selected.telemetry_arn.replace(resource.account_id, wrong_account))
    assert (await _verify(lifecycle, binding, foreign, adapter))["reason"] == (
        "selected setup role, account or Region does not match broker readback")
    unselected = replace(selected, resource_arn=selected.resource_arn.replace("example-api", "other-api"))
    assert (await _verify(lifecycle, binding, unselected, adapter))["reason"] == "unselected_policy_resource"
    assert function.calls == [] and logs.calls == [] and reader.calls == []
    assert all("eks" not in session.calls for session in sessions_used)
    assert lifecycle.adapter.client.associations == []
    with pytest.raises(ValueError, match="without Kubernetes fields"):
        replace(selected, namespace="other-team").fingerprint()


@pytest.mark.asyncio
async def test_lost_association_response_reconciles_without_repeating_create(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    selected, function, logs, sessions_used = non_eks_scenario(access_scenario)
    native = lifecycle.adapter.client
    native.lose_association_response = True
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == "association_outcome_unknown"
    assert native.creates_for_association == 1
    native.lose_association_response = False
    observed = await _verify(lifecycle, binding, selected, adapter)
    assert observed["association_status"] == "valid"
    assert native.creates_for_association == 1


@pytest.mark.asyncio
async def test_uncertain_association_without_readback_is_not_repeated(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    selected, function, logs, sessions_used = non_eks_scenario(access_scenario)
    native = lifecycle.adapter.client
    native.fail_before_association = True
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == "association_outcome_unknown"
    native.fail_before_association = False
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == "association_outcome_unknown"
    assert native.creates_for_association == 1
    assert native.associations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code,reason", [("AccessDeniedException", "association_readback_denied"),
                                        ("InternalServerException", "association_readback_unavailable")])
async def test_association_read_failure_reports_step_and_never_recreates(access_scenario, monkeypatch, caplog, code, reason):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    selected, function, logs, sessions_used = non_eks_scenario(access_scenario)
    native = lifecycle.adapter.client

    def fail(**kwargs):
        raise ClientError({"Error": {"Code": code, "Message": "private-provider-payload"},
                           "ResponseMetadata": {"RequestId": "request-example"}}, "GetAssociation")

    monkeypatch.setattr(native, "get_association", fail)
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == reason
    assert native.creates_for_association == 1
    assert "operation=get_association" in caplog.text and "request_id=request-example" in caplog.text
    assert "private-provider-payload" not in caplog.text and selected.role_arn not in caplog.text
    # A subsequent absent listing cannot establish that the earlier create had no effect.
    native.associations.clear()
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == "association_outcome_unknown"
    assert native.creates_for_association == 1


@pytest.mark.asyncio
async def test_association_discovery_failure_is_actionable_and_does_not_create(access_scenario, monkeypatch):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    selected, function, logs, sessions_used = non_eks_scenario(access_scenario)
    native = lifecycle.adapter.client

    def fail(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "ListAssociations")

    monkeypatch.setattr(native, "list_associations", fail)
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == "association_discovery_denied"
    assert native.creates_for_association == 0
    monkeypatch.setattr(native, "list_associations", lambda **kwargs: {})
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == "association_discovery_incomplete"
    assert native.creates_for_association == 0


@pytest.mark.asyncio
async def test_existing_association_with_wider_role_is_not_modified(access_scenario):
    sessions, lifecycle, binding, resource, adapter, provider, reader, broker_calls, iam = access_scenario
    selected, function, logs, sessions_used = non_eks_scenario(access_scenario)
    existing = {"agentSpaceId": "space-example", "associationId": "existing-broad",
                "serviceId": "aws", "status": "valid",
                "configuration": {"sourceAws": {"accountId": selected.account_id,
                                                "accountType": "source",
                                                "assumableRoleArn": f"arn:aws:iam::{selected.account_id}:role/broad"}}}
    native = lifecycle.adapter.client
    native.associations.append(existing)
    assert (await _verify(lifecycle, binding, selected, adapter))["reason"] == "association_scope_conflict"
    assert native.associations == [existing]
    assert native.creates_for_association == 0


@pytest.mark.asyncio
async def test_kubernetes_reader_reads_only_selected_namespace(monkeypatch):
    import boto3

    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    session = boto3.Session(aws_access_key_id="synthetic", aws_secret_access_key="synthetic",
                            aws_session_token="synthetic", region_name="us-east-1")
    cluster = {"endpoint": "https://cluster.example.invalid",
               "certificateAuthority": {"data": base64.b64encode(b"synthetic CA").decode()}}
    requested = []

    def respond(request):
        requested.append(request)
        assert request.headers["authorization"].startswith("Bearer k8s-aws-v1.")
        if "/other-team/" in request.url.path:
            return httpx.Response(403)
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [{"metadata": {"name": "pod-example"}}]})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/pods/pod-example/log"):
            return httpx.Response(200, text="synthetic log contents")
        raise AssertionError("unselected Kubernetes resource read")

    reader = KubernetesReader(transport=httpx.MockTransport(respond))
    assert await reader.read(session, cluster, "cluster-example", "team-example") == {
        "pods": 1, "events": 0, "logs_read": 1}
    assert [request.url.path for request in requested] == [
        "/api/v1/namespaces/team-example/pods", "/api/v1/namespaces/team-example/events",
        "/api/v1/namespaces/team-example/pods/pod-example/log"]
    with pytest.raises(AccessDenied, match="namespace, network or log"):
        await reader.read(session, cluster, "cluster-example", "other-team")

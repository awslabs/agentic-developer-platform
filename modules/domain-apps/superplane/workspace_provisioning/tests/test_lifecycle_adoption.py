"""Adoption preserves observed identities and refuses incomplete discovery."""

import asyncio
import base64
import copy
from types import SimpleNamespace

import pytest
from account_factory.modes import ClusterOwnership

from workspace_provisioning import adoption
from workspace_provisioning.artifacts import canonical
from workspace_provisioning.runtime_config import LifecycleRefused


def fixture(
    monkeypatch,
    *,
    account="000000000002",
    region="us-east-1",
    cluster="adopted",
    org="org",
    workspace="workspace",
):
    request = SimpleNamespace(
        mode=SimpleNamespace(value="bring-existing-cluster"),
        cluster_ownership=ClusterOwnership.ADOPTED,
        target_account_id=account,
        region=region,
        existing_cluster_name=cluster,
        workspace_id=workspace,
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=SimpleNamespace(org_id=org, workspace_id=workspace))
    )
    responses = {
        "describe_cluster": {
            "cluster": {
                "arn": f"arn:aws:eks:{region}:{account}:cluster/{cluster}",
                "name": cluster,
                "status": "ACTIVE",
                "accessConfig": {"authenticationMode": "API"},
                "endpoint": "https://workspace.eks.example.invalid",
                "certificateAuthority": {
                    "data": base64.b64encode(
                        b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
                    ).decode()
                },
                "resourcesVpcConfig": {
                    "vpcId": "vpc-0123456789abcdef0",
                    "clusterSecurityGroupId": "sg-11111111111111111",
                },
            }
        },
        "list_nodegroups": {"nodegroups": ["nodes"]},
        "describe_nodegroup": {
            "nodegroup": {
                "status": "ACTIVE",
                "clusterName": cluster,
                "nodegroupName": "nodes",
                "nodegroupArn": f"arn:aws:eks:{region}:{account}:nodegroup/{cluster}/nodes/identifier",
                "nodeRole": f"arn:aws:iam::{account}:role/nodes",
                "taints": [
                    {"key": "superplane.aws-e/bootstrap", "effect": "NO_SCHEDULE"}
                ],
                "launchTemplate": {"id": "lt-0123456789abcdef0", "version": "2"},
            }
        },
        "describe_launch_template_versions": {
            "LaunchTemplateVersions": [
                {
                    "LaunchTemplateId": "lt-0123456789abcdef0",
                    "VersionNumber": 2,
                    "LaunchTemplateData": {
                        "SecurityGroupIds": ["sg-22222222222222222"],
                        "MetadataOptions": {
                            "HttpTokens": "required",
                            "HttpPutResponseHopLimit": 1,
                        },
                    },
                }
            ]
        },
        "describe_vpc_endpoints": {
            "VpcEndpoints": [
                {
                    "State": "available",
                    "OwnerId": account,
                    "ServiceName": f"com.amazonaws.{region}.sts",
                    "VpcEndpointId": "vpce-0123456789abcdef0",
                    "VpcId": "vpc-0123456789abcdef0",
                    "VpcEndpointType": "Interface",
                    "PrivateDnsEnabled": True,
                    "Groups": [{"GroupId": "sg-33333333333333333"}],
                }
            ]
        },
        "describe_addon": {
            "addon": {
                "status": "ACTIVE",
                "serviceAccountRoleArn": f"arn:aws:iam::{account}:role/cni",
                "addonVersion": "v1.19.2-eksbuild.1",
            }
        },
    }
    calls = []
    responses["describe_security_group_rules"] = {
        "SecurityGroupRules": [
            {
                "SecurityGroupRuleId": "sgr-0123456789abcdef0",
                "GroupId": "sg-33333333333333333",
                "GroupOwnerId": account,
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "ReferencedGroupInfo": {
                    "GroupId": "sg-22222222222222222",
                    "UserId": account,
                },
            }
        ]
    }

    class ReadSDK:
        def client(self, service, *, region_name):
            assert region_name == region and service in {"eks", "ec2"}
            return self

        def __getattr__(self, method):
            assert method in responses, "discovery cannot invent a provider mutation"

            def call(**arguments):
                calls.append((method, arguments))
                return copy.deepcopy(responses[method])

            return call

    async def current(actual, context):
        assert actual is operation
        return actual

    monkeypatch.setattr(adoption, "current_operation", current)
    return operation, request, ReadSDK(), responses, calls


def discover(monkeypatch):
    operation, request, session, responses, calls = fixture(monkeypatch)
    target, metadata = asyncio.run(
        adoption.prepare_adoption(operation, object(), request, session)
    )
    row = {
        "org_id": "org",
        "workspace_id": "workspace",
        "account_id": request.target_account_id,
        "target_json": canonical(target),
        "artifact_metadata_json": canonical(metadata),
    }
    return request, row, responses, calls


def test_discovered_target_and_historical_proposal_share_one_verified_shape(
    monkeypatch,
):
    request, row, _, calls = discover(monkeypatch)
    outputs = adoption.verify_adoption_artifact(row, request)
    assert outputs["sts_endpoint_id"] == "vpce-0123456789abcdef0"
    assert len(calls) == 7
    assert calls[3] == (
        "describe_launch_template_versions",
        {"LaunchTemplateId": "lt-0123456789abcdef0", "Versions": ["2"]},
    )


@pytest.mark.parametrize(
    "fault",
    [
        "cluster",
        "nodegroup",
        "version",
        "endpoint-owner",
        "endpoint-pagination",
        "interlock",
    ],
)
def test_discovery_refuses_mismatched_or_incomplete_provider_facts(monkeypatch, fault):
    operation, request, session, responses, _ = fixture(monkeypatch)
    if fault == "cluster":
        responses["describe_cluster"]["cluster"]["arn"] += "-other"
    elif fault == "nodegroup":
        responses["describe_nodegroup"]["nodegroup"]["nodegroupName"] = "other"
    elif fault == "version":
        responses["describe_launch_template_versions"]["LaunchTemplateVersions"][0][
            "VersionNumber"
        ] = 3
    elif fault == "endpoint-owner":
        responses["describe_vpc_endpoints"]["VpcEndpoints"][0]["OwnerId"] = (
            "000000000003"
        )
    elif fault == "endpoint-pagination":
        responses["describe_vpc_endpoints"]["NextToken"] = "more"
    else:
        responses["describe_nodegroup"]["nodegroup"]["taints"] = []
    with pytest.raises(LifecycleRefused):
        asyncio.run(adoption.prepare_adoption(operation, object(), request, session))


@pytest.mark.parametrize(
    "fault",
    ["account", "namespace-target", "ca", "endpoint", "proofs", "role", "ownership"],
)
def test_historical_adoption_artifact_cannot_relabel_its_target(monkeypatch, fault):
    import json

    request, row, _, _ = discover(monkeypatch)
    metadata = json.loads(row["artifact_metadata_json"])
    outputs = metadata["outputs"]
    if fault == "account":
        row["account_id"] = "000000000003"
    elif fault == "namespace-target":
        outputs["workspace_id"]["value"] = "another-workspace"
    elif fault == "ca":
        outputs["cluster_certificate_authority_data"]["value"] = "broken"
    elif fault == "endpoint":
        outputs["cluster_endpoint"]["value"] = (
            "https://user:password@wrong.example.invalid"
        )
    elif fault == "proofs":
        outputs["tenant_scheduling_prerequisites"]["value"]["required_proofs"] = []
    elif fault == "role":
        outputs["node_role_arn"]["value"] = "arn:aws:iam::000000000003:role/nodes"
    else:
        metadata["inventory"]["cluster_ownership"] = "adp-created"
    row["artifact_metadata_json"] = canonical(metadata)
    with pytest.raises(LifecycleRefused):
        adoption.verify_adoption_artifact(row, request)

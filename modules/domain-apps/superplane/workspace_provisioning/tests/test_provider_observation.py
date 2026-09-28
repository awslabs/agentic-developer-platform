"""Read-only recovery cannot accept replacement resources or invent completion."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.provider_observation import observe_applied_target
from workspace_provisioning.recovery_observer import observe_result
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_adoption import discover
from .test_lifecycle_artifacts import artifact, Reader


def fixture(monkeypatch):
    _, discovered, responses, _ = discover(monkeypatch)
    outputs = {
        key: value["value"]
        for key, value in json.loads(discovered["artifact_metadata_json"])[
            "outputs"
        ].items()
    }
    outputs["workspace_node_group"] = {
        "name": "nodes",
        "arn": responses["describe_nodegroup"]["nodegroup"]["nodegroupArn"],
        "launch_template_id": "lt-0123456789abcdef0",
        "launch_template_version": "2",
    }
    responses["describe_cluster"]["cluster"]["resourcesVpcConfig"].update(
        clusterSecurityGroupId=outputs["workspace_node_security_group_id"],
        securityGroupIds=[outputs["workspace_api_security_group_id"]],
    )
    launch = responses["describe_launch_template_versions"]["LaunchTemplateVersions"][
        0
    ]["LaunchTemplateData"]
    launch.pop("SecurityGroupIds")
    responses["get_caller_identity"] = {"Account": outputs["account_id"]}
    calls = []

    async def read(service, method, **arguments):
        assert service in {"sts", "eks", "ec2"}
        assert method == "get_caller_identity" or method.startswith(
            ("describe_", "list_")
        )
        calls.append((service, method, arguments))
        if method == "describe_security_groups":
            return {
                "SecurityGroups": [
                    {
                        "GroupId": group,
                        "OwnerId": outputs["account_id"],
                        "VpcId": outputs["vpc_id"],
                    }
                    for group in arguments["GroupIds"]
                ]
            }
        return deepcopy(responses[method])

    return outputs, responses, read, calls


def test_applied_observation_binds_exact_nodes_template_and_retained_sts(monkeypatch):
    outputs, _, read, calls = fixture(monkeypatch)
    observed = asyncio.run(observe_applied_target(outputs, read))
    assert observed["nodegroup_arn"] == outputs["workspace_node_group"]["arn"]
    assert observed["retained_sts_rule_id"] == outputs["sts_endpoint_rule_id"]
    assert len(calls) == 9
    assert (
        "ec2",
        "describe_launch_template_versions",
        {"LaunchTemplateId": "lt-0123456789abcdef0", "Versions": ["2"]},
    ) in calls


@pytest.mark.parametrize(
    "fault",
    [
        "account",
        "cluster-ca",
        "node-arn",
        "node-role",
        "node-pagination",
        "node-list",
        "taint",
        "template-version",
        "template-pagination",
        "explicit-sg",
        "imds",
        "cni-role",
        "sts-rule",
    ],
)
def test_apply_observation_refuses_changed_or_incomplete_targets(monkeypatch, fault):
    outputs, responses, read, _ = fixture(monkeypatch)
    nodes = responses["describe_nodegroup"]["nodegroup"]
    versions = responses["describe_launch_template_versions"]
    if fault == "account":
        responses["get_caller_identity"]["Account"] = "000000000003"
    elif fault == "cluster-ca":
        responses["describe_cluster"]["cluster"]["certificateAuthority"]["data"] = (
            "replacement"
        )
    elif fault == "node-arn":
        nodes["nodegroupArn"] += "-replacement"
    elif fault == "node-role":
        nodes["nodeRole"] += "-other"
    elif fault == "node-pagination":
        responses["list_nodegroups"]["nextToken"] = "more"
    elif fault == "node-list":
        responses["list_nodegroups"]["nodegroups"].append("other")
    elif fault == "taint":
        nodes["taints"] = []
    elif fault == "template-version":
        versions["LaunchTemplateVersions"][0]["VersionNumber"] = 3
    elif fault == "template-pagination":
        versions["NextToken"] = "more"
    elif fault == "explicit-sg":
        versions["LaunchTemplateVersions"][0]["LaunchTemplateData"][
            "SecurityGroupIds"
        ] = [outputs["workspace_node_security_group_id"]]
    elif fault == "imds":
        versions["LaunchTemplateVersions"][0]["LaunchTemplateData"]["MetadataOptions"][
            "HttpPutResponseHopLimit"
        ] = 2
    elif fault == "cni-role":
        responses["describe_addon"]["addon"]["serviceAccountRoleArn"] += "-other"
    else:
        responses["describe_security_group_rules"]["SecurityGroupRules"][0][
            "SecurityGroupRuleId"
        ] += "1"
    with pytest.raises(LifecycleRefused):
        asyncio.run(observe_applied_target(outputs, read))


def result_fixture(monkeypatch):
    outputs, responses, read, calls = fixture(monkeypatch)
    snapshot = asyncio.run(observe_applied_target(outputs, read))
    calls.clear()
    source = artifact()
    source["org_id"], source["workspace_id"] = (
        outputs["org_id"],
        outputs["workspace_id"],
    )
    target = {
        key: outputs[key]
        for key in ("account_id", "aws_region", "org_id", "workspace_id")
    }
    source["target_json"] = canonical(target)
    source["artifact_metadata_json"] = canonical(
        {"next_phase": "apply-infrastructure", "module_sha256": "c" * 64}
    )
    source["artifact_id"] = digest(
        {
            key: value
            for key, value in source.items()
            if key not in {"artifact_id", "created_at"}
        }
    )
    operation = SimpleNamespace(
        request=SimpleNamespace(
            parameters={
                "lifecycle_phase": "apply-infrastructure",
                "lifecycle_artifact_id": source["artifact_id"],
            }
        )
    )
    result = {
        "org_id": outputs["org_id"],
        "workspace_id": outputs["workspace_id"],
        "source_operation_id": "apply-operation",
        "target_json": canonical(target),
        "artifact_metadata_json": canonical(
            {
                "next_phase": "bootstrap-workspace",
                "module_sha256": "c" * 64,
                "allocation_source_operation_id": "apply-operation",
                "source_artifact_id": source["artifact_id"],
                "provider_snapshot": snapshot,
                "outputs": {key: {"value": value} for key, value in outputs.items()},
            }
        ),
    }
    context = SimpleNamespace(domain_connect=Reader(source).connect)
    return operation, context, result, SimpleNamespace(aws_read=read), calls, responses


def test_api_observer_returns_facts_without_claiming_readiness_or_budget_release(
    monkeypatch,
):
    operation, context, result, provider, calls, _ = result_fixture(monkeypatch)
    facts = asyncio.run(
        observe_result(operation, context, artifact=result, provider=provider)
    )
    assert set(facts) == {"provider_snapshot", "source_artifact_id"}
    assert (
        facts["source_artifact_id"]
        == operation.request.parameters["lifecycle_artifact_id"]
    )
    assert calls and all(
        method.startswith(("get_", "describe_", "list_")) for _, method, _ in calls
    )


@pytest.mark.parametrize(
    "fault", ["bootstrap", "missing-snapshot", "wrong-source", "changed-snapshot"]
)
def test_api_result_observer_requires_original_anchored_apply_result(
    monkeypatch, fault
):
    operation, context, result, provider, calls, _ = result_fixture(monkeypatch)
    metadata = json.loads(result["artifact_metadata_json"])
    if fault == "bootstrap":
        operation.request.parameters["lifecycle_phase"] = "bootstrap-workspace"
    elif fault == "missing-snapshot":
        metadata.pop("provider_snapshot")
    elif fault == "wrong-source":
        metadata["source_artifact_id"] = "d" * 64
    else:
        metadata["provider_snapshot"]["nodegroup_arn"] += "-replacement"
    result["artifact_metadata_json"] = canonical(metadata)
    with pytest.raises(LifecycleRefused):
        asyncio.run(
            observe_result(operation, context, artifact=result, provider=provider)
        )
    if fault != "changed-snapshot":
        assert calls == []

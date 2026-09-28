"""Read complete adopted-cluster prerequisites before offering bootstrap approval."""

import asyncio
import base64
import json
import re
from urllib.parse import urlsplit

from .authority import current_operation
from .runtime_config import LifecycleRefused


def verify_adoption_artifact(row, request):
    """Verify one authenticated historical discovery; this is not fresh readiness."""
    from superplane_bootstrap.admission import required_proofs

    metadata = json.loads(row["artifact_metadata_json"])
    target = json.loads(row["target_json"])
    expected = {
        "account_id": request.target_account_id,
        "cluster_arn": f"arn:aws:eks:{request.region}:{request.target_account_id}:cluster/{request.existing_cluster_name}",
        "org_id": row["org_id"],
        "workspace_id": row["workspace_id"],
        "aws_region": request.region,
    }
    if (
        request.mode.value != "bring-existing-cluster"
        or target != expected
        or row["account_id"] != request.target_account_id
        or metadata.get("next_phase") != "bootstrap-workspace"
    ):
        raise LifecycleRefused(
            "adoption artifact differs from its original requested target"
        )
    raw = metadata.get("outputs")
    if not isinstance(raw, dict) or any(
        not isinstance(value, dict) or set(value) != {"value"} for value in raw.values()
    ):
        raise LifecycleRefused("adoption output inventory is malformed")
    outputs = {key: value["value"] for key, value in raw.items()}
    if (
        any(outputs.get(key) != value for key, value in expected.items())
        or outputs.get("cluster_name") != request.existing_cluster_name
    ):
        raise LifecycleRefused("adoption outputs name a different workspace")
    origin = urlsplit(str(outputs.get("cluster_endpoint", "")))
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in {"", "/"}
    ):
        raise LifecycleRefused("adoption endpoint is not an exact HTTPS cluster origin")
    try:
        certificate = base64.b64decode(
            outputs["cluster_certificate_authority_data"], validate=True
        )
    except (KeyError, TypeError, ValueError):
        raise LifecycleRefused("adoption cluster CA is malformed") from None
    if (
        not certificate.startswith(b"-----BEGIN CERTIFICATE-----")
        or b"-----END CERTIFICATE-----" not in certificate
    ):
        raise LifecycleRefused("adoption cluster CA is not a PEM certificate")
    for key in (
        "workspace_api_security_group_id",
        "workspace_node_security_group_id",
        "sts_endpoint_security_group_id",
    ):
        if not isinstance(outputs.get(key), str) or not re.fullmatch(
            r"sg-[a-f0-9]{8,17}", outputs[key]
        ):
            raise LifecycleRefused("adoption security group inventory is malformed")
    if not re.fullmatch(
        r"vpc-[a-f0-9]{8,17}", str(outputs.get("vpc_id", ""))
    ) or outputs.get("sts_endpoint_vpc_id") != outputs.get("vpc_id"):
        raise LifecycleRefused("adoption private STS is outside the discovered VPC")
    inventory = metadata.get("inventory", {})
    if not re.fullmatch(
        r"sgr-[a-f0-9]{8,17}", str(outputs.get("sts_endpoint_rule_id", ""))
    ):
        raise LifecycleRefused("adoption retained STS rule identity is missing")
    if (
        inventory.get("cluster_ownership") != request.cluster_ownership.value
        or not str(inventory.get("nodegroup_arn", "")).startswith(
            f"arn:aws:eks:{request.region}:{request.target_account_id}:nodegroup/{request.existing_cluster_name}/"
        )
        or inventory.get("sts_endpoint_id") != outputs.get("sts_endpoint_id")
        or not re.fullmatch(
            r"vpce-[a-f0-9]{8,17}", str(outputs.get("sts_endpoint_id", ""))
        )
    ):
        raise LifecycleRefused(
            "adoption resource inventory differs from the discovered workspace"
        )
    prerequisites = outputs.get("tenant_scheduling_prerequisites", {})
    if (
        prerequisites.get("bootstrap_taint_key") != "superplane.aws-e/bootstrap"
        or type(prerequisites.get("node_imds_hop_limit")) is not int
        or prerequisites["node_imds_hop_limit"] != 1
        or prerequisites.get("required_proofs") != list(required_proofs())
        or not prerequisites.get("cni_addon_version")
    ):
        raise LifecycleRefused("adoption prerequisite proof inventory is incomplete")
    for arn in (outputs.get("node_role_arn"), prerequisites.get("cni_role_arn")):
        if not isinstance(arn, str) or not re.fullmatch(
            rf"arn:aws:iam::{request.target_account_id}:role/[A-Za-z0-9+=,.@_/-]+", arn
        ):
            raise LifecycleRefused("adoption role belongs to a different account")
    return outputs


async def prepare_adoption(operation, context, request, session):
    from superplane_bootstrap.admission import required_proofs

    if request.mode.value != "bring-existing-cluster":
        raise LifecycleRefused("adoption discovery requires the adopted-cluster mode")
    eks = session.client("eks", region_name=request.region)
    ec2 = session.client("ec2", region_name=request.region)

    async def read(method, **arguments):
        await current_operation(operation, context)
        result = await asyncio.to_thread(method, **arguments)
        await current_operation(operation, context)
        return result

    cluster = (await read(eks.describe_cluster, name=request.existing_cluster_name))[
        "cluster"
    ]
    account = request.target_account_id
    expected_arn = f"arn:aws:eks:{request.region}:{account}:cluster/{request.existing_cluster_name}"
    if (
        cluster.get("arn") != expected_arn
        or cluster.get("name") != request.existing_cluster_name
        or cluster.get("status") != "ACTIVE"
        or cluster.get("accessConfig", {}).get("authenticationMode") != "API"
    ):
        raise LifecycleRefused(
            "adopted cluster is not the active approved EKS API target"
        )
    groups = await read(eks.list_nodegroups, clusterName=request.existing_cluster_name)
    if groups.get("nextToken") or len(groups["nodegroups"]) != 1:
        raise LifecycleRefused(
            "adoption currently requires one fully inventoried managed node group"
        )
    nodes = (
        await read(
            eks.describe_nodegroup,
            clusterName=request.existing_cluster_name,
            nodegroupName=groups["nodegroups"][0],
        )
    )["nodegroup"]
    if (
        nodes.get("status") != "ACTIVE"
        or nodes.get("clusterName") != request.existing_cluster_name
        or nodes.get("nodegroupName") != groups["nodegroups"][0]
        or not nodes.get("nodegroupArn", "").startswith(
            f"arn:aws:eks:{request.region}:{account}:nodegroup/{request.existing_cluster_name}/{groups['nodegroups'][0]}/"
        )
    ):
        raise LifecycleRefused("adopted node-group identity is unavailable")
    taints = [
        taint
        for taint in nodes.get("taints", [])
        if taint.get("key") == "superplane.aws-e/bootstrap"
        and taint.get("effect") == "NO_SCHEDULE"
    ]
    if len(taints) != 1:
        raise LifecycleRefused(
            "adopted node group must already carry its explicit bootstrap scheduling interlock"
        )
    template = nodes.get("launchTemplate", {})
    if not template.get("id") or not template.get("version"):
        raise LifecycleRefused("adopted nodes have no exact launch-template identity")
    versions = (
        await read(
            ec2.describe_launch_template_versions,
            LaunchTemplateId=template["id"],
            Versions=[str(template["version"])],
        )
    )["LaunchTemplateVersions"]
    if (
        len(versions) != 1
        or versions[0]["LaunchTemplateId"] != template["id"]
        or str(versions[0].get("VersionNumber")) != str(template["version"])
    ):
        raise LifecycleRefused("adopted launch template is ambiguous")
    launch = versions[0]["LaunchTemplateData"]
    if (
        len(launch.get("SecurityGroupIds", [])) != 1
        or launch.get("MetadataOptions", {}).get("HttpPutResponseHopLimit") != 1
        or launch.get("MetadataOptions", {}).get("HttpTokens") != "required"
    ):
        raise LifecycleRefused(
            "adopted nodes lack a unique security group and constrained IMDS metadata"
        )
    vpc = cluster["resourcesVpcConfig"]["vpcId"]
    endpoints = await read(
        ec2.describe_vpc_endpoints,
        Filters=[
            {"Name": "vpc-id", "Values": [vpc]},
            {"Name": "service-name", "Values": [f"com.amazonaws.{request.region}.sts"]},
        ],
    )
    if endpoints.get("NextToken") or len(endpoints["VpcEndpoints"]) != 1:
        raise LifecycleRefused(
            "adopted private STS endpoint is not uniquely inventoried"
        )
    endpoint = endpoints["VpcEndpoints"][0]
    if (
        endpoint.get("State") != "available"
        or endpoint.get("VpcId") != vpc
        or endpoint.get("OwnerId") != account
        or endpoint.get("ServiceName") != f"com.amazonaws.{request.region}.sts"
        or endpoint.get("VpcEndpointType") != "Interface"
        or endpoint.get("PrivateDnsEnabled") is not True
        or len(endpoint.get("Groups", [])) != 1
    ):
        raise LifecycleRefused("adopted private STS endpoint is not ready and isolated")
    cni = (
        await read(
            eks.describe_addon,
            clusterName=request.existing_cluster_name,
            addonName="vpc-cni",
        )
    )["addon"]
    if (
        cni.get("status") != "ACTIVE"
        or not cni.get("serviceAccountRoleArn", "").startswith(
            f"arn:aws:iam::{account}:role/"
        )
        or not nodes.get("nodeRole", "").startswith(f"arn:aws:iam::{account}:role/")
    ):
        raise LifecycleRefused("adopted CNI and node role identities are not verified")
    lease = operation.grant.lease
    values = {
        "account_id": account,
        "aws_region": request.region,
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
        "cluster_name": cluster["name"],
        "cluster_arn": cluster["arn"],
        "cluster_endpoint": cluster["endpoint"],
        "cluster_certificate_authority_data": cluster["certificateAuthority"]["data"],
        "node_role_arn": nodes["nodeRole"],
        "vpc_id": vpc,
        "workspace_api_security_group_id": cluster["resourcesVpcConfig"][
            "clusterSecurityGroupId"
        ],
        "workspace_node_security_group_id": launch["SecurityGroupIds"][0],
        "sts_endpoint_vpc_id": endpoint["VpcId"],
        "sts_endpoint_id": endpoint["VpcEndpointId"],
        "sts_endpoint_security_group_id": endpoint["Groups"][0]["GroupId"],
        "tenant_scheduling_prerequisites": {
            "bootstrap_taint_key": taints[0]["key"],
            "node_imds_hop_limit": 1,
            "cni_role_arn": cni["serviceAccountRoleArn"],
            "cni_addon_version": cni["addonVersion"],
            "required_proofs": list(required_proofs()),
        },
    }
    from .network import observe_retained_sts_rule

    async def sdk_read(service, method, **arguments):
        if service != "ec2":
            raise LifecycleRefused("adoption network inventory only reads EC2")
        return await read(getattr(ec2, method), **arguments)

    values["sts_endpoint_rule_id"] = await observe_retained_sts_rule(values, sdk_read)
    return {
        "account_id": account,
        "cluster_arn": cluster["arn"],
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
        "aws_region": request.region,
    }, {
        "next_phase": "bootstrap-workspace",
        "outputs": {key: {"value": value} for key, value in values.items()},
        "inventory": {
            "cluster_ownership": request.cluster_ownership.value,
            "nodegroup_arn": nodes["nodegroupArn"],
            "sts_endpoint_id": endpoint["VpcEndpointId"],
        },
    }

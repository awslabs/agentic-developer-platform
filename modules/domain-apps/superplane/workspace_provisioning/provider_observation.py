"""Fresh bounded AWS reads, anchored to the applied module's exact output identities."""

from .network import observe_retained_sts_rule, verify_network_target
from .runtime_config import LifecycleRefused


async def observe_applied_target(outputs, read):
    """Observe managed infrastructure without claiming bootstrap or cost absence."""
    cluster = await verify_network_target(outputs, read)
    expected = outputs.get("workspace_node_group")
    if not isinstance(expected, dict) or set(expected) != {
        "name",
        "arn",
        "launch_template_id",
        "launch_template_version",
    }:
        raise LifecycleRefused("applied node-group identity inventory is missing")
    account, region = outputs["account_id"], outputs["aws_region"]
    name = outputs["cluster_name"]
    if not all(
        isinstance(value, str) and value for value in expected.values()
    ) or not expected["arn"].startswith(
        f"arn:aws:eks:{region}:{account}:nodegroup/{name}/{expected['name']}/"
    ):
        raise LifecycleRefused("applied node-group identity is malformed")
    response = await read("eks", "list_nodegroups", clusterName=name)
    if response.get("nextToken") or response.get("nodegroups") != [expected["name"]]:
        raise LifecycleRefused("applied node-group inventory is incomplete or changed")
    nodes = (
        await read(
            "eks",
            "describe_nodegroup",
            clusterName=name,
            nodegroupName=expected["name"],
        )
    )["nodegroup"]
    if any(
        nodes.get(key) != value
        for key, value in {
            "status": "ACTIVE",
            "clusterName": name,
            "nodegroupName": expected["name"],
            "nodegroupArn": expected["arn"],
            "nodeRole": outputs["node_role_arn"],
        }.items()
    ):
        raise LifecycleRefused("applied node group was replaced or changed")
    prerequisites = outputs["tenant_scheduling_prerequisites"]
    taints = [
        item
        for item in nodes.get("taints", [])
        if item.get("key") == prerequisites["bootstrap_taint_key"]
        and item.get("effect") == "NO_SCHEDULE"
    ]
    if len(taints) != 1:
        raise LifecycleRefused(
            "applied nodes lack their bootstrap scheduling interlock"
        )
    template = nodes.get("launchTemplate", {})
    if (template.get("id"), str(template.get("version"))) != (
        expected["launch_template_id"],
        expected["launch_template_version"],
    ):
        raise LifecycleRefused("applied launch-template reference changed")
    response = await read(
        "ec2",
        "describe_launch_template_versions",
        LaunchTemplateId=expected["launch_template_id"],
        Versions=[expected["launch_template_version"]],
    )
    versions = response.get("LaunchTemplateVersions", [])
    if (
        response.get("NextToken")
        or len(versions) != 1
        or versions[0].get("LaunchTemplateId") != expected["launch_template_id"]
        or str(versions[0].get("VersionNumber")) != expected["launch_template_version"]
    ):
        raise LifecycleRefused("applied launch-template version is unavailable")
    launch = versions[0]["LaunchTemplateData"]
    # The maintained managed module delegates SG attachment to EKS. An explicit
    # launch-template SG disables that EKS behavior and changes private STS access.
    if (
        launch.get("SecurityGroupIds")
        or launch.get("SecurityGroups")
        or launch.get("NetworkInterfaces")
        or outputs["workspace_node_security_group_id"]
        != cluster["resourcesVpcConfig"]["clusterSecurityGroupId"]
        or launch.get("MetadataOptions", {}).get("HttpTokens") != "required"
        or launch.get("MetadataOptions", {}).get("HttpPutResponseHopLimit") != 1
    ):
        raise LifecycleRefused(
            "applied nodes differ from the managed networking and IMDS recipe"
        )
    addon = (
        await read("eks", "describe_addon", clusterName=name, addonName="vpc-cni")
    )["addon"]
    if any(
        addon.get(key) != value
        for key, value in {
            "status": "ACTIVE",
            "serviceAccountRoleArn": prerequisites["cni_role_arn"],
            "addonVersion": prerequisites["cni_addon_version"],
        }.items()
    ):
        raise LifecycleRefused("applied CNI identity or version changed")
    retained_rule = await observe_retained_sts_rule(outputs, read)
    return {
        "cluster_arn": outputs["cluster_arn"],
        "nodegroup_arn": nodes["nodegroupArn"],
        "node_role_arn": nodes["nodeRole"],
        "launch_template_id": expected["launch_template_id"],
        "launch_template_version": expected["launch_template_version"],
        "cni_role_arn": addon["serviceAccountRoleArn"],
        "cni_addon_version": addon["addonVersion"],
        "sts_endpoint_id": outputs["sts_endpoint_id"],
        "retained_sts_rule_id": retained_rule,
    }

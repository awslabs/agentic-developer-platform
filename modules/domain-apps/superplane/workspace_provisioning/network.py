"""Only management API ingress is created; private STS ingress is verified and retained."""

import asyncio
import json

from .effects import LifecycleEffects
from .runtime_config import LifecycleRefused


async def observe_retained_sts_rule(outputs, read):
    response = await read(
        "ec2",
        "describe_security_group_rules",
        Filters=[
            {"Name": "group-id", "Values": [outputs["sts_endpoint_security_group_id"]]}
        ],
    )
    if response.get("NextToken"):
        raise LifecycleRefused("private STS rule inventory is incomplete")
    matches = [
        rule
        for rule in response.get("SecurityGroupRules", [])
        if rule.get("IsEgress") is False
        and rule.get("IpProtocol") == "tcp"
        and rule.get("FromPort") == 443
        and rule.get("ToPort") == 443
        and rule.get("ReferencedGroupInfo", {}).get("GroupId")
        == outputs["workspace_node_security_group_id"]
    ]
    if len(matches) != 1:
        raise LifecycleRefused(
            "one exact private STS node ingress rule must already exist"
        )
    rule = matches[0]
    if (
        rule.get("GroupId") != outputs["sts_endpoint_security_group_id"]
        or rule.get("GroupOwnerId") != outputs["account_id"]
        or rule.get("ReferencedGroupInfo", {}).get("UserId") != outputs["account_id"]
        or not isinstance(rule.get("SecurityGroupRuleId"), str)
        or not rule["SecurityGroupRuleId"].startswith("sgr-")
    ):
        raise LifecycleRefused("private STS rule identity is not verified")
    expected = outputs.get("sts_endpoint_rule_id")
    if expected is not None and rule["SecurityGroupRuleId"] != expected:
        raise LifecycleRefused("reviewed private STS rule was replaced")
    return rule["SecurityGroupRuleId"]


async def verify_network_target(outputs, read):
    """Fresh read-only target proof shared by execution and protected recovery."""
    account, region = outputs["account_id"], outputs["aws_region"]
    identity = await read("sts", "get_caller_identity")
    if identity.get("Account") != account:
        raise LifecycleRefused("network observer is in another AWS account")
    cluster = (await read("eks", "describe_cluster", name=outputs["cluster_name"]))[
        "cluster"
    ]
    if (
        any(
            cluster.get(key) != value
            for key, value in {
                "name": outputs["cluster_name"],
                "arn": outputs["cluster_arn"],
                "endpoint": outputs["cluster_endpoint"],
                "status": "ACTIVE",
            }.items()
        )
        or cluster.get("certificateAuthority", {}).get("data")
        != outputs["cluster_certificate_authority_data"]
    ):
        raise LifecycleRefused("cluster changed since its reviewed network proposal")
    vpc = cluster.get("resourcesVpcConfig", {})
    if (
        vpc.get("vpcId") != outputs["vpc_id"]
        or outputs["workspace_api_security_group_id"]
        not in {*vpc.get("securityGroupIds", []), vpc.get("clusterSecurityGroupId")}
        or cluster.get("accessConfig", {}).get("authenticationMode") != "API"
    ):
        raise LifecycleRefused("cluster network or EKS API authentication differs")
    response = await read(
        "ec2", "describe_vpc_endpoints", VpcEndpointIds=[outputs["sts_endpoint_id"]]
    )
    endpoints = response.get("VpcEndpoints", [])
    if response.get("NextToken") or len(endpoints) != 1:
        raise LifecycleRefused("private STS endpoint identity is incomplete")
    endpoint = endpoints[0]
    endpoint_groups = endpoint.get("Groups", [])
    if (
        any(
            endpoint.get(key) != value
            for key, value in {
                "VpcEndpointId": outputs["sts_endpoint_id"],
                "OwnerId": account,
                "VpcId": outputs["sts_endpoint_vpc_id"],
                "State": "available",
                "ServiceName": f"com.amazonaws.{region}.sts",
                "VpcEndpointType": "Interface",
                "PrivateDnsEnabled": True,
            }.items()
        )
        or len(endpoint_groups) != 1
        or endpoint_groups[0].get("GroupId")
        != outputs["sts_endpoint_security_group_id"]
    ):
        raise LifecycleRefused(
            "private STS endpoint changed from its reviewed identity"
        )
    expected = {
        outputs["workspace_api_security_group_id"]: outputs["vpc_id"],
        outputs["workspace_node_security_group_id"]: outputs["vpc_id"],
        outputs["sts_endpoint_security_group_id"]: outputs["sts_endpoint_vpc_id"],
    }
    response = await read("ec2", "describe_security_groups", GroupIds=sorted(expected))
    groups = response.get("SecurityGroups", [])
    if (
        response.get("NextToken")
        or len(groups) != len(expected)
        or {group.get("GroupId") for group in groups} != set(expected)
        or any(
            group.get("OwnerId") != account
            or group.get("VpcId") != expected[group["GroupId"]]
            for group in groups
        )
    ):
        raise LifecycleRefused("reviewed network groups changed account or VPC")
    return cluster


def network_recipe(operation, config, outputs):
    account = outputs["account_id"]
    management_account = json.loads(operation.request.parameters["lifecycle_request"])[
        "management_account_id"
    ]
    tags = [
        {"Key": "OrgId", "Value": operation.grant.lease.org_id},
        {"Key": "WorkspaceId", "Value": operation.grant.lease.workspace_id},
    ]
    pairs = {
        "management-api-rule": (
            outputs["workspace_api_security_group_id"],
            config["management_security_group_id"],
        ),
    }
    return {
        key: {
            "service": "ec2",
            "method": "authorize_security_group_ingress",
            "account_id": account,
            "arguments": {
                "GroupId": group,
                "IpPermissions": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 443,
                        "ToPort": 443,
                        "UserIdGroupPairs": [
                            {
                                "GroupId": source,
                                "UserId": management_account
                                if key == "management-api-rule"
                                else account,
                            }
                        ],
                    }
                ],
                "TagSpecifications": [
                    {"ResourceType": "security-group-rule", "Tags": tags}
                ],
            },
        }
        for key, (group, source) in pairs.items()
    }


async def establish_network(operation, context, config, outputs, session):
    recipe = network_recipe(operation, config, outputs)
    journal = LifecycleEffects(
        operation, context, phase="bootstrap-workspace", recipe=recipe
    )
    client = session.client("ec2", region_name=outputs["aws_region"])

    async def sdk_read(service, method, **arguments):
        await journal.authority()
        result = await asyncio.to_thread(
            getattr(session.client(service, region_name=outputs["aws_region"]), method),
            **arguments,
        )
        await journal.authority()
        return result

    await verify_network_target(outputs, sdk_read)
    if not outputs.get("sts_endpoint_rule_id"):
        raise LifecycleRefused(
            "bootstrap needs the reviewed retained STS rule identity"
        )
    retained_sts = await observe_retained_sts_rule(outputs, sdk_read)

    async def read(arguments):
        await journal.authority()
        response = await asyncio.to_thread(
            client.describe_security_group_rules,
            Filters=[{"Name": "group-id", "Values": [arguments["GroupId"]]}],
        )
        await journal.authority()
        if response.get("NextToken"):
            raise LifecycleRefused(
                "bootstrap rule inventory exceeded one complete page"
            )
        source = arguments["IpPermissions"][0]["UserIdGroupPairs"][0]["GroupId"]
        source_account = arguments["IpPermissions"][0]["UserIdGroupPairs"][0]["UserId"]
        matches = [
            row
            for row in response["SecurityGroupRules"]
            if row.get("IsEgress") is False
            and row.get("IpProtocol") == "tcp"
            and row.get("FromPort") == 443
            and row.get("ToPort") == 443
            and row.get("ReferencedGroupInfo", {}).get("GroupId") == source
        ]
        if len(matches) > 1:
            raise LifecycleRefused("bootstrap network rule identity is ambiguous")
        if not matches:
            return None
        actual = matches[0]
        tags = {item["Key"]: item["Value"] for item in actual.get("Tags", [])}
        if (
            any(
                tags.get(tag["Key"]) != tag["Value"]
                for tag in arguments["TagSpecifications"][0]["Tags"]
            )
            or actual.get("GroupId") != arguments["GroupId"]
            or actual.get("GroupOwnerId") != outputs["account_id"]
            or actual.get("ReferencedGroupInfo", {}).get("UserId") != source_account
            or not actual.get("SecurityGroupRuleId")
        ):
            raise LifecycleRefused(
                "existing bootstrap rule lacks exact workspace attribution"
            )
        return actual

    for key, descriptor in recipe.items():
        arguments = descriptor["arguments"]
        await journal.authority()
        groups = await asyncio.to_thread(
            client.describe_security_groups, GroupIds=[arguments["GroupId"]]
        )
        await journal.authority()
        expected_vpc = outputs["vpc_id"]
        if len(groups["SecurityGroups"]) != 1 or any(
            groups["SecurityGroups"][0].get(field) != value
            for field, value in {
                "GroupId": arguments["GroupId"],
                "OwnerId": outputs["account_id"],
                "VpcId": expected_vpc,
            }.items()
        ):
            raise LifecycleRefused(
                "bootstrap rule target security group differs from the reviewed account and VPC"
            )
        recorded = await journal.intend(key, descriptor)
        actual = await read(arguments)
        if recorded is not None:
            if actual is None or actual["SecurityGroupRuleId"] != recorded["rule_id"]:
                raise LifecycleRefused("recorded bootstrap network rule was replaced")
            continue
        created = actual is None
        if created:
            await journal.authority()
            response = await asyncio.to_thread(
                client.authorize_security_group_ingress, **arguments
            )
            await journal.authority()
            identities = response.get("SecurityGroupRules", [])
            if len(identities) != 1:
                raise LifecycleRefused(
                    "bootstrap rule create returned no unique identity"
                )
            actual = await read(arguments)
            if actual is None or actual["SecurityGroupRuleId"] != identities[0].get(
                "SecurityGroupRuleId"
            ):
                raise LifecycleRefused(
                    "created bootstrap network rule was not observed"
                )
        await journal.confirm(
            key,
            descriptor,
            {"rule_id": actual["SecurityGroupRuleId"], "created": created},
        )
    evidence = await journal.complete()
    evidence["private-sts-rule"] = {"rule_id": retained_sts, "created": False}
    return evidence


class OwnedNetworkObservations:
    def __init__(self, access, evidence):
        self.access = access
        self.owned = {
            value["rule_id"] for value in evidence.values() if value["created"] is True
        }

    def __getattr__(self, name):
        return getattr(self.access, name)

    def security_group_rule(self, *args):
        result = dict(self.access.security_group_rule(*args))
        result["created_by_bootstrap"] = result.get("rule_id") in self.owned
        return result

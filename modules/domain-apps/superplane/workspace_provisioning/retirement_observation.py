"""Provider reads for bootstrap and Terraform retirement, including retained handles.

The catalog comes from the completed ownership journal and the reviewed saved
plan's BEFORE identities. Fresh cloud listings add leaked/dependent resources;
unknown resource types or unreadable providers stay unknown and cannot release.
"""

import asyncio
import hashlib
import json

from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    ResourceObservation,
    ResourcePresence,
)
from superplane_bootstrap.component_journal import component_identity


def reference(kind, **identity):
    if kind in {"instance", "volume", "network_interface", "address"}:
        return identity["id"]
    encoded = json.dumps(
        {"kind": kind, **identity}, sort_keys=True, separators=(",", ":")
    )
    return "retirement:" + kind + ":" + hashlib.sha256(encoded.encode()).hexdigest()


class RetirementObservations:
    def __init__(self, *, session, kubernetes, eks):
        self.session, self.kubernetes, self.eks = session, kubernetes, eks
        self.descriptors = {}

    def _client(self, service):
        return self.session.client(service, region_name=self.kubernetes.target.region)

    def _scope(self, inventory):
        target = self.kubernetes.target
        if inventory.cluster_arn != target.cluster_arn:
            raise OperationRefused("retirement inventory target changed")
        if self._client("sts").get_caller_identity()["Account"] != target.account_id:
            raise OperationRefused("retirement inventory provider account changed")

    def catalog(
        self, inventory, artifact, parameters, known, creation_keys=frozenset()
    ):
        self._scope(inventory)
        result = dict(known)

        def add(kind, **identity):
            ref = reference(kind, **identity)
            self.descriptors[ref] = {"kind": kind, **identity}
            if ref not in result:
                result[ref] = AllocationResource(ref, "aws", ref, kind, creation_keys)

        for item in inventory.components:
            if item.owned and item.desired["kind"] not in {
                "ClusterRole",
                "ClusterRoleBinding",
            }:
                add("bootstrap-component", body=item.desired, identity=item.identity)
        for item in inventory.grants:
            if item.spec.get("body", {}).get("kind") not in {
                "ClusterRole",
                "ClusterRoleBinding",
            }:
                add("bootstrap-grant", spec=item.spec, identity=item.identity)
        for item in inventory.prerequisites:
            if item.removable:
                add(
                    "bootstrap-prerequisite",
                    prerequisite_kind=item.kind,
                    identifier=item.identifier,
                )
        if inventory.remove_namespace:
            add(
                "owned-namespace", name=inventory.namespace, uid=inventory.namespace_uid
            )
        if not inventory.components_complete:
            # A durable uncertainty is part of membership, not an empty-success
            # branch. No provider observation can turn missing ownership into zero.
            add("unresolved-bootstrap-ownership", workspace=inventory.workspace_id)
        if inventory.cluster_ownership == "adp-created":
            if artifact is None:
                raise OperationRefused(
                    "managed retirement requires its reviewed state inventory"
                )
            _, rendered, _ = artifact.read(inventory, parameters)
            document = json.loads(rendered)
            for change in document["resource_changes"]:
                if (
                    change.get("mode", "managed") != "managed"
                    or change.get("type") == "terraform_data"
                ):
                    continue
                before = change.get("change", {}).get("before")
                if before is None:
                    continue
                kind = change.get("type")
                keys = {
                    "id",
                    "arn",
                    "name",
                    "role",
                    "policy_arn",
                    "cluster_name",
                    "node_group_name",
                    "addon_name",
                    "route_table_id",
                    "allocation_id",
                    "unique_id",
                    "key_id",
                    "target_key_id",
                }
                identity = {
                    key: before[key]
                    for key in keys
                    if isinstance(before.get(key), str) and before[key]
                }
                if not identity:
                    raise OperationRefused(
                        "reviewed Terraform resource lacks provider identity"
                    )
                add("terraform-resource", resource_type=kind, identity=identity)
            self._discover(inventory, document, add)
        return result

    def _discover(self, inventory, document, add):
        ec2 = self._client("ec2")
        vpcs = [
            change["change"]["before"]["id"]
            for change in document["resource_changes"]
            if change.get("type") == "aws_vpc"
            and change.get("change", {}).get("before")
        ]
        if len(vpcs) != 1:
            raise OperationRefused(
                "managed inventory requires one reviewed workspace VPC"
            )
        vpc = vpcs[0]
        filters = [{"Name": "vpc-id", "Values": [vpc]}]
        for page in ec2.get_paginator("describe_instances").paginate(Filters=filters):
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    add("instance", id=instance["InstanceId"])
                    for block in instance.get("BlockDeviceMappings", []):
                        if "Ebs" in block:
                            add("volume", id=block["Ebs"]["VolumeId"])
        for page in ec2.get_paginator("describe_network_interfaces").paginate(
            Filters=filters
        ):
            for interface in page["NetworkInterfaces"]:
                add("network_interface", id=interface["NetworkInterfaceId"])
                if allocation := interface.get("Association", {}).get("AllocationId"):
                    add("address", id=allocation)
        tags = [
            {"Name": "tag:WorkspaceId", "Values": [inventory.workspace_id]},
            {"Name": "tag:OrgId", "Values": [inventory.org_id]},
        ]
        for page in ec2.get_paginator("describe_volumes").paginate(Filters=tags):
            for volume in page["Volumes"]:
                add("volume", id=volume["VolumeId"])
        for item in ec2.describe_addresses(Filters=tags)["Addresses"]:
            add("address", id=item["AllocationId"])
        # A tagged resource outside the admitted state must not disappear from the
        # accounting answer. Unsupported discovered ARNs deliberately stay unknown.
        for page in (
            self._client("resourcegroupstaggingapi")
            .get_paginator("get_resources")
            .paginate(
                TagFilters=[
                    {"Key": "OrgId", "Values": [inventory.org_id]},
                    {"Key": "WorkspaceId", "Values": [inventory.workspace_id]},
                ]
            )
        ):
            for item in page["ResourceTagMappingList"]:
                arn = item["ResourceARN"]
                covered = any(
                    change.get("change", {}).get("before", {}).get("arn") == arn
                    for change in document["resource_changes"]
                    if change.get("change", {}).get("before")
                )
                if not covered:
                    parts = arn.split(":", 5)
                    family, separator, identifier = parts[-1].partition("/")
                    simple = {
                        "instance": "instance",
                        "volume": "volume",
                        "network-interface": "network_interface",
                        "elastic-ip": "address",
                    }
                    mapped = {
                        "vpc": "aws_vpc",
                        "subnet": "aws_subnet",
                        "internet-gateway": "aws_internet_gateway",
                        "natgateway": "aws_nat_gateway",
                        "route-table": "aws_route_table",
                        "security-group": "aws_security_group",
                        "security-group-rule": "aws_vpc_security_group_egress_rule",
                        "launch-template": "aws_launch_template",
                    }
                    if (
                        len(parts) == 6
                        and parts[2] == "ec2"
                        and separator
                        and family in simple
                    ):
                        add(simple[family], id=identifier)
                    elif (
                        len(parts) == 6
                        and parts[2] == "ec2"
                        and separator
                        and family in mapped
                    ):
                        add(
                            "terraform-resource",
                            resource_type=mapped[family],
                            identity={"id": identifier},
                        )
                    else:
                        add("unresolved-discovered-resource", arn=arn)

    def _cluster_absent(self, inventory):
        try:
            cluster = self._client("eks").describe_cluster(
                name=inventory.cluster_arn.rsplit("/", 1)[-1]
            )["cluster"]
        except Exception as exc:
            if self._code(exc) == "ResourceNotFoundException":
                return True
            raise
        if cluster["arn"] != inventory.cluster_arn:
            raise OperationRefused("retirement cluster identity changed")
        return False

    @staticmethod
    def _code(exc):
        return getattr(exc, "response", {}).get("Error", {}).get("Code")

    def observe(self, inventory, resource):
        ref = resource.provider_reference
        try:
            self._scope(inventory)
            value = self.descriptors.get(ref) or {"kind": resource.kind, "id": ref}
            kind = value["kind"]
            if kind in {"bootstrap-component", "owned-namespace", "bootstrap-grant"}:
                if (
                    inventory.cluster_ownership == "adp-created"
                    and self._cluster_absent(inventory)
                ):
                    present = False
                else:
                    present = self._bootstrap(kind, value, inventory)
            elif kind == "bootstrap-prerequisite":
                if value["prerequisite_kind"] == "EksAccessEntry":
                    arn = value["identifier"].partition("#")[0]
                    matches = [
                        g for g in inventory.grants if g.identity.get("arn") == arn
                    ]
                    if len(matches) != 1:
                        raise OperationRefused(
                            "access prerequisite lacks immutable grant identity"
                        )
                    present = self.eks.observe(matches[0].spec) is not None
                else:
                    present = self._ec2(
                        "describe_security_group_rules",
                        "SecurityGroupRules",
                        {"SecurityGroupRuleIds": [value["identifier"]]},
                        {"InvalidSecurityGroupRuleId.NotFound"},
                    )
            elif kind == "terraform-resource":
                present = self._terraform(value["resource_type"], value["identity"])
            elif kind in {"instance", "volume", "network_interface", "address"}:
                mapping = {
                    "instance": (
                        "describe_instances",
                        "Reservations",
                        "InstanceIds",
                        "InvalidInstanceID.NotFound",
                    ),
                    "volume": (
                        "describe_volumes",
                        "Volumes",
                        "VolumeIds",
                        "InvalidVolume.NotFound",
                    ),
                    "network_interface": (
                        "describe_network_interfaces",
                        "NetworkInterfaces",
                        "NetworkInterfaceIds",
                        "InvalidNetworkInterfaceID.NotFound",
                    ),
                    "address": (
                        "describe_addresses",
                        "Addresses",
                        "AllocationIds",
                        "InvalidAllocationID.NotFound",
                    ),
                }
                method, key, parameter, missing = mapping[kind]
                present = self._ec2(
                    method, key, {parameter: [value["id"]]}, {missing}, kind=kind
                )
            else:
                raise OperationRefused("resource observation adapter unavailable")
            return ResourceObservation(
                ResourcePresence.PRESENT if present else ResourcePresence.ABSENT,
                ref,
                provider_state="provider reports present" if present else None,
                detail="provider reports present"
                if present
                else "provider reports absent",
            )
        except Exception:
            return ResourceObservation(
                ResourcePresence.UNKNOWN,
                ref,
                provider_state=None,
                detail="provider identity or observation unavailable",
            )

    def _bootstrap(self, kind, value, inventory):
        if kind == "bootstrap-grant":
            adapter = (
                self.kubernetes if value["spec"]["kind"] == "kubernetes" else self.eks
            )
            current = adapter.observe(value["spec"])
            if current is not None and current != value["identity"]:
                raise OperationRefused("retained grant identity changed")
            return current is not None
        body = value.get("body") or {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": value["name"]},
        }
        current = self.kubernetes._get(
            {"cluster_arn": inventory.cluster_arn, "body": body}
        )
        if current is not None:
            if kind == "bootstrap-component":
                if component_identity(current) != value["identity"]:
                    raise OperationRefused("retained component identity changed")
            elif current["metadata"]["uid"] != value["uid"]:
                raise OperationRefused("owned namespace replaced")
        return current is not None

    def _ec2(self, method, key, arguments, missing, *, kind=None):
        try:
            rows = getattr(self._client("ec2"), method)(**arguments)[key]
        except Exception as exc:
            if self._code(exc) in missing:
                return False
            raise
        if kind == "instance":
            rows = [
                i
                for r in rows
                for i in r["Instances"]
                if i["State"]["Name"] != "terminated"
            ]
        if kind == "nat":
            rows = [r for r in rows if r["State"] != "deleted"]
        return bool(rows)

    def _terraform(self, kind, identity):
        ec2 = {
            "aws_vpc": ("describe_vpcs", "Vpcs", "VpcIds", "InvalidVpcID.NotFound"),
            "aws_subnet": (
                "describe_subnets",
                "Subnets",
                "SubnetIds",
                "InvalidSubnetID.NotFound",
            ),
            "aws_internet_gateway": (
                "describe_internet_gateways",
                "InternetGateways",
                "InternetGatewayIds",
                "InvalidInternetGatewayID.NotFound",
            ),
            "aws_eip": (
                "describe_addresses",
                "Addresses",
                "AllocationIds",
                "InvalidAllocationID.NotFound",
            ),
            "aws_nat_gateway": (
                "describe_nat_gateways",
                "NatGateways",
                "NatGatewayIds",
                "NatGatewayNotFound",
            ),
            "aws_route_table": (
                "describe_route_tables",
                "RouteTables",
                "RouteTableIds",
                "InvalidRouteTableID.NotFound",
            ),
            "aws_security_group": (
                "describe_security_groups",
                "SecurityGroups",
                "GroupIds",
                "InvalidGroup.NotFound",
            ),
            "aws_vpc_security_group_egress_rule": (
                "describe_security_group_rules",
                "SecurityGroupRules",
                "SecurityGroupRuleIds",
                "InvalidSecurityGroupRuleId.NotFound",
            ),
            "aws_launch_template": (
                "describe_launch_templates",
                "LaunchTemplates",
                "LaunchTemplateIds",
                "InvalidLaunchTemplateId.NotFound",
            ),
        }
        if kind in ec2:
            method, key, argument, missing = ec2[kind]
            return self._ec2(
                method,
                key,
                {argument: [identity["id"]]},
                {missing},
                kind="nat" if kind == "aws_nat_gateway" else None,
            )
        try:
            if kind == "aws_eks_cluster":
                result = self._client("eks").describe_cluster(name=identity["name"])[
                    "cluster"
                ]
                if result["arn"] != identity["arn"]:
                    raise OperationRefused("EKS identity replaced")
            elif kind == "aws_eks_node_group":
                self._client("eks").describe_nodegroup(
                    clusterName=identity["cluster_name"],
                    nodegroupName=identity["node_group_name"],
                )
            elif kind == "aws_eks_addon":
                self._client("eks").describe_addon(
                    clusterName=identity["cluster_name"],
                    addonName=identity["addon_name"],
                )
            elif kind == "aws_iam_role":
                result = self._client("iam").get_role(RoleName=identity["name"])["Role"]
                if (
                    result["Arn"] != identity["arn"]
                    or result["RoleId"] != identity["unique_id"]
                ):
                    raise OperationRefused("IAM role identity replaced")
            elif kind == "aws_iam_role_policy":
                self._client("iam").get_role_policy(
                    RoleName=identity["role"], PolicyName=identity["name"]
                )
            elif kind == "aws_iam_openid_connect_provider":
                self._client("iam").get_open_id_connect_provider(
                    OpenIDConnectProviderArn=identity["arn"]
                )
            elif kind == "aws_kms_key":
                # PendingDeletion remains present until the provider establishes
                # final absence; a scheduled deletion never means zero exposure.
                self._client("kms").describe_key(
                    KeyId=identity.get("key_id") or identity["id"]
                )
            elif kind == "aws_iam_role_policy_attachment":
                return any(
                    item["PolicyArn"] == identity["policy_arn"]
                    for page in self._client("iam")
                    .get_paginator("list_attached_role_policies")
                    .paginate(RoleName=identity["role"])
                    for item in page["AttachedPolicies"]
                )
            elif kind == "aws_kms_alias":
                return any(
                    item["AliasName"] == identity["name"]
                    for page in self._client("kms")
                    .get_paginator("list_aliases")
                    .paginate()
                    for item in page["Aliases"]
                )
            elif kind == "aws_cloudwatch_log_group":
                return any(
                    item["logGroupName"] == identity["name"]
                    for page in self._client("logs")
                    .get_paginator("describe_log_groups")
                    .paginate(logGroupNamePrefix=identity["name"])
                    for item in page["logGroups"]
                )
            elif kind == "aws_route_table_association":
                return any(
                    item["RouteTableAssociationId"] == identity["id"]
                    for table in self._client("ec2").describe_route_tables(
                        RouteTableIds=[identity["route_table_id"]]
                    )["RouteTables"]
                    for item in table["Associations"]
                )
            else:
                raise OperationRefused("Terraform resource type is not observable")
        except Exception as exc:
            expected = {
                "aws_eks_cluster": "ResourceNotFoundException",
                "aws_eks_node_group": "ResourceNotFoundException",
                "aws_eks_addon": "ResourceNotFoundException",
                "aws_kms_key": "NotFoundException",
                "aws_route_table_association": "InvalidRouteTableID.NotFound",
            }.get(kind)
            if self._code(exc) == expected and expected is not None:
                return False
            if kind.startswith("aws_iam_") and self._code(exc) == "NoSuchEntity":
                return False
            raise
        return True

    async def snapshot(self, inventory, artifact, parameters, known):
        resources = await asyncio.to_thread(
            self.catalog, inventory, artifact, parameters, known
        )
        observations = {}
        for resource in resources.values():
            observations[resource.resource_id] = await asyncio.to_thread(
                self.observe, inventory, resource
            )
        return resources, observations

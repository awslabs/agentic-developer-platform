"""Bounded provider census, not authoritative ownership or complete cleanup proof."""

import json
import re

from .demo1_aws import _resource
from .demo1_evidence import EvidenceError, identifier
from .demo1_report import reference

COLLECTIONS = {
    "instances": ("Reservations", "InstanceId", "instance", "i", "VpcId"),
    "volumes": ("Volumes", "VolumeId", "volume", "vol", None),
    "snapshots": ("Snapshots", "SnapshotId", "snapshot", "snap", None),
    "network-interfaces": (
        "NetworkInterfaces",
        "NetworkInterfaceId",
        "network-interface",
        "eni",
        "VpcId",
    ),
    "addresses": ("Addresses", "AllocationId", "elastic-ip", "eipalloc", None),
    "nat-gateways": ("NatGateways", "NatGatewayId", "natgateway", "nat", "VpcId"),
    "route-tables": ("RouteTables", "RouteTableId", "route-table", "rtb", "VpcId"),
    "internet-gateways": (
        "InternetGateways",
        "InternetGatewayId",
        "internet-gateway",
        "igw",
        "Attachments",
    ),
    "vpc-endpoints": ("VpcEndpoints", "VpcEndpointId", "vpc-endpoint", "vpce", "VpcId"),
    "launch-templates": (
        "LaunchTemplates",
        "LaunchTemplateId",
        "launch-template",
        "lt",
        None,
    ),
    "subnets": ("Subnets", "SubnetId", "subnet", "subnet", "VpcId"),
    "security-groups": ("SecurityGroups", "GroupId", "security-group", "sg", "VpcId"),
    "vpcs": ("Vpcs", "VpcId", "vpc", "vpc", None),
}
MAX_PAGES = 64
MAX_RESOURCES = 200


def require(condition):
    if not condition:
        raise EvidenceError(
            "discovery: incomplete, foreign or unbounded provider census"
        )


class ProviderCensus:
    def __init__(self, reader, query, org_id, known_states=None):
        require(
            all(
                getattr(reader, field) == getattr(query, field)
                for field in ("connection_id", "account", "region")
            )
        )
        require(reader.role_name == query.role)
        self.reader, self.query = reader, query
        self.org_id = identifier(org_id, "discovery organization")
        identifier(query.workspace_id, "discovery workspace")
        for resource in query.expected_owned + query.expected_survivors:
            _resource(resource, query)
        self.known_states = known_states or {}
        partitions = {value.split(":")[1] for value in query.expected_owned}
        require(len(partitions) == 1 and partitions <= {"aws", "aws-us-gov", "aws-cn"})
        self.partition = next(iter(partitions))
        self.resources, self.unsupported, self.attachments = {}, set(), set()
        self.pages = 0

    def pages_for(self, service, operation, collection, *arguments, paginated=True):
        token_key = (
            "PaginationToken" if service == "resourcegroupstaggingapi" else "NextToken"
        )
        token_arg = (
            "--pagination-token"
            if service == "resourcegroupstaggingapi"
            else "--next-token"
        )
        token, seen, identities = None, set(), set()
        while True:
            self.pages += 1
            require(self.pages <= MAX_PAGES and self.reader._identity())
            code, result, _ = self.reader._execute(
                service,
                operation,
                *arguments,
                *((token_arg, token) if token else ()),
                "--region",
                self.query.region,
                "--no-paginate",
            )
            require(
                code == 0
                and isinstance(result, dict)
                and isinstance(result.get(collection), list)
            )
            require(
                not any(
                    result.get(key)
                    for key in ("nextToken", "NextToken", "PaginationToken")
                    if key != token_key
                )
            )
            rows = result[collection]
            require(len(rows) <= MAX_RESOURCES)
            for row in rows:
                require(isinstance(row, dict))
                encoded = json.dumps(row, sort_keys=True)
                require(encoded not in identities)
                identities.add(encoded)
                yield row
            token = result.get(token_key)
            if token in (None, ""):
                return
            require(
                paginated
                and isinstance(token, str)
                and 0 < len(token) <= 4096
                and token not in seen
            )
            seen.add(token)

    def tagged(self, tags):
        require(isinstance(tags, list))
        values = {}
        for tag in tags:
            require(
                isinstance(tag, dict)
                and isinstance(tag.get("Key"), str)
                and isinstance(tag.get("Value"), str)
                and tag["Key"] not in values
            )
            values[tag["Key"]] = tag["Value"]
        require(
            values.get("OrgId") == self.org_id
            and values.get("WorkspaceId") == self.query.workspace_id
        )

    def resource(self, kind, prefix, identity, basis, *, presence="present"):
        require(
            isinstance(identity, str)
            and re.fullmatch(rf"{prefix}-[0-9a-f]{{8}}(?:[0-9a-f]{{9}})?", identity)
        )
        arn = f"arn:{self.partition}:ec2:{self.query.region}:{self.query.account}:{kind}/{identity}"
        require(arn not in self.query.expected_survivors)
        require(self.known_states.get(arn) != "absent")
        prior = self.resources.setdefault(
            arn, {"kind": kind, "presence": presence, "basis": set()}
        )
        require(prior["presence"] == presence)
        prior["basis"].add(basis)
        require(len(self.resources) + len(self.unsupported) <= MAX_RESOURCES)

    def entries(self, operation, *, vpc=None):
        collection, identity_key, kind, prefix, vpc_key = COLLECTIONS[operation]
        filters = (
            [
                {
                    "Name": "attachment.vpc-id"
                    if vpc_key == "Attachments"
                    else "vpc-id",
                    "Values": [vpc],
                }
            ]
            if vpc
            else [
                {"Name": "tag:OrgId", "Values": [self.org_id]},
                {"Name": "tag:WorkspaceId", "Values": [self.query.workspace_id]},
            ]
        )
        arguments = [
            "--filter" if operation == "nat-gateways" else "--filters",
            json.dumps(filters),
        ]
        if operation != "addresses":
            arguments += ["--max-results", "100"]
        if operation == "snapshots":
            arguments += ["--owner-ids", "self"]
        seen = set()
        for row in self.pages_for(
            "ec2",
            "describe-" + operation,
            collection,
            *arguments,
            paginated=operation != "addresses",
        ):
            if operation == "instances":
                require(
                    row.get("OwnerId") == self.query.account
                    and isinstance(row.get("Instances"), list)
                )
                rows = row["Instances"]
            else:
                rows = [row]
            require(len(rows) <= MAX_RESOURCES)
            for item in rows:
                require(
                    isinstance(item, dict)
                    and isinstance(item.get(identity_key), str)
                    and item[identity_key] not in seen
                )
                seen.add(item[identity_key])
                if "OwnerId" in item:
                    require(item["OwnerId"] == self.query.account)
                if vpc:
                    if vpc_key == "Attachments":
                        require(
                            isinstance(item.get("Attachments"), list)
                            and any(
                                isinstance(entry, dict) and entry.get("VpcId") == vpc
                                for entry in item["Attachments"]
                            )
                        )
                    else:
                        require(item.get(vpc_key) == vpc)
                else:
                    self.tagged(item.get("Tags"))
                presence = "present"
                if operation == "instances":
                    require(
                        isinstance(item.get("State"), dict)
                        and item["State"].get("Name")
                        in {
                            "pending",
                            "running",
                            "stopping",
                            "stopped",
                            "shutting-down",
                            "terminated",
                        }
                    )
                    if item["State"]["Name"] == "terminated":
                        presence = "terminated"
                    blocks = item.get("BlockDeviceMappings", [])
                    require(isinstance(blocks, list))
                    for block in blocks:
                        require(isinstance(block, dict))
                        if "Ebs" in block:
                            require(
                                isinstance(block["Ebs"], dict)
                                and isinstance(block["Ebs"].get("VolumeId"), str)
                            )
                            self.attachments.add(("volumes", block["Ebs"]["VolumeId"]))
                if operation == "network-interfaces" and item.get("Association"):
                    association = item["Association"]
                    require(isinstance(association, dict))
                    if association.get("AllocationId"):
                        require(isinstance(association["AllocationId"], str))
                        self.attachments.add(("addresses", association["AllocationId"]))
                require(len(self.attachments) <= MAX_RESOURCES)
                self.resource(
                    kind,
                    prefix,
                    item[identity_key],
                    "owned-vpc" if vpc else "workspace-tags",
                    presence=presence,
                )

    def collect(self):
        owned_vpcs = [
            value.rsplit("/", 1)[1]
            for value in self.query.expected_owned
            if ":vpc/" in value
        ]
        require(len(owned_vpcs) <= 1)
        for operation, descriptor in COLLECTIONS.items():
            self.entries(operation)
            if owned_vpcs and descriptor[-1]:
                self.entries(operation, vpc=owned_vpcs[0])
        for operation, identity in sorted(self.attachments):
            collection, identity_key, kind, prefix, _ = COLLECTIONS[operation]
            self.resource(kind, prefix, identity, "provider-attachment")
            rows = list(
                self.pages_for(
                    "ec2",
                    "describe-" + operation,
                    collection,
                    "--volume-ids" if operation == "volumes" else "--allocation-ids",
                    identity,
                    paginated=False,
                )
            )
            require(len(rows) == 1 and rows[0].get(identity_key) == identity)
            if "OwnerId" in rows[0]:
                require(rows[0]["OwnerId"] == self.query.account)
        tag_filters = [
            {"Key": "OrgId", "Values": [self.org_id]},
            {"Key": "WorkspaceId", "Values": [self.query.workspace_id]},
        ]
        tagged = set()
        for item in self.pages_for(
            "resourcegroupstaggingapi",
            "get-resources",
            "ResourceTagMappingList",
            "--tag-filters",
            json.dumps(tag_filters),
            "--resources-per-page",
            "100",
        ):
            self.tagged(item.get("Tags"))
            arn = item.get("ResourceARN")
            require(isinstance(arn, str) and len(arn) <= 1024 and arn not in tagged)
            require(
                re.fullmatch(
                    r"arn:(?:aws|aws-us-gov|aws-cn):[a-z0-9-]+:[a-z0-9-]*:[0-9]*:[!-~]+",
                    arn,
                )
            )
            parts = arn.split(":", 5)
            require(
                len(parts) == 6
                and parts[0] == "arn"
                and parts[1] == self.partition
                and parts[2]
                and parts[5]
                and parts[3] in (self.query.region, "")
                and (parts[4] == self.query.account or (parts[2:5] == ["s3", "", ""]))
            )
            require(arn not in self.query.expected_survivors)
            require(self.known_states.get(arn) != "absent")
            tagged.add(arn)
            if arn not in self.resources and arn not in self.query.expected_owned:
                self.unsupported.add(arn)
            require(len(self.resources) + len(self.unsupported) <= MAX_RESOURCES)
        return {
            "status": "OBSERVED",
            "listing_complete": True,
            "inventory_complete": False,
            "observed_at": self.reader.clock().isoformat(),
            "resources": {
                key: {**value, "basis": sorted(value["basis"])}
                for key, value in self.resources.items()
            },
            "unresolved_resources": sorted(self.unsupported),
            "owned_vpc_scanned": bool(owned_vpcs),
        }


def discovery_report(observed):
    return {
        **{
            key: observed[key]
            for key in (
                "status",
                "listing_complete",
                "inventory_complete",
                "observed_at",
                "owned_vpc_scanned",
            )
        },
        "resources": [
            {"resource_ref": reference(arn), **details}
            for arn, details in sorted(observed["resources"].items())
        ],
        "unresolved_resource_refs": [
            reference(arn) for arn in observed["unresolved_resources"]
        ],
        "scope": "current tagged and owned-VPC census only; not durable ownership, fenced completeness, cost or cleanup authority",
    }

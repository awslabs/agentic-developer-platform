"""Stateful AWS contract fixture; every SDK request is checked against botocore."""

import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import botocore.session
from botocore import xform_name
from botocore.validate import validate_parameters
from superplane_executor.network_runtime import Network
from superplane_executor.plan import Plan

ACCOUNT = "123456789012"
HOME = "us-east-1"
REMOTE = "us-west-2"


def side(index):
    suffix = str(index) * 8
    return {
        "vpc_id": "vpc-" + suffix,
        "vpc_cidr": f"10.{index}.0.0/16",
        "node_cidr": f"10.{index}.0.0/24",
        "pod_cidr": f"10.{index + 10}.0.0/16",
        "subnet_ids": ["subnet-" + suffix],
        "transit_gateway_id": "tgw-" + suffix,
        "attachment_id": None,
        "transit_gateway_route_table_id": "tgw-rtb-" + suffix,
        "vpc_route_table_ids": ["rtb-" + suffix],
        "security_group_id": "sg-" + suffix,
    }


def policy(cluster):
    return {
        "cluster": {
            "version": 1,
            "cluster_id": cluster,
            "membership_generation": "a" * 64,
            "network": side(1),
        },
        "regions": {
            REMOTE: {
                "network": side(2),
                "peering_id": None,
                "dns": {
                    "rule_id": "rslvr-rr-12345678",
                    "association_id": "rslvr-rrassoc-12345678",
                    "outbound_endpoint_id": "rslvr-out-12345678",
                    "inbound_endpoint_id": "rslvr-in-12345678",
                },
            }
        },
    }


class AWS:
    def __init__(self):
        self.sides = {HOME: side(1), REMOTE: side(2)}
        self.attachments = {}
        self.peerings = {}
        self.associations = {}
        self.routes = {}
        self.rules = {}
        self.calls = []
        self.next = 10
        self.models = {}
        self.lost = None
        self.denied = None

    def client(self, service, *, region_name):
        owner = self
        if service not in self.models:
            model = botocore.session.get_session().get_service_model(service)
            self.models[service] = (
                model,
                {xform_name(name): name for name in model.operation_names},
            )
        model, names = self.models[service]

        class Client:
            def __getattr__(self, name):
                def call(**arguments):
                    shape = model.operation_model(names[name]).input_shape
                    validate_parameters(arguments, shape)
                    owner.calls.append((region_name, name, deepcopy(arguments)))
                    if owner.denied == name:
                        raise PermissionError("fixture denied")
                    result = owner.respond(region_name, service, name, arguments)
                    if owner.lost == name:
                        owner.lost = None
                        raise TimeoutError("fixture lost successful mutation response")
                    return deepcopy(result)

                return call

        return Client()

    def ident(self, prefix):
        self.next += 1
        return f"{prefix}-{self.next:017x}"

    def respond(self, region, service, name, a):
        s = self.sides[region]
        if name == "describe_vpcs":
            return {
                "Vpcs": [
                    {
                        "VpcId": s["vpc_id"],
                        "OwnerId": ACCOUNT,
                        "CidrBlock": s["vpc_cidr"],
                        "State": "available",
                    }
                ]
            }
        if name == "describe_security_groups":
            return {
                "SecurityGroups": [
                    {
                        "GroupId": s["security_group_id"],
                        "VpcId": s["vpc_id"],
                        "OwnerId": ACCOUNT,
                    }
                ]
            }
        if name == "describe_subnets":
            return {
                "Subnets": [
                    {
                        "SubnetId": n,
                        "VpcId": s["vpc_id"],
                        "OwnerId": ACCOUNT,
                        "State": "available",
                    }
                    for n in s["subnet_ids"]
                ]
            }
        if name == "describe_transit_gateways":
            return {
                "TransitGateways": [
                    {
                        "TransitGatewayId": s["transit_gateway_id"],
                        "OwnerId": ACCOUNT,
                        "State": "available",
                    }
                ]
            }
        if name == "describe_transit_gateway_route_tables":
            return {
                "TransitGatewayRouteTables": [
                    {
                        "TransitGatewayRouteTableId": s[
                            "transit_gateway_route_table_id"
                        ],
                        "TransitGatewayId": s["transit_gateway_id"],
                        "State": "available",
                    }
                ]
            }
        if name == "describe_cluster":
            return {
                "cluster": {
                    "arn": f"arn:aws:eks:{HOME}:{ACCOUNT}:cluster/test",
                    "endpoint": "https://test.eks.amazonaws.com",
                    "certificateAuthority": {"data": "fixture-ca"},
                    "resourcesVpcConfig": {
                        "vpcId": self.sides[HOME]["vpc_id"],
                        "endpointPrivateAccess": True,
                        "clusterSecurityGroupId": self.sides[HOME]["security_group_id"],
                    },
                }
            }
        if name == "get_resolver_rule":
            return {
                "ResolverRule": {
                    "OwnerId": ACCOUNT,
                    "RuleType": "FORWARD",
                    "Status": "COMPLETE",
                    "DomainName": "test.eks.amazonaws.com.",
                    "ResolverEndpointId": "rslvr-out-12345678",
                    "TargetIps": [{"Ip": "10.1.0.10", "Port": 53}],
                }
            }
        if name == "get_resolver_rule_association":
            return {
                "ResolverRuleAssociation": {
                    "ResolverRuleId": "rslvr-rr-12345678",
                    "VPCId": s["vpc_id"],
                    "Status": "COMPLETE",
                }
            }
        if name == "get_resolver_endpoint":
            return {
                "ResolverEndpoint": {
                    "Direction": "OUTBOUND" if region == REMOTE else "INBOUND",
                    "HostVPCId": s["vpc_id"],
                    "Status": "OPERATIONAL",
                }
            }
        if name == "list_resolver_endpoint_ip_addresses":
            return {"IpAddresses": [{"Ip": "10.1.0.10", "Status": "ATTACHED"}]}
        if name == "create_transit_gateway_vpc_attachment":
            ident = self.ident("tgw-attach")
            self.attachments[ident] = {
                "TransitGatewayAttachmentId": ident,
                "VpcId": a["VpcId"],
                "VpcOwnerId": ACCOUNT,
                "SubnetIds": a["SubnetIds"],
                "TransitGatewayId": a["TransitGatewayId"],
                "State": "available",
                "Tags": a["TagSpecifications"][0]["Tags"],
                "region": region,
            }
            return {
                "TransitGatewayVpcAttachment": {
                    k: v for k, v in self.attachments[ident].items() if k != "region"
                }
            }
        if name == "describe_transit_gateway_vpc_attachments":
            rows = [v for v in self.attachments.values() if v["region"] == region]
            if "TransitGatewayAttachmentIds" in a:
                rows = [
                    v
                    for v in rows
                    if v["TransitGatewayAttachmentId"]
                    in a["TransitGatewayAttachmentIds"]
                ]
            for f in a.get("Filters", []):
                field = {
                    "vpc-id": "VpcId",
                    "transit-gateway-id": "TransitGatewayId",
                    "state": "State",
                    "transit-gateway-attachment-id": "TransitGatewayAttachmentId",
                }[f["Name"]]
                rows = [v for v in rows if v[field] in f["Values"]]
            return {
                "TransitGatewayVpcAttachments": [
                    {k: v for k, v in row.items() if k != "region"} for row in rows
                ]
            }
        if name == "delete_transit_gateway_vpc_attachment":
            self.attachments.pop(a["TransitGatewayAttachmentId"], None)
            return {}
        if name == "create_transit_gateway_peering_attachment":
            ident = self.ident("tgw-attach")
            self.peerings[ident] = {
                "TransitGatewayAttachmentId": ident,
                "RequesterTgwInfo": {
                    "TransitGatewayId": a["TransitGatewayId"],
                    "OwnerId": ACCOUNT,
                    "Region": HOME,
                },
                "AccepterTgwInfo": {
                    "TransitGatewayId": a["PeerTransitGatewayId"],
                    "OwnerId": a["PeerAccountId"],
                    "Region": a["PeerRegion"],
                },
                "State": "pendingAcceptance",
                "Tags": a["TagSpecifications"][0]["Tags"],
            }
            return {"TransitGatewayPeeringAttachment": self.peerings[ident]}
        if name == "describe_transit_gateway_peering_attachments":
            rows = list(self.peerings.values())
            if "TransitGatewayAttachmentIds" in a:
                rows = [
                    v
                    for v in rows
                    if v["TransitGatewayAttachmentId"]
                    in a["TransitGatewayAttachmentIds"]
                ]
            for f in a.get("Filters", []):

                def field(v, f=f):
                    if f["Name"] == "state":
                        return v["State"]
                    if f["Name"] == "transit-gateway-attachment-id":
                        return v["TransitGatewayAttachmentId"]
                    return v[
                        "RequesterTgwInfo"
                        if f["Name"].startswith("requester")
                        else "AccepterTgwInfo"
                    ]["TransitGatewayId"]

                rows = [v for v in rows if field(v) in f["Values"]]
            return {"TransitGatewayPeeringAttachments": rows}
        if name == "accept_transit_gateway_peering_attachment":
            assert region == REMOTE
            self.peerings[a["TransitGatewayAttachmentId"]]["State"] = "available"
            return {
                "TransitGatewayPeeringAttachment": self.peerings[
                    a["TransitGatewayAttachmentId"]
                ]
            }
        if name == "delete_transit_gateway_peering_attachment":
            self.peerings.pop(a["TransitGatewayAttachmentId"], None)
            return {}
        if name == "associate_transit_gateway_route_table":
            self.associations[
                (
                    region,
                    a["TransitGatewayRouteTableId"],
                    a["TransitGatewayAttachmentId"],
                )
            ] = True
            return {}
        if name == "get_transit_gateway_route_table_associations":
            ids = a["Filters"][0]["Values"]
            return {
                "Associations": [
                    {"TransitGatewayAttachmentId": ident, "State": "associated"}
                    for (r, t, ident) in self.associations
                    if r == region
                    and t == a["TransitGatewayRouteTableId"]
                    and ident in ids
                ]
            }
        if name == "disassociate_transit_gateway_route_table":
            self.associations.pop(
                (
                    region,
                    a["TransitGatewayRouteTableId"],
                    a["TransitGatewayAttachmentId"],
                ),
                None,
            )
            return {}
        if name == "create_transit_gateway_route":
            self.routes[
                (region, a["TransitGatewayRouteTableId"], a["DestinationCidrBlock"])
            ] = {
                "DestinationCidrBlock": a["DestinationCidrBlock"],
                "State": "active",
                "Type": "static",
                "TransitGatewayAttachments": [
                    {"TransitGatewayAttachmentId": a["TransitGatewayAttachmentId"]}
                ],
            }
            return {}
        if name == "search_transit_gateway_routes":
            row = self.routes.get(
                (region, a["TransitGatewayRouteTableId"], a["Filters"][0]["Values"][0])
            )
            return {"Routes": [row] if row else [], "AdditionalRoutesAvailable": False}
        if name == "create_route":
            self.routes[(region, a["RouteTableId"], a["DestinationCidrBlock"])] = {
                "DestinationCidrBlock": a["DestinationCidrBlock"],
                "State": "active",
                "TransitGatewayId": a["TransitGatewayId"],
            }
            return {"Return": True}
        if name == "describe_route_tables":
            return {
                "RouteTables": [
                    {
                        "RouteTableId": table,
                        "VpcId": s["vpc_id"],
                        "OwnerId": ACCOUNT,
                        "Routes": [
                            row
                            for (r, t, c), row in self.routes.items()
                            if r == region and t == table
                        ],
                    }
                    for table in a["RouteTableIds"]
                ]
            }
        if name in {"delete_route", "delete_transit_gateway_route"}:
            self.routes.pop(
                (
                    region,
                    a.get("RouteTableId", a.get("TransitGatewayRouteTableId")),
                    a["DestinationCidrBlock"],
                ),
                None,
            )
            return {}
        if name == "describe_security_group_rules":
            return {
                "SecurityGroupRules": [
                    r
                    for r in self.rules.values()
                    if r["GroupId"] in a["Filters"][0]["Values"]
                ]
            }
        if name.startswith("authorize_security_group_"):
            ident = self.ident("sgr")
            p = a["IpPermissions"][0]
            self.rules[ident] = {
                "SecurityGroupRuleId": ident,
                "GroupId": a["GroupId"],
                "GroupOwnerId": ACCOUNT,
                "IsEgress": name.endswith("egress"),
                "IpProtocol": p["IpProtocol"],
                "CidrIpv4": p["IpRanges"][0]["CidrIp"],
                "FromPort": p.get("FromPort"),
                "ToPort": p.get("ToPort"),
            }
            return {"Return": True, "SecurityGroupRules": [self.rules[ident]]}
        if name.startswith("revoke_security_group_"):
            for ident in a["SecurityGroupRuleIds"]:
                self.rules.pop(ident, None)
            return {"Return": True}
        raise AssertionError((region, service, name, a))


async def schema(pool):
    migration = (
        Path(__file__).resolve().parents[2]
        / "src/superplane-api/alembic/versions/035_controller_network_journal.py"
    )
    tree = ast.parse(migration.read_text())
    ddl = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DDL" for t in n.targets)
    )
    async with pool.acquire() as c:
        await c.execute(ddl)


async def make_network(
    pool, aws=None, *, org=None, workspace=None, cluster=None, allocation=None
):
    org, workspace, cluster = (
        org or str(uuid4()),
        workspace or str(uuid4()),
        cluster or str(uuid4()),
    )
    aws = aws or AWS()
    net = policy(cluster)
    lease = SimpleNamespace(
        org_id=org,
        workspace_id=workspace,
        operation_id=str(uuid4()),
        attempt_id=str(uuid4()),
        fence_token=1,
    )
    params = {"allocation_id": allocation or str(uuid4())}
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease),
        plan_digest="a" * 64,
        request=SimpleNamespace(parameters=params),
    )
    plan = Plan(
        {
            "version": 4,
            "provider_account_id": ACCOUNT,
            "cluster_arn": f"arn:aws:eks:{HOME}:{ACCOUNT}:cluster/test",
            "endpoint": "https://test.eks.amazonaws.com",
            "certificate_authority": "fixture-ca",
            "regions": [{"region": REMOTE}],
        },
        "capacity",
        (),
        net,
    )
    target = {"cluster_id": cluster}

    async def authorize():
        return None

    provider = SimpleNamespace(domain_pool=pool)
    runtime = Network(provider, operation, target, plan, aws, authorize)
    # The journal tests isolate concurrency/provider behavior from the separate
    # registered-worker tests. They never claim these synthetic leases authorize
    # production execution; the real runtime.authority is tested independently.
    runtime.authority = authorize
    runtime.journal.authorize = authorize
    return runtime, aws

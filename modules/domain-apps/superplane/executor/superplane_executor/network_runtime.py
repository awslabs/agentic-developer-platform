"""Approved AWS network composition inside the existing launch/removal effect.

TGWs, route tables, Resolver endpoints/rules and SGs are reviewed prerequisites.
Owned VPC attachments/peering, routes, associations and rules are journalled before
mutation. SDK clients are region-explicit and every call revalidates authority.
"""

import asyncio
import hashlib
import json
from urllib.parse import urlsplit

from harness_jobs.identity import OperationRefused

from .network_journal import NetworkJournal, NetworkPending
from .network_plan import canonical


class Network:
    def __init__(self, provider, operation, target, plan, session, authorize):
        self.provider, self.operation, self.target, self.plan = (
            provider,
            operation,
            target,
            plan,
        )
        self.session, self.authorize = session, authorize
        self.journal = NetworkJournal(
            provider.domain_pool, operation, plan, self.authority
        )
        self.home = plan.network["cluster"]["network"]
        self.account = plan.data["provider_account_id"]
        self.recipes = []

    async def authority(self):
        await self.authorize()
        binding = self.plan.network["cluster"]
        if self.target["cluster_id"] != binding["cluster_id"]:
            raise OperationRefused("network workspace cluster changed")
        lease = self.operation.grant.lease
        async with self.provider.domain_pool.acquire() as c:
            row = await c.fetchrow(
                """SELECT w.cluster_id::text,w.org_id::text,c.eks_cluster_arn,
              w.namespace_name,c.workspace_id::text AS owner_workspace_id,
              (SELECT count(*) FROM workspaces peers WHERE peers.cluster_id=c.id) AS members
              FROM workspaces w JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id
              WHERE w.id::text=$1 AND w.org_id::text=$2""",
                lease.workspace_id,
                lease.org_id,
            )
            if (
                row is None
                or row["cluster_id"] != binding["cluster_id"]
                or row["eks_cluster_arn"] != self.plan.data["cluster_arn"]
            ):
                raise OperationRefused("registered network cluster identity changed")
            # Shared-cluster registration publishes an explicit active membership.
            # Never accept a caller-supplied generation as database authority.
            if await c.fetchval(
                "SELECT to_regclass('cluster_memberships') IS NOT NULL"
            ):
                membership = await c.fetchrow(
                    """SELECT generation,namespace FROM cluster_memberships
                    WHERE workspace_id::text=$1 AND org_id::text=$2
                    AND cluster_id::text=$3 AND state='active'""",
                    lease.workspace_id,
                    lease.org_id,
                    binding["cluster_id"],
                )
                if membership is not None:
                    if (
                        membership["generation"] != binding["membership_generation"]
                        or membership["namespace"] != row["namespace_name"]
                    ):
                        raise OperationRefused("network workspace membership changed")
                    return
            if row["members"] != 1 or row["owner_workspace_id"] != lease.workspace_id:
                raise OperationRefused(
                    "shared network placement requires active membership"
                )
            registered = await c.fetchrow(
                """SELECT r.identity_json,r.attempt_token,a.claim,a.progress_json,a.revoked
                FROM workspace_bootstrap_reservations r
                JOIN workspace_bootstrap_authority a USING(workspace_id)
                WHERE r.workspace_id=$1 AND r.state='registered' AND a.org_id=$2
                AND a.cluster_arn=$3 AND a.generation=$4""",
                lease.workspace_id,
                lease.org_id,
                self.plan.data["cluster_arn"],
                binding["membership_generation"],
            )
        if registered is None:
            raise OperationRefused("network membership lacks completed registration")
        identity = json.loads(registered["identity_json"])
        progress = json.loads(registered["progress_json"])
        if (
            registered["claim"]
            != hashlib.sha256(
                b"superplane-workspace-bootstrap-claim:v1:"
                + registered["attempt_token"].encode()
            ).hexdigest()
            or registered["revoked"] is not True
            or progress.get("complete") is not True
            or progress.get("retain_workspace") is not True
            or any(
                identity.get(key) != value
                for key, value in {
                    "org_id": lease.org_id,
                    "workspace_id": lease.workspace_id,
                    "cluster_arn": row["eks_cluster_arn"],
                    "namespace": row["namespace_name"],
                }.items()
            )
        ):
            raise OperationRefused("network registration generation changed")

    async def sdk(self, region, service, method, **arguments):
        await self.authority()
        result = await asyncio.to_thread(
            getattr(self.session.client(service, region_name=region), method),
            **arguments,
        )
        await self.authority()
        return result

    def key(self, region, kind, identity):
        digest = hashlib.sha256(canonical(identity).encode()).hexdigest()
        return f"aws/{self.account}/{region}/{kind}/{digest}"

    async def listed(self, region, method, response_key, **arguments):
        result = await self.sdk(region, "ec2", method, **arguments)
        if (
            result.get("NextToken")
            or result.get("AdditionalRoutesAvailable")
            or response_key not in result
            or not isinstance(result[response_key], list)
        ):
            raise OperationRefused("network provider inventory is incomplete")
        return result[response_key]

    @staticmethod
    def one(items):
        if len(items) > 1:
            raise OperationRefused("network native identity is ambiguous")
        return items[0] if items else None

    async def prerequisites(self, region):
        remote = self.plan.network["regions"][region]
        for location, side in (
            (self.plan.cluster_region, self.home),
            (region, remote["network"]),
        ):
            vpc = self.one(
                await self.listed(
                    location, "describe_vpcs", "Vpcs", VpcIds=[side["vpc_id"]]
                )
            )
            if (
                vpc is None
                or vpc.get("OwnerId") != self.account
                or vpc.get("CidrBlock") != side["vpc_cidr"]
                or vpc.get("State") != "available"
            ):
                raise OperationRefused("approved network VPC differs from AWS")
            groups = await self.listed(
                location,
                "describe_security_groups",
                "SecurityGroups",
                GroupIds=[side["security_group_id"]],
            )
            if (
                len(groups) != 1
                or groups[0].get("OwnerId") != self.account
                or groups[0].get("VpcId") != side["vpc_id"]
            ):
                raise OperationRefused(
                    "approved network security group differs from AWS"
                )
            subnets = await self.listed(
                location, "describe_subnets", "Subnets", SubnetIds=side["subnet_ids"]
            )
            if {s.get("SubnetId") for s in subnets} != set(side["subnet_ids"]) or any(
                s.get("VpcId") != side["vpc_id"]
                or s.get("OwnerId") != self.account
                or s.get("State") != "available"
                for s in subnets
            ):
                raise OperationRefused("approved network subnets differ from AWS")
            tables = await self.listed(
                location,
                "describe_route_tables",
                "RouteTables",
                RouteTableIds=side["vpc_route_table_ids"],
            )
            if {t.get("RouteTableId") for t in tables} != set(
                side["vpc_route_table_ids"]
            ) or any(
                t.get("VpcId") != side["vpc_id"] or t.get("OwnerId") != self.account
                for t in tables
            ):
                raise OperationRefused("approved VPC route table differs from AWS")
            gateways = await self.listed(
                location,
                "describe_transit_gateways",
                "TransitGateways",
                TransitGatewayIds=[side["transit_gateway_id"]],
            )
            table = self.one(
                await self.listed(
                    location,
                    "describe_transit_gateway_route_tables",
                    "TransitGatewayRouteTables",
                    TransitGatewayRouteTableIds=[
                        side["transit_gateway_route_table_id"]
                    ],
                )
            )
            if (
                len(gateways) != 1
                or gateways[0].get("OwnerId") != self.account
                or gateways[0].get("State") != "available"
                or table is None
                or table.get("TransitGatewayId") != side["transit_gateway_id"]
                or table.get("State") != "available"
            ):
                raise OperationRefused(
                    "approved Transit Gateway or table differs from AWS"
                )
        cluster = (
            await self.sdk(
                self.plan.cluster_region,
                "eks",
                "describe_cluster",
                name=self.plan.data["cluster_arn"].split("/")[-1],
            )
        )["cluster"]
        vpc = cluster["resourcesVpcConfig"]
        if (
            cluster["arn"] != self.plan.data["cluster_arn"]
            or cluster["endpoint"] != self.plan.data["endpoint"]
            or cluster["certificateAuthority"]["data"]
            != self.plan.data["certificate_authority"]
            or vpc["vpcId"] != self.home["vpc_id"]
            or not vpc.get("endpointPrivateAccess")
            or self.home["security_group_id"]
            not in {vpc.get("clusterSecurityGroupId"), *vpc.get("securityGroupIds", [])}
        ):
            raise OperationRefused("private EKS network differs from approved cluster")
        await self.dns(region, remote["dns"])

    async def dns(self, region, config):
        """Verify a retained Resolver path; never assume access to EKS's managed zone."""
        rule = (
            await self.sdk(
                region,
                "route53resolver",
                "get_resolver_rule",
                ResolverRuleId=config["rule_id"],
            )
        )["ResolverRule"]
        association = (
            await self.sdk(
                region,
                "route53resolver",
                "get_resolver_rule_association",
                ResolverRuleAssociationId=config["association_id"],
            )
        )["ResolverRuleAssociation"]
        if (
            rule.get("OwnerId") != self.account
            or rule.get("RuleType") != "FORWARD"
            or rule.get("Status") != "COMPLETE"
            or rule.get("DomainName", "").rstrip(".")
            != urlsplit(self.plan.data["endpoint"]).hostname
            or rule.get("ResolverEndpointId") != config["outbound_endpoint_id"]
            or association.get("ResolverRuleId") != config["rule_id"]
            or association.get("VPCId")
            != self.plan.network["regions"][region]["network"]["vpc_id"]
            or association.get("Status") != "COMPLETE"
        ):
            raise OperationRefused("approved EKS Resolver forwarding rule differs")
        for location, key, direction, vpc in (
            (
                region,
                "outbound_endpoint_id",
                "OUTBOUND",
                self.plan.network["regions"][region]["network"]["vpc_id"],
            ),
            (
                self.plan.cluster_region,
                "inbound_endpoint_id",
                "INBOUND",
                self.home["vpc_id"],
            ),
        ):
            endpoint = (
                await self.sdk(
                    location,
                    "route53resolver",
                    "get_resolver_endpoint",
                    ResolverEndpointId=config[key],
                )
            )["ResolverEndpoint"]
            if (
                endpoint.get("Direction") != direction
                or endpoint.get("HostVPCId") != vpc
                or endpoint.get("Status") != "OPERATIONAL"
            ):
                raise OperationRefused("approved Resolver endpoint differs")
        addresses = await self.sdk(
            self.plan.cluster_region,
            "route53resolver",
            "list_resolver_endpoint_ip_addresses",
            ResolverEndpointId=config["inbound_endpoint_id"],
        )
        ips = {
            item["Ip"]
            for item in addresses.get("IpAddresses", [])
            if item.get("Status") == "ATTACHED"
        }
        targets = rule.get("TargetIps", [])
        if (
            addresses.get("NextToken")
            or not targets
            or any(
                item.get("Ip") not in ips or item.get("Port", 53) != 53
                for item in targets
            )
        ):
            raise OperationRefused(
                "Resolver forwarding targets are not the approved inbound endpoint"
            )

    async def resource(self, key, descriptor, adopted, observe, create, delete):
        if getattr(self, "cleaning", False):
            import json

            async with self.provider.domain_pool.acquire() as c:
                row = await c.fetchrow(
                    "SELECT provider_reference,state FROM controller_network_resources WHERE resource_key=$1",
                    key,
                )
            if row is None:
                return None
            if row["provider_reference"] is None:
                raise OperationRefused(
                    "network creation is unresolved; retain cleanup exposure"
                )
            result = json.loads(row["provider_reference"])
        else:
            result = await self.journal.ensure(
                key, descriptor, adopted=adopted, observe=observe, create=create
            )
        self.recipes.append((key, observe, delete))
        return result

    async def attachment(self, region, side):
        identity = {k: side[k] for k in ("vpc_id", "subnet_ids", "transit_gateway_id")}
        key = self.key(region, "attachment", identity)

        async def observe(expected):
            items = await self.listed(
                region,
                "describe_transit_gateway_vpc_attachments",
                "TransitGatewayVpcAttachments",
                Filters=[
                    {"Name": "vpc-id", "Values": [side["vpc_id"]]},
                    {
                        "Name": "transit-gateway-id",
                        "Values": [side["transit_gateway_id"]],
                    },
                    {
                        "Name": "state",
                        "Values": ["pending", "available", "modifying", "deleting"],
                    },
                ],
            )
            row = self.one(items)
            if row is None:
                return None
            if (
                set(row.get("SubnetIds", [])) != set(side["subnet_ids"])
                or row.get("VpcOwnerId") != self.account
                or (
                    side["attachment_id"]
                    and row["TransitGatewayAttachmentId"] != side["attachment_id"]
                )
            ):
                raise OperationRefused(
                    "VPC attachment does not match reviewed identity"
                )
            if (
                isinstance(expected, str)
                and {t["Key"]: t["Value"] for t in row.get("Tags", [])}.get(
                    "SuperplaneNetworkToken"
                )
                != expected
            ):
                raise OperationRefused("attachment has no original creation provenance")
            result = {
                "id": row["TransitGatewayAttachmentId"],
                "region": region,
                "kind": "attachment",
            }
            if isinstance(expected, dict) and expected != result:
                raise OperationRefused("attachment replaced")
            return result

        async def create(token):
            await self.sdk(
                region,
                "ec2",
                "create_transit_gateway_vpc_attachment",
                TransitGatewayId=side["transit_gateway_id"],
                VpcId=side["vpc_id"],
                SubnetIds=side["subnet_ids"],
                Options={
                    "DnsSupport": "enable",
                    "Ipv6Support": "disable",
                    "ApplianceModeSupport": "disable",
                },
                TagSpecifications=[
                    {
                        "ResourceType": "transit-gateway-attachment",
                        "Tags": [{"Key": "SuperplaneNetworkToken", "Value": token}],
                    }
                ],
            )

        async def delete(ref):
            await self.sdk(
                region,
                "ec2",
                "delete_transit_gateway_vpc_attachment",
                TransitGatewayAttachmentId=ref["id"],
            )

        result = await self.resource(
            key,
            identity,
            bool(side["attachment_id"]),
            observe,
            create,
            delete,
        )
        if result is None:
            return None
        if not getattr(self, "cleaning", False):
            await self.wait_attachment(region, result["id"], False)
        await self.association(region, side, result["id"])
        return result

    async def wait_attachment(self, region, attachment, peering):
        method, response = (
            (
                "describe_transit_gateway_peering_attachments",
                "TransitGatewayPeeringAttachments",
            )
            if peering
            else (
                "describe_transit_gateway_vpc_attachments",
                "TransitGatewayVpcAttachments",
            )
        )
        for _ in range(60):
            row = self.one(
                await self.listed(
                    region, method, response, TransitGatewayAttachmentIds=[attachment]
                )
            )
            if row and row.get("State") == "available":
                return
            if row is None or row.get("State") not in {
                "initiating",
                "initiatingRequest",
                "pending",
                "pendingAcceptance",
                "modifying",
            }:
                raise OperationRefused("network attachment unavailable")
            await asyncio.sleep(2)
        raise OperationRefused(
            "network attachment is pending; retain its original handle"
        )

    async def association(self, region, side, attachment):
        table = side["transit_gateway_route_table_id"]
        key = self.key(region, "association", {"attachment": attachment})

        async def observe(expected):
            rows = await self.listed(
                region,
                "get_transit_gateway_route_table_associations",
                "Associations",
                TransitGatewayRouteTableId=table,
                Filters=[
                    {"Name": "transit-gateway-attachment-id", "Values": [attachment]}
                ],
            )
            row = self.one(rows)
            if row is None:
                return None
            if row.get("State") != "associated":
                raise NetworkPending("TGW association has not converged")
            return {
                "id": attachment,
                "table": table,
                "region": region,
                "kind": "association",
            }

        async def create(token):
            await self.sdk(
                region,
                "ec2",
                "associate_transit_gateway_route_table",
                TransitGatewayRouteTableId=table,
                TransitGatewayAttachmentId=attachment,
            )

        async def delete(ref):
            await self.sdk(
                region,
                "ec2",
                "disassociate_transit_gateway_route_table",
                TransitGatewayRouteTableId=table,
                TransitGatewayAttachmentId=attachment,
            )

        return await self.resource(
            key,
            {"table": table, "attachment": attachment},
            None,
            observe,
            create,
            delete,
        )

    async def peering(self, region, remote):
        hub = self.home["transit_gateway_id"]
        spoke = remote["network"]["transit_gateway_id"]
        identity = {"requester": hub, "accepter": spoke, "accepter_region": region}
        key = self.key(self.plan.cluster_region, "peering", identity)

        async def rows():
            values = await self.listed(
                self.plan.cluster_region,
                "describe_transit_gateway_peering_attachments",
                "TransitGatewayPeeringAttachments",
                Filters=[
                    {"Name": "requester-tgw-info.transit-gateway-id", "Values": [hub]},
                    {"Name": "accepter-tgw-info.transit-gateway-id", "Values": [spoke]},
                    {
                        "Name": "state",
                        "Values": [
                            "initiatingRequest",
                            "pendingAcceptance",
                            "pending",
                            "available",
                            "modifying",
                            "deleting",
                        ],
                    },
                ],
            )
            return self.one(values)

        async def observe(expected):
            row = await rows()
            if row is None:
                return None
            if (
                row.get("RequesterTgwInfo", {}).get("OwnerId") != self.account
                or row.get("AccepterTgwInfo", {}).get("OwnerId") != self.account
                or row["AccepterTgwInfo"].get("Region") != region
                or (
                    remote["peering_id"]
                    and row["TransitGatewayAttachmentId"] != remote["peering_id"]
                )
            ):
                raise OperationRefused("peering native account/region differs")
            if (
                isinstance(expected, str)
                and {t["Key"]: t["Value"] for t in row.get("Tags", [])}.get(
                    "SuperplaneNetworkToken"
                )
                != expected
            ):
                raise OperationRefused(
                    "peering original creation provenance unavailable"
                )
            result = {
                "id": row["TransitGatewayAttachmentId"],
                "region": self.plan.cluster_region,
                "kind": "peering",
            }
            if isinstance(expected, dict) and expected != result:
                raise OperationRefused("peering replaced")
            return result

        async def create(token):
            await self.sdk(
                self.plan.cluster_region,
                "ec2",
                "create_transit_gateway_peering_attachment",
                TransitGatewayId=hub,
                PeerTransitGatewayId=spoke,
                PeerAccountId=self.account,
                PeerRegion=region,
                TagSpecifications=[
                    {
                        "ResourceType": "transit-gateway-attachment",
                        "Tags": [{"Key": "SuperplaneNetworkToken", "Value": token}],
                    }
                ],
            )

        async def delete(ref):
            await self.sdk(
                self.plan.cluster_region,
                "ec2",
                "delete_transit_gateway_peering_attachment",
                TransitGatewayAttachmentId=ref["id"],
            )

        result = await self.resource(
            key,
            identity,
            bool(remote["peering_id"]),
            observe,
            create,
            delete,
        )
        if getattr(self, "cleaning", False):
            if result is not None:
                await self.association(
                    self.plan.cluster_region, self.home, result["id"]
                )
                await self.association(region, remote["network"], result["id"])
            return result
        for _ in range(60):
            row = await rows()
            if row and row.get("State") == "pendingAcceptance":

                async def accept():
                    await self.sdk(
                        region,
                        "ec2",
                        "accept_transit_gateway_peering_attachment",
                        TransitGatewayAttachmentId=result["id"],
                    )

                async def accepted():
                    current = await rows()
                    return (
                        {"id": result["id"], "accepted": True}
                        if current and current.get("State") in {"pending", "available"}
                        else None
                    )

                await self.journal.change(
                    key,
                    "accept",
                    {"id": result["id"], "region": region},
                    accept,
                    accepted,
                )
                break
            if row and row.get("State") == "available":
                break
            await asyncio.sleep(2)
        await self.wait_attachment(self.plan.cluster_region, result["id"], True)
        async with self.provider.domain_pool.acquire() as c:
            pending_accept = await c.fetchval(
                "SELECT EXISTS(SELECT 1 FROM controller_network_effects e JOIN controller_network_resources r USING(resource_key,generation) WHERE e.resource_key=$1 AND e.action='accept' AND e.confirmed_at IS NULL)",
                key,
            )
        if pending_accept:

            async def no_replay():
                raise OperationRefused("existing acceptance intent cannot be replayed")

            async def observed_acceptance():
                current = await rows()
                return (
                    {"id": result["id"], "accepted": True}
                    if current and current.get("State") == "available"
                    else None
                )

            await self.journal.change(
                key,
                "accept",
                {"id": result["id"], "region": region},
                no_replay,
                observed_acceptance,
            )
        # Each side's incoming peering uses the explicitly approved TGW table.
        await self.association(self.plan.cluster_region, self.home, result["id"])
        await self.association(region, remote["network"], result["id"])
        return result

    async def route(self, region, *, table, cidr, destination, tgw):
        identity = {
            "table": table,
            "cidr": cidr,
            "destination": destination,
            "tgw": tgw,
        }
        # Key excludes destination so a conflicting route cannot be owned twice.
        key = self.key(region, "route", {"table": table, "cidr": cidr})

        async def observe(expected):
            if tgw:
                rows = await self.listed(
                    region,
                    "search_transit_gateway_routes",
                    "Routes",
                    TransitGatewayRouteTableId=table,
                    Filters=[{"Name": "route-search.exact-match", "Values": [cidr]}],
                )
            else:
                tables = await self.listed(
                    region,
                    "describe_route_tables",
                    "RouteTables",
                    RouteTableIds=[table],
                )
                if len(tables) != 1:
                    raise OperationRefused("VPC route table unavailable")
                rows = [
                    r
                    for r in tables[0].get("Routes", [])
                    if r.get("DestinationCidrBlock") == cidr
                ]
            row = self.one(rows)
            if row is None:
                return None
            target = (
                (
                    [
                        item.get("TransitGatewayAttachmentId")
                        for item in row.get("TransitGatewayAttachments", [])
                    ]
                )
                if tgw
                else [row.get("TransitGatewayId")]
            )
            if (
                row.get("State") != "active"
                or target != [destination]
                or (tgw and row.get("Type") != "static")
            ):
                raise OperationRefused("network route target differs or is not active")
            return {"region": region, "kind": "route", **identity}

        async def create(token):
            if tgw:
                await self.sdk(
                    region,
                    "ec2",
                    "create_transit_gateway_route",
                    TransitGatewayRouteTableId=table,
                    DestinationCidrBlock=cidr,
                    TransitGatewayAttachmentId=destination,
                )
            else:
                await self.sdk(
                    region,
                    "ec2",
                    "create_route",
                    RouteTableId=table,
                    DestinationCidrBlock=cidr,
                    TransitGatewayId=destination,
                )

        async def delete(ref):
            if tgw:
                await self.sdk(
                    region,
                    "ec2",
                    "delete_transit_gateway_route",
                    TransitGatewayRouteTableId=table,
                    DestinationCidrBlock=cidr,
                )
            else:
                await self.sdk(
                    region,
                    "ec2",
                    "delete_route",
                    RouteTableId=table,
                    DestinationCidrBlock=cidr,
                )

        return await self.resource(key, identity, None, observe, create, delete)

    async def security(self, region, group, cidr, protocol, port, egress=False):
        identity = {
            "group": group,
            "cidr": cidr,
            "protocol": protocol,
            "port": port,
            "egress": egress,
        }
        key = self.key(region, "security-rule", identity)

        async def observe(expected):
            rows = await self.listed(
                region,
                "describe_security_group_rules",
                "SecurityGroupRules",
                Filters=[{"Name": "group-id", "Values": [group]}],
            )
            rows = [
                r
                for r in rows
                if r.get("IsEgress") == egress
                and r.get("CidrIpv4") == cidr
                and r.get("IpProtocol") == protocol
                and (
                    protocol == "-1"
                    or (r.get("FromPort") == port and r.get("ToPort") == port)
                )
            ]
            row = self.one(rows)
            if row is None:
                return None
            if row.get("GroupOwnerId") != self.account:
                raise OperationRefused("security rule owner differs")
            result = {
                "id": row["SecurityGroupRuleId"],
                "region": region,
                "kind": "security-rule",
                "group": group,
                "egress": egress,
            }
            if isinstance(expected, dict) and expected != result:
                raise OperationRefused("security rule replaced")
            return result

        async def create(token):
            permission = {"IpProtocol": protocol, "IpRanges": [{"CidrIp": cidr}]}
            if protocol != "-1":
                permission.update(FromPort=port, ToPort=port)
            await self.sdk(
                region,
                "ec2",
                "authorize_security_group_egress"
                if egress
                else "authorize_security_group_ingress",
                GroupId=group,
                IpPermissions=[permission],
                TagSpecifications=[
                    {
                        "ResourceType": "security-group-rule",
                        "Tags": [{"Key": "SuperplaneNetworkToken", "Value": token}],
                    }
                ],
            )

        async def delete(ref):
            await self.sdk(
                region,
                "ec2",
                "revoke_security_group_egress"
                if egress
                else "revoke_security_group_ingress",
                GroupId=group,
                SecurityGroupRuleIds=[ref["id"]],
            )

        return await self.resource(key, identity, None, observe, create, delete)

    async def compose(self, region):
        remote = self.plan.network["regions"][region]
        hub = await self.attachment(self.plan.cluster_region, self.home)
        spoke = await self.attachment(region, remote["network"])
        peer = await self.peering(region, remote)
        if hub is None or spoke is None or peer is None:
            return
        for location, side, other, local in (
            (self.plan.cluster_region, self.home, remote["network"], hub),
            (region, remote["network"], self.home, spoke),
        ):
            for cidr in sorted({side["vpc_cidr"], side["pod_cidr"]}):
                await self.route(
                    location,
                    table=side["transit_gateway_route_table_id"],
                    cidr=cidr,
                    destination=local["id"],
                    tgw=True,
                )
            for cidr in sorted({other["vpc_cidr"], other["pod_cidr"]}):
                await self.route(
                    location,
                    table=side["transit_gateway_route_table_id"],
                    cidr=cidr,
                    destination=peer["id"],
                    tgw=True,
                )
                for table in side["vpc_route_table_ids"]:
                    await self.route(
                        location,
                        table=table,
                        cidr=cidr,
                        destination=side["transit_gateway_id"],
                        tgw=False,
                    )
            # Routed pod traffic uses approved CIDRs. Tenant NetworkPolicy remains
            # mandatory; these infrastructure rules do not grant workspace access.
            for cidr in sorted({other["pod_cidr"], other["node_cidr"]}):
                await self.security(
                    location, side["security_group_id"], cidr, "-1", None
                )
                await self.security(
                    location, side["security_group_id"], cidr, "-1", None, True
                )
        await self.security(
            self.plan.cluster_region,
            self.home["security_group_id"],
            remote["network"]["node_cidr"],
            "tcp",
            443,
        )
        await self.security(
            region,
            remote["network"]["security_group_id"],
            self.home["vpc_cidr"],
            "tcp",
            10250,
        )

    async def establish(self, region):
        if region == self.plan.cluster_region:
            await self.authority()
            async with self.provider.domain_pool.acquire() as c:
                await c.execute(
                    "INSERT INTO controller_network_completion(operation_id,allocation_id,plan_digest,compute_region) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                    self.operation.grant.lease.operation_id,
                    self.operation.request.parameters["allocation_id"],
                    self.operation.plan_digest,
                    region,
                )
            return
        if region not in self.plan.network["regions"]:
            raise OperationRefused("network compute region is not approved")
        await self.prerequisites(region)
        for _ in range(60):
            try:
                await self.compose(region)
                await self.authority()
                async with self.provider.domain_pool.acquire() as c:
                    await c.execute(
                        "INSERT INTO controller_network_completion(operation_id,allocation_id,plan_digest,compute_region) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                        self.operation.grant.lease.operation_id,
                        self.operation.request.parameters["allocation_id"],
                        self.operation.plan_digest,
                        region,
                    )
                return
            except NetworkPending:
                await asyncio.sleep(2)
        raise OperationRefused(
            "network setup remains pending under its original journal"
        )

    async def cleanup(self):
        # Reconstruct the original finite recipes using journalled parent IDs;
        # this path never creates or adopts resources while building callbacks.
        self.cleaning = True
        for region in self.plan.network["regions"]:
            await self.compose(region)
        for key, observe, delete in reversed(self.recipes):
            for _ in range(60):
                try:
                    await self.journal.release(key, observe=observe, delete=delete)
                    break
                except NetworkPending:
                    await asyncio.sleep(2)
            else:
                raise OperationRefused(
                    "network cleanup remains pending; retain cost exposure"
                )

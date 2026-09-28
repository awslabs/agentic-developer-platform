"""Fresh provider observations for durable allocation-scoped network dependencies."""

import asyncio
import json

from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    ResourceObservation,
    ResourcePresence,
)


async def rows(provider, operation):
    lease = operation.grant.lease
    async with provider.domain_pool.acquire() as c:
        result = await c.fetch(
            """SELECT r.*,m.allocation_id,m.workspace_id,m.cluster_id,
          m.membership_generation,m.source_operation_id,m.released_at
          FROM controller_network_resources r JOIN controller_network_members m USING(resource_key)
          WHERE r.org_id=$1 AND m.org_id=$1 AND m.workspace_id=$2 AND m.allocation_id=$3
          ORDER BY r.resource_key LIMIT 501""",
            lease.org_id,
            lease.workspace_id,
            operation.request.parameters["allocation_id"],
        )
    if len(result) > 500:
        raise OperationRefused("network inventory is incomplete")
    source = operation.request.parameters.get(
        "controller_source_operation_id", lease.operation_id
    )
    if any(row["source_operation_id"] != source for row in result):
        raise OperationRefused("network inventory source operation differs")
    return result


def reference(row):
    return "network:" + row["allocation_id"] + ":" + row["resource_key"]


async def discover(provider, operation, keys):
    return {
        reference(row): AllocationResource(
            reference(row), "aws", reference(row), "network_dependency", keys
        )
        for row in await rows(provider, operation)
    }


async def observe_native(session, account, regions, row, *, require_ready=False):
    ref = json.loads(row["provider_reference"]) if row["provider_reference"] else None
    if ref is None:
        return ResourcePresence.UNKNOWN
    region = ref["region"]
    if region not in regions or not row["resource_key"].startswith(
        f"aws/{account}/{region}/"
    ):
        raise OperationRefused("network provider scope differs")
    ec2 = session.client("ec2", region_name=region)

    async def listed(method, key, **kwargs):
        data = await asyncio.to_thread(getattr(ec2, method), **kwargs)
        if (
            data.get("NextToken")
            or data.get("AdditionalRoutesAvailable")
            or not isinstance(data.get(key), list)
        ):
            raise OperationRefused("network provider observation incomplete")
        return data[key]

    kind = ref["kind"]
    if kind in {"attachment", "peering"}:
        method, key = (
            ("describe_transit_gateway_vpc_attachments", "TransitGatewayVpcAttachments")
            if kind == "attachment"
            else (
                "describe_transit_gateway_peering_attachments",
                "TransitGatewayPeeringAttachments",
            )
        )
        values = await listed(
            method,
            key,
            Filters=[{"Name": "transit-gateway-attachment-id", "Values": [ref["id"]]}],
        )
        if not values or all(v.get("State") == "deleted" for v in values):
            return ResourcePresence.ABSENT
        if len(values) != 1 or values[0].get("TransitGatewayAttachmentId") != ref["id"]:
            raise OperationRefused("network attachment observation ambiguous")
    elif kind == "association":
        values = await listed(
            "get_transit_gateway_route_table_associations",
            "Associations",
            TransitGatewayRouteTableId=ref["table"],
            Filters=[{"Name": "transit-gateway-attachment-id", "Values": [ref["id"]]}],
        )
        if not values:
            return ResourcePresence.ABSENT
        if len(values) != 1 or values[0].get("TransitGatewayAttachmentId") != ref["id"]:
            raise OperationRefused("network association observation ambiguous")
    elif kind == "security-rule":
        values = await listed(
            "describe_security_group_rules",
            "SecurityGroupRules",
            Filters=[{"Name": "group-id", "Values": [ref["group"]]}],
        )
        values = [
            item for item in values if item.get("SecurityGroupRuleId") == ref["id"]
        ]
        if not values:
            return ResourcePresence.ABSENT
        if len(values) != 1 or values[0].get("GroupOwnerId") != account:
            raise OperationRefused("network rule ownership changed")
    elif kind == "route":
        if ref["tgw"]:
            values = await listed(
                "search_transit_gateway_routes",
                "Routes",
                TransitGatewayRouteTableId=ref["table"],
                Filters=[{"Name": "route-search.exact-match", "Values": [ref["cidr"]]}],
            )
        else:
            tables = await listed(
                "describe_route_tables", "RouteTables", RouteTableIds=[ref["table"]]
            )
            if len(tables) != 1:
                raise OperationRefused("network route table unavailable")
            values = [
                r
                for r in tables[0].get("Routes", [])
                if r.get("DestinationCidrBlock") == ref["cidr"]
            ]
        if not values:
            return ResourcePresence.ABSENT
        if len(values) != 1:
            raise OperationRefused("network route observation ambiguous")
        actual = (
            [
                x.get("TransitGatewayAttachmentId")
                for x in values[0].get("TransitGatewayAttachments", [])
            ]
            if ref["tgw"]
            else [values[0].get("TransitGatewayId")]
        )
        if actual != [ref["destination"]]:
            raise OperationRefused("network route target replaced")
    else:
        raise OperationRefused("unknown network resource kind")
    if require_ready:
        expected_state = {
            "attachment": "available",
            "peering": "available",
            "association": "associated",
            "route": "active",
        }.get(kind)
        if expected_state and values[0].get("State") != expected_state:
            return ResourcePresence.UNKNOWN
        if kind == "security-rule":
            descriptor = json.loads(row["descriptor"])
            rule = values[0]
            if (
                rule.get("IsEgress") != descriptor["egress"]
                or rule.get("IpProtocol") != descriptor["protocol"]
                or rule.get("CidrIpv4") != descriptor["cidr"]
                or (
                    descriptor["protocol"] != "-1"
                    and (
                        rule.get("FromPort") != descriptor["port"]
                        or rule.get("ToPort") != descriptor["port"]
                    )
                )
            ):
                return ResourcePresence.UNKNOWN
    return ResourcePresence.PRESENT


async def observe(provider, operation, plan, resource):
    ref = resource.provider_reference
    try:
        found = [
            row for row in await rows(provider, operation) if reference(row) == ref
        ]
        if len(found) != 1:
            raise OperationRefused("network dependency unavailable")
        row = found[0]
        if row["released_at"] is not None:
            # This allocation's dependency was durably released while peers were
            # retained, or after owned provider absence. This is not a claim that
            # shared infrastructure or its other members stopped billing.
            return ResourceObservation(
                ResourcePresence.ABSENT, ref, "allocation_dependency_released"
            )
        session, _ = await provider.session_for(operation, plan)
        presence = await observe_native(
            session,
            plan.data["provider_account_id"],
            {plan.cluster_region, *plan.network["regions"]},
            row,
        )
        if row["state"] == "intended":
            presence = ResourcePresence.UNKNOWN
        return ResourceObservation(presence, ref, "network_dependency")
    except Exception:  # noqa: BLE001 - incomplete provider inventory must remain unknown
        return ResourceObservation(
            ResourcePresence.UNKNOWN,
            ref,
            detail="network ownership or provider observation incomplete",
        )

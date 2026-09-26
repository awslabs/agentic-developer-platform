"""Pure, bounded reconstruction of approved network removal identities."""

import hashlib
import json

from harness_jobs.identity import OperationRefused

from .network_plan import canonical


def network_recipes(plan, rows):
    """Return reverse dependency order without constructing SDK callbacks."""
    if len(rows) > 500:
        raise OperationRefused("cleanup network inventory exceeds its bound")
    indexed = {row["resource_key"]: row for row in rows}
    if len(indexed) != len(rows):
        raise OperationRefused("duplicate cleanup network identity")
    if plan.network is None:
        if rows:
            raise OperationRefused("network membership has no approved network plan")
        return []
    ordered = {}
    account, home = plan.data["provider_account_id"], plan.network["cluster"]["network"]

    def add(region, kind, identity, *, key_identity=None):
        key = (
            f"aws/{account}/{region}/{kind}/"
            + hashlib.sha256(canonical(key_identity or identity).encode()).hexdigest()
        )
        row = indexed.get(key)
        if row is None:
            raise OperationRefused("approved cleanup recipe lacks original membership")
        if json.loads(row["descriptor"]) != identity or not row["provider_reference"]:
            raise OperationRefused(
                "cleanup network descriptor or native identity differs"
            )
        ref = json.loads(row["provider_reference"])
        if ref.get("region") != region or ref.get("kind") != kind:
            raise OperationRefused("cleanup native provider scope differs")
        if kind == "route" and ref != {"region": region, "kind": kind, **identity}:
            raise OperationRefused("cleanup route identity differs")
        if kind == "association" and ref != {
            "region": region,
            "kind": kind,
            "id": identity["attachment"],
            "table": identity["table"],
        }:
            raise OperationRefused("cleanup association identity differs")
        if kind == "security-rule" and (
            ref.get("group") != identity["group"]
            or ref.get("egress") != identity["egress"]
            or not ref.get("id")
        ):
            raise OperationRefused("cleanup security rule identity differs")
        if kind in {"attachment", "peering"} and not ref.get("id"):
            raise OperationRefused("cleanup parent native identity missing")
        value = {
            "key": key,
            "generation": row["generation"],
            "membership_generation": row["membership_generation"],
            "descriptor": identity,
            "reference": ref,
            "owned": row["owned"],
        }
        if type(value["generation"]) is not int or value["generation"] < 1:
            raise OperationRefused("cleanup network generation invalid")
        if key in ordered and ordered[key] != value:
            raise OperationRefused("conflicting approved cleanup recipe")
        ordered[key] = value
        return ref

    def association(region, side, attachment):
        add(
            region,
            "association",
            {
                "table": side["transit_gateway_route_table_id"],
                "attachment": attachment,
            },
            key_identity={"attachment": attachment},
        )

    def attachment(region, side):
        ref = add(
            region,
            "attachment",
            {key: side[key] for key in ("vpc_id", "subnet_ids", "transit_gateway_id")},
        )
        if side["attachment_id"] and side["attachment_id"] != ref["id"]:
            raise OperationRefused("cleanup attachment differs from approved native ID")
        association(region, side, ref["id"])
        return ref

    def route(region, table, cidr, destination, tgw):
        add(
            region,
            "route",
            {
                "table": table,
                "cidr": cidr,
                "destination": destination,
                "tgw": tgw,
            },
            key_identity={"table": table, "cidr": cidr},
        )

    def security(region, side, cidr, protocol, port, egress=False):
        add(
            region,
            "security-rule",
            {
                "group": side["security_group_id"],
                "cidr": cidr,
                "protocol": protocol,
                "port": port,
                "egress": egress,
            },
        )

    # A successful same-region launch has no regional dependencies. Otherwise
    # reconstruct only regions actually represented by original membership.
    active_regions = {
        json.loads(row["provider_reference"])["region"]
        for row in rows
        if row["provider_reference"]
    } - {plan.cluster_region}
    if not active_regions <= set(plan.network["regions"]):
        raise OperationRefused("cleanup network region is not approved")
    for region in sorted(active_regions):
        remote = plan.network["regions"][region]
        side = remote["network"]
        hub, spoke = attachment(plan.cluster_region, home), attachment(region, side)
        peer = add(
            plan.cluster_region,
            "peering",
            {
                "requester": home["transit_gateway_id"],
                "accepter": side["transit_gateway_id"],
                "accepter_region": region,
            },
        )
        if remote["peering_id"] and remote["peering_id"] != peer["id"]:
            raise OperationRefused("cleanup peering differs from approved native ID")
        association(plan.cluster_region, home, peer["id"])
        association(region, side, peer["id"])
        for location, local_side, other, local in (
            (plan.cluster_region, home, side, hub),
            (region, side, home, spoke),
        ):
            for cidr in sorted({local_side["vpc_cidr"], local_side["pod_cidr"]}):
                route(
                    location,
                    local_side["transit_gateway_route_table_id"],
                    cidr,
                    local["id"],
                    True,
                )
            for cidr in sorted({other["vpc_cidr"], other["pod_cidr"]}):
                route(
                    location,
                    local_side["transit_gateway_route_table_id"],
                    cidr,
                    peer["id"],
                    True,
                )
                for table in local_side["vpc_route_table_ids"]:
                    route(
                        location, table, cidr, local_side["transit_gateway_id"], False
                    )
            for cidr in sorted({other["pod_cidr"], other["node_cidr"]}):
                security(location, local_side, cidr, "-1", None)
                security(location, local_side, cidr, "-1", None, True)
        security(plan.cluster_region, home, side["node_cidr"], "tcp", 443)
        security(region, side, home["vpc_cidr"], "tcp", 10250)
    if set(ordered) != set(indexed):
        raise OperationRefused("original network membership is not fully represented")
    return list(reversed(ordered.values()))

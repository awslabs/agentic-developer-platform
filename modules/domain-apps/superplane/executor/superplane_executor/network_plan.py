"""Versioned network policy carried in the same immutable approval as capacity."""

import ipaddress
import json
import re
from urllib.parse import urlsplit

from harness_jobs.identity import MAX_PARAMETER_VALUE_LENGTH, OperationRefused

SIDE_FIELDS = {
    "vpc_id",
    "vpc_cidr",
    "node_cidr",
    "pod_cidr",
    "subnet_ids",
    "transit_gateway_id",
    "attachment_id",
    "transit_gateway_route_table_id",
    "vpc_route_table_ids",
    "security_group_id",
}
PARAMETERS = {"controller_network_cluster", "controller_network_regions"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(
        prefix + r"-[a-f0-9]{8,17}", value
    ):
        raise ValueError("invalid network identity")


def _side(value):
    if not isinstance(value, dict) or set(value) != SIDE_FIELDS:
        raise ValueError("closed network side required")
    for key, prefix in (
        ("vpc_id", "vpc"),
        ("transit_gateway_id", "tgw"),
        ("transit_gateway_route_table_id", "tgw-rtb"),
        ("security_group_id", "sg"),
    ):
        _id(value[key], prefix)
    if value["attachment_id"] is not None:
        _id(value["attachment_id"], "tgw-attach")
    for key, prefix in (("subnet_ids", "subnet"), ("vpc_route_table_ids", "rtb")):
        values = value[key]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 4
            or len(set(values)) != len(values)
        ):
            raise ValueError("bounded unique network identities required")
        for item in values:
            _id(item, prefix)
    ranges = [
        ipaddress.ip_network(value[key], strict=True)
        for key in ("vpc_cidr", "node_cidr", "pod_cidr")
    ]
    if any(item.version != 4 for item in ranges) or not ranges[1].subnet_of(ranges[0]):
        raise ValueError("supported IPv4 node range must belong to VPC")
    return ranges


def read_network(parameters, data):
    """Old approvals remain unchanged; only an explicit v1 network contract opts in."""
    present = PARAMETERS & set(parameters)
    if not present:
        return None
    try:
        if present != PARAMETERS or data["version"] != 4:
            raise ValueError("network contract requires regional plan")
        if any(len(parameters[key]) > MAX_PARAMETER_VALUE_LENGTH for key in PARAMETERS):
            raise ValueError("network approval exceeds parameter bound")
        cluster = json.loads(parameters["controller_network_cluster"])
        regions = json.loads(parameters["controller_network_regions"])
        if (
            set(cluster)
            != {"version", "network", "cluster_id", "membership_generation"}
            or cluster["version"] != 1
        ):
            raise ValueError("unsupported network contract")
        import uuid

        uuid.UUID(cluster["cluster_id"])
        if not isinstance(cluster["membership_generation"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", cluster["membership_generation"]
        ):
            raise ValueError("membership generation required")
        home = _side(cluster["network"])
        home_region = data["cluster_arn"].split(":")[3]
        bindings = {entry["region"]: entry for entry in data["regions"]}
        expected = set(bindings) - {home_region}
        if not expected or set(regions) != expected:
            raise ValueError("network contract must cover every approved remote region")
        for region, value in regions.items():
            if set(value) != {"network", "dns", "peering_id"}:
                raise ValueError("closed regional network contract required")
            remote = _side(value["network"])
            if any(a.overlaps(b) for a in home for b in remote):
                raise ValueError("cluster and remote address ranges overlap")
            if any(
                ipaddress.ip_network(data["service_cidr"]).overlaps(item)
                for item in home + remote
            ):
                raise ValueError("service and routed address ranges overlap")
            net, binding = value["network"], bindings[region]
            if (net["vpc_id"], net["security_group_id"]) != (
                binding["vpc_id"],
                binding["security_group_id"],
            ) or not set(net["subnet_ids"]) <= set(binding["subnet_ids"]):
                raise ValueError("network differs from approved regional capacity")
            if value["peering_id"] is not None:
                _id(value["peering_id"], "tgw-attach")
            dns = value["dns"]
            if not isinstance(dns, dict) or set(dns) != {
                "rule_id",
                "association_id",
                "outbound_endpoint_id",
                "inbound_endpoint_id",
            }:
                raise ValueError("explicit Resolver forwarding path required")
            for key, prefix in (
                ("rule_id", "rslvr-rr"),
                ("association_id", "rslvr-rrassoc"),
                ("outbound_endpoint_id", "rslvr-out"),
                ("inbound_endpoint_id", "rslvr-in"),
            ):
                if not isinstance(dns[key], str) or not re.fullmatch(
                    prefix + r"-[a-z0-9]{8,32}", dns[key]
                ):
                    raise ValueError("invalid Resolver identity")
        if urlsplit(data["endpoint"]).scheme != "https":
            raise ValueError("private TLS endpoint required")
        return {"cluster": cluster, "regions": regions}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise OperationRefused(
            "approved network policy is invalid or unsupported"
        ) from None

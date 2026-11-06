"""Reviewed native-only public HTTPS and exact private transport policy."""

import copy
import ipaddress
import re

from .api_adapters import closed
from .config import require


EXCLUDED = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
]


def enabled(env):
    value = env.get("paid_worker", {}).get("egress", {})
    return isinstance(value, dict) and value.get("mode") == "public-https"


def validate(env):
    require(
        env["paid_worker"]["mode"] == "native-lifecycle",
        "public HTTPS egress is native lifecycle only",
    )
    value = env["paid_worker"]["egress"]
    closed(
        value,
        {"mode", "database"}
        | ({"interface_endpoints"} if "interface_endpoints" in value else set()),
        "native public egress",
    )
    if "interface_endpoints" in value:
        validate_interface_endpoints(value["interface_endpoints"])
    closed(value["database"], {"cidr", "port"}, "native database egress")
    try:
        address = ipaddress.ip_network(value["database"]["cidr"], strict=True)
    except (ValueError, TypeError):
        require(False, "native database egress requires one private IPv4 /32")
    require(
        address.version == 4
        and address.prefixlen == 32
        and any(
            address.subnet_of(ipaddress.ip_network(cidr))
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        and value["database"]["port"] == 5432,
        "native database egress requires one private IPv4 /32 on TCP5432",
    )
    require(
        bool(env.get("api_adapters", {}).get("vault", {}).get("transport")),
        "native egress requires the verified Gateway Service transport",
    )


def validate_interface_endpoints(endpoints):
    require(
        isinstance(endpoints, dict)
        and bool(endpoints)
        and set(endpoints) <= {"sts", "sqs"},
        "Only reviewed STS/SQS interface endpoints are supported",
    )
    for endpoint in endpoints.values():
        closed(
            endpoint,
            {"id", "vpc_id", "network_interface_ids", "ipv4_addresses"},
            "native interface endpoint",
        )
        require(
            isinstance(endpoint["id"], str)
            and re.fullmatch(r"vpce-[0-9a-f]{17}", endpoint["id"]),
            "Invalid interface endpoint identity",
        )
        require(
            isinstance(endpoint["vpc_id"], str)
            and re.fullmatch(r"vpc-[0-9a-f]{17}", endpoint["vpc_id"]),
            "Invalid endpoint VPC identity",
        )
        ids, addresses = endpoint["network_interface_ids"], endpoint["ipv4_addresses"]
        require(
            isinstance(ids, list)
            and 1 <= len(ids) <= 6
            and all(
                isinstance(item, str) and re.fullmatch(r"eni-[0-9a-f]{17}", item)
                for item in ids
            )
            and len(set(ids)) == len(ids),
            "Exact unique interface identities required",
        )
        require(
            isinstance(addresses, list)
            and len(addresses) == len(ids)
            and all(isinstance(item, str) for item in addresses)
            and len(set(addresses)) == len(addresses),
            "Exact unique endpoint IPv4 addresses required",
        )
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                require(False, "Invalid endpoint IPv4 address")
            require(
                address.version == 4
                and any(
                    address in ipaddress.ip_network(cidr)
                    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                ),
                "Interface endpoints require RFC1918 IPv4 addresses",
            )


def dns_rule():
    return {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [{"protocol": protocol, "port": 53} for protocol in ("TCP", "UDP")],
    }


def rules(env, *, database=True):
    validate(env)
    transport = env["api_adapters"]["vault"]["transport"]
    result = [
        {
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(EXCLUDED)}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": transport["namespace"]
                        }
                    },
                    "podSelector": {
                        "matchLabels": copy.deepcopy(transport["selector"])
                    },
                }
            ],
            "ports": [
                {"protocol": "TCP", "port": port}
                for port in sorted({transport["port"], transport["target_port"]})
            ],
        },
    ]
    endpoints = env["paid_worker"]["egress"].get("interface_endpoints", {})
    addresses = sorted(
        {
            address
            for endpoint in endpoints.values()
            for address in endpoint["ipv4_addresses"]
        }
    )
    if addresses:
        result.append(
            {
                "to": [{"ipBlock": {"cidr": address + "/32"}} for address in addresses],
                "ports": [{"protocol": "TCP", "port": 443}],
            }
        )
    if database:
        peer = env["paid_worker"]["egress"]["database"]
        result.append(
            {
                "to": [{"ipBlock": {"cidr": peer["cidr"]}}],
                "ports": [{"protocol": "TCP", "port": peer["port"]}],
            }
        )
    if env.get("recovery_transport"):
        from .config import LABEL, identity

        result.append(
            {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": env["namespace"]
                            }
                        },
                        "podSelector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": "superplane-api",
                                LABEL: identity(env),
                            }
                        },
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 8443}],
            }
        )
    dns = dns_rule()
    from .config import cluster_dns_address

    resolver = cluster_dns_address(env)
    if resolver is not None:
        dns["to"].append({"ipBlock": {"cidr": f"{resolver}/{resolver.max_prefixlen}"}})
    result.append(dns)
    return result

"""Reviewed native-only public HTTPS and exact private transport policy."""

import copy
import ipaddress

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
    closed(value, {"mode", "database"}, "native public egress")
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
    if database:
        peer = env["paid_worker"]["egress"]["database"]
        result.append(
            {
                "to": [{"ipBlock": {"cidr": peer["cidr"]}}],
                "ports": [{"protocol": "TCP", "port": peer["port"]}],
            }
        )
    result.append(dns_rule())
    return result

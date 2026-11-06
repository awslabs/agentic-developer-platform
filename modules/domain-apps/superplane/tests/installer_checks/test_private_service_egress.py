"""Exact private AWS endpoints preserve the default private-network denial."""

import copy
import json
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from installation import native_egress, native_egress_probe
from installation.config import Refusal
from .test_native_public_egress import configure
from .test_paid_worker import native

__all__ = ["native"]


def endpoint():
    return {
        "id": "vpce-" + "a" * 17,
        "vpc_id": "vpc-" + "b" * 17,
        "network_interface_ids": ["eni-" + "c" * 17],
        "ipv4_addresses": ["10.0.1.5"],
    }


def configured(native):
    env, lock = configure(native)
    env["paid_worker"]["egress"]["interface_endpoints"] = {"sts": endpoint()}
    return env, lock


def test_rules_allow_only_exact_endpoint_addresses_on_https(native):
    env, _ = configured(native)
    rules = native_egress.rules(env)
    assert rules[0]["to"][0]["ipBlock"]["except"] == native_egress.EXCLUDED
    assert rules[2] == {
        "to": [{"ipBlock": {"cidr": "10.0.1.5/32"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    }
    assert rules[3]["to"] == [{"ipBlock": {"cidr": "10.0.1.3/32"}}]


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [],
        {"ec2": endpoint()},
        {"sts": dict(endpoint(), ipv4_addresses=["8.8.8.8"])},
        {"sts": dict(endpoint(), ipv4_addresses=["169.254.169.254"])},
        {"sts": dict(endpoint(), ipv4_addresses=["10.0.0.0/8"])},
        {"sts": dict(endpoint(), network_interface_ids=[])},
        {"sts": dict(endpoint(), port=80)},
        {
            "sts": dict(
                endpoint(),
                network_interface_ids=["eni-" + "c" * 17] * 2,
                ipv4_addresses=["10.0.1.5"] * 2,
            )
        },
    ],
)
def test_malformed_or_broad_endpoint_declarations_refused(native, value):
    env, _ = configured(native)
    env["paid_worker"]["egress"]["interface_endpoints"] = value
    with pytest.raises(Refusal):
        native_egress.rules(env)


def observations(env):
    selected = endpoint()
    return {
        "describe-cluster": {
            "cluster": {
                "arn": f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['cluster']}",
                "status": "ACTIVE",
                "resourcesVpcConfig": {"vpcId": selected["vpc_id"]},
            }
        },
        "describe-vpc-endpoints": {
            "VpcEndpoints": [
                {
                    "VpcEndpointId": selected["id"],
                    "VpcId": selected["vpc_id"],
                    "OwnerId": env["account_id"],
                    "State": "available",
                    "VpcEndpointType": "Interface",
                    "PrivateDnsEnabled": True,
                    "ServiceName": f"com.amazonaws.{env['region']}.sts",
                    "NetworkInterfaceIds": selected["network_interface_ids"],
                }
            ]
        },
        "describe-network-interfaces": {
            "NetworkInterfaces": [
                {
                    "NetworkInterfaceId": selected["network_interface_ids"][0],
                    "VpcId": selected["vpc_id"],
                    "OwnerId": env["account_id"],
                    "Status": "in-use",
                    "InterfaceType": "vpc_endpoint",
                    "PrivateIpAddress": "10.0.1.5",
                }
            ]
        },
    }


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "cluster-vpc",
        "cluster-owner",
        "endpoint-owner",
        "endpoint-vpc",
        "service",
        "dns",
        "state",
        "eni-set",
        "eni-owner",
        "eni-vpc",
        "eni-type",
        "eni-state",
        "eni-address",
        "extra-endpoint",
        "extra-eni",
    ],
)
def test_live_identity_is_required_for_private_resolution(native, fault):
    env, _ = configured(native)
    live = observations(env)
    cluster = live["describe-cluster"]["cluster"]
    ep = live["describe-vpc-endpoints"]["VpcEndpoints"][0]
    eni = live["describe-network-interfaces"]["NetworkInterfaces"][0]
    if fault == "cluster-vpc":
        cluster["resourcesVpcConfig"]["vpcId"] = "foreign"
    elif fault == "cluster-owner":
        cluster["arn"] = "foreign"
    elif fault == "endpoint-owner":
        ep["OwnerId"] = "999999999999"
    elif fault == "endpoint-vpc":
        ep["VpcId"] = "foreign"
    elif fault == "service":
        ep["ServiceName"] = f"com.amazonaws.{env['region']}.ec2"
    elif fault == "dns":
        ep["PrivateDnsEnabled"] = False
    elif fault == "state":
        ep["State"] = "pending"
    elif fault == "eni-set":
        ep["NetworkInterfaceIds"] = []
    elif fault == "eni-owner":
        eni["OwnerId"] = "999999999999"
    elif fault == "eni-vpc":
        eni["VpcId"] = "foreign"
    elif fault == "eni-type":
        eni["InterfaceType"] = "interface"
    elif fault == "eni-state":
        eni["Status"] = "available"
    elif fault == "eni-address":
        eni["PrivateIpAddress"] = "10.0.1.6"
    elif fault == "extra-endpoint":
        live["describe-vpc-endpoints"]["VpcEndpoints"].append(copy.deepcopy(ep))
    elif fault == "extra-eni":
        live["describe-network-interfaces"]["NetworkInterfaces"].append(
            copy.deepcopy(eni)
        )
    installer = SimpleNamespace(
        env=env, json=lambda obj: obj, aws=lambda *args: live[args[1]]
    )
    if fault:
        with pytest.raises(Refusal):
            native_egress_probe.verified_private_hosts(installer)
    else:
        assert native_egress_probe.verified_private_hosts(installer) == {
            f"sts.{env['region']}.amazonaws.com": ["10.0.1.5"]
        }


@pytest.mark.parametrize(
    "selected,actual,passes",
    [
        (None, ["8.8.8.8"], True),
        (None, ["10.0.1.5"], False),
        (["10.0.1.5"], ["10.0.1.5"], True),
        (["10.0.1.5"], ["10.0.1.6"], False),
        (["10.0.1.5"], ["10.0.1.5", "10.0.1.6"], False),
        (["10.0.1.5"], ["8.8.8.8"], False),
    ],
)
def test_actual_probe_refuses_unreviewed_dns_and_preserves_hostname_tls(
    monkeypatch, selected, actual, passes
):
    host = "sts.us-east-1.amazonaws.com"
    targets = {
        "https": [host],
        "database_host": "db",
        "database_ip": "10.0.1.3",
        "gateway": ["gateway", 80],
        "recipe_sha256": "recipe",
    }
    if selected is not None:
        targets["private_https"] = {host: selected}
    monkeypatch.setenv("ADP_NATIVE_NETWORK_PROBE", json.dumps(targets))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda name, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))
            for ip in (actual if name == host else ["10.0.1.3"])
        ],
    )
    transport = MagicMock()
    tls = MagicMock()
    monkeypatch.setattr(socket, "create_connection", transport)
    monkeypatch.setattr(ssl, "create_default_context", lambda: tls)
    if passes:
        exec(native_egress_probe.PROGRAM, {})
        assert tls.wrap_socket.call_args.kwargs["server_hostname"] == host
    else:
        with pytest.raises(AssertionError):
            exec(native_egress_probe.PROGRAM, {})
        transport.assert_not_called()

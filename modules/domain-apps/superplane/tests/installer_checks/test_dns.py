"""Native DNS must remain reachable without widening the restricted boundary."""

import json
from types import SimpleNamespace

import pytest

from installation.cluster_probe import ClusterProbe
from installation.config import Refusal, validate, verify_cluster_dns
from installation.manifests import dns_egress, render
from installation.runner import Installer


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "0.0.0.0/0",
        "172.20.0.10/32",
        "example.com",
        "169.254.169.254",
        "127.0.0.1",
        "0.0.0.0",
    ],
)
def test_rejects_dns_ranges_and_non_resolvers(environment, release, value):
    environment["cluster_dns_ip"] = value
    with pytest.raises(Refusal, match="cluster_dns_ip"):
        validate(environment, release)


@pytest.mark.parametrize(
    "enabled,actual,requested,valid",
    [
        (True, "172.20.0.0/16", "172.20.0.10", True),
        (True, "10.100.0.0/16", "10.100.0.10", True),
        (False, "172.20.0.0/16", "172.20.0.10", False),
        (True, "10.100.0.0/16", "172.20.0.10", False),
        (True, "172.20.0.0/16", "8.8.8.8", False),
        (True, "invalid", "172.20.0.10", False),
    ],
)
def test_native_resolver_matches_selected_cluster(enabled, actual, requested, valid):
    cluster = {
        "computeConfig": {"enabled": enabled},
        "kubernetesNetworkConfig": {"ipFamily": "ipv4", "serviceIpv4Cidr": actual},
    }
    if valid:
        verify_cluster_dns({"cluster_dns_ip": requested}, cluster)
    else:
        with pytest.raises(Refusal, match="cluster_dns_ip"):
            verify_cluster_dns({"cluster_dns_ip": requested}, cluster)


def test_legacy_dns_remains_compatible():
    verify_cluster_dns({}, {})
    assert dns_egress({})["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
            }
        }
    ]


@pytest.mark.parametrize(
    "family,cidr,expected",
    [
        ("ipv4", "172.20.0.0/16", "172.20.0.10"),
        ("ipv4", "10.100.0.0/16", "10.100.0.10"),
        ("ipv6", "fd00:1234::/108", "fd00:1234::a"),
    ],
)
def test_native_dns_discovered_without_input(family, cidr, expected):
    environment = {}
    cluster = {
        "computeConfig": {"enabled": True},
        "kubernetesNetworkConfig": {
            "ipFamily": family,
            f"serviceIpv{4 if family == 'ipv4' else 6}Cidr": cidr,
        },
    }
    assert verify_cluster_dns(environment, cluster) == expected
    assert environment == {}


@pytest.mark.parametrize(
    "network",
    [
        {},
        None,
        {"ipFamily": "unknown"},
        {"ipFamily": "ipv4"},
        {"ipFamily": "ipv4", "serviceIpv4Cidr": "bad"},
        {"ipFamily": "ipv4", "serviceIpv4Cidr": None},
        {"ipFamily": "ipv4", "serviceIpv4Cidr": "172.20.0.0/32"},
        {"ipFamily": "ipv4", "serviceIpv4Cidr": "fd00::/108"},
    ],
)
def test_auto_mode_never_silently_falls_back_when_discovery_fails(network):
    with pytest.raises(Refusal, match="discover cluster_dns_ip"):
        verify_cluster_dns(
            {}, {"computeConfig": {"enabled": True}, "kubernetesNetworkConfig": network}
        )


@pytest.mark.parametrize("management_only", [False, True])
def test_native_dns_only_adds_resolver_on_dns_ports(
    environment, release, management_only
):
    original = render(environment, release, control_plane_only=management_only)
    environment["cluster_dns_ip"] = "172.20.0.10"
    validate(environment, release)
    changed = render(environment, release, control_plane_only=management_only)
    updates = 0
    for before, after in zip(original, changed, strict=True):
        if before == after:
            continue
        assert after["kind"] == "NetworkPolicy"
        rule = after["spec"]["egress"][0]
        assert rule["ports"] == [
            {"port": 53, "protocol": "UDP"},
            {"port": 53, "protocol": "TCP"},
        ]
        assert rule["to"].pop() == {"ipBlock": {"cidr": "172.20.0.10/32"}}
        assert before == after  # No other network, pod, identity or image changes.
        updates += 1
    assert updates == 4


def test_database_probe_shares_runtime_dns_rule(
    tmp_path, environment, release, monkeypatch
):
    environment["cluster_dns_ip"] = "172.20.0.10"
    installer = Installer(environment, release, tmp_path)
    probe = ClusterProbe(installer)
    policies = []
    monkeypatch.setattr(probe, "policy", lambda name, spec: policies.append(spec))
    probe.isolate()
    assert policies[-1]["egress"] == []  # Offline image checks retain deny-all.
    probe.isolate(database_cidrs=["10.0.1.2/32"])
    assert policies[-1]["egress"] == [
        dns_egress(environment),
        {
            "to": [{"ipBlock": {"cidr": "10.0.1.2/32"}}],
            "ports": [{"protocol": "TCP", "port": 5432}],
        },
    ]


@pytest.mark.parametrize("exit_code,verified", [(1, True), (137, True), (0, False)])
def test_dns_process_failure_never_counts_as_ready(
    tmp_path, environment, release, monkeypatch, exit_code, verified
):
    installer = Installer(environment, release, tmp_path)
    probe = ClusterProbe(installer)
    monkeypatch.setattr(
        probe,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=exit_code,
            stdout=json.dumps(
                {
                    "verified": verified,
                    "names": ["kubernetes.default.svc.cluster.local", "db.test"],
                    "protocols": ["UDP", "TCP"],
                }
            ),
        ),
    )
    with pytest.raises(Refusal, match="DNS"):
        probe.prove_dns("db.test")
    assert "management_dns" not in installer.receipt

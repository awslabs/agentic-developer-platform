"""Public lifecycle policy remains mode-specific and private destinations exact."""

import ipaddress
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from installation import lifecycle_worker, native_egress, paid_worker
from installation.config import LABEL, Refusal
from .test_paid_worker import lifecycle_config, native

__all__ = ["native"]


def configure(native):
    env, lock = native
    lifecycle_config(env)
    env["paid_worker"]["egress"] = {
        "mode": "public-https",
        "database": {"cidr": "10.0.1.3/32", "port": 5432},
    }
    return env, lock


def test_exact_public_policy_is_saved_while_worker_stays_paused(native):
    env, lock = configure(native)
    paid_worker.validate(env, lock)
    docs = [{"metadata": {"labels": {LABEL: "owned"}}}]
    paid_worker.project(env, lock, docs)
    policy = next(doc for doc in docs[1:] if doc["kind"] == "NetworkPolicy")
    assert policy["spec"]["egress"] == []
    approved = json.loads(
        policy["metadata"]["annotations"]["adp.aws-e.io/activation-egress"]
    )
    installer = SimpleNamespace(env=env, lock=lock, docs=docs[1:])
    active = next(
        doc
        for doc in lifecycle_worker.worker_documents(installer, active=True)
        if doc["kind"] == "NetworkPolicy"
    )
    assert active["spec"]["egress"] == approved == native_egress.rules(env)
    internet = approved[0]
    assert internet["ports"] == [{"protocol": "TCP", "port": 443}]
    excludes = [
        ipaddress.ip_network(value) for value in internet["to"][0]["ipBlock"]["except"]
    ]
    for forbidden in (
        "10.2.3.4",
        "172.16.2.3",
        "192.168.1.1",
        "169.254.169.254",
        "127.0.0.1",
        "100.100.100.100",
    ):
        assert any(ipaddress.ip_address(forbidden) in block for block in excludes)
    assert approved[1]["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "gateway"}
            },
            "podSelector": {"matchLabels": {"app": "gateway"}},
        }
    ]
    assert approved[2]["to"] == [{"ipBlock": {"cidr": "10.0.1.3/32"}}]


@pytest.mark.parametrize(
    "fault", ["controller", "public-db", "wide-db", "imds", "wrong-port", "extra-peer"]
)
def test_public_egress_cannot_expand_mode_or_private_peers(native, fault):
    env, _ = configure(native)
    if fault == "controller":
        env["paid_worker"]["mode"] = "native-controller"
    elif fault == "wrong-port":
        env["paid_worker"]["egress"]["database"]["port"] = 443
    elif fault == "extra-peer":
        env["paid_worker"]["egress"]["workspace"] = {"cidr": "10.0.0.0/8", "port": 443}
    else:
        env["paid_worker"]["egress"]["database"]["cidr"] = {
            "public-db": "8.8.8.8/32",
            "wide-db": "10.0.0.0/8",
            "imds": "169.254.169.254/32",
        }[fault]
    with pytest.raises(Refusal):
        native_egress.validate(env)


def test_failed_live_network_probe_prevents_activation(native, monkeypatch):
    env, lock = configure(native)
    installer = SimpleNamespace(
        env=env,
        lock=lock,
        receipt={
            "adapter_stage": {
                "native_worker": {
                    "snapshot": "exact",
                    "proof": {"binding_sha256": "bound"},
                }
            }
        },
        apply=Mock(),
    )
    monkeypatch.setattr(
        lifecycle_worker, "installed_snapshot", lambda *args, **kwargs: "exact"
    )
    monkeypatch.setattr(
        lifecycle_worker, "proof", lambda *args: {"binding_sha256": "bound"}
    )

    def denied(*args):
        raise Refusal("network unreachable")

    monkeypatch.setattr("installation.native_egress_probe.verify_network", denied)
    with pytest.raises(Refusal, match="unreachable"):
        lifecycle_worker.activate(installer)
    installer.apply.assert_not_called()


def test_live_probe_uses_reviewed_worker_placement_and_no_credentials(native):
    from installation.config import digest
    from installation.native_egress_probe import verify_network

    env, lock = configure(native)
    proof = {
        "version": 1,
        "reachable": True,
        "recipe_sha256": digest(native_egress.rules(env)),
    }
    installer = SimpleNamespace(
        env=env,
        lock=lock,
        owner="installation",
        existing=lambda doc: None,
        aws=lambda *args: {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": env["database"]["identifier"],
                    "Endpoint": {"Address": "database.example.invalid", "Port": 5432},
                    "DbiResourceId": "db-resource",
                }
            ]
        },
        kube=lambda *args: proof,
        json=lambda value: value,
        apply=Mock(),
        wait_job=Mock(),
    )
    result = verify_network(installer)
    account, policy, job = installer.apply.call_args.args[0]
    assert account["automountServiceAccountToken"] is False
    assert not account["metadata"].get("annotations")
    assert policy["spec"]["egress"] == native_egress.rules(env)
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == env["paid_worker"]["node_selector"]
    assert not pod.get("volumes") and not pod.get("initContainers")
    assert [entry["name"] for entry in pod["containers"][0]["env"]] == [
        "ADP_NATIVE_NETWORK_PROBE"
    ]
    installer.wait_job.assert_called_once_with(job)
    assert result["recipe_sha256"] == proof["recipe_sha256"]

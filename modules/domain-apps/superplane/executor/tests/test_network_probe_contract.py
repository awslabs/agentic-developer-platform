"""Approved Service receipt composition; all network responses are declared fixtures."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor.network_probe_contract import endpoint, verify_result
from superplane_executor.network_workload_probe import run
from superplane_executor.workspace import Workspace


@pytest.mark.parametrize(
    "change",
    [
        None,
        "service_uid",
        "service_namespace",
        "service_ip",
        "service_port",
        "external_service",
        "service_replaced",
        "old_nonce",
        "wrong_dns_address",
        "host_network",
        "host_aliases",
        "custom_dns",
        "default_dns_policy",
        "pod_replaced",
        "log_denied",
        "result_mismatch",
        "missing_result",
        "revoked",
    ],
)
async def test_probe_result_requires_original_service_and_matching_pod_log(change):
    workspace = Workspace("/unused", "https://management.example")
    contract = {
        "service_name": "acceptance",
        "namespace": "tenant",
        "service_uid": "service-uid",
        "port": 8080,
        "cidrs": ["172.20.0.0/16"],
        "nonce": "fresh",
    }
    service = {
        "metadata": {"name": "acceptance", "namespace": "tenant", "uid": "service-uid"},
        "spec": {
            "type": "ClusterIP",
            "clusterIP": "172.20.1.4",
            "ports": [{"port": 8080}],
        },
    }
    pod = {
        "metadata": {
            "name": "probe-pod",
            "uid": "pod-uid",
            "namespace": "tenant",
            "labels": {"superplane.ai/capacity": "allocation"},
        },
        "spec": {"nodeName": "allocated-node", "hostNetwork": False},
        "status": {"phase": "Succeeded"},
    }
    value = {
        "version": 1,
        "nonce": "fresh",
        "source": "pod",
        "url": endpoint(contract),
        "addresses": ["172.20.1.4"],
        "responses": [{"address": "172.20.1.4", "status": 200}],
        "tls_verified": False,
    }
    if change == "service_uid":
        service["metadata"]["uid"] = "foreign"
    elif change == "service_namespace":
        service["metadata"]["namespace"] = "other-tenant"
    elif change == "service_ip":
        service["spec"]["clusterIP"] = "8.8.8.8"
    elif change == "service_port":
        service["spec"]["ports"][0]["port"] = 80
    elif change == "external_service":
        service["spec"]["type"] = "ExternalName"
    elif change == "old_nonce":
        value["nonce"] = "stale"
    elif change == "wrong_dns_address":
        value["addresses"] = ["172.20.9.9"]
        value["responses"][0]["address"] = "172.20.9.9"
    elif change == "host_network":
        pod["spec"]["hostNetwork"] = True
    elif change == "host_aliases":
        pod["spec"]["hostAliases"] = [
            {"ip": "172.20.1.4", "hostnames": ["acceptance.tenant.svc.cluster.local"]}
        ]
    elif change == "custom_dns":
        pod["spec"]["dnsConfig"] = {"nameservers": ["172.20.0.53"]}
    elif change == "default_dns_policy":
        pod["spec"]["dnsPolicy"] = "Default"
    content = json.dumps(value)
    if change == "result_mismatch":
        content = json.dumps({**value, "nonce": "different"})
    elif change == "missing_result":
        content = ""
    service_reads = pod_reads = authority_checks = 0

    async def request(operation, target, method, path, **kwargs):
        nonlocal service_reads, pod_reads
        assert method == "GET" and path.startswith("/api/v1/namespaces/tenant/")
        if "/services/" in path:
            service_reads += 1
            current = deepcopy(service)
            if change == "service_replaced" and service_reads > 1:
                current["metadata"]["uid"] = "replacement"
            return httpx.Response(200, json=current)
        if "/log?" in path:
            return httpx.Response(
                403 if change == "log_denied" else 200, text=json.dumps(value)
            )
        pod_reads += 1
        current = deepcopy(pod)
        if change == "pod_replaced" and pod_reads > 1:
            current["metadata"]["uid"] = "replacement"
        return httpx.Response(200, json=current)

    async def authorize():
        nonlocal authority_checks
        authority_checks += 1
        if change == "revoked" and authority_checks > 1:
            raise OperationRefused("revoked")

    workspace.request = request
    call = verify_result(
        workspace,
        None,
        {"namespace": "tenant"},
        SimpleNamespace(cluster_name="allocation"),
        contract,
        pod,
        content,
        authorize,
    )
    if change is None:
        await call
        assert service_reads == 2 and pod_reads == 2 and authority_checks == 2
    else:
        with pytest.raises(OperationRefused):
            await call


def test_probe_image_emits_same_bounded_log_and_termination_receipt(monkeypatch):
    import superplane_executor.network_workload_probe as entry

    calls = []

    def packets(url, *, allowed_cidrs):
        calls.append((url, allowed_cidrs))
        return {
            "url": url,
            "addresses": ["172.20.1.4"],
            "responses": [{"address": "172.20.1.4", "status": 200}],
            "tls_verified": False,
        }

    monkeypatch.setattr(entry, "probe", packets)
    contract = {
        "service_name": "acceptance",
        "namespace": "tenant",
        "port": 8080,
        "cidrs": ["172.20.1.4/32"],
        "nonce": "fresh",
    }
    log, result = run(contract)
    assert calls == [(endpoint(contract), contract["cidrs"])]
    assert json.loads(result) == {"superplane_result_version": 1, "text": log}
    assert json.loads(log)["nonce"] == "fresh"


def test_probe_image_does_not_return_result_when_packets_fail(monkeypatch):
    import superplane_executor.network_workload_probe as entry

    def failed(*args, **kwargs):
        raise OSError("declared DNS/HTTP fixture failure")

    monkeypatch.setattr(entry, "probe", failed)
    with pytest.raises(OSError):
        run(
            {
                "service_name": "acceptance",
                "namespace": "tenant",
                "port": 8080,
                "cidrs": ["172.20.1.4/32"],
                "nonce": "fresh",
            }
        )

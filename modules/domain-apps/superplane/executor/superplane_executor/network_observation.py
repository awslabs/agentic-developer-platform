"""Collect real probe receipts; configuration/Ready endpoints are not traffic proof.

The AWS-06 driver launches network_probe through the governed node transport and
ordinary pod workload APIs, using a fresh nonce and the approved endpoint/CIDRs.
This collector reads pod identity/logs through the existing scoped Workspace
client. A successful pod-log read independently exercises API-to-kubelet traffic.
Node transport receipts must be verified by that transport against the allocation's
provider instance identity, never accepted from an issue comment or request flag.
"""

import ipaddress
import json
from urllib.parse import quote

from harness_jobs.identity import OperationRefused


def receipt(raw, *, nonce, source, endpoint, cidrs):
    try:
        value = json.loads(raw)
        ranges = [ipaddress.ip_network(c, strict=True) for c in cidrs]
        if (
            set(value)
            != {
                "version",
                "nonce",
                "source",
                "url",
                "addresses",
                "responses",
                "tls_verified",
            }
            or value["version"] != 1
            or (value["nonce"], value["source"], value["url"])
            != (nonce, source, endpoint)
        ):
            raise ValueError("probe identity differs")
        addresses = value["addresses"]
        if (
            not isinstance(addresses, list)
            or not addresses
            or len(addresses) > 32
            or len(set(addresses)) != len(addresses)
            or any(
                not any(ipaddress.ip_address(a) in r for r in ranges) for a in addresses
            )
        ):
            raise ValueError("probe resolved an unapproved address")
        responses = value["responses"]
        if (
            not isinstance(responses, list)
            or len(responses) != len(addresses)
            or {r["address"] for r in responses} != set(addresses)
            or any(
                type(r["status"]) is not int
                or not (
                    200 <= r["status"] < 300
                    or (source == "node" and r["status"] in {401, 403})
                )
                for r in responses
            )
        ):
            raise ValueError("packet response missing or unsuccessful")
        if source == "node" and (
            not endpoint.startswith("https://") or value["tls_verified"] is not True
        ):
            raise ValueError("private API TLS was not verified")
        return value
    except (ValueError, KeyError, TypeError):
        raise OperationRefused(
            "network probe receipt failed identity or traffic validation"
        ) from None


async def pod_service(
    workspace,
    operation,
    target,
    *,
    pod_name,
    pod_uid,
    node_name,
    nonce,
    endpoint,
    cidrs,
    allocation_label,
):
    path = f"/api/v1/namespaces/{quote(target['namespace'], safe='')}/pods/{quote(pod_name, safe='')}"
    response = await workspace.request(operation, target, "GET", path)
    if response.status_code != 200:
        raise OperationRefused("network probe pod unavailable")
    pod = response.json()
    if (
        pod.get("metadata", {}).get("uid") != pod_uid
        or pod.get("metadata", {}).get("labels", {}).get("superplane.ai/capacity")
        != allocation_label
        or pod.get("spec", {}).get("nodeName") != node_name
        or pod.get("spec", {}).get("hostNetwork", False)
        # A matching Service address from /etc/hosts or a custom resolver does
        # not prove cluster DNS. These checks apply only to the probe workload.
        or pod.get("spec", {}).get("dnsPolicy", "ClusterFirst") != "ClusterFirst"
        or pod.get("spec", {}).get("hostAliases", []) != []
        or pod.get("spec", {}).get("dnsConfig", {}) != {}
        or pod.get("status", {}).get("phase") != "Succeeded"
    ):
        raise OperationRefused(
            "ordinary network probe pod identity or placement differs"
        )
    logs = await workspace.request(
        operation, target, "GET", path + "/log?limitBytes=8192"
    )
    if logs.status_code != 200 or len(logs.content) > 8192:
        raise OperationRefused("API-to-kubelet probe log path failed")
    result = receipt(
        logs.text, nonce=nonce, source="pod", endpoint=endpoint, cidrs=cidrs
    )
    # Re-read immutable pod identity after the slow log call to reject replacement.
    current = await workspace.request(operation, target, "GET", path)
    if (
        current.status_code != 200
        or current.json().get("metadata", {}).get("uid") != pod_uid
        or current.json().get("spec") != pod.get("spec")
    ):
        raise OperationRefused("network probe pod replaced during observation")
    return {
        "pod_service": result,
        "api_to_kubelet": {
            "pod_uid": pod_uid,
            "node_name": node_name,
            "log_read": True,
        },
    }

"""Read serving Product pod identities without Kubernetes exec or write authority."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote

from tests.e2e.cli_uplift import deployment_provenance as provenance


def observe(aws, cfg, expected_revision, *, profile_enabled=True):
    provenance.require(
        type(profile_enabled) is bool, "Product admissions expectation is invalid"
    )
    selected = provenance.binding(cfg)
    record = {}
    revision = provenance.resolve(aws, cfg, record)
    provenance.require(
        re.fullmatch(r"[a-f0-9]{40}", expected_revision)
        and revision == expected_revision,
        "Serving Product source does not match the selected revision",
    )
    proof = record["revision_evidence"]
    digest = proof["image_digest"]
    cluster = aws.call("eks", "describe_cluster", name=selected["cluster"])["cluster"]
    provenance.require(
        cluster.get("arn") == selected["cluster_arn"]
        and cluster.get("status") == "ACTIVE"
        and cluster.get("name") == selected["cluster"],
        "Product cluster differs from reviewed deployment",
    )
    endpoint = cluster["endpoint"].rstrip("/")
    provenance.require(
        re.fullmatch(r"https://[A-Za-z0-9.-]+\.eks\.amazonaws\.com", endpoint),
        "Product cluster endpoint is not an EKS API",
    )
    bearer = provenance.eks_bearer(aws, selected["cluster"], selected["region"])
    ca = cluster["certificateAuthority"]["data"]
    namespace = quote(selected["namespace"], safe="")

    def read(path):
        return provenance.get_json(endpoint + path, bearer, ca)

    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{quote(selected['deployment'], safe='')}"
    deployment = read(path)
    metadata = deployment["metadata"]
    provenance.require(
        metadata.get("uid") == proof["deployment_uid"]
        and metadata.get("generation") == proof["generation"]
        and not metadata.get("deletionTimestamp"),
        "Product deployment changed during observation",
    )
    desired = deployment["spec"].get("replicas")
    provenance.require(
        type(desired) is int
        and 0 < desired <= 100
        and deployment["status"].get("observedGeneration") == metadata["generation"]
        and deployment["status"].get("readyReplicas") == desired,
        "Product deployment has no stable serving replicas",
    )
    replicas = read(f"/apis/apps/v1/namespaces/{namespace}/replicasets")
    pods = read(f"/api/v1/namespaces/{namespace}/pods")
    provenance.require(
        replicas.get("kind") == "ReplicaSetList"
        and not replicas.get("metadata", {}).get("continue")
        and pods.get("kind") == "PodList"
        and not pods.get("metadata", {}).get("continue"),
        "Product pod inventory is unavailable or incomplete",
    )
    owners = {
        item["metadata"]["uid"]
        for item in replicas["items"]
        if any(
            owner.get("uid") == metadata["uid"] and owner.get("controller") is True
            for owner in item["metadata"].get("ownerReferences", [])
        )
    }
    provenance.require(owners, "Product replica ownership is not observable")
    selector = selected["selector"]
    active = [
        item
        for item in pods["items"]
        if not item["metadata"].get("deletionTimestamp")
        and all(
            item["metadata"].get("labels", {}).get(key) == value
            for key, value in selector.items()
        )
    ]
    provenance.require(
        len(active) == desired, "Product serving pod count differs from deployment"
    )
    inventory = []
    for pod in active:
        identity, status = pod["metadata"], pod.get("status", {})
        containers = [
            item
            for item in pod["spec"]["containers"]
            if item.get("name") == selected["container"]
        ]
        running = [
            item
            for item in status.get("containerStatuses", [])
            if item.get("name") == selected["container"]
        ]
        provenance.require(
            identity.get("namespace") == selected["namespace"]
            and isinstance(identity.get("uid"), str)
            and bool(identity["uid"])
            and any(
                owner.get("uid") in owners and owner.get("controller") is True
                for owner in identity.get("ownerReferences", [])
            )
            and len(containers) == len(running) == 1
            and containers[0].get("image")
            == selected["image_repository"] + "@" + digest
            and running[0].get("ready") is True
            and running[0].get("imageID", "").rsplit("@", 1)[-1] == digest
            and "running" in running[0].get("state", {})
            and any(
                item.get("type") == "Ready" and item.get("status") == "True"
                for item in status.get("conditions", [])
            ),
            "Product serving pod identity or image differs from the release",
        )
        provenance.require(
            containers[0].get("ports") == [{"containerPort": 8080, "protocol": "TCP"}],
            "Product serving pod has no reviewed private HTTP probe port",
        )
        env = containers[0].get("env", [])
        provenance.require(
            [
                item.get("value")
                for item in env
                if item.get("name") == "BG_PRODUCT_PROFILE_ENABLED"
            ]
            == [str(profile_enabled).lower()]
            and [
                item.get("value")
                for item in env
                if item.get("name") == "BG_PRODUCT_ONBOARDING_ENABLED"
            ]
            == ["false"],
            "Product serving pod capability is not configured",
        )
        address = status.get("podIP")
        try:
            parsed = ipaddress.ip_address(address)
            private = (
                isinstance(parsed, ipaddress.IPv4Address)
                and parsed.is_private
                and not parsed.is_loopback
            )
        except ValueError:
            private = False
        provenance.require(private, "Product serving pod has no private probe address")
        inventory.append({"uid": identity["uid"], "address": address, "digest": digest})
    provenance.require(
        len({item["uid"] for item in inventory}) == desired,
        "Product serving pod identities repeat",
    )
    after = read(path)
    provenance.require(
        after["metadata"].get("uid") == metadata["uid"]
        and after["metadata"].get("generation") == metadata["generation"]
        and after["spec"]["template"] == deployment["spec"]["template"],
        "Product deployment changed during observation",
    )
    return {
        "source_revision": revision,
        "digest": digest,
        "pods": sorted(inventory, key=lambda item: item["uid"]),
    }

"""Reclaim only terminated, durably recorded Transform workload resources."""

import asyncio
import re

from l5_services.workers.execution import AuthorityRevoked


FINALIZER = "adp.dev/transform-termination-observed"


def termination(pod, container, image):
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if (len(statuses) != 1 or statuses[0].get("name") != container
            or not isinstance(statuses[0].get("state", {}).get("terminated"), dict)):
        return None
    if not statuses[0].get("imageID", "").endswith(image.split("@", 1)[1]):
        raise AuthorityRevoked("Transform terminal workload image changed")
    return statuses[0]["state"]["terminated"]


async def snapshot_resources(api, namespace, pod, labels, authority):
    metadata = pod["metadata"]
    name = metadata["name"]
    if metadata.get("namespace") != namespace or metadata.get("labels") != labels or not metadata.get("uid"):
        raise AuthorityRevoked("Transform cleanup owner changed")
    names = set()
    for volume in pod["spec"].get("volumes", []):
        for source in volume.get("projected", {}).get("sources", []):
            config = source.get("configMap", {}).get("name")
            if not isinstance(config, str) or not re.fullmatch(re.escape(name) + r"-part-[0-9]{4}", config):
                raise AuthorityRevoked("Transform cleanup input is outside the owned job")
            names.add(("configmaps", config))
    for container in pod["spec"].get("containers", []):
        for source in container.get("envFrom", []):
            if source.get("secretRef", {}).get("name") != name:
                raise AuthorityRevoked("Transform cleanup secret is outside the owned job")
            names.add(("secrets", name))
    resources = []
    for kind, resource_name in sorted(names):
        authority.assert_current()
        path = f"/api/v1/namespaces/{namespace}/{kind}/{resource_name}"
        resource = await asyncio.to_thread(api.request, "GET", path, timeout_seconds=2)
        if (resource is None or resource.get("metadata", {}).get("labels") != labels
                or not resource["metadata"].get("uid")):
            raise AuthorityRevoked("Transform cleanup input identity changed")
        resources.append({"kind": kind, "name": resource_name, "uid": resource["metadata"]["uid"]})
    return {"namespace": namespace, "name": name, "uid": metadata["uid"], "labels": labels,
            "resources": resources}


async def cleanup_resources(api, *, snapshot, namespace, name, labels, authority):
    if (snapshot.get("namespace") != namespace or snapshot.get("name") != name
            or snapshot.get("labels") != labels or not snapshot.get("uid")):
        raise AuthorityRevoked("Transform retained cleanup ownership changed")

    async def read(kind, resource_name, uid):
        authority.assert_current()
        path = f"/api/v1/namespaces/{namespace}/{kind}/{resource_name}"
        resource = await asyncio.to_thread(api.request, "GET", path, timeout_seconds=2)
        if resource is not None and (resource.get("metadata", {}).get("uid") != uid
                                    or resource["metadata"].get("labels") != labels):
            raise AuthorityRevoked("Transform cleanup resource UID or owner changed")
        return resource, path

    pod, path = await read("pods", name, snapshot["uid"])
    if pod is not None:
        metadata = pod["metadata"]
        finalizers = metadata.get("finalizers", [])
        if FINALIZER in finalizers:
            authority.assert_current()
            await asyncio.to_thread(api.request, "PATCH", path, timeout_seconds=2,
                body=[{"op": "test", "path": "/metadata/uid", "value": snapshot["uid"]},
                      {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
                      {"op": "remove", "path": f"/metadata/finalizers/{finalizers.index(FINALIZER)}"}])
        authority.assert_current()
        await asyncio.to_thread(api.request, "DELETE", path, timeout_seconds=2,
            body={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": snapshot["uid"]}})
    for resource in snapshot["resources"]:
        kind, resource_name = resource["kind"], resource["name"]
        if not (kind == "secrets" and resource_name == name or kind == "configmaps"
                and re.fullmatch(re.escape(name) + r"-part-[0-9]{4}", resource_name)):
            raise AuthorityRevoked("Transform cleanup resource is outside the owned job")
        current, resource_path = await read(kind, resource_name, resource["uid"])
        if current is not None:
            authority.assert_current()
            await asyncio.to_thread(api.request, "DELETE", resource_path, timeout_seconds=2,
                body={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": resource["uid"]}})

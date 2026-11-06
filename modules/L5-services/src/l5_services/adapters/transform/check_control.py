"""Nonblocking L5 checks; Kubernetes, not repository output, establishes termination."""

import asyncio
import base64
import json
import time
from hashlib import sha256

from l5_services.adapters.transform.cleanup import cleanup_resources, snapshot_resources
from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.adapters.transform.provider_runner import MAX_PAYLOAD_BYTES
from l5_services.adapters.transform.pod_isolation import CHUNK_BYTES, input_sources, pod_manifest, validate_pod
from l5_services.adapters.transform.test_output import completed_test_count
from l5_services.adapters.transform.verification import DetachedCheckPending
from l5_services.workers.execution import AuthorityRevoked


class TransformCheckControl:
    nonblocking = True

    def __init__(self, api, artifacts, *, namespace, image, node_name, node_uid):
        from l5_services.adapters.transform.pod_control import TransformPodControl

        self.pods = TransformPodControl(api, namespace=namespace, node_name=node_name,
                                       node_uid=node_uid, provider_image=image)
        self.api, self.artifacts = api, artifacts
        self.namespace, self.image = namespace, image

    async def run_check(self, *, policy, authority, **request):
        if (request["image_digest"] != self.image.split("@", 1)[1]
                or request["image_digest"] != policy["image_digest"]
                or request["bundle_sha256"] != policy["bundle_sha256"]
                or request["oracle_version"] != policy["oracle_version"]
                or request["command"] not in policy["commands"] or request["stage"] not in {"before", "after"}):
            raise AuthorityRevoked("Transform check differs from the admitted test policy")
        files, modes = request["files"], request["file_modes"]
        tree = compare_source_trees(files, files, base_modes=modes, candidate_modes=modes)["base_tree"]
        if tree != request["source_tree"]:
            raise ValueError("Transform check source tree changed")
        payload = {key: request[key] for key in (
            "stage", "source_tree", "command", "image_digest", "bundle_sha256", "oracle_version", "file_modes")}
        payload.update(lock_sha256=policy["lock_sha256"],
                       files={key: base64.b64encode(value).decode() for key, value in files.items()})
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(raw.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("Transform check input exceeds its bound")
        scope = authority.scope
        run_id = authority.source_request["operation_id"]
        owner = sha256(json.dumps([scope, run_id], sort_keys=True).encode()).hexdigest()[:40]
        identity = sha256((owner + raw).encode()).hexdigest()
        name = "tf-check-" + identity[:40]
        labels = {"adp.dev/transform-check": owner}
        base = f"/api/v1/namespaces/{self.namespace}"
        chunks = [raw[offset:offset + CHUNK_BYTES] for offset in range(0, len(raw), CHUNK_BYTES)]
        expected = pod_manifest(name=name, namespace=self.namespace, labels=labels,
            node_name=self.pods.node_name, image=self.image, container="check",
            command=["python", "-m", "l5_services.adapters.transform.check_runner",
                     "--input-sha256=" + sha256(raw.encode()).hexdigest()],
            sources=input_sources(name, len(chunks), check=True),
            deadline=request["command"]["timeout_seconds"], annotations={"adp.dev/input-sha256": identity})
        checkpoint = await self.artifacts.read_checkpoint(name=name, authority=authority)
        if checkpoint is not None:
            if checkpoint.get("input_sha256") != identity:
                raise AuthorityRevoked("Transform retained check input differs from current work")
            await cleanup_resources(self.api, snapshot=checkpoint["cleanup"], namespace=self.namespace,
                                    name=name, labels=labels, authority=authority)
            return checkpoint["receipt"]

        def action():
            deadline = time.monotonic() + 4

            def call(method, path, *, timeout_seconds=2, **kwargs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DetachedCheckPending("Transform check control window elapsed")
                return self.api.request(method, path, timeout_seconds=min(timeout_seconds, remaining), **kwargs)

            authority.assert_current()
            node = call("GET", "/api/v1/nodes/" + self.pods.node_name, timeout_seconds=2)
            if (not isinstance(node, dict) or node.get("metadata", {}).get("uid") != self.pods.node_uid
                    or node.get("metadata", {}).get("labels", {}).get("adp.dev/transform-isolation") != "v1"):
                raise AuthorityRevoked("Transform check node qualification changed")
            network = call("GET", f"/apis/networking.k8s.io/v1/namespaces/{self.namespace}/networkpolicies/deny-all",
                                       timeout_seconds=2)
            spec = network.get("spec", {}) if isinstance(network, dict) else {}
            if (spec.get("podSelector") != {} or spec.get("ingress") or spec.get("egress")
                    or set(spec.get("policyTypes", [])) != {"Ingress", "Egress"}):
                raise AuthorityRevoked("Transform check network isolation changed")
            pod = call("GET", base + "/pods/" + name, timeout_seconds=2)
            if pod is None:
                progress_name = name + "-part-9999"
                progress_path = base + "/configmaps/" + progress_name
                progress = call("GET", progress_path)
                if progress is None:
                    progress = call("POST", base + "/configmaps", body={
                        "apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {"name": progress_name, "namespace": self.namespace, "labels": labels,
                                     "annotations": {"adp.dev/input-sha256": identity}},
                        "data": {"next_chunk": "0"}})
                metadata = progress.get("metadata", {})
                next_chunk = progress.get("data", {}).get("next_chunk", "")
                if (metadata.get("labels") != labels or not metadata.get("uid") or not metadata.get("resourceVersion")
                        or metadata.get("annotations", {}).get("adp.dev/input-sha256") != identity
                        or not isinstance(next_chunk, str) or not next_chunk.isdecimal()
                        or not 0 <= int(next_chunk) <= len(chunks)):
                    raise AuthorityRevoked("Transform check upload progress ownership changed")
                for index in range(int(next_chunk), len(chunks)):
                    part = f"part-{index:04}"
                    config_name = name + "-" + part
                    data = {part: chunks[index]}
                    previous = call("GET", base + "/configmaps/" + config_name, timeout_seconds=2)
                    if previous is None:
                        call("POST", base + "/configmaps", timeout_seconds=2, body={
                            "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                            "metadata": {"name": config_name, "namespace": self.namespace, "labels": labels}, "data": data})
                    elif (previous.get("data") != data or previous.get("immutable") is not True
                          or previous.get("metadata", {}).get("labels") != labels):
                        raise AuthorityRevoked("Transform check input ownership changed")
                    authority.assert_current()
                    metadata = progress["metadata"]
                    progress = call("PATCH", progress_path, body=[
                        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
                        {"op": "replace", "path": "/data/next_chunk", "value": str(index + 1)}])
                authority.assert_current()
                created = call("POST", base + "/pods", timeout_seconds=2, body=expected)
                validate_pod(created, expected)
                raise DetachedCheckPending("Transform check launched")
            validate_pod(pod, expected)
            metadata = pod["metadata"]
            statuses = pod.get("status", {}).get("containerStatuses", [])
            if len(statuses) != 1 or statuses[0].get("name") != "check":
                raise DetachedCheckPending("Transform check has not started")
            terminated = statuses[0].get("state", {}).get("terminated")
            if not isinstance(terminated, dict):
                raise DetachedCheckPending("Transform check is running")
            if not statuses[0].get("imageID", "").endswith(request["image_digest"]):
                raise AuthorityRevoked("Transform check runtime image differs")
            output = call("GET", base + "/pods/" + name + "/log", timeout_seconds=2,
                params={"container": "check", "limitBytes": 1024 * 1024}, raw=True, maximum=1024 * 1024)
            latest = call("GET", base + "/pods/" + name, timeout_seconds=2)
            validate_pod(latest, expected)
            if latest is None or latest.get("metadata", {}).get("uid") != metadata["uid"]:
                raise AuthorityRevoked("Transform check Pod changed during log read")
            if not isinstance(output, bytes) or len(output) >= 1024 * 1024:
                raise ValueError("Transform check output is incomplete")
            authority.assert_current()
            return terminated, output, pod

        terminated, output, pod = await asyncio.wait_for(asyncio.to_thread(action), timeout=5)
        reference = await self.artifacts.put(tenant_id=scope["tenant_id"], project_id=scope["project_id"], run_id=run_id,
            name=name + "-output", contents=output, media_type="text/plain", authority=authority)
        receipt = {key: payload[key] for key in ("stage", "source_tree", "image_digest", "bundle_sha256",
                                                 "lock_sha256", "oracle_version")}
        receipt.update({key: request["command"][key] for key in ("argv", "cwd", "environment")})
        result = {**receipt, "started_at": terminated.get("startedAt"), "ended_at": terminated.get("finishedAt"),
                "exit_code": terminated.get("exitCode"),
                "test_count": completed_test_count(request["command"]["argv"], output),
                "timed_out": terminated.get("reason") == "DeadlineExceeded", "executor_uid": pod["metadata"]["uid"],
                "stream_layout": "kubernetes_combined",
                "stdout": {key: reference[key] for key in ("artifact_id", "version", "sha256", "bytes")},
                "stderr": {key: reference[key] for key in ("artifact_id", "version", "sha256", "bytes")}}
        cleanup = await snapshot_resources(self.api, self.namespace, pod, labels, authority)
        await self.artifacts.save_checkpoint(name=name,
            value={"input_sha256": identity, "receipt": result, "cleanup": cleanup}, authority=authority)
        await cleanup_resources(self.api, snapshot=cleanup, namespace=self.namespace,
                                name=name, labels=labels, authority=authority)
        return result

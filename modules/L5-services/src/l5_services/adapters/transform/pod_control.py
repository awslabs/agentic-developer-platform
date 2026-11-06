"""UID-fenced Kubernetes observation and local termination for Transform jobs."""

import asyncio
import base64
import json
import re
from datetime import UTC, datetime
from hashlib import sha256

from l5_services.adapters.transform.cleanup import cleanup_resources, snapshot_resources, termination
from l5_services.adapters.transform.provider_runner import MAX_PAYLOAD_BYTES
from l5_services.adapters.transform.provider_endpoint import TransformProviderEndpoint
from l5_services.adapters.transform.pod_isolation import input_sources, pod_manifest, validate_pod
from l5_services.workers.execution import AuthorityRevoked


class TransformPodControl:
    def __init__(self, api, *, namespace, node_name, node_uid, source=None, artifacts=None, provider_image=None,
                 provider_endpoint=None):
        if (not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", namespace)
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node_name)
                or not isinstance(node_uid, str) or not re.fullmatch(r"[a-f0-9-]{36}", node_uid)):
            raise ValueError("Transform isolated executor identity unavailable")
        self.api = api
        self.namespace = namespace
        self.node_name = node_name
        self.node_uid = node_uid
        if provider_image is not None and not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[a-f0-9]{64}", provider_image):
            raise ValueError("Transform requires a protected immutable provider image")
        self.source, self.artifacts, self.provider_image = source, artifacts, provider_image
        if provider_endpoint is not None:
            if not isinstance(provider_endpoint, TransformProviderEndpoint):
                raise ValueError("Transform provider routing must come from protected installation configuration")
            provider_endpoint = TransformProviderEndpoint.from_installed(provider_endpoint.as_json(),
                region=provider_endpoint.region, provider_image=provider_image)
        self.provider_endpoint = provider_endpoint

    def _provider_environment(self):
        return [] if self.provider_endpoint is None else [
            {"name": "L5_TRANSFORM_PROVIDER_ENDPOINT_REF", "value": self.provider_endpoint.as_json()},
            {"name": "L5_TRANSFORM_PROVIDER_IMAGE", "value": self.provider_image},
        ]

    def _owner(self, intent, authority):
        scope = getattr(authority, "scope", None)
        request = getattr(authority, "source_request", None)
        if (not callable(getattr(authority, "assert_current", None))
                or not isinstance(scope, dict) or not isinstance(request, dict)
                or scope.get("tenant_id") != intent.get("tenant_id")
                or scope.get("product_id") != intent.get("product_id")
                or scope.get("project_id") != intent.get("project_id")
                or request.get("operation_id") != intent.get("run_id")
                or not isinstance(request.get("attempt_id"), str) or not request["attempt_id"]
                or not isinstance(request.get("lease_owner"), str) or not request["lease_owner"]
                or type(request.get("lease_fence")) is not int or request["lease_fence"] < 1
                or not isinstance(request.get("delegation_id"), str)
                or not isinstance(intent.get("attempt_token"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", intent["attempt_token"])
                or not isinstance(intent.get("executor_name"), str)
                or not re.fullmatch(r"tf-[a-f0-9]{40}", intent["executor_name"])):
            raise AuthorityRevoked("Transform Pod does not match current Product work")
        authority.assert_current()
        return sha256(f"{scope['tenant_id']}:{scope['product_id']}:{scope['project_id']}:"
                      f"{request['operation_id']}:{intent['attempt_token']}".encode()).hexdigest()[:40]

    def _request(self, method, path, **kwargs):
        return self.api.request(method, path, timeout_seconds=2, **kwargs)

    def _validate_pod(self, pod, intent, authority):
        if self.provider_image is None:
            return
        if not isinstance(pod, dict):
            raise AuthorityRevoked("Transform provider Pod readback is unavailable")
        spec = pod.get("spec", {})
        if not isinstance(spec, dict):
            raise AuthorityRevoked("Transform provider Pod specification is unavailable")
        deadline = spec.get("activeDeadlineSeconds")
        admitted = getattr(authority, "provider_deadline_seconds", None)
        if (type(admitted) is not int or not 60 <= admitted <= 86400
                or type(deadline) is not int or not 1 <= deadline <= admitted
                or self.provider_image.split("@", 1)[1] != intent["image_digest"]):
            raise AuthorityRevoked("Transform provider image or admitted deadline changed")
        volumes = spec.get("volumes", [])
        if (not isinstance(volumes, list) or len(volumes) != 2 or not isinstance(volumes[0], dict)
                or not isinstance(volumes[0].get("projected"), dict)
                or not isinstance(volumes[0]["projected"].get("sources"), list)):
            raise AuthorityRevoked("Transform provider source projection changed")
        sources = input_sources(intent["executor_name"], len(volumes[0]["projected"]["sources"]))
        expected = pod_manifest(name=intent["executor_name"], namespace=self.namespace,
            labels={"adp.dev/transform-work": self._owner(intent, authority)}, node_name=self.node_name,
            image=self.provider_image, container="provider",
            command=["python", "-m", "l5_services.adapters.transform.provider_runner"],
            sources=sources, deadline=deadline, environment=self._provider_environment())
        validate_pod(pod, expected)

    def _read(self, intent, authority, *, validate_isolation=True):
        owner = self._owner(intent, authority)
        node = self._request("GET", "/api/v1/nodes/" + self.node_name)
        if (not isinstance(node, dict) or node.get("metadata", {}).get("uid") != self.node_uid
                or node.get("metadata", {}).get("labels", {}).get("adp.dev/transform-isolation") != "v1"):
            raise AuthorityRevoked("Transform executor node qualification changed")
        path = f"/api/v1/namespaces/{self.namespace}/pods/{intent['executor_name']}"
        pod = self._request("GET", path)
        authority.assert_current()
        if pod is None:
            return None, path
        metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
        if (metadata.get("name") != intent["executor_name"]
                or metadata.get("namespace") != self.namespace
                or metadata.get("labels", {}).get("adp.dev/transform-work") != owner
                or "adp.dev/transform-termination-observed" not in metadata.get("finalizers", [])
                or not isinstance(metadata.get("uid"), str) or not metadata["uid"]
                or pod.get("spec", {}).get("nodeName") not in {None, self.node_name}):
            raise AuthorityRevoked("Transform Pod identity or qualified node changed")
        if validate_isolation:
            self._validate_pod(pod, intent, authority)
        return pod, path

    async def read_provider(self, *, intent, authority):
        retained = await self._retained(intent, authority)
        if retained is not None:
            return {key: value for key, value in retained.items()
                    if key not in {"patch", "candidate_files", "candidate_modes"}}
        pod, _ = await asyncio.wait_for(asyncio.to_thread(self._read, intent, authority), timeout=5)
        if pod is None:
            return None
        observed = await self._observation(pod, intent, authority)
        if observed is not None and observed["readback"]["state"] in {"failed", "stopped"}:
            terminal = termination(pod, "provider", self.provider_image)
            if terminal is not None:
                labels = {"adp.dev/transform-work": self._owner(intent, authority)}
                cleanup = await snapshot_resources(self.api, self.namespace, pod, labels, authority)
                await self.artifacts.save_checkpoint(name=intent["executor_name"],
                    value={"observation": observed, "termination": terminal, "cleanup": cleanup}, authority=authority)
                observed = await self._retained(intent, authority)
        return {"executor_name": intent["executor_name"], "executor_uid": pod["metadata"]["uid"],
                "readback": None} if observed is None else {
                    key: value for key, value in observed.items()
                    if key not in {"patch", "candidate_files", "candidate_modes"}}

    async def _retained(self, intent, authority):
        owner = self._owner(intent, authority)
        if self.artifacts is None:
            return None
        checkpoint = await self.artifacts.read_checkpoint(name=intent["executor_name"], authority=authority)
        if checkpoint is None:
            return None
        receipt = checkpoint["observation"]
        if any(receipt.get(key) != intent[key] for key in (
                "executor_name", "tenant_id", "product_id", "project_id", "run_id", "attempt_token",
                "input_hash", "executor_binding_ref", "image_digest", "binary_sha256", "definition_revision")):
            raise AuthorityRevoked("Transform retained provider observation differs from current work")
        if receipt.get("executor_uid") != checkpoint["cleanup"]["uid"]:
            raise AuthorityRevoked("Transform retained provider UID differs from cleanup evidence")
        await cleanup_resources(self.api, snapshot=checkpoint["cleanup"], namespace=self.namespace,
            name=intent["executor_name"], labels={"adp.dev/transform-work": owner}, authority=authority)
        return receipt

    async def completed_observation(self, *, intent, authority):
        retained = await self._retained(intent, authority)
        if retained is not None:
            return retained if retained["readback"]["state"] == "completed" else None
        pod, _ = await asyncio.to_thread(self._read, intent, authority)
        if pod is None:
            return None
        receipt = await self._observation(pod, intent, authority)
        if receipt is None or receipt["readback"]["state"] != "completed":
            return None
        terminal = termination(pod, "provider", self.provider_image)
        if terminal is None:
            return None
        if terminal.get("exitCode") != 0:
            raise ValueError("Transform provider reported completion but exited unsuccessfully")
        if not isinstance(receipt.get("patch"), str):
            raise ValueError("Transform original patch is unavailable")
        patch = base64.b64decode(receipt["patch"], validate=True)
        reference = await self.artifacts.put(tenant_id=intent["tenant_id"], project_id=intent["project_id"],
            run_id=intent["run_id"], name="provider-original-patch", contents=patch,
            media_type="text/x-diff", authority=authority)
        reference = {key: reference[key] for key in (
            "tenant_id", "project_id", "artifact_id", "version", "sha256", "bytes", "media_type")}
        labels = {"adp.dev/transform-work": self._owner(intent, authority)}
        cleanup = await snapshot_resources(self.api, self.namespace, pod, labels, authority)
        await self.artifacts.save_checkpoint(name=intent["executor_name"],
            value={"observation": receipt, "termination": terminal, "cleanup": cleanup,
                   "original_patch": reference}, authority=authority)
        return await self._retained(intent, authority)

    async def _observation(self, pod, intent, authority):
        if self.provider_image is None:
            return None
        self._validate_pod(pod, intent, authority)
        path = f"/api/v1/namespaces/{self.namespace}/pods/{intent['executor_name']}/log"
        raw = await asyncio.to_thread(self._request, "GET", path,
                                      params={"container": "provider", "limitBytes": MAX_PAYLOAD_BYTES},
                                      raw=True, maximum=MAX_PAYLOAD_BYTES)
        authority.assert_current()
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or len(raw.encode()) >= MAX_PAYLOAD_BYTES:
            raise ValueError("Transform provider observation exceeds its bound")
        observed = None
        for line in raw.splitlines():
            receipt = json.loads(line)
            if (not isinstance(receipt, dict) or receipt.get("kind") != "transform-provider-observation-v1"
                    or any(receipt.get(key) != intent[key] for key in (
                        "executor_name", "tenant_id", "product_id", "project_id", "run_id", "attempt_token",
                        "input_hash", "executor_binding_ref", "image_digest", "binary_sha256", "definition_revision"))):
                raise ValueError("Transform provider observation differs from the sealed work")
            from l5_services.adapters.transform.qualification import NativeReadback

            readback = NativeReadback.model_validate(receipt["readback"])
            if (not readback.native_id or readback.attempt_token != intent["attempt_token"]
                    or readback.input_hash != intent["input_hash"]
                    or observed is not None and (readback.native_id != observed["readback"]["native_id"]
                        or observed["readback"]["state"] != "running")):
                raise ValueError("Transform provider conversation identity changed")
            observed = receipt
        current, _ = await asyncio.to_thread(self._read, intent, authority)
        if current is None or current["metadata"]["uid"] != pod["metadata"]["uid"]:
            raise AuthorityRevoked("Transform Pod changed during observation")
        if observed is not None:
            observed["executor_uid"] = pod["metadata"]["uid"]
        return observed

    async def launch_provider(self, *, intent, manifest, authority):
        if self.source is None or self.artifacts is None or self.provider_image is None:
            raise AuthorityRevoked("Transform provider host resources are unavailable")
        job = intent.get("provider_job")
        if (not isinstance(job, dict) or job.get("argv") != ["/usr/local/bin/atx", "json"]
                or job.get("image_digest") != self.provider_image.split("@", 1)[1]
                or intent.get("image_digest") != job["image_digest"]
                or not isinstance(job.get("session_request"), dict)
                or job.get("attempt_token") != intent["attempt_token"]
                or job.get("input_hash") != intent["input_hash"]):
            raise AuthorityRevoked("Transform provider job is not the pinned NDJSON command")
        admitted = getattr(authority, "provider_deadline_seconds", None)
        if (type(admitted) is not int or not 60 <= admitted <= 86400
                or type(job.get("active_deadline_seconds")) is not int
                or job["active_deadline_seconds"] != admitted):
            raise AuthorityRevoked("Transform provider deadline differs from admitted work")
        retained = await self._retained(intent, authority)
        if retained is not None:
            return {key: value for key, value in retained.items()
                    if key not in {"patch", "candidate_files", "candidate_modes"}}
        pod, path = await asyncio.to_thread(self._read, intent, authority)
        if pod is not None:
            return await self.read_provider(intent=intent, authority=authority)
        files = await self.source.read_sealed_source(manifest=manifest, authority=authority)
        raw = json.dumps({"intent": intent, "manifest": manifest,
                          "files": {key: base64.b64encode(value).decode() for key, value in files.items()}},
                         sort_keys=True, separators=(",", ":"))
        if len(raw.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("Transform provider input exceeds its bound")
        owner = self._owner(intent, authority)
        labels = {"adp.dev/transform-work": owner}
        name = intent["executor_name"]

        def create_once(kind, body):
            authority.assert_current()
            collection = f"/api/v1/namespaces/{self.namespace}/{kind}"
            existing = self._request("GET", collection + "/" + body["metadata"]["name"])
            if existing is not None:
                if any(existing.get(key) != body[key] for key in ("immutable", "data")):
                    raise AuthorityRevoked("Transform input name is already owned by other content")
                if existing.get("metadata", {}).get("labels") != labels:
                    raise AuthorityRevoked("Transform input ownership changed")
                return
            self._request("POST", collection, body=body)

        sources = []
        for index, offset in enumerate(range(0, len(raw), 256 * 1024)):
            part = f"part-{index:04}"
            config_name = f"{name}-{part}"
            body = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                    "metadata": {"name": config_name, "namespace": self.namespace, "labels": labels},
                    "data": {part: raw[offset:offset + 256 * 1024]}}
            await asyncio.to_thread(create_once, "configmaps", body)
            sources.append({"configMap": {"name": config_name}})
        credentials = authority.provider_credentials()
        values = {"AWS_ACCESS_KEY_ID": credentials["access_key_id"],
                  "AWS_SECRET_ACCESS_KEY": credentials["secret_access_key"],
                  "AWS_SESSION_TOKEN": credentials["session_token"],
                  "AWS_REGION": authority.provider_identity["region"]}
        if self.provider_endpoint is not None and self.provider_endpoint.region != values["AWS_REGION"]:
            raise AuthorityRevoked("Transform provider region differs from the installed endpoint")
        secret = {"apiVersion": "v1", "kind": "Secret", "immutable": True,
                  "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
                  "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()}}
        await asyncio.to_thread(create_once, "secrets", secret)
        deadline = min(job["active_deadline_seconds"], int((authority.expires_at - datetime.now(UTC)).total_seconds()))
        if deadline < 1:
            raise AuthorityRevoked("Transform credentials expire before launch")
        body = pod_manifest(name=name, namespace=self.namespace, labels=labels, node_name=self.node_name,
            image=self.provider_image, container="provider",
            command=["python", "-m", "l5_services.adapters.transform.provider_runner"],
            sources=sources, deadline=deadline, environment=self._provider_environment())
        authority.assert_current()
        created = await asyncio.to_thread(self._request, "POST", path.rsplit("/", 1)[0], body=body)
        validate_pod(created, body)
        self._validate_pod(created, intent, authority)
        authority.assert_current()
        return await self.read_provider(intent=intent, authority=authority)

    async def request_stop(self, *, intent, native_job_ref, authority):
        def stop():
            # Isolation drift must not prevent termination of the exact owned UID.
            pod, path = self._read(intent, authority, validate_isolation=False)
            if (pod is None or not isinstance(native_job_ref, dict)
                    or native_job_ref.get("executor_uid") != pod["metadata"]["uid"]
                    or native_job_ref.get("executor_name") != intent["executor_name"]):
                raise AuthorityRevoked("Transform stop lacks the committed Pod UID")
            result = self._request("DELETE", path, body={
                "apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 1,
                "preconditions": {"uid": pod["metadata"]["uid"]}})
            authority.assert_current()
            return {"local_stop_requested": result is not None, "remote_provider_stopped": False}

        return await asyncio.wait_for(asyncio.to_thread(stop), timeout=5)

    async def collect_provider(self, *, intent, authority):
        receipt = await self.completed_observation(intent=intent, authority=authority)
        if receipt is None or receipt["readback"]["state"] != "completed":
            return None
        if self.artifacts is None or not isinstance(receipt.get("patch"), str):
            raise ValueError("Transform original patch is unavailable")
        checkpoint = await self.artifacts.read_checkpoint(name=intent["executor_name"], authority=authority)
        reference = checkpoint["original_patch"]
        native = {key: intent[key] for key in (
            "tenant_id", "project_id", "executor_name", "attempt_token", "input_hash")}
        native.update(executor_uid=receipt["executor_uid"], native_id=receipt["readback"]["native_id"])
        return {"native_job_ref": native, "state": "completed", "artifacts": {"original_patch": reference}}

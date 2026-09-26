"""Opt-in host executor for an independently provisioned validation namespace.

Only the trusted host talks to Kubernetes. Source-only immutable ConfigMaps are
projected into a credential-free Pod. A finalizer preserves termination evidence
until the host observes it; an unreachable node is an unknown outcome, not clean
cancellation. This module does not provision or enable its deployment boundary.
"""

from __future__ import annotations

from contextlib import contextmanager

import base64
import gzip
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests
import rfc8785

from lib.codex_source_limits import MAX_PROVIDER_ARCHIVE_BYTES, MAX_VALIDATION_ARCHIVE_BYTES
from lib.codex_validation import (
    RepositoryValidationExecutor,
    ValidationCancelled,
    ValidationUnavailable,
)

FINALIZER = "adp.dev/validation-termination-observed"
LABEL = "adp.dev/validation-task"
CHUNK_BYTES = 512 * 1024


def from_host_configuration(task_id):
    """Explicit host file only; never use ambient kubeconfig or SDK arguments."""
    path = os.environ.get("ADP_CODEX_VALIDATION_KUBERNETES_CONFIG")
    if not path or not Path(path).is_absolute():
        raise ValidationUnavailable("Validation Kubernetes host configuration unavailable")
    with Path(path).open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValidationUnavailable("Validation host configuration exceeds bound")
    config = json.loads(raw)
    if (not isinstance(config, dict) or set(config) != {"endpoint", "namespace", "token_file", "ca_file"}
            or any(not isinstance(value, str) for value in config.values())
            or any(not Path(config[key]).is_absolute() for key in ["token_file", "ca_file"])):
        raise ValidationUnavailable("Invalid validation host configuration")
    return KubernetesValidationExecutor(
        api=KubernetesValidationAPI(**{key: config[key] for key in ["endpoint", "token_file", "ca_file"]}),
        namespace=config["namespace"], task_id=task_id,
    )


class KubernetesValidationAPI:
    """Pinned host configuration, rotating token file, bounded calls, no retries."""

    def __init__(self, *, endpoint, token_file, ca_file, session=None):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Invalid validation API endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.token_file, self.ca_file = Path(token_file), str(ca_file)
        self.session = session or requests.Session()
        self.session.trust_env = False

    def request(self, method, path, *, body=None, params=None, raw=False, maximum=1024 * 1024, timeout_seconds=3):
        token = self.token_file.read_text().strip()
        if not token or len(token) > 16384 or any(c.isspace() for c in token):
            raise ValidationUnavailable("Validation API credential unavailable")
        try:
            with self.session.request(
                method, self.endpoint + path, json=body, params=params,
                headers={"Authorization": "Bearer " + token,
                         "Content-Type": "application/json-patch+json" if method == "PATCH" else "application/json"},
                verify=self.ca_file, timeout=(timeout_seconds, timeout_seconds), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code == 404 and method in {"GET", "DELETE"}:
                    return None
                if not 200 <= response.status_code < 300:
                    raise ValidationUnavailable("Validation API refused operation")
                content = response.raw.read(maximum + 1, decode_content=True)
                if len(content) > maximum:
                    raise ValidationUnavailable("Validation API response exceeds bound")
                if raw:
                    return content
                return json.loads(content)
        except ValidationUnavailable:
            raise
        except Exception:
            raise ValidationUnavailable("Validation API outcome unavailable") from None


class KubernetesValidationExecutor(RepositoryValidationExecutor):
    def __init__(self, *, api, namespace, task_id, clock=time.monotonic, sleep=time.sleep):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", namespace):
            raise ValueError("Invalid validation namespace")
        # The caller supplies the authenticated Task ID, never a tool argument.
        if not re.fullmatch(r"tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", task_id):
            raise ValueError("Invalid validation Task identity")
        self.api, self.namespace = api, namespace
        self.scope = hashlib.sha256(task_id.encode()).hexdigest()[:40]
        self.clock, self.sleep = clock, sleep
        self.base = f"/api/v1/namespaces/{namespace}"
        self._deadline = None

    @contextmanager
    def _bounded(self, seconds):
        previous = self._deadline
        self._deadline = min(previous or float("inf"), self.clock() + seconds)
        try:
            yield
        finally:
            self._deadline = previous

    def _request(self, *args, **kwargs):
        remaining = self._deadline - self.clock() if self._deadline is not None else 6
        if remaining <= 0.1:
            raise ValidationUnavailable("Validation API deadline elapsed")
        return self.api.request(*args, **kwargs, timeout_seconds=min(3, remaining / 2))

    def _boundary(self):
        namespace = self._request("GET", "/api/v1/namespaces/" + self.namespace)
        labels = (namespace or {}).get("metadata", {}).get("labels", {})
        if (labels.get("pod-security.kubernetes.io/enforce") != "restricted"
                or labels.get("adp.dev/validation") != "true"):
            raise ValidationUnavailable("Restricted validation namespace unavailable")
        policies = self._request(
            "GET", f"/apis/networking.k8s.io/v1/namespaces/{self.namespace}/networkpolicies"
        )
        items = (policies or {}).get("items", [])
        # An additional allow policy would override default deny. Fail closed.
        spec = items[0].get("spec", {}) if len(items) == 1 else {}
        if (len(items) != 1 or spec.get("podSelector") != {}
                or set(spec.get("policyTypes", [])) != {"Ingress", "Egress"}
                or spec.get("ingress", []) != [] or spec.get("egress", []) != []):
            raise ValidationUnavailable("Validation network isolation differs")

    def _metadata(self, name, **labels):
        return {"name": name, "labels": {LABEL: self.scope, **labels}}

    def recover(self):
        with self._bounded(10):
            return self._recover()

    def _recover(self):
        """Stop prior work for this authenticated Task before more work/settlement.

        Intents never authorize another execution. Missing Pod evidence after a
        lost create response remains unknown and requires external reconciliation.
        """
        response = self._request("GET", self.base + "/configmaps", params={
            "labelSelector": f"{LABEL}={self.scope},adp.dev/validation-intent=true", "limit": 129,
        })
        if (not isinstance(response, dict) or not isinstance(response.get("items"), list)
                or response.get("metadata", {}).get("continue")
                or len(response.get("items", [])) > 128):
            raise ValidationUnavailable("Validation cleanup inventory unavailable")
        for intent in response.get("items", []):
            metadata, data = intent.get("metadata", {}), intent.get("data", {})
            name = metadata.get("name", "")
            chunks = data.get("chunks", "").split(",")
            if (metadata.get("labels", {}).get(LABEL) != self.scope
                    or not re.fullmatch(r"adp-validation-[a-f0-9]{32}", name) or data.get("pod") != name
                    or not 1 <= len(chunks) <= MAX_PROVIDER_ARCHIVE_BYTES // CHUNK_BYTES
                    or chunks != [f"{name}-{index:04d}" for index in range(len(chunks))]):
                raise ValidationUnavailable("Validation cleanup intent binding differs")
            pod = self._request("GET", self.base + "/pods/" + name)
            if pod is None:
                if self._termination_receipt(name) is None:
                    raise ValidationUnavailable("Validation create outcome requires reconciliation")
            else:
                uid = pod.get("metadata", {}).get("uid")
                if not isinstance(uid, str) or not uid:
                    raise ValidationUnavailable("Validation cleanup Pod identity unavailable")
                self._owned(pod, name, uid)
                self._remove(name, uid)
            # The intent is deleted before its proof; an interruption cannot
            # leave an intent whose removed Pod has lost termination evidence.
            for chunk in [*chunks, name, name + "-terminated"]:
                self._request("DELETE", self.base + "/configmaps/" + chunk)
        return True

    def _termination_receipt(self, name, uid=None):
        receipt = self._request("GET", self.base + "/configmaps/" + name + "-terminated")
        if receipt is None:
            return None
        data = receipt.get("data", {})
        if (receipt.get("metadata", {}).get("labels", {}).get(LABEL) != self.scope
                or receipt.get("immutable") is not True or data.get("pod") != name
                or not isinstance(data.get("uid"), str) or not data["uid"]
                or (uid is not None and data["uid"] != uid)):
            raise ValidationUnavailable("Validation termination receipt differs")
        return receipt

    def _pod(self, name, chunks, check):
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {**self._metadata(name), "finalizers": [FINALIZER]},
            "spec": {
                "restartPolicy": "Never", "activeDeadlineSeconds": check.timeout_seconds,
                # Operator qualification includes enforced NetworkPolicy and
                # kubelet podPidsLimit <= 128. Do not schedule on arbitrary nodes.
                "nodeSelector": {"adp.dev/validation-isolation": "v1"},
                "terminationGracePeriodSeconds": 1, "automountServiceAccountToken": False,
                "enableServiceLinks": False, "hostNetwork": False, "hostPID": False, "hostIPC": False,
                "dnsPolicy": "None", "dnsConfig": {"nameservers": ["127.0.0.1"]},
                "securityContext": {"runAsNonRoot": True, "runAsUser": 65534,
                                    "runAsGroup": 65534, "fsGroup": 65534,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "check", "image": check.image, "imagePullPolicy": "IfNotPresent",
                    "workingDir": "/work",
                    "command": ["/bin/sh", "-c", 'cat /input/* | gzip -dc | tar -xf - -C /work && exec "$@"',
                                "adp-validation", *check.argv],
                    "env": [{"name": name, "value": value} for name, value in
                            [("HOME", "/tmp"), ("BG_CONFIG_DIR", "/tmp/bg"), ("TMPDIR", "/tmp")]],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {kind: {"cpu": str(check.cpus), "memory": f"{check.memory_mb}Mi"}
                                  for kind in ["requests", "limits"]},
                    "volumeMounts": [{"name": "source", "mountPath": "/input", "readOnly": True},
                                     {"name": "work", "mountPath": "/work"}, {"name": "tmp", "mountPath": "/tmp"}],
                }],
                "volumes": [
                    {"name": "source", "projected": {"defaultMode": 292, "sources": [
                        {"configMap": {"name": chunk, "items": [{"key": "data", "path": f"{index:04d}"}]}}
                        for index, chunk in enumerate(chunks)
                    ]}},
                    {"name": "work", "emptyDir": {"medium": "Memory", "sizeLimit": "512Mi"}},
                    {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
                ],
            },
        }

    @staticmethod
    def _terminated(pod):
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if len(statuses) == 1 and statuses[0].get("name") == "check":
            terminated = statuses[0].get("state", {}).get("terminated")
            if isinstance(terminated, dict) and type(terminated.get("exitCode")) is int:
                return terminated
        return None

    def _owned(self, pod, name, uid):
        metadata = pod.get("metadata", {})
        if (metadata.get("name") != name or metadata.get("uid") != uid
                or metadata.get("labels", {}).get(LABEL) != self.scope):
            raise ValidationUnavailable("Validation Pod identity changed")

    def _remove(self, name, uid):
        with self._bounded(15):
            return self._remove_bounded(name, uid)

    def _remove_bounded(self, name, uid):
        """Never remove the evidence finalizer before kubelet-observed exit."""
        path = self.base + "/pods/" + name
        self._request("DELETE", path, body={
            "apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 1,
            "preconditions": {"uid": uid},
        })
        deadline = self.clock() + 15
        while self.clock() < deadline:
            pod = self._request("GET", path)
            if pod is None:
                raise ValidationUnavailable("Validation termination evidence disappeared")
            self._owned(pod, name, uid)
            # An unscheduled, deleting pod cannot subsequently be scheduled.
            never_scheduled = (not pod.get("spec", {}).get("nodeName")
                               and pod["metadata"].get("deletionTimestamp"))
            if self._terminated(pod) is not None or never_scheduled:
                finalizers = pod["metadata"].get("finalizers", [])
                if FINALIZER not in finalizers:
                    raise ValidationUnavailable("Validation evidence fence unavailable")
                if self._termination_receipt(name, uid) is None:
                    self._request("POST", self.base + "/configmaps", body={
                        "apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": self._metadata(name + "-terminated"), "immutable": True,
                        "data": {"pod": name, "uid": uid},
                    })
                self._request("PATCH", path, body=[
                    {"op": "test", "path": "/metadata/uid", "value": uid},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": pod["metadata"]["resourceVersion"]},
                    {"op": "test", "path": "/metadata/finalizers", "value": finalizers},
                    {"op": "remove", "path": f"/metadata/finalizers/{finalizers.index(FINALIZER)}"},
                ])
                while self.clock() < deadline:
                    remaining = self._request("GET", path)
                    if remaining is None:
                        return
                    self._owned(remaining, name, uid)
                    self.sleep(0.1)
                break
            self.sleep(0.1)
        raise ValidationUnavailable("Validation process termination is unconfirmed")

    def run(self, *, check, archive, archive_sha256, commit, cancelled=None):
        with self._bounded(check.timeout_seconds + 30):
            return self._run(check=check, archive=archive, archive_sha256=archive_sha256,
                             commit=commit, cancelled=cancelled)

    def _run(self, *, check, archive, archive_sha256, commit, cancelled=None):
        specification = check.document()
        if "@sha256:" not in check.image:
            raise ValueError("Hosted validation requires a registry-qualified digest")
        if (not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", commit)
                or not re.fullmatch(r"[a-f0-9]{64}", archive_sha256)):
            raise ValueError("Invalid validation source binding")
        if cancelled is not None and cancelled.is_set():
            raise ValidationCancelled("Validation cancelled before execution")
        self._boundary()
        self.recover()
        name = "adp-validation-" + uuid.uuid4().hex
        chunks, pod_uid = [], None
        attempted = False
        cleanup_attempted = False
        cleanup_confirmed = False
        started = self.clock()
        try:
            with tempfile.TemporaryFile() as compressed:
                digest, total = hashlib.sha256(), 0
                with archive.open("rb") as source, gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as output:
                    while content := source.read(65536):
                        total += len(content)
                        if total > MAX_VALIDATION_ARCHIVE_BYTES:
                            raise ValueError("Validation archive exceeds bound")
                        digest.update(content)
                        output.write(content)
                if not total or digest.hexdigest() != archive_sha256:
                    raise ValueError("Validation source archive digest mismatch")
                if compressed.tell() > MAX_PROVIDER_ARCHIVE_BYTES:
                    raise ValueError("Compressed validation source exceeds bound")
                chunks = [f"{name}-{index:04d}" for index in
                          range((compressed.tell() + CHUNK_BYTES - 1) // CHUNK_BYTES)]
                # Commit inventory before uploading source or attempting a Pod.
                self._request("POST", self.base + "/configmaps", body={
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": self._metadata(name, **{"adp.dev/validation-intent": "true"}),
                    "immutable": True, "data": {"pod": name, "chunks": ",".join(chunks),
                        "commit": commit, "archiveSha256": archive_sha256,
                        "specificationDigest": hashlib.sha256(rfc8785.dumps(specification)).hexdigest()},
                })
                compressed.seek(0)
                index = 0
                while content := compressed.read(CHUNK_BYTES):
                    if cancelled is not None and cancelled.is_set():
                        raise ValidationCancelled("Validation cancelled before Pod creation")
                    chunk = chunks[index]
                    index += 1
                    self._request("POST", self.base + "/configmaps", body={
                        "apiVersion": "v1", "kind": "ConfigMap", "metadata": self._metadata(chunk),
                        "immutable": True, "binaryData": {"data": base64.b64encode(content).decode()},
                    })
            if cancelled is not None and cancelled.is_set():
                raise ValidationCancelled("Validation cancelled before Pod creation")
            if self.clock() - started >= check.timeout_seconds:
                raise ValidationUnavailable("Validation source preparation exceeded deadline")
            attempted = True
            pod = self._request("POST", self.base + "/pods", body=self._pod(name, chunks, check))
            pod_uid = pod["metadata"]["uid"]
            self._owned(pod, name, pod_uid)
            output, stop, terminated, image_id = b"", None, None, None
            while self.clock() - started < check.timeout_seconds:
                if cancelled is not None and cancelled.is_set():
                    stop = "cancelled"
                    break
                pod = self._request("GET", self.base + "/pods/" + name)
                if pod is None:
                    raise ValidationUnavailable("Validation Pod disappeared")
                self._owned(pod, name, pod_uid)
                statuses = pod.get("status", {}).get("containerStatuses", [])
                if statuses and statuses[0].get("containerID"):
                    image_id = statuses[0].get("imageID")
                    output = self._request("GET", self.base + f"/pods/{name}/log",
                        params={"container": "check", "limitBytes": check.max_output_bytes + 1},
                        raw=True, maximum=check.max_output_bytes + 1)
                    if len(output) > check.max_output_bytes:
                        stop = "output_limit"
                        break
                terminated = self._terminated(pod)
                if terminated is not None:
                    if not isinstance(image_id, str) or not image_id or len(image_id) > 1024:
                        raise ValidationUnavailable("Validation runtime image identity unavailable")
                    break
                self.sleep(0.25)
            else:
                stop = "timeout"
            cleanup_attempted = True
            self._remove(name, pod_uid)
            cleanup_confirmed = True
            text = output.decode("utf-8", errors="replace")
            if len(text.encode()) > check.max_output_bytes:
                stop = stop or "output_limit"
                text = text.encode()[:check.max_output_bytes].decode("utf-8", errors="ignore")
            passed = stop is None and terminated is not None and terminated["exitCode"] == 0
            return {
                "check": check.name, "commit": commit, "archiveSha256": archive_sha256,
                "specificationDigest": hashlib.sha256(rfc8785.dumps(specification)).hexdigest(),
                "environmentDigest": hashlib.sha256(rfc8785.dumps({
                    "backend": "kubernetes-isolated-v1", "namespace": self.namespace,
                    "image": check.image, "runtimeImage": image_id, "network": "none",
                    "isolationRevision": "v1",
                    "uid": 65534, "memory_mb": check.memory_mb, "cpus": check.cpus,
                })).hexdigest(),
                "status": "passed" if passed else "failed",
                "exitCode": terminated["exitCode"] if terminated else None,
                "reason": stop or ("completed" if passed else "process_failed"),
                "durationSeconds": round(self.clock() - started, 3), "output": text,
                "executionIdentity": {"backend": "kubernetes", "podUid": pod_uid},
            }
        finally:
            if pod_uid is not None and not cleanup_confirmed and not cleanup_attempted:
                self._remove(name, pod_uid)
                cleanup_confirmed = True
            # A lost Pod-create response retains its intent/source for recovery.
            # GET 404 immediately after a timeout cannot prove no delayed create.
            if not attempted or cleanup_confirmed:
                for chunk in [*chunks, name, name + "-terminated"]:
                    self._request("DELETE", self.base + "/configmaps/" + chunk)

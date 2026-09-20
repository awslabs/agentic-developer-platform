"""Disposable worker loss, fenced by the authenticated graph and Kubernetes UID."""

import json
import re
from types import SimpleNamespace

from .definitions import DEFINITION_HASH
from .http import Unsupported, Client
from .kubernetes import ScopedKubernetes, KubernetesUnavailable


class WorkerProvider:
    kind = "qualification-worker"

    def __init__(self, session):
        self.session = session
        self.node = None

    @classmethod
    def for_config(cls, config, manifest):
        return cls(
            SimpleNamespace(
                config=config,
                manifest=manifest,
                client=Client(config, manifest),
                inventory=SimpleNamespace(qualification_id=None),
            )
        )

    def _binding(self, resource):
        session = self.session
        qualification_id = (
            session.inventory.qualification_id or resource["qualification_id"]
        )
        if not re.fullmatch(r"q-[a-z0-9-]{8,50}", qualification_id):
            raise Unsupported("invalid worker qualification identity")
        for key in (
            "flow_id",
            "node_id",
            "invocation_id",
            "pod_name",
            "pod_uid",
            "job_name",
            "job_uid",
            "namespace",
        ):
            if key in resource and not re.fullmatch(
                r"[A-Za-z0-9_-]{1,253}", resource[key]
            ):
                raise Unsupported("invalid worker resource identity")
        graph = session.client.get(f"/orchestration/flows/{resource['flow_id']}")
        if graph["slug"] != qualification_id:
            raise Unsupported("worker is not in this qualification flow")
        plans = session.client.get(f"/orchestration/flows/{resource['flow_id']}/plans")
        current = [p for p in plans if p["superseded_at"] is None]
        if (
            len(current) != 1
            or current[0]["plan_document"].get("spec_revision") != DEFINITION_HASH
        ):
            raise Unsupported("worker fixture plan changed")
        if "plan_hash" in resource and (
            current[0]["plan_hash"],
            current[0]["version"],
        ) != (resource["plan_hash"], resource["plan_version"]):
            raise Unsupported("worker accepted authority changed")
        node = next(n for n in graph["nodes"] if n["id"] == resource["node_id"])
        history = node.get("execution_history") or {}
        if not history.get("history_complete") or not any(
            r["invocation_id"] == resource["invocation_id"] for r in history["runs"]
        ):
            raise Unsupported("worker's authenticated lineage is unavailable")
        invocation = session.client.get(
            "/me/agent-invocations/" + resource["invocation_id"]
        )
        if (
            invocation["repo"] != session.config.repository
            or str(invocation["issue_number"]) != str(node["issue_ref"])
            or invocation["correlation_id"] != node["run_id"]
        ):
            raise Unsupported(
                "worker invocation does not match the exact current story"
            )
        if resource.get("job_name") and invocation["run_id"] != resource["job_name"]:
            raise Unsupported("pod is not in the authenticated invocation's job")
        return node, invocation

    def create(self, *, intended_identity, ownership_tags, idempotency_token):
        session = self.session
        if (
            intended_identity != session.inventory.qualification_id + "/worker-loss"
            or ownership_tags
            != session.config.ownership_tags(session.inventory.qualification_id)
        ):
            raise ValueError("invalid disposable worker fixture")
        if self.node is None or not self.node.get("activity"):
            raise Unsupported("no live fixture worker")
        resource = {
            "qualification_id": session.inventory.qualification_id,
            "flow_id": session.flow_id,
            "node_id": self.node["id"],
            "invocation_id": self.node["activity"]["invocation_id"],
            "plan_hash": session.accepted["plan_hash"],
            "plan_version": session.accepted["plan_version"],
        }
        _, invocation = self._binding(resource)
        job_name = invocation.get("run_id") or ""
        if invocation["liveness"] != "live" or not re.fullmatch(
            r"agent-scaledjob-[a-z0-9-]+", job_name
        ):
            raise Unsupported("worker is not a live disposable ScaledJob")
        target = session.manifest.runtime["worker"]
        kube = ScopedKubernetes(session.client, target.cluster)
        if target.kind != "scaledjob":
            raise Unsupported("worker loss requires a disposable ScaledJob")
        factory = kube.get(
            f"/apis/keda.sh/v1alpha1/namespaces/{target.namespace}/scaledjobs/{target.deployment}"
        )
        job = kube.get(f"/apis/batch/v1/namespaces/{target.namespace}/jobs/{job_name}")
        if not any(
            r["kind"] == "ScaledJob"
            and r["name"] == target.deployment
            and r["uid"] == factory["metadata"]["uid"]
            and r.get("controller")
            for r in job["metadata"]["ownerReferences"]
        ):
            raise Unsupported("job is not owned by the registered worker factory")
        pods = kube.get(
            f"/api/v1/namespaces/{target.namespace}/pods?labelSelector=job-name%3D{job_name}"
        )["items"]
        if len(pods) != 1:
            raise Unsupported("disposable job does not resolve to one pod")
        pod = pods[0]
        if not any(
            r["kind"] == "Job" and r["uid"] == job["metadata"]["uid"]
            for r in pod["metadata"]["ownerReferences"]
        ):
            raise Unsupported("pod job UID mismatch")
        resource.update(
            namespace=target.namespace,
            pod_name=pod["metadata"]["name"],
            pod_uid=pod["metadata"]["uid"],
            job_name=job_name,
            job_uid=job["metadata"]["uid"],
        )
        return json.dumps(resource, sort_keys=True)

    def find(self, *, intended_identity, idempotency_token):
        raise Unsupported(
            "ambiguous worker adoption must be reconciled from the saved authenticated lineage"
        )

    def read_tags(self, resource_id):
        resource = json.loads(resource_id)
        self._binding(resource)
        target = self.session.manifest.runtime["worker"]
        if resource["namespace"] != target.namespace:
            return None
        kube = ScopedKubernetes(self.session.client, target.cluster)
        try:
            pod = kube.get(
                f"/api/v1/namespaces/{target.namespace}/pods/{resource['pod_name']}"
            )
        except KubernetesUnavailable as exc:
            if exc.status_code == 404:
                return self.session.config.ownership_tags(resource["qualification_id"])
            raise
        if pod["metadata"]["uid"] != resource["pod_uid"]:
            return None
        self._pod_owner(kube, resource, pod)
        return self.session.config.ownership_tags(resource["qualification_id"])

    @staticmethod
    def _pod_owner(kube, resource, pod):
        job = kube.get(
            f"/apis/batch/v1/namespaces/{resource['namespace']}/jobs/{resource['job_name']}"
        )
        if job["metadata"]["uid"] != resource["job_uid"] or not any(
            r["kind"] == "Job" and r["uid"] == resource["job_uid"]
            for r in pod["metadata"]["ownerReferences"]
        ):
            raise Unsupported("pod is not owned by the authenticated job UID")

    def inject(self, name, resource_id):
        if name != "worker-loss":
            raise Unsupported("only disposable worker loss uses this provider")
        resource = json.loads(resource_id)
        _, invocation = self._binding(resource)
        if invocation["liveness"] != "live":
            raise Unsupported("worker already exited; loss was not injected")
        target = self.session.manifest.runtime["worker"]
        if resource["namespace"] != target.namespace:
            raise Unsupported("worker namespace changed")
        kube = ScopedKubernetes(self.session.client, target.cluster)
        path = f"/api/v1/namespaces/{target.namespace}/pods/{resource['pod_name']}"
        pod = kube.get(path)
        if (
            pod["metadata"]["uid"] != resource["pod_uid"]
            or pod["status"]["phase"] != "Running"
        ):
            raise Unsupported("worker pod generation changed or is not running")
        self._pod_owner(kube, resource, pod)
        # A UID precondition prevents name reuse from deleting a replacement.
        # Only this Pod object, never the Job, ScaledJob, deployment or queue.
        response = kube.request(
            "DELETE",
            path,
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": resource["pod_uid"]},
                "gracePeriodSeconds": 0,
            },
        )
        return {
            "pod_uid": resource["pod_uid"],
            "invocation_id": resource["invocation_id"],
            "api_response": response,
            "before": pod,
            "scope": "single-inventoried-pod",
        }

    def delete(self, resource_id):
        # Cleanup must not turn a still-running worker into another fault.
        resource = json.loads(resource_id)
        self._binding(resource)
        target = self.session.manifest.runtime["worker"]
        if resource["namespace"] != target.namespace:
            raise Unsupported("worker namespace changed")
        try:
            pod = ScopedKubernetes(self.session.client, target.cluster).get(
                f"/api/v1/namespaces/{target.namespace}/pods/{resource['pod_name']}"
            )
        except KubernetesUnavailable as exc:
            if exc.status_code == 404:
                return
            raise
        if pod["metadata"]["uid"] != resource["pod_uid"]:
            raise Unsupported("replacement pod is not owned by this fixture")
        raise Unsupported(
            "worker still exists; cleanup never stops an additional worker"
        )

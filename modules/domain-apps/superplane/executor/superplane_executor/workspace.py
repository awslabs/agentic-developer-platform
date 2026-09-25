"""Scoped Kubernetes REST access from one explicit projected workspace credential."""

import base64
import os
import ssl
import stat
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from harness_jobs.identity import OperationRefused


class Workspace:
    def __init__(self, directory, management_endpoint):
        self.directory = Path(directory)
        self.management_endpoint = management_endpoint.rstrip("/")
        if not self.directory.is_absolute() or not self.management_endpoint.startswith(
            "https://"
        ):
            raise ValueError(
                "workspace credential mount and management endpoint exclusion required"
            )

    def credentials(self, operation, target):
        try:
            path = self.directory / (operation.grant.lease.workspace_id + ".kubeconfig")
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("not a credential file")
                raw = source.read(2**20 + 1)
            if len(raw) > 2**20:
                raise ValueError("oversize credential")
            data = yaml.safe_load(raw)
            if any(len(data[name]) != 1 for name in ("clusters", "contexts", "users")):
                raise ValueError("ambiguous credential")
            cluster, context, user = (
                data[name][0] for name in ("clusters", "contexts", "users")
            )
            if (
                data["current-context"] != target["cluster_arn"]
                or context["name"] != data["current-context"]
                or context["context"]
                != {
                    "cluster": cluster["name"],
                    "user": user["name"],
                    "namespace": target["namespace"],
                }
                or set(cluster["cluster"]) != {"server", "certificate-authority-data"}
                or set(user["user"]) != {"token"}
                or not user["user"]["token"]
                or cluster["cluster"]["server"] != target["endpoint"]
                or target["endpoint"].rstrip("/") == self.management_endpoint
            ):
                raise ValueError("workspace credential boundary mismatch")
            ca = base64.b64decode(
                cluster["cluster"]["certificate-authority-data"], validate=True
            ).decode()
            tls = ssl.create_default_context(cadata=ca)
            return user["user"]["token"], tls
        except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError):
            raise OperationRefused(
                "workspace credential unavailable or refused"
            ) from None

    async def request(
        self, operation, target, method, path, *, body=None, headers=None
    ):
        token, tls = self.credentials(operation, target)
        # Call sites construct paths from the verified namespace and fixed API resources.
        async with httpx.AsyncClient(
            verify=tls, trust_env=False, follow_redirects=False, timeout=15
        ) as client:
            response = await client.request(
                method,
                target["endpoint"].rstrip("/") + path,
                json=body,
                headers={"Authorization": "Bearer " + token, **(headers or {})},
            )
            if len(response.content) > 4 * 2**20:
                raise OperationRefused("workspace response too large")
            return response

    async def verify(self, operation, target):
        ns = quote(target["namespace"], safe="")
        response = await self.request(
            operation, target, "GET", "/api/v1/namespaces/" + ns
        )
        if (
            response.status_code != 200
            or response.json().get("status", {}).get("phase") != "Active"
        ):
            raise OperationRefused("workspace namespace unavailable")
        for resource, root in (
            ("nodepools", "/apis/superplane.ai/v1"),
            ("superplanenodes", f"/apis/superplane.ai/v1/namespaces/{ns}"),
        ):
            response = await self.request(
                operation,
                target,
                "GET",
                f"{root}/{resource}?limit=1",
            )
            if response.status_code != 200:
                raise OperationRefused("workspace CRDs unavailable")

    @staticmethod
    def path(target, kind, name=""):
        roots = {
            "Job": ("/apis/batch/v1", "jobs"),
            "Deployment": ("/apis/apps/v1", "deployments"),
            "Service": ("/api/v1", "services"),
            "Pod": ("/api/v1", "pods"),
            "Secret": ("/api/v1", "secrets"),
        }
        root, resource = roots[kind]
        path = f"{root}/namespaces/{quote(target['namespace'], safe='')}/{resource}"
        return path + ("/" + quote(name, safe="") if name else "")

    def objects(self, operation, target, plan):
        spec = plan.data["workload"]
        labels = {"superplane.ai/capacity": plan.cluster_name}
        resources = {"cpu": spec["cpu"], "memory": spec["memory"]}
        if spec["gpu_count"]:
            resources["nvidia.com/gpu"] = str(spec["gpu_count"])
        container = {
            "name": "workload",
            "image": spec["image"],
            "command": spec["command"],
            "args": spec["args"],
            "resources": {"requests": resources, "limits": resources},
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "runAsNonRoot": True,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        }
        pod = {
            "automountServiceAccountToken": False,
            "securityContext": {"fsGroup": 65532},
            "nodeSelector": labels,
            "tolerations": [
                {
                    "key": "superplane.ai/capacity",
                    "operator": "Equal",
                    "value": plan.cluster_name,
                    "effect": "NoSchedule",
                }
            ],
            "containers": [container],
            "restartPolicy": "Never" if spec["kind"] == "batch" else "Always",
        }
        template = {"metadata": {"labels": labels}, "spec": pod}
        metadata = {
            "name": spec["name"],
            "namespace": target["namespace"],
            "labels": labels,
        }
        if "controller_deployment_id" in operation.request.parameters:
            # Ownership describes the workload, not its capacity selector. Adding
            # this label to nodeSelector would require the EKS nodes to carry it.
            ownership = {
                "superplane.io/workspace": operation.grant.lease.workspace_id,
                "superplane.io/component": "model-serving"
                if spec["kind"] == "serving"
                else "batch",
            }
            metadata["labels"] = {**labels, **ownership}
            template["metadata"]["labels"] = {**labels, **ownership}
            metadata["annotations"] = {
                "superplane.io/deployment": operation.request.parameters[
                    "controller_deployment_id"
                ],
                "superplane.io/approved-request": operation.plan_digest,
            }
        if spec["kind"] == "batch":
            return [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": metadata,
                    "spec": {
                        "backoffLimit": 0,
                        "activeDeadlineSeconds": max(
                            1,
                            min(
                                operation.max_runtime_seconds,
                                int(
                                    (
                                        operation.grant.lease.runtime_deadline
                                        - datetime.now(UTC)
                                    ).total_seconds()
                                ),
                            ),
                        ),
                        "template": template,
                    },
                }
            ]
        pod["volumes"] = [
            {
                "name": "auth",
                "secret": {"secretName": spec["auth_secret"], "defaultMode": 0o440},
            }
        ]
        container["volumeMounts"] = [
            {"name": "auth", "mountPath": "/run/superplane-auth", "readOnly": True}
        ]
        container["env"] = [
            {
                "name": "SUPERPLANE_AUTH_TOKEN_FILE",
                "value": "/run/superplane-auth/token",
            }
        ]
        container["ports"] = [{"containerPort": spec["port"], "name": "http"}]
        return [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": metadata,
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": template,
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": metadata,
                "spec": {
                    "type": "ClusterIP",
                    "selector": labels,
                    "ports": [
                        {"name": "http", "port": spec["port"], "targetPort": "http"}
                    ],
                },
            },
        ]

    async def apply(self, operation, target, plan, authorize, *, record_created=None):
        governed = "controller_deployment_id" in operation.request.parameters
        if governed and record_created is None:
            raise OperationRefused("governed workload requires durable UID capture")
        references = []
        for obj in self.objects(operation, target, plan):
            await authorize()
            response = await self.request(
                operation, target, "POST", self.path(target, obj["kind"]), body=obj
            )
            # A duplicate intent is handled by the shared executor. An unexpected
            # existing object cannot authorize adopting or patching someone else's work.
            if response.status_code != 201:
                raise OperationRefused(
                    "workspace workload creation uncertain or refused"
                )
            observed = response.json()
            metadata = observed.get("metadata", {})
            uid = metadata.get("uid")
            if (
                metadata.get("namespace") != target["namespace"]
                or metadata.get("name") != obj["metadata"]["name"]
                or not isinstance(uid, str)
                or not uid
                or ":" in uid
            ):
                raise OperationRefused("created workload UID response is invalid")
            reference = self.reference(obj["kind"], observed)
            if record_created is not None:
                # Persist the provider response itself before another write or
                # later GET can replace its original identity with a namesake.
                await record_created(reference)
            references.append(reference)
        return references

    @staticmethod
    def reference(kind, obj):
        metadata = obj["metadata"]
        return "kubernetes:" + ":".join(
            (kind, metadata["namespace"], metadata["name"], metadata["uid"])
        )

    async def ready_nodes(self, operation, target, plan, instances):
        # Preserve provider-observed location, not merely the instance-ID suffix.
        # A label selector is a query hint, not evidence of node ownership.
        import re

        try:
            workspace_id = operation.grant.lease.workspace_id
            approved_regions = {binding["region"] for binding in plan.region_bindings}
            expected = {}
            for instance in instances:
                instance_id = instance["InstanceId"]
                region = instance["SuperplaneRegion"]
                zone = instance["Placement"]["AvailabilityZone"]
                if (
                    not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", instance_id)
                    or region not in approved_regions
                    or not re.fullmatch(
                        re.escape(region) + r"(?:[a-z]|-[a-z0-9-]+)", zone
                    )
                ):
                    return False
                provider_id = f"aws:///{zone}/{instance_id}"
                if provider_id in expected:
                    return False
                expected[provider_id] = (region, zone)
            if (
                len(expected) != plan.data["node_count"]
                or len({region for region, _ in expected.values()}) != 1
            ):
                return False
        except (KeyError, TypeError, AttributeError):
            return False
        selector = quote("superplane.ai/capacity=" + plan.cluster_name, safe="")
        response = await self.request(
            operation, target, "GET", "/api/v1/nodes?labelSelector=" + selector
        )
        if response.status_code != 200:
            return False
        nodes = response.json().get("items", [])
        if not isinstance(nodes, list) or len(nodes) != plan.data["node_count"]:
            return False
        # A joined, Ready node is not yet a usable GPU node: the device plugin
        # publishes allocatable nvidia.com/gpu only once the driver/runtime/CNI
        # stack on that node is actually working. Requiring it here, in the same
        # check that gates workload admission, is what turns "the node registered"
        # into "the node can run the requested GPU workload" -- a CPU-only batch
        # workload (gpu_count 0) is unaffected.
        required_gpus = plan.data["workload"]["gpu_count"] or 0
        observed = set()
        try:
            for node in nodes:
                provider_id = node["spec"]["providerID"]
                if provider_id not in expected or provider_id in observed:
                    return False
                region, zone = expected[provider_id]
                labels = node["metadata"]["labels"]
                if any(
                    labels.get(key) != value
                    for key, value in {
                        "superplane.ai/capacity": plan.cluster_name,
                        "superplane.ai/workspace": workspace_id,
                        "topology.kubernetes.io/region": region,
                        "topology.kubernetes.io/zone": zone,
                    }.items()
                ):
                    return False
                if not any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in node.get("status", {}).get("conditions", [])
                ) or (
                    required_gpus > 0 and self._allocatable_gpus(node) < required_gpus
                ):
                    return False
                observed.add(provider_id)
            return observed == set(expected)
        except (KeyError, TypeError, AttributeError):
            return False

    @staticmethod
    def _allocatable_gpus(node):
        import re
        from decimal import Decimal, DecimalException

        status = node.get("status")
        allocatable = status.get("allocatable") if isinstance(status, dict) else None
        raw = (
            allocatable.get("nvidia.com/gpu", "0")
            if isinstance(allocatable, dict)
            else "0"
        )
        if type(raw) not in {str, int} or len(str(raw)) > 64:
            return 0
        match = re.fullmatch(
            r"([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))([eE][+-]?[0-9]+|[numkMGTPE]|[KMGTPE]i)?",
            str(raw),
        )
        if match is None:
            return 0
        try:
            number, unit = match.groups()
            value = Decimal(number)
            if unit:
                if unit.endswith("i"):
                    value *= Decimal(1024) ** ("KMGTPE".index(unit[0]) + 1)
                elif unit[0] in "eE" and len(unit) > 1:
                    value *= Decimal(10) ** int(unit[1:])
                else:
                    value *= (
                        Decimal(10)
                        ** {
                            "n": -9,
                            "u": -6,
                            "m": -3,
                            "k": 3,
                            "M": 6,
                            "G": 9,
                            "T": 12,
                            "P": 15,
                            "E": 18,
                        }[unit]
                    )
            return (
                int(value)
                if 0 <= value <= 2**63 - 1 and value == value.to_integral_value()
                else 0
            )
        except (DecimalException, ValueError, OverflowError):
            return 0

    async def workload_ready(
        self, operation, target, plan, *, known_references=None, authorize=None
    ):
        spec = plan.data["workload"]
        kind = "Job" if spec["kind"] == "batch" else "Deployment"
        governed = operation is not None and (
            "controller_deployment_id" in operation.request.parameters
        )
        if governed and (known_references is None or authorize is None):
            raise OperationRefused("readiness requires original workload authority")

        def original(obj, object_kind):
            metadata = obj.get("metadata", {})
            prefix = f"kubernetes:{object_kind}:{target['namespace']}:{spec['name']}:"
            uid = metadata.get("uid")
            if (
                metadata.get("namespace") != target["namespace"]
                or metadata.get("name") != spec["name"]
                or not isinstance(uid, str)
                or not uid
                or ":" in uid
                or {r for r in known_references if r.startswith(prefix)}
                != {prefix + uid}
                or metadata.get("deletionTimestamp") is not None
                or metadata.get("annotations", {}).get("superplane.io/deployment")
                != operation.request.parameters["controller_deployment_id"]
                or metadata.get("annotations", {}).get("superplane.io/approved-request")
                != operation.plan_digest
            ):
                raise OperationRefused("original workload UID readiness unavailable")
            return (
                uid,
                metadata.get("generation"),
                metadata.get("resourceVersion"),
            )

        response = await self.request(
            operation, target, "GET", self.path(target, kind, spec["name"])
        )
        if response.status_code != 200:
            return False
        obj = response.json()
        identity = original(obj, kind) if governed else None
        if governed:
            containers = (
                obj.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            )
            if len(containers) != 1 or any(
                containers[0].get(key, [] if key == "args" else None) != value
                for key, value in {
                    "name": "workload",
                    "image": spec["image"],
                    "command": spec["command"],
                    "args": spec["args"],
                }.items()
            ):
                raise OperationRefused("approved workload readiness changed")
        if (
            obj.get("metadata", {}).get("labels", {}).get("superplane.ai/capacity")
            != plan.cluster_name
        ):
            return False
        if kind == "Job":
            return obj.get("status", {}).get("succeeded", 0) == 1
        status = obj.get("status", {})
        if (
            status.get("observedGeneration", 0)
            < obj.get("metadata", {}).get("generation", 1)
            or status.get("availableReplicas", 0) < 1
        ):
            return False

        async def service_identity():
            response = await self.request(
                operation, target, "GET", self.path(target, "Service", spec["name"])
            )
            if response.status_code != 200:
                raise OperationRefused("original serving Service unavailable")
            service = response.json()
            identity = original(service, "Service")
            service_spec = service.get("spec", {})
            ports = service_spec.get("ports", [])
            if (
                service_spec.get("type") != "ClusterIP"
                or service_spec.get("selector")
                != {"superplane.ai/capacity": plan.cluster_name}
                or len(ports) != 1
                or any(
                    ports[0].get(key) != value
                    for key, value in {
                        "name": "http",
                        "port": spec["port"],
                        "targetPort": "http",
                    }.items()
                )
            ):
                raise OperationRefused("approved serving route changed")
            return identity

        async def unchanged():
            response = await self.request(
                operation, target, "GET", self.path(target, kind, spec["name"])
            )
            if (
                response.status_code != 200
                or original(response.json(), kind) != identity
            ):
                raise OperationRefused("workload changed during readiness")
            if await service_identity() != service:
                raise OperationRefused("Service changed during readiness")
            await authorize()

        if governed:
            service = await service_identity()
            await authorize()
        secret = await self.request(
            operation, target, "GET", self.path(target, "Secret", spec["auth_secret"])
        )
        if secret.status_code != 200:
            return False
        token = (
            base64.b64decode(
                secret.json().get("data", {}).get("token", ""), validate=True
            )
            .decode()
            .strip()
        )
        if len(token) < 32:
            return False
        # Serving images must implement this explicit token header contract. Probe
        # the same private Service using Kubernetes's scoped service proxy; only a
        # 401/403 without the token followed by 200 with it establishes readiness.
        path = (
            self.path(target, "Service", "http:" + spec["name"] + ":http")
            + "/proxy/healthz"
        )
        denied = await self.request(operation, target, "GET", path)
        if denied.status_code not in (401, 403):
            return False
        if governed:
            await unchanged()
        admitted = await self.request(
            operation, target, "GET", path, headers={"X-Superplane-Token": token}
        )
        if governed:
            await unchanged()
        return admitted.status_code == 200

    async def delete(
        self, operation, target, plan, authorize, *, known_references=None
    ):
        observed_objects = []
        for obj in reversed(self.objects(operation, target, plan)):
            path = self.path(target, obj["kind"], obj["metadata"]["name"])
            observed = await self.request(operation, target, "GET", path)
            if observed.status_code == 404:
                continue
            if (
                observed.status_code != 200
                or observed.json()
                .get("metadata", {})
                .get("labels", {})
                .get("superplane.ai/capacity")
                != plan.cluster_name
            ):
                raise OperationRefused("workload ownership unavailable")
            uid = observed.json()["metadata"]["uid"]
            if "controller_deployment_id" in operation.request.parameters:
                reference = self.reference(obj["kind"], observed.json())
                prefix = reference.rsplit(":", 1)[0] + ":"
                originals = {
                    item for item in (known_references or ()) if item.startswith(prefix)
                }
                if originals != {reference}:
                    raise OperationRefused(
                        "original workload UID ownership unavailable"
                    )
            observed_objects.append((path, uid))
        # Verify every object before the first delete. UID preconditions also
        # refuse replacement between this read and the actual provider mutation.
        for path, uid in observed_objects:
            await authorize()
            response = await self.request(
                operation,
                target,
                "DELETE",
                path,
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "propagationPolicy": "Foreground",
                    "preconditions": {"uid": uid},
                },
            )
            if response.status_code not in (200, 202, 404):
                raise OperationRefused("workload removal uncertain")

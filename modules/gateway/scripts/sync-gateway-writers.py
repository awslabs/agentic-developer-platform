"""Release every registered gateway application writer without replacing its spec.

The ordinary gateway and optional restricted chat listener are maintained here.
Other Deployments require ADP_GATEWAY_WRITER_INVENTORY in the protected deployment
environment: [{"deployment": "operator-app", "uid": "observed-kubernetes-uid",
"container": "gateway", "entrypoint": "gateway"}]. Entrypoint is gateway or
chat-tls. Bindings are exact, durable identities, not image/name selectors.

--preview is read-only. Image and worker-trust updates use JSON Patch with UID,
generation and complete spec tests. Trust is copied only from the existing worker
authority ConfigMap; no Secrets, TLS resources, authority flags or schedules are
read or changed. Unknown application processes fail closed, even if their
current flags are disabled: a later configuration rollout can enable cleanup.
--verify-only never patches and honors --timeout across initialization, HPA
readiness and proven process turnover before attesting a stable full inventory.
"""

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PRIMARY = "bedrockgateway"
MAINTAINED = {
    PRIMARY: {"container": PRIMARY, "entrypoint": "gateway", "enrollment": "primary"},
    "chat-sandbox-gateway": {
        "container": "chat-sandbox-gateway",
        "entrypoint": "chat-tls",
        "enrollment": "maintained-chat",
    },
}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
SOURCE_FILES = ("src/app.py", "src/orchestration/work_admission.py", "src/orchestration/work_claims.py")
TRUST_KEYS = ("AGENT_WORKER_IMAGE_DIGESTS", "ADP_TASK_WORKER_IMAGE_DIGESTS")
TRUST_READ = """import hashlib,json,os
keys=('AGENT_WORKER_IMAGE_DIGESTS','ADP_TASK_WORKER_IMAGE_DIGESTS')
print(json.dumps({'worker_trust_sha256':hashlib.sha256(json.dumps(
 {k:os.environ.get(k,'') for k in keys},sort_keys=True,separators=(',',':')).encode()).hexdigest()}))
"""
INACTIVE_BOOTSTRAP_READ = """import json,os
flags=('AGENT_AUTHORITY_ENABLED','ADP_AGENT_AUTHORITY_ENABLED','ADP_WORK_CLAIMS_ENABLED','ADP_RUN_TASKS_ENABLED',
 'ADP_TASK_API_ADMISSION_ENABLED','ADP_TASK_API_READ_ENABLED','ADP_TASK_API_WORKER_ENABLED','ADP_TASK_API_RECOVERY_ENABLED',
 'ADP_TASK_API_HUMAN_ENABLED')
trust=('AGENT_WORKER_IMAGE_DIGESTS','ADP_TASK_WORKER_IMAGE_DIGESTS')
references=('AGENT_WORKER_ROLE_ARN','AGENT_TASK_SOURCE_ROLE_ARN',
 'AGENT_DISPATCH_QUEUE_URL','ADP_RUN_TASK_QUEUE_URL','ADP_TASK_API_QUEUE_URL')
print(json.dumps({
 'authority_disabled':all(os.environ.get(k,'').lower() in ('','false') for k in flags),
 'worker_trust_unconfigured':all(os.environ.get(k,'') in ('','disabled') for k in trust),
 'worker_references_absent':all(not os.environ.get(k,'') for k in references)}))
"""
RUNTIME_READ = """import hashlib,json,os
from pathlib import Path
print(json.dumps({
 'authority_enabled':os.environ.get('AGENT_AUTHORITY_ENABLED','false').lower()=='true',
 'claims_enabled':os.environ.get('ADP_WORK_CLAIMS_ENABLED','false').lower()=='true',
 'source_hashes':{p:hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest() for p in
 ['src/app.py','src/orchestration/work_admission.py','src/orchestration/work_claims.py']}
}))
"""


def command(args, *, timeout=60):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=timeout).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        permission_check = (
            args[:1] == ["kubectl"] and args[1:2] == ["-n"] and args[3:5] == ["auth", "can-i"]
            and (len(args) == 7 or (len(args) == 8 and args[7] == "--subresource=exec"))
        )
        if permission_check:
            # kubectl reports an ordinary authorization denial with stdout=no
            # and exit 1. Do not mistake authentication/transport errors for a
            # denied verb, or leak their potentially sensitive stderr.
            if isinstance(exc, subprocess.CalledProcessError) and exc.returncode == 1 and (exc.output or "").strip() == "no" and not (exc.stderr or "").strip():
                return "no"
            resource = args[6] + ("/exec" if len(args) == 8 else "")
            raise RuntimeError(
                f"Unable to check release permission: {args[5]} {resource} in namespace {args[2]}; "
                "verify Kubernetes authentication and API connectivity before changing RBAC"
            ) from None
        # Never echo API objects, complete specs or exec output on failure.
        raise RuntimeError(f"Release inspection command failed: {args[0]} {args[1]}") from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bindings(raw):
    if len(raw) > 8192:
        raise ValueError("Writer inventory is too large")
    entries = json.loads(raw)
    if not isinstance(entries, list) or len(entries) > 16:
        raise ValueError("Writer inventory must be an array of at most 16 bindings")
    result = copy.deepcopy(MAINTAINED)
    seen_uids = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"deployment", "uid", "container", "entrypoint"}:
            raise ValueError("Writer binding requires deployment, uid, container and entrypoint")
        if any(not isinstance(entry[k], str) for k in entry):
            raise ValueError("Writer binding fields must be strings")
        name, uid = entry["deployment"], entry["uid"]
        if not NAME.fullmatch(name) or not NAME.fullmatch(entry["container"]):
            raise ValueError("Invalid writer binding name")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,127}", uid):
            raise ValueError("Invalid writer binding UID")
        if name in result or uid in seen_uids:
            raise ValueError("Duplicate or conflicting writer binding")
        if entry["entrypoint"] not in {"gateway", "chat-tls"}:
            raise ValueError("Unsupported writer entrypoint")
        seen_uids.add(uid)
        result[name] = {**entry, "enrollment": "explicit-inventory"}
    return result


def entrypoint(container, registry):
    """Recognize the actual command, never enroll a container by its image alone."""
    image = container.get("image", "")
    gateway_image = image.startswith(registry + "@") or image.startswith(registry + ":")
    argv = container.get("command", []) + container.get("args", [])
    claims_config = any(e.get("name") in {"AGENT_AUTHORITY_ENABLED", "ADP_WORK_CLAIMS_ENABLED"} for e in container.get("env", [])) or any(
        e.get("configMapRef", {}).get("name") in {"bedrockgateway-config", "adp-worker-authority-config"} for e in container.get("envFrom", [])
    )
    if not argv:
        return "gateway" if gateway_image else ("unknown" if claims_config else None)  # Gateway Dockerfile CMD.
    argv = [Path(argv[0]).name, *argv[1:]]
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", argv[0]):
        if len(argv) >= 3 and argv[1:3] == ["-m", "src.agentauth.chat_tls"]:
            return "chat-tls"
        if len(argv) >= 3 and argv[1:3] == ["-m", "controller.worker"]:
            return "controller"  # Shares the image; does not run src.app.
        if gateway_image and argv[1:] in (["-m", "src.gitlab.home_worker"], ["-m", "src.gitlab.home_worker", "--once"]):
            # The optional GitLab module owns this reconciler. It imports its
            # Product models, never src.app or orchestration claim cleanup.
            return "gitlab-reconciler"
        if gateway_image and argv[1:] == ["-m", "src.shared.identity.oauth_setup_worker", "--once"]:
            # The separately deployed CronJob reconciles Identity setup/grants;
            # it never starts src.app or orchestration work-claim cleanup.
            return "identity-recovery"
        if len(argv) >= 3 and argv[1:3] == ["-m", "uvicorn"]:
            argv = argv[2:]
    if argv[0] == "uvicorn" and "src.app:create_app" in argv[1:]:
        return "gateway"
    if argv[0] == "uvicorn" and "src.agentauth.chat_tls:create_app" in argv[1:]:
        return "chat-tls"
    if gateway_image or claims_config or any("src.app" in x or "src.agentauth.chat_tls" in x for x in argv):
        return "unknown"
    return None


def containers(spec):
    return [c for field in ("containers", "initContainers", "ephemeralContainers") for c in spec.get(field, [])]


def active_pod_containers(pod):
    for field, status_field in (
        ("containers", "containerStatuses"),
        ("initContainers", "initContainerStatuses"),
        ("ephemeralContainers", "ephemeralContainerStatuses"),
    ):
        statuses = {s["name"]: s for s in pod.get("status", {}).get(status_field, [])}
        for container in pod["spec"].get(field, []):
            terminated = statuses.get(container["name"], {}).get("state", {}).get("terminated")
            if terminated is not None and (
                field == "ephemeralContainers"
                or (field == "initContainers" and container.get("restartPolicy") != "Always" and terminated.get("exitCode") == 0)
            ):
                continue  # These processes have finished; ordinary init must succeed and cannot be a native sidecar.
            yield field, container


def starting_init(pod, field, container):
    """Only ordinary, pre-application Pod initialization may be waited on."""
    return (
        field == "initContainers"
        and container.get("restartPolicy") != "Always"
        and pod.get("status", {}).get("phase") == "Pending"
        and not any("running" in s.get("state", {}) for s in pod.get("status", {}).get("containerStatuses", []))
    )


def potential_writer(container, spec, registry):
    kind = entrypoint(container, registry)
    if kind == "gitlab-reconciler" and spec.get("serviceAccountName") != "gitlab-home-reconciler":
        return "unknown"  # The exclusion requires the module's dedicated principal.
    # A renamed image running as the gateway principal must not evade discovery.
    return "unknown" if kind is None and spec.get("serviceAccountName") == "gateway-service" else kind


class InventoryTurnoverError(ValueError):
    """A fresh read proved process turnover; bounded retries may observe its successor."""


class WriterStartupPendingError(ValueError):
    """Unknown ordinary init is still active; never attested as a safe writer."""


def runtime_fingerprint(managed, pods):
    return digest(
        {
            "deployments": {
                name: {
                    "uid": d["metadata"]["uid"],
                    "spec": d["spec"],
                    "generation": d["metadata"].get("generation"),
                    "status": {
                        k: d.get("status", {}).get(k) for k in ("observedGeneration", "updatedReplicas", "readyReplicas", "availableReplicas")
                    },
                }
                for name, d in managed.items()
            },
            "pods": sorted(
                [
                    {
                        "uid": p["metadata"]["uid"],
                        "name": p["metadata"]["name"],
                        "deletion": p["metadata"].get("deletionTimestamp"),
                        "spec": p["spec"],
                        "phase": p.get("status", {}).get("phase"),
                        "containers": p.get("status", {}).get("containerStatuses"),
                    }
                    for _, p, _ in pods
                ],
                key=lambda p: p["uid"],
            ),
        }
    )


def source_blobs(gateway_root, revision):
    """Derive evidence from immutable Git blobs, not an arbitrary SHA label."""
    checkout = Path(command(["git", "-C", str(gateway_root), "rev-parse", "--show-toplevel"]))
    if gateway_root.resolve() != (checkout / "modules/gateway").resolve():
        raise ValueError("Gateway root must be modules/gateway in the selected checkout")
    blobs = {}
    for path in SOURCE_FILES:
        try:
            blob = subprocess.check_output(
                ["git", "-C", str(checkout), "show", f"{revision}:modules/gateway/{path}"], stderr=subprocess.PIPE, timeout=30
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Selected source revision is not available in the checkout") from exc
        if (gateway_root / path).read_bytes() != blob:
            raise ValueError("Local gateway source differs from the selected immutable revision")
        blobs[path] = blob
    return blobs


class Writers:
    patch_primary = False

    def __init__(self, *, namespace, image, inventory, source_hashes, timeout=1200):
        self.namespace, self.image = namespace, image
        self.registry = image.split("@", 1)[0]
        self.inventory, self.source_hashes, self.timeout = inventory, source_hashes, timeout

    def kubectl(self, *args, timeout=60):
        return command(["kubectl", "-n", self.namespace, *args], timeout=timeout)

    def permissions(self):
        checks = [(verb, resource) for resource in ("deployments", "replicasets", "pods") for verb in ("get", "list")]
        for verb, resource in checks:
            self.require_permission(verb, resource)
        # RESOURCE/NAME means a named Pod to kubectl, not a subresource. The
        # explicit flag checks the existing exec grant without requiring Pod creation.
        for verb in ("get", "create"):
            self.require_permission(verb, "pods", subresource="exec")

    def require_permission(self, verb, resource, *, subresource=None):
        flags = ["--subresource=" + subresource] if subresource else []
        if self.kubectl("auth", "can-i", verb, resource, *flags) != "yes":
            display_resource = resource + ("/" + subresource if subresource else "")
            raise ValueError(
                f"Release identity lacks {verb} {display_resource} in namespace {self.namespace}; "
                "reconcile the release Role and explicitly enrolled writer grants before retrying "
                "(platform/automation-infra/README.md)"
            )

    def patch_permissions(self, managed):
        for name in managed:
            if name != PRIMARY or self.patch_primary:
                self.require_permission("patch", "deployment/" + name)

    def snapshot(self):
        result = {}
        for kind in ("deployments", "replicasets", "pods"):
            items = json.loads(self.kubectl("get", kind, "-o", "json"))["items"]
            if not isinstance(items, list) or len(items) > 1000:
                raise ValueError("Writer inventory exceeds bounded namespace scan")
            if any(x["metadata"].get("namespace") != self.namespace for x in items):
                raise ValueError("Writer inventory crossed the selected namespace")
            result[kind] = items
        return result

    def inspect(self, snapshot, *, allow_starting_init=False):
        deployments = {x["metadata"]["name"]: x for x in snapshot["deployments"]}
        if PRIMARY not in deployments:
            raise ValueError("Primary gateway is missing")
        managed = {}
        for name, binding in self.inventory.items():
            deployment = deployments.get(name)
            if deployment is None:
                if binding["enrollment"] == "maintained-chat":
                    continue  # Delegated chat is optional, not a fresh install prerequisite.
                raise ValueError(f"Enrolled writer Deployment is missing: {name}")
            meta = deployment["metadata"]
            if not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp"):
                raise ValueError(f"Writer Deployment identity is unavailable: {name}")
            if binding.get("uid", meta["uid"]) != meta["uid"]:
                raise ValueError(f"Writer Deployment UID changed: {name}")
            spec = deployment["spec"]["template"]["spec"]
            target = [x for x in spec["containers"] if x["name"] == binding["container"]]
            if spec.get("serviceAccountName") != "gateway-service" or len(target) != 1:
                raise ValueError(f"Writer service account or container changed: {name}")
            if entrypoint(target[0], self.registry) != binding["entrypoint"]:
                raise ValueError(f"Writer entrypoint changed: {name}")
            if not re.fullmatch(re.escape(self.registry) + r"@sha256:[0-9a-f]{64}", target[0]["image"]):
                raise ValueError(f"Writer image must be a pinned target gateway image: {name}")
            managed[name] = deployment
        for name, deployment in deployments.items():
            for container in containers(deployment["spec"]["template"]["spec"]):
                kind = potential_writer(container, deployment["spec"]["template"]["spec"], self.registry)
                if kind in {None, "controller", "identity-recovery", "gitlab-reconciler"}:
                    continue
                if name not in managed or container["name"] != self.inventory[name]["container"]:
                    raise ValueError(f"Unregistered or uncertain application writer: {name}/{container['name']}")
        replicasets = {x["metadata"]["uid"]: x for x in snapshot["replicasets"]}
        pods = []
        pending_init = []
        for pod in snapshot["pods"]:
            if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
                continue
            for field, container in active_pod_containers(pod):
                kind = potential_writer(container, pod["spec"], self.registry)
                if kind in {None, "controller", "identity-recovery", "gitlab-reconciler"}:
                    continue
                owners = [x for x in pod["metadata"].get("ownerReferences", []) if x.get("controller") and x["kind"] == "ReplicaSet"]
                rs = replicasets.get(owners[0]["uid"]) if len(owners) == 1 else None
                parents = [x for x in (rs or {}).get("metadata", {}).get("ownerReferences", []) if x.get("controller") and x["kind"] == "Deployment"]
                parent = parents[0] if len(parents) == 1 else {}
                name = parent.get("name")
                if name not in managed or parent.get("uid") != managed[name]["metadata"]["uid"]:
                    raise ValueError(f"Unregistered or uncertain active writer pod: {pod['metadata']['name']}")
                if allow_starting_init and starting_init(pod, field, container):
                    pending_init.append(pod["metadata"]["name"])
                    continue  # Scan every remaining process before permitting a bounded startup wait.
                if container["name"] != self.inventory[name]["container"] or kind != self.inventory[name]["entrypoint"]:
                    raise ValueError(f"Unregistered or uncertain active writer pod: {pod['metadata']['name']}")
                pods.append((name, pod, container))  # Include terminating old processes.
        if pending_init:
            raise WriterStartupPendingError(f"Writer initialization is still active: {','.join(sorted(set(pending_init)))}")
        return managed, pods

    def observe(self, name, pod, container):
        observation = json.loads(
            self.kubectl(
                "exec",
                "pod/" + pod["metadata"]["name"],
                "-c",
                container["name"],
                "--",
                "python",
                "-S",
                "-c",
                RUNTIME_READ,
            )
        )
        if (
            set(observation) != {"authority_enabled", "claims_enabled", "source_hashes"}
            or type(observation["authority_enabled"]) is not bool
            or type(observation["claims_enabled"]) is not bool
            or not isinstance(observation["source_hashes"], dict)
            or set(observation["source_hashes"]) != set(SOURCE_FILES)
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in observation["source_hashes"].values())
        ):
            raise ValueError("Invalid bounded writer runtime observation")
        statuses = [x for x in pod.get("status", {}).get("containerStatuses", []) if x["name"] == container["name"]]
        status = statuses[0] if len(statuses) == 1 else {}
        return {
            "deployment": name,
            "enrollment": self.inventory[name]["enrollment"],
            "pod": pod["metadata"]["name"],
            "pod_uid": pod["metadata"]["uid"],
            "container": container["name"],
            "image_id": status.get("imageID", "").removeprefix("docker-pullable://"),
            "terminating": bool(pod["metadata"].get("deletionTimestamp")),
            **observation,
        }

    def observe_stable(self, managed, pods):
        before = runtime_fingerprint(managed, pods)
        try:
            # Bound API load while observing independent pods. map retains inventory
            # order; exiting the pool joins every probe before the after-snapshot.
            with ThreadPoolExecutor(max_workers=4) as pool:
                observations = list(pool.map(lambda item: self.observe(*item), pods))
        except RuntimeError:
            fresh = self.inspect(self.snapshot(), allow_starting_init=True)
            if runtime_fingerprint(*fresh) != before:
                raise InventoryTurnoverError("Writer inventory changed during runtime inspection") from None
            raise  # Stable permission, missing source or exec errors are not rollout retries.
        fresh = self.inspect(self.snapshot(), allow_starting_init=True)
        if runtime_fingerprint(*fresh) != before:
            raise InventoryTurnoverError("Writer inventory changed during runtime inspection")
        return observations

    def patch(self, deployment):
        meta = deployment["metadata"]
        name = meta["name"]
        containers = deployment["spec"]["template"]["spec"]["containers"]
        index = next(i for i, c in enumerate(containers) if c["name"] == self.inventory[name]["container"])
        expected = copy.deepcopy(deployment["spec"])
        expected["template"]["spec"]["containers"][index]["image"] = self.image
        if containers[index]["image"] == self.image:
            return expected
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
            # Status updates advance resourceVersion without changing the desired
            # configuration. Generation and the complete spec still fence a
            # competing configuration update, including a change reverted later.
            {"op": "test", "path": "/metadata/generation", "value": meta["generation"]},
            {"op": "test", "path": "/spec", "value": deployment["spec"]},
            {"op": "replace", "path": f"/spec/template/spec/containers/{index}/image", "value": self.image},
        ]
        # Avoid placing a complete (potentially sensitive) spec in argv or logs.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(patch, file)
            file.flush()
            self.kubectl("patch", "deployment/" + name, "--type=json", "--patch-file", file.name, "-o", "name")
        after = json.loads(self.kubectl("get", "deployment/" + name, "-o", "json"))
        if after["metadata"]["uid"] != meta["uid"] or after["spec"] != expected:
            raise ValueError(f"Writer image-only patch readback changed identity or spec: {name}")
        return expected

    def converged(self, managed, pods, expected_specs):
        for name, deployment in managed.items():
            if name in expected_specs and deployment["spec"] != expected_specs[name]:
                raise ValueError(f"Writer spec changed during rollout: {name}")
            target = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == self.inventory[name]["container"])
            status, replicas = deployment.get("status", {}), deployment["spec"].get("replicas", 1)
            if type(replicas) is not int or replicas < (1 if name == PRIMARY else 0):
                raise ValueError(f"Invalid active writer replica count: {name}")
            if (
                target["image"] != self.image
                or status.get("observedGeneration", 0) < deployment["metadata"].get("generation", 1)
                or any(status.get(k, 0) != replicas for k in ("updatedReplicas", "readyReplicas", "availableReplicas"))
            ):
                return False
            ready = [p for owner, p, _ in pods if owner == name and not p["metadata"].get("deletionTimestamp")]
            if len(ready) < replicas:
                return False
        for _, pod, container in pods:
            statuses = [c for c in pod.get("status", {}).get("containerStatuses", []) if c["name"] == container["name"]]
            if (
                pod.get("status", {}).get("phase") != "Running"
                or len(statuses) != 1
                or statuses[0].get("imageID", "").removeprefix("docker-pullable://") != self.image
                or container["image"] != self.image
                or (not pod["metadata"].get("deletionTimestamp") and not statuses[0].get("ready"))
            ):
                return False
        return True

    def plans(self, managed):
        return [
            {
                "deployment": name,
                "uid": d["metadata"]["uid"],
                "resource_version": d["metadata"]["resourceVersion"],
                "enrollment": self.inventory[name]["enrollment"],
                "entrypoint": self.inventory[name]["entrypoint"],
                "container": self.inventory[name]["container"],
                "spec_sha256": digest(d["spec"]),
                "image_before": next(
                    c["image"] for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == self.inventory[name]["container"]
                ),
            }
            for name, d in managed.items()
        ]

    def run(self, *, preview=False, verify_only=False):
        self.permissions()
        deadline = time.monotonic() + self.timeout
        snapshot = self.snapshot()

        def enrolled_uids(observed):
            return {d["metadata"]["name"]: d["metadata"]["uid"] for d in observed["deployments"] if d["metadata"]["name"] in self.inventory}

        original_uids = enrolled_uids(snapshot)
        while True:
            if enrolled_uids(snapshot) != original_uids:
                raise ValueError("Writer inventory changed during initial verification")
            try:
                # The complete scan still refuses unknown processes before a
                # managed ordinary init is allowed to consume the wait budget.
                # Initial instrumentation can still be running when deployment
                # begins, before we patch auxiliary images. Wait for it under
                # the same deadline as rollout; preview remains an immediate read.
                managed, pods = self.inspect(snapshot, allow_starting_init=not preview)
                break
            except WriterStartupPendingError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(5, max(0, deadline - time.monotonic())))
                snapshot = self.snapshot()
        plans = self.plans(managed)
        report = {
            "namespace": self.namespace,
            "image": self.image,
            "expected_source_hashes": self.source_hashes,
            "deployments": plans,
            "status": "preview" if preview else "verified",
        }
        if preview:
            for attempt in range(3):
                try:
                    report["pods"] = self.observe_stable(managed, pods)
                    report["deployments"] = self.plans(managed)
                    return report
                except InventoryTurnoverError:
                    if attempt == 2:
                        raise
                    managed, pods = self.inspect(self.snapshot())
        expected_specs = {}
        if not verify_only:
            # Primary image rollout is owned by the gateway manifest stage.
            primary = next(p for p in plans if p["deployment"] == PRIMARY)
            if primary["image_before"] != self.image:
                raise ValueError("Primary gateway must already use the selected immutable release")
            self.patch_permissions(managed)
            for name, deployment in managed.items():
                if name != PRIMARY or self.patch_primary:
                    expected_specs[name] = self.patch(deployment)
        while True:
            try:
                managed, pods = self.inspect(self.snapshot(), allow_starting_init=True)
            except WriterStartupPendingError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(5, max(0, deadline - time.monotonic())))
                continue
            if {n: d["metadata"]["uid"] for n, d in managed.items()} != original_uids:
                raise ValueError("Writer inventory changed during release")
            if self.converged(managed, pods, expected_specs):
                try:
                    observations = self.observe_stable(managed, pods)
                except (InventoryTurnoverError, WriterStartupPendingError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(min(5, max(0, deadline - time.monotonic())))
                    continue
                if self.source_hashes is not None and any(p["source_hashes"] != self.source_hashes for p in observations):
                    raise ValueError("Running claim-cleanup source differs from the selected release")
                report["pods"] = observations
                for plan in report["deployments"]:
                    plan["image_after"] = self.image
                return report
            if time.monotonic() >= deadline:
                raise ValueError("Writer release is incomplete, including old terminating processes")
            time.sleep(min(5, max(0, deadline - time.monotonic())))


def approved_worker_trust(data):
    values = {key: data.get(key) for key in TRUST_KEYS}
    value = values[TRUST_KEYS[0]]
    if not isinstance(value, str) or not value or len(value) > 8192 or any(v != value for v in values.values()):
        raise ValueError("Worker authority ConfigMap trust keys are missing or inconsistent")
    parts = value.split(",")
    if value != "disabled" and (len(set(parts)) != len(parts) or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", x) for x in parts)):
        raise ValueError("Worker authority ConfigMap has invalid image trust")
    return values


class WorkerTrust(Writers):
    """Reconcile explicit overrides for the same enrolled writer identities.

    The source ConfigMap is reread throughout the rollout. Existing deployment
    images and every setting except these two env entries remain unchanged.
    Old terminating pods must also leave before worker promotion is safe.
    Gateway bootstrap alone can prove absent, entirely inactive authority;
    shared worker rollout never opts into that read-only exception.
    """

    patch_primary = True

    def __init__(self, *, namespace, image, inventory, timeout=1200, allow_inactive_bootstrap=False):
        super().__init__(namespace=namespace, image=image, inventory=inventory, source_hashes=None, timeout=timeout)
        self.trust = self.authority_uid = None
        self.preview = False
        self.verify_only = False
        self.allow_inactive_bootstrap = allow_inactive_bootstrap
        self.inactive_bootstrap = False

    def run(self, *, preview=False, verify_only=False):
        self.preview = preview
        self.verify_only = verify_only
        result = super().run(preview=preview, verify_only=verify_only)
        if self.inactive_bootstrap:
            result.update(status="inactive_bootstrap", worker_authority_config_present=False, trust_mutations=0)
        return result

    def permissions(self):
        super().permissions()
        self.require_permission("get", "configmap/adp-worker-authority-config")

    def snapshot(self):
        raw = self.kubectl("get", "configmap/adp-worker-authority-config", "--ignore-not-found", "-o", "json")
        if not raw:
            if not self.allow_inactive_bootstrap or self.trust is not None or self.authority_uid is not None:
                raise ValueError("Worker authority ConfigMap is missing or disappeared during rollout")
            # Gateway can precede worker installation. Absence only permits a
            # read-only proof that every running/terminating writer is inactive;
            # permission/transport errors are never converted into absence.
            self.inactive_bootstrap = True
            return super().snapshot()
        if self.inactive_bootstrap:
            raise ValueError("Worker authority ConfigMap appeared during inactive bootstrap verification")
        cm = json.loads(raw)
        trust = approved_worker_trust(cm.get("data", {}))
        uid = cm.get("metadata", {}).get("uid")
        if not uid or cm.get("metadata", {}).get("namespace") != self.namespace:
            raise ValueError("Worker authority ConfigMap identity is unavailable")
        if self.trust is None:
            self.trust, self.authority_uid = trust, uid
        if trust != self.trust or uid != self.authority_uid:
            raise ValueError("Worker authority ConfigMap changed during writer rollout")
        return super().snapshot()

    def inspect(self, snapshot, *, allow_starting_init=False):
        managed, pods = super().inspect(snapshot, allow_starting_init=allow_starting_init)
        for name, deployment in managed.items():
            target = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == self.inventory[name]["container"])
            if any(sum(e.get("name") == key for e in target.get("env", [])) > 1 for key in TRUST_KEYS):
                raise ValueError("Duplicate explicit worker image-trust environment key")
            if not self.preview and target["image"] != self.image:
                raise ValueError("Complete the Gateway image rollout before synchronizing worker trust")
            if not self.inactive_bootstrap:
                self.expected_env(target)
        return managed, pods

    def expected_env(self, container):
        env = copy.deepcopy(container.get("env", []))
        for key, value in self.trust.items():
            indices = [i for i, entry in enumerate(env) if entry.get("name") == key]
            if len(indices) > 1:
                raise ValueError("Duplicate explicit worker image-trust environment key")
            entry = {"name": key, "value": value}
            if indices:
                env[indices[0]] = entry
            else:
                env.append(entry)
        return env

    def patch(self, deployment):
        if self.inactive_bootstrap:
            return copy.deepcopy(deployment["spec"])
        meta, name = deployment["metadata"], deployment["metadata"]["name"]
        original = deployment["spec"]["template"]["spec"]["containers"]
        index = next(i for i, c in enumerate(original) if c["name"] == self.inventory[name]["container"])
        env = self.expected_env(original[index])
        expected = copy.deepcopy(deployment["spec"])
        expected["template"]["spec"]["containers"][index]["env"] = env
        if expected == deployment["spec"]:
            return expected
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
            {"op": "test", "path": "/metadata/generation", "value": meta["generation"]},
            {"op": "test", "path": "/spec", "value": deployment["spec"]},
            {"op": "add", "path": f"/spec/template/spec/containers/{index}/env", "value": env},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(patch, file)
            file.flush()
            self.kubectl("patch", "deployment/" + name, "--type=json", "--patch-file", file.name, "-o", "name")
        after = json.loads(self.kubectl("get", "deployment/" + name, "-o", "json"))
        if after["metadata"]["uid"] != meta["uid"] or after["spec"] != expected:
            raise ValueError("Writer trust-only patch readback changed identity or spec")
        return expected

    def converged(self, managed, pods, expected_specs):
        return super().converged(managed, pods, expected_specs) and all(
            self.inactive_bootstrap or self.verify_only or container.get("env", []) == self.expected_env(container) for _, _, container in pods
        )

    def observe(self, name, pod, container):
        probe = INACTIVE_BOOTSTRAP_READ if self.inactive_bootstrap else TRUST_READ
        observation = json.loads(self.kubectl("exec", "pod/" + pod["metadata"]["name"], "-c", container["name"], "--", "python", "-S", "-c", probe))
        if self.inactive_bootstrap:
            if set(observation) != {"authority_disabled", "worker_trust_unconfigured", "worker_references_absent"} or any(value is not True for value in observation.values()):
                raise ValueError("Missing worker authority ConfigMap on an enabled or configured Gateway")
            return {"deployment": name, "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
                    "container": container["name"], "inactive_authority_proven": True}
        if set(observation) != {"worker_trust_sha256"} or not isinstance(observation["worker_trust_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", observation["worker_trust_sha256"]):
            raise ValueError("Invalid worker-trust runtime observation")
        matches = observation == {"worker_trust_sha256": digest(self.trust)}
        if not self.preview and not matches:
            raise ValueError("Running Gateway worker trust differs from the authority ConfigMap")
        return {"deployment": name, "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
                "container": container["name"], "enrollment": self.inventory[name]["enrollment"], "worker_trust_matches": matches, **observation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--gateway-root", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    inventory = bindings(os.environ.get("ADP_GATEWAY_WRITER_INVENTORY", "[]"))
    registry = f"{args.account}.dkr.ecr.{args.region}.amazonaws.com/adp-gateway"
    if (
        not re.fullmatch(r"[0-9]{12}", args.account)
        or not NAME.fullmatch(args.namespace)
        or not re.fullmatch(r"[0-9a-f]{40}", args.source_revision)
        or not 0 <= args.timeout <= 1800
        or not re.fullmatch(re.escape(registry) + r"@sha256:[0-9a-f]{64}", args.image)
    ):
        parser.error("Invalid immutable release target")
    identity = json.loads(command(["aws", "sts", "get-caller-identity", "--output", "json"]))
    cluster = command(["kubectl", "config", "view", "--minify", "-o", "jsonpath={.contexts[0].context.cluster}"])
    if identity.get("Account") != args.account or cluster != f"arn:aws:eks:{args.region}:{args.account}:cluster/{args.cluster}":
        raise ValueError("AWS account or active Kubernetes cluster differs from release target")
    gateway_root = Path(args.gateway_root)
    blobs = source_blobs(gateway_root, args.source_revision)
    claims = ast.parse(blobs["src/orchestration/work_claims.py"])
    release = next((node for node in claims.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "release_work"), None)
    if (
        not any(isinstance(node, ast.AsyncFunctionDef) and node.name == "_unfinished_bound_review" for node in claims.body)
        or release is None
        or not any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_unfinished_bound_review"
            for node in ast.walk(release)
        )
    ):
        raise ValueError("Selected release lacks the bound delivery claim-retention guard")
    source_hashes = {p: hashlib.sha256(blob).hexdigest() for p, blob in blobs.items()}
    result = Writers(namespace=args.namespace, image=args.image, inventory=inventory, source_hashes=source_hashes, timeout=args.timeout).run(
        preview=args.preview, verify_only=args.verify_only
    )
    result["worker_trust"] = WorkerTrust(namespace=args.namespace, image=args.image, inventory=inventory, timeout=args.timeout,
                                        allow_inactive_bootstrap=True).run(
        preview=args.preview, verify_only=args.verify_only
    )
    result["source_revision"] = args.source_revision
    with os.fdopen(os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        json.dump(result, file, indent=2)
        file.write("\n")
    print(json.dumps({"status": result["status"], "deployments": len(result["deployments"]), "pods": len(result["pods"])}))


if __name__ == "__main__":
    main()

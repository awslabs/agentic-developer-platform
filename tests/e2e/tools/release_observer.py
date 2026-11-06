"""Repository-owned, read-only runtime observation from private resource selectors.

This module never receives the release manifest or its expected digests. Each
phase performs new AWS/Kubernetes reads; unsupported or incomplete reads fail.
"""

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from modules.tools.platform.scripts.target_metadata import target_revision_values


NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+\Z")
FIELDS = {
    "gateway": {"namespace", "deployment", "container"},
    "tool-adapters": {"namespace", "deployment", "container"},
    "facade and reconciler": {"namespace", "deployment", "container", "cronjob", "reconciler_container"},
    "shared Lambda and Browser": {"function_arn", "namespace", "deployment", "container"},
    "platform targets": {"gateway_id", "target_ids"},
}


def private_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Release configuration must be an owned private regular file")
    return json.loads(path.read_text())


def command(*arguments):
    try:
        result = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=30)
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, subprocess.SubprocessError, ValueError):
        # Never copy provider stderr, URLs, credentials or response bodies into results.
        raise ValueError(
            "Independent AWS/Kubernetes metadata read failed; check read permissions and availability"
        ) from None


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Runtime timestamp is unavailable or invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("Runtime timestamp must include a timezone")
    return parsed


class Observer:
    def __init__(self, selectors, *, account, region, cluster, kubeconfig, evidence, read=command, now=None):
        if (
            not isinstance(selectors, dict)
            or set(selectors) != {"version", "account", "region", "cluster", "components"}
            or selectors["version"] != 1
            or selectors["account"] != account
            or selectors["region"] != region
            or selectors["cluster"] != cluster
            or not re.fullmatch(r"[0-9]{12}", account)
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
            or not NAME.fullmatch(cluster)
        ):
            raise ValueError("Private release selectors must bind the protected account, region and cluster")
        components = selectors["components"]
        if (
            not isinstance(components, dict)
            or not {"gateway", "facade and reconciler"} <= set(components)
            or not set(components) <= set(FIELDS)
        ):
            raise ValueError("Independent observer does not support the selected component set")
        for name, selected in components.items():
            if not isinstance(selected, dict) or set(selected) != FIELDS[name]:
                raise ValueError("Supply resource selectors only, never expected digests or readbacks")
            if name == "tool-adapters" and (selected["deployment"] != "tool-adapters"
                                            or selected["container"] != "tool-adapters"):
                raise ValueError("Select the standalone adapter workload")
            for field in FIELDS[name] - {"function_arn", "gateway_id", "target_ids"}:
                if not isinstance(selected[field], str) or not NAME.fullmatch(selected[field]):
                    raise ValueError("Invalid Kubernetes resource selector")
            if name == "shared Lambda and Browser" and not re.fullmatch(
                rf"arn:aws:lambda:{region}:{account}:function:[A-Za-z0-9_-]+", selected["function_arn"]
            ):
                raise ValueError("Select an unqualified Lambda in the protected account and region")
            if name == "platform targets":
                ids = selected["target_ids"]
                if (
                    not isinstance(selected["gateway_id"], str)
                    or not IDENTIFIER.fullmatch(selected["gateway_id"])
                    or not isinstance(ids, list)
                    or not ids
                    or not all(isinstance(value, str) and IDENTIFIER.fullmatch(value) for value in ids)
                    or len(ids) != len(set(ids))
                ):
                    raise ValueError("Select the complete set of existing gateway target IDs")
        self.components = components
        self.account, self.region, self.cluster = account, region, cluster
        self.kubeconfig, self.evidence = str(kubeconfig), Path(evidence)
        self.read = read
        self.now = now or (lambda: datetime.now(UTC))

    def aws(self, service, operation, *arguments):
        return self.read("aws", service, operation, *arguments, "--region", self.region, "--output", "json")

    def kube(self, kind, namespace, *arguments):
        return self.read(
            "kubectl",
            "--kubeconfig",
            self.kubeconfig,
            "--request-timeout=20s",
            "get",
            kind,
            *arguments,
            "-n",
            namespace,
            "-o",
            "json",
        )

    def image_digest(self, image):
        match = re.fullmatch(
            rf"{self.account}\.dkr\.ecr\.{self.region}\.amazonaws\.com/[a-z0-9][a-z0-9._/-]*@(sha256:[a-f0-9]{{64}})",
            image,
        )
        if not match:
            raise ValueError("Runtime image must use an immutable digest in the protected account and region")
        return match[1]

    @staticmethod
    def metadata(value, *, name, namespace):
        metadata = value["metadata"]
        if (
            metadata["name"] != name
            or metadata["namespace"] != namespace
            or not metadata.get("uid")
            or metadata.get("deletionTimestamp")
        ):
            raise ValueError("Runtime workload identity changed or is terminating")
        return metadata

    @staticmethod
    def owner(value, kind, *, name=None, uid=None):
        owners = [
            owner for owner in value["metadata"].get("ownerReferences", []) if owner.get("controller") is True
        ]
        if (
            len(owners) != 1
            or owners[0].get("kind") != kind
            or not owners[0].get("name")
            or not owners[0].get("uid")
            or (name is not None and owners[0]["name"] != name)
            or (uid is not None and owners[0]["uid"] != uid)
        ):
            raise ValueError("Pod ownership does not match the selected workload")
        return owners[0]

    def container_image(self, spec, container):
        values = [item["image"] for item in spec["containers"] if item["name"] == container]
        if len(values) != 1:
            raise ValueError("Selected runtime container is missing or duplicated")
        self.image_digest(values[0])
        return values[0]

    def pod_image(self, pod, container, image, *, completed=False):
        if self.container_image(pod["spec"], container) != image:
            raise ValueError("Pod spec differs from its workload image")
        statuses = [item for item in pod["status"]["containerStatuses"] if item["name"] == container]
        if len(statuses) != 1:
            raise ValueError("Pod runtime container is missing or duplicated")
        status = statuses[0]
        image_id = status.get("imageID", "").removeprefix("docker-pullable://")
        # containerd may report a config digest in status.image. The pod spec
        # binds the requested image; imageID is the resolved manifest identity.
        if image_id != image:
            raise ValueError("Pod runtime image differs from its immutable workload image")
        if completed:
            terminated = status.get("state", {}).get("terminated", {})
            if (
                pod["status"]["phase"] != "Succeeded"
                or terminated.get("exitCode") != 0
                or not 0 <= (self.now() - timestamp(terminated["finishedAt"])).total_seconds() <= 300
            ):
                raise ValueError("Reconciler needs a successful runtime pod within the last five minutes")
        elif pod["status"]["phase"] != "Running" or status.get("ready") is not True:
            raise ValueError("Selected runtime pod is not ready")

    def deployment(self, selected):
        name, namespace, container = (selected[key] for key in ("deployment", "namespace", "container"))
        value = self.kube("deployment", namespace, name)
        metadata = self.metadata(value, name=name, namespace=namespace)
        spec, status = value["spec"], value["status"]
        replicas = spec["replicas"]
        if (
            type(replicas) is not int
            or replicas < 1
            or status.get("observedGeneration", 0) < metadata["generation"]
            or any(
                status.get(key) != replicas
                for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            )
        ):
            raise ValueError("Selected deployment has missing, mixed or unready replicas")
        image = self.container_image(spec["template"]["spec"], container)
        labels = spec["selector"].get("matchLabels", {})
        if not labels or spec["selector"].get("matchExpressions"):
            raise ValueError("Selected deployment requires explicit matchLabels")
        pods = self.kube(
            "pods", namespace, "-l", ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        )["items"]
        if len(pods) != replicas:
            raise ValueError("Deployment pods are missing, terminating or mixed")
        for pod in pods:
            self.metadata(pod, name=pod["metadata"]["name"], namespace=namespace)
            owner = self.owner(pod, "ReplicaSet")
            replica_set = self.kube("replicaset", namespace, owner["name"])
            if self.metadata(replica_set, name=owner["name"], namespace=namespace)["uid"] != owner["uid"]:
                raise ValueError("Pod ReplicaSet identity changed")
            self.owner(replica_set, "Deployment", name=name, uid=metadata["uid"])
            self.pod_image(pod, container, image)
        return self.image_digest(image)

    def reconciler(self, selected):
        name, namespace, container = (
            selected[key] for key in ("cronjob", "namespace", "reconciler_container")
        )
        cron = self.kube("cronjob", namespace, name)
        metadata = self.metadata(cron, name=name, namespace=namespace)
        if cron["spec"].get("suspend", False):
            raise ValueError("Reconciler CronJob is suspended")
        image = self.container_image(cron["spec"]["jobTemplate"]["spec"]["template"]["spec"], container)
        # Jobs have generated names. Verify their controller UID, then select their pods.
        jobs = [
            job
            for job in self.kube("jobs", namespace)["items"]
            if any(
                owner.get("uid") == metadata["uid"] and owner.get("controller") is True
                for owner in job["metadata"].get("ownerReferences", [])
            )
        ]
        successful = [
            job
            for job in jobs
            if job.get("status", {}).get("succeeded") == 1 and job.get("status", {}).get("completionTime")
        ]
        if not successful:
            raise ValueError("Reconciler has no completed owned runtime Job")
        latest = max(successful, key=lambda job: timestamp(job["status"]["completionTime"]))
        if any(
            job.get("status", {}).get("failed", 0) > 0
            and timestamp(job["metadata"]["creationTimestamp"])
            >= timestamp(latest["status"]["completionTime"])
            for job in jobs
        ):
            raise ValueError("Reconciler failed after its last successful runtime Job")
        active = [job for job in jobs if job.get("status", {}).get("active", 0) > 0]
        for job in [latest, *active]:
            job_name = job["metadata"]["name"]
            job_metadata = self.metadata(job, name=job_name, namespace=namespace)
            self.owner(job, "CronJob", name=name, uid=metadata["uid"])
            if self.container_image(job["spec"]["template"]["spec"], container) != image:
                raise ValueError("Reconciler Job differs from its CronJob image")
            pods = self.kube("pods", namespace, "-l", f"batch.kubernetes.io/job-name={job_name}")["items"]
            if len(pods) != 1:
                raise ValueError("Reconciler needs exactly one observed pod for each selected Job")
            pod = pods[0]
            self.metadata(pod, name=pod["metadata"]["name"], namespace=namespace)
            self.owner(pod, "Job", name=job_name, uid=job_metadata["uid"])
            self.pod_image(pod, container, image, completed=job is latest)
        return self.image_digest(image)

    def targets(self, selected):
        identifier = selected["gateway_id"]
        arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{identifier}"
        gateway = self.aws("bedrock-agentcore-control", "get-gateway", "--gateway-identifier", identifier)
        if (
            gateway.get("gatewayArn") != arn
            or gateway.get("gatewayId") != identifier
            or gateway.get("status") != "READY"
        ):
            raise ValueError("Selected AgentCore gateway is unavailable or mismatched")
        # The AWS CLI paginates this list; reject any unconsumed continuation token.
        listing = self.aws(
            "bedrock-agentcore-control", "list-gateway-targets", "--gateway-identifier", identifier
        )
        ids = [target["targetId"] for target in listing["items"]]
        if (
            listing.get("nextToken")
            or listing.get("NextToken")
            or sorted(ids) != sorted(selected["target_ids"])
        ):
            raise ValueError("Gateway targets changed or the independent listing is incomplete")
        targets = []
        for target_id in sorted(ids):
            target = self.aws(
                "bedrock-agentcore-control",
                "get-gateway-target",
                "--gateway-identifier",
                identifier,
                "--target-id",
                target_id,
            )
            if (
                target.get("gatewayArn") != arn
                or target.get("targetId") != target_id
                or target.get("status") != "READY"
            ):
                raise ValueError("Selected AgentCore target is unavailable or mismatched")
            targets.append(target)
        return "sha256:" + target_revision_values(targets)

    def readback(self, phase):
        if phase not in {"before", "after"}:
            raise ValueError("Unknown release observation phase")
        observed = {}
        if self.aws("sts", "get-caller-identity").get("Account") != self.account:
            raise ValueError("Independent observer identity differs from the protected account")
        for name, selected in self.components.items():
            if name == "gateway":
                observed[name] = {"image": self.deployment(selected)}
            elif name == "tool-adapters":
                image = self.deployment(selected)
                configmap = self.kube("configmap", selected["namespace"], "tool-adapters-contract")
                self.metadata(configmap, name="tool-adapters-contract", namespace=selected["namespace"])
                data = configmap.get("data")
                if not isinstance(data, dict) or set(data) != {"config.json"}:
                    raise ValueError("Adapter configuration is missing or changed")
                content = data["config.json"]
                if not isinstance(content, str) or not 0 < len(content.encode()) <= 65536:
                    raise ValueError("Adapter configuration is unavailable or oversized")
                try:
                    configuration = json.loads(content)
                    release = configuration["release"]
                except (ValueError, TypeError, KeyError):
                    raise ValueError("Adapter configuration is invalid") from None
                if (not isinstance(release, dict) or release.get("image_digest") != image
                        or configuration.get("schema_version") != "1.0"):
                    raise ValueError("Adapter configuration differs from the observed image")
                observed[name] = {"image": image, "configuration": "sha256:" + hashlib.sha256(content.encode()).hexdigest()}
            elif name == "facade and reconciler":
                observed[name] = {"image": self.deployment(selected), "reconciler": self.reconciler(selected)}
            elif name == "shared Lambda and Browser":
                function = self.aws("lambda", "get-function", "--function-name", selected["function_arn"])
                config = function["Configuration"]
                if (
                    config.get("FunctionArn") != selected["function_arn"]
                    or config.get("State") != "Active"
                    or config.get("LastUpdateStatus") != "Successful"
                ):
                    raise ValueError("Shared Lambda is unavailable or mismatched")
                observed[name] = {
                    "lambda": self.image_digest(function["Code"]["ResolvedImageUri"]),
                    "browser": self.deployment(selected),
                }
            else:
                observed[name] = {"targets": self.targets(selected)}
        self.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.evidence / f"runtime-{phase}.json"
        with open(
            path, "w", opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600)
        ) as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(
                {"phase": phase, "observed_at": self.now().isoformat(), "runtime": observed},
                stream,
                sort_keys=True,
            )
            stream.write("\n")
        return observed


def from_environment(environment=os.environ):
    kubeconfig = Path(environment["RUNNER_TEMP"]) / "tools-eval-kubeconfig"
    if (
        environment.get("KUBECONFIG") != str(kubeconfig)
        or kubeconfig.is_symlink()
        or not kubeconfig.is_file()
        or kubeconfig.stat().st_mode & 0o077
    ):
        raise ValueError("Independent observer requires the workflow-owned private kubeconfig")
    return Observer(
        private_json(environment["ADP_TOOLS_RELEASE_SELECTORS_FILE"]),
        account=environment["EXPECTED_ACCOUNT"],
        region=environment["AWS_REGION"],
        cluster=environment["EKS_CLUSTER"],
        kubeconfig=kubeconfig,
        evidence=Path(environment["RUNNER_TEMP"]) / "tools-eval-evidence",
    )

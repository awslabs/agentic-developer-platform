"""Read the selected running API artifact through the maintained installer probes."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .demo1_aws import AwsProviderReader
from .demo1_evidence import DemoInput, EvidenceError, digest, fields, text
from .demo1_report import reference

COMPONENT = "superplane-api"


def _require(condition: bool) -> None:
    if not condition:
        raise EvidenceError("runtime: denied, incomplete or mismatched observation")


@dataclass(frozen=True)
class RuntimeTarget:
    connection_id: str
    broker_label: str
    account: str
    role: str
    region: str
    cluster_name: str
    namespace: str
    release_id: str

    @classmethod
    def parse(cls, value: object) -> RuntimeTarget:
        target = fields(value, set(cls.__dataclass_fields__), "runtime target")
        AwsProviderReader(
            connection_id=target["connection_id"],
            broker_label=target["broker_label"],
            account=target["account"],
            role_name=target["role"],
            region=target["region"],
        )
        _require(
            bool(
                re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}",
                    text(target["cluster_name"], "runtime cluster"),
                )
            )
        )
        _require(
            bool(
                re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                    text(target["namespace"], "runtime namespace"),
                )
            )
        )
        digest(target["release_id"], "runtime release")
        return cls(**target)


class RuntimeReader:
    def __init__(
        self,
        selected: DemoInput,
        target: RuntimeTarget,
        *,
        runner=subprocess.run,
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
    ):
        self.selected, self.target = selected, target
        self.runner, self.clock, self.monotonic = runner, clock, monotonic
        self.aws = AwsProviderReader(
            connection_id=target.connection_id,
            broker_label=target.broker_label,
            account=target.account,
            role_name=target.role,
            region=target.region,
            runner=self._run,
        )

    def _run(self, command, **options):
        remaining = min(
            self.expires - self.monotonic(),
            (self.selected.deadline - self.clock()).total_seconds(),
        )
        _require(self.selected.authorized_at <= self.clock() and remaining > 0)
        options["timeout"] = min(30, remaining)
        return self.runner(command, **options)

    def _kube(self, config: Path, *arguments: str) -> dict:
        _require(self.aws._identity())
        result = self._run(
            [
                "adp-cred",
                "assume",
                "--service",
                "aws",
                "--label",
                self.target.broker_label,
                "--exec",
                "kubectl",
                "--kubeconfig",
                str(config),
                "--request-timeout=30s",
                "--namespace",
                self.target.namespace,
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        _require(result.returncode == 0)
        value = json.loads(result.stdout)
        _require(isinstance(value, dict))
        return value

    def _pod(self, pod: dict) -> tuple[str, str, str]:
        metadata = pod["metadata"]
        _require(
            metadata.get("namespace") == self.target.namespace
            and not metadata.get("deletionTimestamp")
        )
        _require(
            metadata["annotations"].get("adp.aws-e.io/release")
            == self.target.release_id
        )
        _require(pod["status"].get("phase") == "Running")
        _require(
            any(
                item.get("type") == "Ready" and item.get("status") == "True"
                for item in pod["status"]["conditions"]
            )
        )
        containers = [
            item for item in pod["spec"]["containers"] if item.get("name") == COMPONENT
        ]
        statuses = [
            item
            for item in pod["status"]["containerStatuses"]
            if item.get("name") == COMPONENT
        ]
        _require(
            len(containers) == len(statuses) == 1 and statuses[0].get("ready") is True
        )
        suffix = "sha256:" + self.selected.image_digest
        _require(containers[0]["image"].endswith("@" + suffix))
        actual = statuses[0]["imageID"]
        _require(actual == suffix or actual.endswith("@" + suffix))
        _require(bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", metadata["name"])))
        return (
            metadata["name"],
            text(metadata["uid"], "pod UID"),
            text(metadata["resourceVersion"], "pod version"),
        )

    def observe(self, max_runtime_seconds: int, *, ownership_scope=None) -> dict:
        self.expires = self.monotonic() + max_runtime_seconds
        try:
            return self._observe(ownership_scope)
        except (
            OSError,
            subprocess.SubprocessError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
        ):
            raise EvidenceError(
                "runtime: denied, incomplete or mismatched observation; no lifecycle effects"
            ) from None

    def _observe(self, ownership_scope=None) -> dict:
        _require(self.aws._identity())
        code, response, _ = self.aws._execute(
            "eks",
            "describe-cluster",
            "--name",
            self.target.cluster_name,
            "--region",
            self.target.region,
        )
        _require(code == 0)
        cluster = response["cluster"]
        _require(
            cluster["arn"]
            == f"arn:aws:eks:{self.target.region}:{self.target.account}:cluster/{self.target.cluster_name}"
        )
        _require(cluster.get("status") == "ACTIVE")
        endpoint = urlsplit(cluster["endpoint"])
        _require(
            endpoint.scheme == "https"
            and bool(endpoint.hostname)
            and not endpoint.username
            and not endpoint.password
            and endpoint.path in ("", "/")
            and not endpoint.query
            and not endpoint.fragment
        )
        certificate = cluster["certificateAuthority"]["data"]
        _require(bool(base64.b64decode(certificate, validate=True)))
        configuration = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "selected",
            "clusters": [
                {
                    "name": "selected",
                    "cluster": {
                        "server": cluster["endpoint"],
                        "certificate-authority-data": certificate,
                    },
                }
            ],
            "contexts": [
                {
                    "name": "selected",
                    "context": {"cluster": "selected", "user": "selected"},
                }
            ],
            "users": [
                {
                    "name": "selected",
                    "user": {
                        "exec": {
                            "apiVersion": "client.authentication.k8s.io/v1beta1",
                            "command": "aws",
                            "args": [
                                "eks",
                                "get-token",
                                "--cluster-name",
                                self.target.cluster_name,
                                "--region",
                                self.target.region,
                                "--output",
                                "json",
                            ],
                            "interactiveMode": "Never",
                        }
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory(prefix="demo1-runtime-") as directory:
            config = Path(directory) / "kubeconfig.json"
            descriptor = config.open("x", encoding="utf-8")
            config.chmod(0o600)
            with descriptor as stream:
                json.dump(configuration, stream)
            deployment = self._kube(
                config, "get", "deployment/" + COMPONENT, "-o", "json"
            )
            metadata, status = deployment["metadata"], deployment["status"]
            replicas = deployment["spec"]["replicas"]
            _require(
                metadata.get("namespace") == self.target.namespace
                and metadata.get("name") == COMPONENT
                and not metadata.get("deletionTimestamp")
            )
            _require(
                type(replicas) is int
                and 0 < replicas <= 20
                and status["observedGeneration"] == metadata["generation"]
            )
            _require(
                all(
                    status.get(field) == replicas
                    for field in (
                        "replicas",
                        "updatedReplicas",
                        "availableReplicas",
                        "readyReplicas",
                    )
                )
            )
            pods = self._kube(
                config,
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=" + COMPONENT,
                "-o",
                "json",
            )["items"]
            _require(isinstance(pods, list) and len(pods) == replicas)
            observed = []
            ownership = None
            for pod in pods:
                name, uid, version = self._pod(pod)
                owners = [
                    item
                    for item in pod["metadata"]["ownerReferences"]
                    if item.get("controller") is True
                    and item.get("kind") == "ReplicaSet"
                ]
                _require(
                    len(owners) == 1
                    and bool(
                        re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", owners[0]["name"])
                    )
                )
                replica_set = self._kube(
                    config, "get", "replicaset/" + owners[0]["name"], "-o", "json"
                )["metadata"]
                _require(
                    replica_set["uid"] == owners[0]["uid"]
                    and replica_set.get("namespace") == self.target.namespace
                )
                _require(
                    any(
                        item.get("controller") is True
                        and item.get("kind") == "Deployment"
                        and item.get("uid") == metadata["uid"]
                        for item in replica_set["ownerReferences"]
                    )
                )
                command = (
                    "exec",
                    "pod/" + name,
                    "-c",
                    COMPONENT,
                    "--",
                    "python",
                    "-m",
                    "app.installation",
                )
                runtime = self._kube(config, *command, "readiness")
                database = self._kube(config, *command, "database")
                _require(
                    runtime.get("release_id") == self.target.release_id
                    and runtime.get("source_revision") == self.selected.release_source
                    and runtime.get("domain_auth_enforced") is True
                )
                _require(database.get("revision") == self.selected.schema_revision)
                if ownership_scope is not None and ownership is None:
                    probe = (
                        Path(__file__)
                        .with_name("_demo1_ownership_probe.py")
                        .read_text()
                    )
                    ownership = self._kube(
                        config,
                        "exec",
                        "pod/" + name,
                        "-c",
                        COMPONENT,
                        "--",
                        "python",
                        "-c",
                        probe,
                        json.dumps(ownership_scope),
                    )
                _require(
                    self._pod(self._kube(config, "get", "pod/" + name, "-o", "json"))
                    == (name, uid, version)
                )
                observed.append(reference(uid))
            current = self._kube(
                config, "get", "deployment/" + COMPONENT, "-o", "json"
            )["metadata"]
            _require(
                current["uid"] == metadata["uid"]
                and current["resourceVersion"] == metadata["resourceVersion"]
            )
        _require(
            self.monotonic() < self.expires and self.clock() < self.selected.deadline
        )
        return {
            "status": "OBSERVED",
            "component": COMPONENT,
            "release_ref": reference(self.target.release_id),
            "pod_refs": observed,
            "observed_at": self.clock().isoformat(),
            "scope": "API artifact/source/schema only; public route, other components and lifecycle admission unverified",
            **({"ownership": ownership} if ownership_scope is not None else {}),
        }

"""Bounded image/private-database preflight without a local Docker daemon.

Only temporary, UID-attributed namespace resources are created. Probe pods have
no Kubernetes/AWS identity. Credentials travel by Secret stdin, never argv/files.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

from .config import LABEL, MODULE, Refusal, image, require
from .manifests import dns_egress

HTTP_PROBE = """import json,sys,urllib.request,urllib.error
result={'reachable':False,'denied':False}
try:
    r=urllib.request.urlopen(sys.argv[1],timeout=2)
    result['reachable']=r.status==200 and r.read()==b'superplane-policy-ok'
except (TimeoutError,ConnectionRefusedError,ConnectionResetError):
    result['denied']=True
except urllib.error.URLError as error:
    result['denied']=isinstance(error.reason,(TimeoutError,ConnectionRefusedError,ConnectionResetError))
print(json.dumps(result))
"""


class ClusterProbe:
    def __init__(self, installer):
        self.installer = installer
        self.namespace = "sp-preflight-" + installer.run_id
        self.uid = None
        self.isolated = False

    def kube(self, *args, **kw):
        try:
            return self.installer.kube(*args, **kw)
        except Refusal:
            self.installer.receipt["temporary_preflight_failed_operation"] = next(
                (
                    arg
                    for arg in args
                    if arg in {"create", "get", "exec", "delete", "wait", "logs"}
                ),
                "unknown",
            )
            self.installer.save()
            raise

    def create(self, value):
        value.setdefault("metadata", {}).setdefault("labels", {})[LABEL] = (
            self.installer.owner
        )
        if value["kind"] != "Namespace":
            value["metadata"]["namespace"] = self.namespace
        return self.installer.json(
            self.kube("create", "-f", "-", "-o", "json", data=json.dumps(value))
        )

    def __enter__(self):
        existing = self.kube(
            "get", "namespace", self.namespace, "--ignore-not-found", "-o", "json"
        )
        require(
            not existing.stdout.strip(),
            "Temporary preflight namespace already exists; inspect its recorded ownership before retry",
        )
        created = self.create(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": self.namespace,
                    "labels": {
                        "pod-security.kubernetes.io/enforce": "restricted",
                        "pod-security.kubernetes.io/enforce-version": "latest",
                    },
                },
            }
        )
        self.uid = created["metadata"]["uid"]
        self.installer.receipt["temporary_preflight"] = {
            "namespace": self.namespace,
            "uid": self.uid,
            "cleanup_required": True,
        }
        self.installer.save()
        try:
            self.create(
                {
                    "apiVersion": "v1",
                    "kind": "ResourceQuota",
                    "metadata": {"name": "bounded"},
                    "spec": {
                        "hard": {
                            "pods": "5",
                            "requests.cpu": "500m",
                            "requests.memory": "1Gi",
                            "limits.cpu": "2",
                            "limits.memory": "3Gi",
                        }
                    },
                }
            )
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *ignored):
        if self.uid:
            current = self.installer.json(
                self.kube("get", "namespace", self.namespace, "-o", "json")
            )
            require(
                current["metadata"]["uid"] == self.uid
                and current["metadata"]["labels"].get(LABEL) == self.installer.owner,
                "Preflight namespace ownership changed; cleanup refused",
            )
            self.kube(
                "delete",
                "--raw",
                "/api/v1/namespaces/" + self.namespace,
                "-f",
                "-",
                data=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": self.uid},
                    }
                ),
            )
            self.kube(
                "wait",
                "--for=delete",
                "namespace/" + self.namespace,
                "--timeout=120s",
                timeout=140,
            )
            require(
                not self.kube(
                    "get",
                    "namespace",
                    self.namespace,
                    "--ignore-not-found",
                    "-o",
                    "json",
                ).stdout.strip(),
                "Preflight cleanup not verified",
            )
            self.installer.receipt["temporary_preflight"]["cleanup_required"] = False
            self.installer.save()

    def pod(self, name, component, command, *, values=None, deadline_seconds=300):
        environment = []
        if values:
            self.create(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": name},
                    "type": "Opaque",
                    "stringData": values,
                }
            )
            environment = [
                {"name": key, "valueFrom": {"secretKeyRef": {"name": name, "key": key}}}
                for key in values
            ]
        return self.create(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": name,
                    "labels": {"probe": name},
                    # These bare, deadline-bounded pods cannot be recreated by
                    # a controller after Auto Mode consolidates their node.
                    "annotations": {"karpenter.sh/do-not-disrupt": "true"},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": deadline_seconds,
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "fsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image(self.installer.lock, component),
                            "command": command,
                            "env": environment,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"cpu": "400m", "memory": "512Mi"},
                            },
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                        }
                    ],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}],
                },
            }
        )

    def wait(self, name, *, terminal):
        deadline = time.monotonic() + min(self.installer.env["timeout_seconds"], 300)
        while time.monotonic() < deadline:
            pod = self.installer.json(
                self.kube("-n", self.namespace, "get", "pod", name, "-o", "json")
            )
            phase = pod.get("status", {}).get("phase")
            if phase in {"Succeeded", "Failed"} or (
                not terminal and phase == "Running"
            ):
                return pod
            time.sleep(2)
        raise Refusal("Preflight pod timed out; no deployment success is claimed")

    def policy(self, name, spec):
        return self.create(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": name},
                "spec": spec,
            }
        )

    def network_sample(self, role, address):
        result = self.kube(
            "-n",
            self.namespace,
            "exec",
            role,
            "--",
            "python",
            "-c",
            HTTP_PROBE,
            address,
            allow_failure=True,
        )
        if result.returncode:
            return {
                "reachable": False,
                "denied": False,
                "process_exit": result.returncode,
            }
        try:
            value = self.installer.json(result)
            return {
                "reachable": value.get("reachable") is True,
                "denied": value.get("denied") is True,
            }
        except (Refusal, AttributeError):
            return {"reachable": False, "denied": False, "invalid_output": True}

    def prove_network_policy(self):
        program = "import http.server\nclass H(http.server.BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200);self.end_headers();self.wfile.write(b'superplane-policy-ok')\nhttp.server.ThreadingHTTPServer(('0.0.0.0',8080),H).serve_forever()"
        for name in ("server", "allowed", "denied"):
            self.pod(
                name,
                "superplane-api",
                [
                    "python",
                    "-c",
                    program if name == "server" else "import time;time.sleep(880)",
                ],
                deadline_seconds=900,
            )
            require(
                self.wait(name, terminal=False)["status"]["phase"] == "Running",
                "NetworkPolicy probe did not start",
            )
        server = self.installer.json(
            self.kube("-n", self.namespace, "get", "pod", "server", "-o", "json")
        )
        address = "http://" + server["status"]["podIP"] + ":8080/"
        observations = []

        def stage(name, expected):
            deadline, consecutive = time.monotonic() + 120, 0
            while time.monotonic() < deadline:
                samples = [
                    self.network_sample(role, address) for role in ("allowed", "denied")
                ]
                good = all(
                    sample["reachable"] if allow else sample["denied"]
                    for sample, allow in zip(samples, expected, strict=True)
                )
                observations.append(
                    {"stage": name, "samples": samples, "matches": good}
                )
                self.installer.receipt["management_network_policy"] = {
                    "verified": False,
                    "observations": observations,
                    "cluster": self.installer.env["cluster"],
                }
                self.installer.save()
                consecutive = consecutive + 1 if good else 0
                if consecutive == 3:
                    return
                time.sleep(2)
            raise Refusal("Actual NetworkPolicy enforcement failed: " + name)

        stage("baseline", [True, True])
        self.policy(
            "deny-server",
            {
                "podSelector": {"matchLabels": {"probe": "server"}},
                "policyTypes": ["Ingress"],
            },
        )
        stage("deny", [False, False])
        self.policy(
            "allow-client",
            {
                "podSelector": {"matchLabels": {"probe": "server"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {"podSelector": {"matchLabels": {"probe": "allowed"}}}
                        ],
                        "ports": [{"port": 8080, "protocol": "TCP"}],
                    }
                ],
            },
        )
        stage("selective", [True, False])
        self.kube(
            "-n",
            self.namespace,
            "delete",
            "networkpolicy",
            "deny-server",
            "allow-client",
            "--wait=true",
        )
        stage("restored", [True, True])
        self.kube(
            "-n",
            self.namespace,
            "delete",
            "pods",
            "server",
            "allowed",
            "denied",
            "--wait=true",
        )
        self.installer.receipt["management_network_policy"] = {
            "verified": True,
            "observations": observations,
            "cluster": self.installer.env["cluster"],
        }
        self.installer.save()

    def isolate(self, *, database_cidrs=(), database_port=5432):
        egress = []
        if database_cidrs:
            egress = [
                dns_egress(self.installer.network_environment),
                {
                    "to": [{"ipBlock": {"cidr": cidr}} for cidr in database_cidrs],
                    "ports": [{"protocol": "TCP", "port": database_port}],
                },
            ]
        self.policy(
            "isolate",
            {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "egress": egress},
        )
        self.isolated = True

    def prove_dns(self, database_host):
        names = ["kubernetes.default.svc.cluster.local", database_host]
        result = self.run(
            "superplane-api",
            [
                "python",
                "-c",
                (MODULE / "installation/dns_probe.py").read_text(),
                self.installer.network_environment.get("cluster_dns_ip", ""),
                *names,
            ],
        )
        require(
            result.returncode == 0, "Restricted DNS preflight failed over UDP or TCP"
        )
        observed = self.installer.json(result)
        require(
            observed.get("verified") is True
            and observed.get("names") == names
            and observed.get("protocols") == ["UDP", "TCP"],
            "Restricted DNS probe did not verify Kubernetes and database resolution",
        )
        self.installer.receipt["management_dns"] = observed
        self.installer.save()

    def run(self, component, command, *, values=None):
        require(
            self.isolated,
            "Image execution requires the explicit preflight network boundary",
        )
        name = "check-" + uuid.uuid4().hex[:12]
        self.pod(name, component, command, values=values)
        pod = self.wait(name, terminal=True)
        container = pod["status"]["containerStatuses"][0]
        result = SimpleNamespace(
            returncode=container["state"]["terminated"]["exitCode"],
            stdout=self.kube("-n", self.namespace, "logs", name).stdout,
            stderr="",
        )
        # Sequential checks must not accumulate terminal pods against the quota.
        self.kube("-n", self.namespace, "delete", "pod", name, "--wait=true")
        if values:
            self.kube("-n", self.namespace, "delete", "secret", name, "--wait=true")
        return result

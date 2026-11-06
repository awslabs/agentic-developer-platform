"""Collect installed credential-free executor controls, without granting acceptance.

This source-owned component executes a fixed program in digest-pinned images.
It does not invoke a Lambda, broker, provider, shell tool or Task endpoint. The
full TF01 AC-02 additionally needs actual Task/bridge and workload-path evidence.
"""

import base64
import argparse
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

from l5_services.adapters.transform.isolation_probe import PROTOCOL
from qualification_transform_journal import digest

FINALIZER = "adp.dev/transform-isolation-observed"
FIELDS = ("CLUSTER_NAME", "CLUSTER_ENDPOINT", "CLUSTER_CA", "NODE_NAME", "NODE_UID",
          "PROVIDER_NAMESPACE", "CHECK_NAMESPACE", "PROVIDER_IMAGE", "CHECK_IMAGE", "ARTIFACT_BUCKET")
OBSERVATIONS = {"source_read", "admitted_write", "outside_write_denied", "private_read_denied",
                "process_environment_denied", "shell_denied", "credential_free", "root_readonly",
                "capabilities_empty", "seccomp_filter", "no_new_privileges", "metadata_denied", "egress_denied"}
REMAINING = ["actual-check-source-projection-and-command", "actual-provider-tool-mediation",
             "installed-task-identity-rejection", "unqualified-bridge-admission-denial",
             "independent-cross-pod-controls"]
ACTIONS = {
    "target": "Restore the exact protected Transform worker, cluster, node and image pins before collecting isolation controls.",
    "network-control": "Restore the collector's private EKS endpoint reachability; an unreachable positive control cannot prove Pod isolation.",
    "probe": "Inspect the retained exact Pod and fixed probe result; retain unknown executions and repair the failed isolation boundary.",
    "cleanup": "Reconcile only the original probe Pod UID after observed termination; do not force-delete or launch a replacement.",
    "readback": "Restore the original installed node, image and policy identity; a changed target cannot reuse prior isolation observations.",
}


def require(value, message):
    if not value:
        raise ValueError(message)


def installed_target(installed, context, environment):
    """Bind the protected deployment inputs to the actual installed worker/EKS."""
    import boto3
    from botocore.config import Config
    from l5_services.adapters.transform.kubernetes import TransformKubernetesAPI
    from l5_services.runtime_names import functions, queue_scope

    release = installed["release"]
    region, account, stage, service = queue_scope(release["queue_arn"])
    config = json.loads(environment.get("L5_INFRA_CONFIG_JSON", ""))
    require(service == context["service"] == "transform" and stage == context["environment"]
            and account == release["account_id"] and release["source_revision"] == context["source_revision"]
            and config.get("environment") == stage and config.get("account_id") == account
            and config.get("region") == region and config.get("role_arn") == release["role_arn"],
            "protected isolation target differs")
    selected = config["services"]["transform"]
    expected = selected["worker_environment"]
    require(selected["worker_factory"] == "l5_services.runtime_transform:factory"
            and all(isinstance(expected.get("L5_TRANSFORM_" + name), str)
                    and expected["L5_TRANSFORM_" + name] for name in FIELDS), "protected executor inputs unavailable")
    session = boto3.Session(region_name=region)
    bounds = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1})
    identity = session.client("sts", config=bounds).get_caller_identity()
    principal = f"arn:aws:sts::{account}:assumed-role/{release['role_arn'].rsplit('/', 1)[-1]}/"
    require(identity.get("Account") == account and identity.get("Arn", "").startswith(principal),
            "isolation collector role differs")
    worker, _ = functions(stage, "transform")
    current = session.client("lambda", config=bounds).get_function_configuration(FunctionName=worker)
    values = current.get("Environment", {}).get("Variables", {})
    require(current.get("FunctionArn") == f"arn:aws:lambda:{region}:{account}:function:{worker}"
            and current.get("Role") == f"arn:aws:iam::{account}:role/{worker}"
            and current.get("CodeSha256") == release["worker_code_sha256"] and current.get("RevisionId")
            and current.get("State") == "Active" and current.get("LastUpdateStatus") == "Successful"
            and current.get("Handler") == "l5_services.workers.execution.lambda_handler"
            and current.get("Runtime") == "python3.12" and not current.get("Layers")
            and values.get("L5_WORKER_FACTORY") == selected["worker_factory"]
            and values.get("L5_SERVICE_ID") == "transform"
            and all(values.get(key) == value for key, value in expected.items())
            and all(not key.startswith(("AWS_LAMBDA_EXEC_WRAPPER", "PYTHONPATH")) for key in values),
            "installed executor differs from protected inputs")
    cluster_name = expected["L5_TRANSFORM_CLUSTER_NAME"]
    require(cluster_name == installed["selection"]["cluster_name"], "selected executor cluster differs")
    cluster = session.client("eks", config=bounds).describe_cluster(name=cluster_name)["cluster"]
    require(cluster.get("arn") == f"arn:aws:eks:{region}:{account}:cluster/{cluster_name}"
            and cluster.get("status") == "ACTIVE"
            and cluster.get("endpoint") == expected["L5_TRANSFORM_CLUSTER_ENDPOINT"]
            and cluster.get("certificateAuthority", {}).get("data") == expected["L5_TRANSFORM_CLUSTER_CA"],
            "installed cluster endpoint differs")
    namespace = expected["L5_TRANSFORM_CHECK_NAMESPACE"]
    require(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
            and namespace != expected["L5_TRANSFORM_PROVIDER_NAMESPACE"], "executor namespaces differ")
    images = {kind: expected["L5_TRANSFORM_" + field] for kind, field in (
        ("provider", "PROVIDER_IMAGE"), ("check", "CHECK_IMAGE"))}
    for image in images.values():
        require(re.fullmatch(rf"{account}\.dkr\.ecr\.{re.escape(region)}\.amazonaws\.com/"
                rf"adp-{stage}-l5-transform-executor@sha256:[a-f0-9]{{64}}", image), "executor image is not pinned")
    api = TransformKubernetesAPI(endpoint=cluster["endpoint"], cluster=cluster_name,
        ca=cluster["certificateAuthority"]["data"], region=region)
    return api, {"worker_revision": current["RevisionId"], "worker_code_sha256": current["CodeSha256"],
                 "cluster_arn": cluster["arn"], "namespace": namespace, "images": images,
                 "node_name": expected["L5_TRANSFORM_NODE_NAME"], "node_uid": expected["L5_TRANSFORM_NODE_UID"],
                 "endpoint": cluster["endpoint"], "ca": cluster["certificateAuthority"]["data"]}


def boundary(api, target):
    node = api.request("GET", "/api/v1/nodes/" + target["node_name"], maximum=65536, timeout_seconds=2)
    require(isinstance(node, dict) and node.get("metadata", {}).get("uid") == target["node_uid"]
            and node["metadata"].get("name") == target["node_name"] and not node["metadata"].get("deletionTimestamp")
            and any(item.get("type") == "Ready" and item.get("status") == "True"
                    for item in node.get("status", {}).get("conditions", [])), "installed node identity unavailable")
    policy = api.request("GET", f"/apis/networking.k8s.io/v1/namespaces/{target['namespace']}/networkpolicies/deny-all",
                         maximum=65536, timeout_seconds=2)
    require(isinstance(policy, dict) and policy.get("metadata", {}).get("uid")
            and policy["metadata"].get("name") == "deny-all"
            and policy["metadata"].get("namespace") == target["namespace"]
            and not policy["metadata"].get("deletionTimestamp")
            and policy.get("spec", {}).get("podSelector") == {}
            and sorted(policy["spec"].get("policyTypes", [])) == ["Egress", "Ingress"]
            and not policy["spec"].get("ingress") and not policy["spec"].get("egress"), "installed deny policy differs")
    return {"node_uid": target["node_uid"], "node_labels": node["metadata"].get("labels", {}),
            "node_spec": node.get("spec"), "policy_uid": policy["metadata"]["uid"], "policy_spec": policy["spec"]}


def network_control(target):
    host = urlsplit(target["endpoint"]).hostname
    # libc resolver calls have no socket timeout; isolate DNS in a bounded child.
    resolved = subprocess.run([sys.executable, "-I", "-c",
        "import json,socket,sys; print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))",
        host], env={}, capture_output=True, text=True, timeout=3, check=True)
    require(len(resolved.stdout) < 4096, "private DNS control exceeds bound")
    addresses = json.loads(resolved.stdout)
    require(1 <= len(addresses) <= 4 and all(ipaddress.ip_address(ip).is_private
            and not ipaddress.ip_address(ip).is_loopback and not ipaddress.ip_address(ip).is_link_local
            for ip in addresses), "private network control unavailable")
    context = ssl.create_default_context(cadata=base64.b64decode(target["ca"], validate=True).decode())
    for address in addresses:
        with socket.create_connection((address, 443), timeout=2) as transport:
            with context.wrap_socket(transport, server_hostname=host) as tls:
                require(bool(tls.version()), "private network control TLS unavailable")
    return addresses


def pod_manifest(target, image, name, program, selection, binding):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": name, "namespace": target["namespace"],
        "labels": {"adp.dev/transform-isolation": binding[:40]},
        "annotations": {"adp.dev/probe-sha256": digest(selection)}, "finalizers": [FINALIZER]},
        "spec": {"nodeName": target["node_name"], "restartPolicy": "Never", "activeDeadlineSeconds": 90,
            "terminationGracePeriodSeconds": 5, "serviceAccountName": "l5-transform-sandbox",
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            "hostNetwork": False, "hostPID": False, "hostIPC": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10000, "runAsGroup": 10000,
                                "fsGroup": 10000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "isolation", "image": image,
                "command": ["python", "-I", "-c", program, json.dumps(selection, sort_keys=True)],
                "imagePullPolicy": "IfNotPresent", "terminationMessagePath": "/dev/termination-log",
                "terminationMessagePolicy": "File",
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                              "limits": {"cpu": "500m", "memory": "512Mi"}},
                "volumeMounts": [{"name": "work", "mountPath": "/work"}]}],
            "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "64Mi"}}]}}


def checked_pod(value, expected, uid=None):
    require(isinstance(value, dict), "original probe Pod unavailable")
    metadata, spec = value.get("metadata", {}), value.get("spec", {})
    require(metadata.get("name") == expected["metadata"]["name"]
            and metadata.get("namespace") == expected["metadata"]["namespace"]
            and metadata.get("labels") == expected["metadata"]["labels"]
            and metadata.get("annotations", {}).get("adp.dev/probe-sha256") == expected["metadata"]["annotations"]["adp.dev/probe-sha256"]
            and metadata.get("uid") and (uid is None or metadata["uid"] == uid)
            and not spec.get("initContainers") and not spec.get("ephemeralContainers")
            and not value.get("status", {}).get("initContainerStatuses"), "probe identity or workload changed")
    # API defaulting may add harmless fields; selected fields and every container
    # source/identity/command/security field must remain exactly source-owned.
    for key, required in expected["spec"].items():
        if key == "containers":
            require(isinstance(spec.get(key), list) and len(spec[key]) == 1,
                    "probe container inventory changed")
            container = spec[key][0]
            require(not container.get("env") and not container.get("envFrom") and not container.get("args")
                    and not container.get("lifecycle") and not container.get("volumeDevices"),
                    "probe received another command or identity")
            for field, selected in required[0].items():
                require(container.get(field) == selected, "probe container specification changed")
        else:
            default = False if key in {"hostNetwork", "hostPID", "hostIPC"} else None
            require(spec.get(key, default) == required, "probe isolation specification changed")
    return metadata["uid"]


def verified_result(raw, selection):
    require(isinstance(raw, bytes) and 0 < len(raw) < 16384, "probe output incomplete")
    value = json.loads(raw)
    require(isinstance(value, dict) and set(value) == set(selection) | {
            "observations", "source_sha256", "candidate_sha256", "native_calls", "qualified", "live_acceptance"}
            and all(value.get(key) == selected for key, selected in selection.items())
            and value.get("native_calls") is False and value.get("qualified") is False
            and value.get("live_acceptance") == "not_evaluated"
            and value.get("source_sha256") == sha256(b"original-source-v1\n").hexdigest()
            and value.get("candidate_sha256") == sha256(b"admitted-write-v1\n").hexdigest()
            and isinstance(value.get("observations"), dict) and set(value["observations"]) == OBSERVATIONS
            and all(item is True for item in value["observations"].values()), "probe results differ from fixed controls")
    return value


class InstalledIsolation:
    def __init__(self, evidence, installed, *, environment=None):
        self.evidence, self.journal, self.installed = evidence, evidence.journal, installed
        self.environment = os.environ if environment is None else environment
        self.api = None

    def collect(self, *, phase):
        stage = "target"
        try:
            require(phase in {"run", "emit"}, "unknown isolation collection phase")
            self.api, target = installed_target(self.installed, self.journal.binding["context"], self.environment)
            before = boundary(self.api, target)
            stage = "network-control"
            addresses = network_control(target)
            prior = [row for row in self.journal.records if row["kind"] == "executor-isolation-target"]
            selected = {"target": target, "boundary": before, "control_addresses": addresses}
            if prior:
                require(len(prior) == 1 and prior[0]["value"] == selected, "original isolation target changed")
            else:
                require(phase == "run", "original isolation collection is unavailable; emission cannot launch work")
                self.journal.append("executor-isolation-target", selected)
            results = []
            for kind, image in target["images"].items():
                stage = "probe"
                results.append(self.probe(target, kind, image, addresses, phase=phase))
            stage = "readback"
            current_api, current_target = installed_target(self.installed, self.journal.binding["context"], self.environment)
            try:
                require(current_target == target and boundary(current_api, target) == before
                        and network_control(target) == addresses, "installed isolation target changed during probes")
            finally:
                current_api.client.close()
            receipt = self.journal.append("executor-isolation-components", {
                "target_sha256": digest(target), "boundary_sha256": digest(before), "probes": results,
                "remaining": REMAINING, "criterion_complete": False})
            self.evidence.progress.append({"kind": "executor-isolation-components", "criterion": "TF01/AC-02",
                "scenario": "TF01/SCN-02", "outcome": "partial", "collector_receipt": receipt,
                "observed": sorted(OBSERVATIONS), "remaining": REMAINING})
        except Exception as error:
            self.journal.append("executor-isolation-incomplete", {"stage": stage, "exception_type": type(error).__name__})
            self.evidence.progress.append({"kind": "executor-isolation-components", "criterion": "TF01/AC-02",
                "scenario": "TF01/SCN-02", "outcome": "incomplete", "stage": stage,
                "next_action": ACTIONS[stage], "remaining": REMAINING})
        finally:
            if self.api is not None:
                self.api.client.close()

    def probe(self, target, kind, image, addresses, *, phase):
        from l5_services.adapters.transform import isolation_probe
        program = Path(isolation_probe.__file__).read_text()
        selection = {"protocol": PROTOCOL, "nonce": digest([self.journal.binding_sha256, kind]),
                     "program_sha256": sha256(program.encode()).hexdigest(), "control_addresses": addresses}
        name = "tf-isolation-" + digest([self.journal.binding_sha256, kind])[:40]
        expected = pod_manifest(target, image, name, program, selection, self.journal.binding_sha256)
        path = f"/api/v1/namespaces/{target['namespace']}/pods/{name}"
        prior = [row for row in self.journal.records if row["kind"] == "executor-isolation-intent"
                 and row["value"].get("name") == name]
        outputs = [row for row in self.journal.records if row["kind"] == "executor-isolation-output"
                   and row["value"].get("kind") == kind]
        if outputs:
            require(len(outputs) == 1 and len(prior) == 1
                    and prior[0]["value"]["manifest_sha256"] == digest(expected), "original probe evidence changed")
            stored = outputs[0]["value"]
            uid = checked_pod(stored["pod"], expected)
            raw = stored["output"].encode()
            require(sha256(raw).hexdigest() == stored["output_sha256"], "original probe output changed")
            terminated = stored["pod"]["status"]["containerStatuses"][0]["state"]["terminated"]
            require(terminated.get("exitCode") == 0 and terminated.get("reason") == "Completed",
                    "original probe failed; do not replace its observation")
            value = verified_result(raw, selection)
            cleanups = [row for row in self.journal.records if row["kind"] == "executor-isolation-cleanup"
                        and row["value"] == {"path": path, "uid": uid, "observed_absent": True}]
            require(len(cleanups) == 1 and self.api.request("GET", path, metadata_only=True,
                    maximum=65536, timeout_seconds=2) is None, "original probe cleanup remains incomplete")
            return {"kind": kind, "pod_uid": uid, "image": image, "output_receipt": digest(outputs[0]),
                    "observations": value["observations"], "owned_absence": True}
        require(phase == "run", "original probe output is missing; emission cannot launch work")
        # Never recreate an execution that may already have run and disappeared.
        pod = self.api.request("GET", path, maximum=65536, timeout_seconds=2)
        if pod is None:
            require(not prior, "original isolation execution missing; reconcile it without replacement")
            self.journal.append("executor-isolation-intent", {"name": name, "manifest_sha256": digest(expected)})
            pod = self.api.request("POST", path.rsplit("/", 1)[0], body=expected, maximum=65536, timeout_seconds=2)
        else:
            require(len(prior) == 1 and prior[0]["value"].get("manifest_sha256") == digest(expected),
                    "probe name already belongs to another execution")
        uid = checked_pod(pod, expected)
        self.journal.append("executor-isolation-created", {"name": name, "uid": uid})
        deadline = time.monotonic() + 100
        while True:
            checked_pod(pod, expected, uid)
            statuses = pod.get("status", {}).get("containerStatuses", [])
            terminated = statuses[0].get("state", {}).get("terminated") if len(statuses) == 1 else None
            if terminated is not None:
                break
            require(time.monotonic() < deadline, "isolation probe termination unknown; retain its finalizer")
            time.sleep(1)
            pod = self.api.request("GET", path, maximum=65536, timeout_seconds=2)
        require(statuses[0].get("name") == "isolation" and statuses[0].get("imageID", "").endswith(image.split("@", 1)[1])
                and statuses[0].get("restartCount") == 0 and terminated.get("startedAt") and terminated.get("finishedAt"),
                "probe runtime identity or termination evidence differs")
        raw = self.api.request("GET", path + "/log", params={"container": "isolation", "limitBytes": 16384},
                               raw=True, maximum=16384, timeout_seconds=2)
        latest = self.api.request("GET", path, maximum=65536, timeout_seconds=2)
        checked_pod(latest, expected, uid)
        require(latest.get("status", {}).get("containerStatuses") == statuses, "probe termination changed during readback")
        # Preserve failed controls and observed termination before owned cleanup.
        receipt = self.journal.append("executor-isolation-output", {"kind": kind, "pod": pod,
            "output": raw.decode("utf-8"), "output_sha256": sha256(raw).hexdigest()})
        self.cleanup(path, latest, uid)
        require(terminated.get("exitCode") == 0 and terminated.get("reason") == "Completed", "fixed isolation controls failed")
        value = verified_result(raw, selection)
        return {"kind": kind, "pod_uid": uid, "image": image, "output_receipt": receipt,
                "observations": value["observations"], "owned_absence": True}

    def cleanup(self, path, pod, uid):
        metadata = pod["metadata"]
        require(metadata.get("uid") == uid and metadata.get("resourceVersion")
                and metadata.get("finalizers") == [FINALIZER], "probe cleanup ownership changed")
        self.api.request("PATCH", path, body=[
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
            {"op": "test", "path": "/metadata/finalizers", "value": [FINALIZER]},
            {"op": "replace", "path": "/metadata/finalizers", "value": []}], maximum=65536, timeout_seconds=2)
        self.api.request("DELETE", path, body={"apiVersion": "v1", "kind": "DeleteOptions",
                         "preconditions": {"uid": uid}}, maximum=65536, timeout_seconds=2)
        require(self.api.request("GET", path, metadata_only=True, maximum=65536, timeout_seconds=2) is None,
                "original probe absence is not yet observed")
        self.journal.append("executor-isolation-cleanup", {"path": path, "uid": uid, "observed_absent": True})


def main(argv=None):
    """Optional component-only collection; never invoke the native run driver."""
    from l5_services.adapters.transform import qualification_release as native
    from qualification_transform_acceptance import EvidenceSet, catalog
    from qualification_transform_journal import ReleaseJournal, write_private
    from qualification_transform_release import options
    from release_context import authorize_engine, release_identity

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("run", "emit"))
    parser.add_argument("source_sha")
    parser.add_argument("fixture_path")
    parser.add_argument("evidence_dir")
    args = parser.parse_args(argv)
    try:
        context = release_identity(os.environ)
        require(context["service"] == "transform" and context["source_revision"] == args.source_sha,
                "component collection differs from original release identity")
        authorize_engine(os.environ, context)
        fixture = native.load_fixture(args.source_sha, args.fixture_path)
        selected = options(fixture)
        require(selected.get("executor_isolation") is True, "component collection was not selected")
        native.evidence_path(args.evidence_dir, "qualification.json")
        journal = ReleaseJournal(args.evidence_dir, context, sha256(Path(args.fixture_path).read_bytes()).hexdigest(), catalog()[1])
        with journal.locked():
            evidence = EvidenceSet(journal)
            InstalledIsolation(evidence, selected["installed"]).collect(phase=args.phase)
            progress, = evidence.progress
            path = journal.directory / f"executor-isolation-summary-{len(journal.records)}.json"
            write_private(path, {"binding_sha256": journal.binding_sha256, "progress": progress,
                                 "criterion_complete": False, "native_calls": False})
            journal.append("executor-isolation-summary", {"path": path.name, "sha256": sha256(path.read_bytes()).hexdigest()})
        if progress["outcome"] != "partial":
            print(progress["next_action"])
            return 1
        print("Executor component observations retained; original TF01 AC-02 and SCN-02 remain incomplete; no acceptance criterion passed")
        return 0
    except Exception:
        print("Executor component collection incomplete; verify original release identity, private fixture and protected infrastructure configuration")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

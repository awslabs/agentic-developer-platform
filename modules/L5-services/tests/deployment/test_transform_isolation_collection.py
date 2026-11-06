"""Exercise installed component collection and negative controls without cloud calls."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from qualification_transform_acceptance import EvidenceSet, catalog, evaluate, validate_catalog
from qualification_transform_isolation import (
    InstalledIsolation, OBSERVATIONS, PROTOCOL, REMAINING, checked_pod, installed_target,
    pod_manifest, verified_result,
)
import qualification_transform_isolation as isolation
from qualification_transform_journal import ReleaseJournal, digest
from l5_services.adapters.transform import isolation_probe


TARGET = {"worker_revision": "worker-revision", "worker_code_sha256": "worker-code",
    "cluster_arn": "arn:aws:eks:us-east-1:000000000000:cluster/example", "namespace": "transform-checks",
    "images": {"provider": "000000000000.dkr.ecr.us-east-1.amazonaws.com/adp-dev-l5-transform-executor@sha256:" + "a" * 64,
               "check": "000000000000.dkr.ecr.us-east-1.amazonaws.com/adp-dev-l5-transform-executor@sha256:" + "b" * 64},
    "node_name": "example-node", "node_uid": "example-node-uid", "endpoint": "https://example.eks.amazonaws.com", "ca": "certificate"}
CONTEXT = {"service": "transform", "environment": "dev", "source_revision": "c" * 40,
    "definition_revision": "d" * 40, "repository_id": "1", "run_id": "2", "run_attempt": "1",
    "contract_digest": "e" * 64, "correlation": "example-correlation"}


def output(selection):
    return {**selection, "observations": dict.fromkeys(OBSERVATIONS, True),
        "source_sha256": sha256(b"original-source-v1\n").hexdigest(),
        "candidate_sha256": sha256(b"admitted-write-v1\n").hexdigest(),
        "native_calls": False, "qualified": False, "live_acceptance": "not_evaluated"}


class Kubernetes:
    def __init__(self, fault=None):
        self.pods, self.calls, self.outputs, self.fault = {}, [], {}, fault
        self.client = SimpleNamespace(close=lambda: None)

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.startswith("/api/v1/nodes/"):
            return {"metadata": {"uid": TARGET["node_uid"], "name": TARGET["node_name"], "labels": {}},
                    "spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        if "networkpolicies" in path:
            return {"metadata": {"name": "deny-all", "namespace": TARGET["namespace"], "uid": "policy-uid"},
                    "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}
        if method == "POST":
            assert path.endswith("/pods")
            pod = deepcopy(kwargs["body"])
            pod["metadata"].update(uid="uid-" + str(len(self.pods)), resourceVersion="1")
            for key in ("hostNetwork", "hostPID", "hostIPC"):
                pod["spec"].pop(key)  # Kubernetes omits false defaulted booleans.
            container = pod["spec"]["containers"][0]
            selection = json.loads(container["command"][-1])
            value = output(selection)
            pod["status"] = {"containerStatuses": [{"name": "isolation", "restartCount": 0,
                "imageID": "docker-pullable://" + container["image"],
                "state": {"terminated": {"exitCode": 0, "reason": "Completed",
                                          "startedAt": "2026-10-31T12:00:00Z", "finishedAt": "2026-10-31T12:00:10Z"}}}]}
            if self.fault == "foreign_image": pod["status"]["containerStatuses"][0]["imageID"] = "different"
            if self.fault == "incomplete_probe": value["observations"]["metadata_denied"] = False
            if self.fault == "credential_env": container["env"] = [{"name": "AWS_ACCESS_KEY_ID", "value": "never-print"}]
            if self.fault == "unknown_termination": pod["status"] = {}
            name = pod["metadata"]["name"]
            self.pods[path + "/" + name] = pod
            self.outputs[path + "/" + name + "/log"] = json.dumps(value).encode()
            if self.fault == "lost_create":
                self.fault = None
                raise TimeoutError("response lost")
            return deepcopy(pod)
        if method == "GET" and path.endswith("/log"):
            return self.outputs[path]
        if method == "GET":
            return deepcopy(self.pods.get(path))
        if method == "PATCH":
            expected = self.pods[path]["metadata"]
            assert kwargs["body"][:3] == [
                {"op": "test", "path": "/metadata/uid", "value": expected["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": expected["resourceVersion"]},
                {"op": "test", "path": "/metadata/finalizers", "value": [isolation.FINALIZER]}]
            self.pods[path]["metadata"]["finalizers"] = []
            return deepcopy(self.pods[path])
        if method == "DELETE":
            assert kwargs["body"]["preconditions"] == {"uid": self.pods[path]["metadata"]["uid"]}
            assert self.pods[path]["metadata"]["finalizers"] == []
            return self.pods.pop(path)
        raise AssertionError((method, path))


@pytest.fixture
def collection(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    api = Kubernetes()
    monkeypatch.setattr(isolation, "installed_target", lambda *args: (api, deepcopy(TARGET)))
    monkeypatch.setattr(isolation, "network_control", lambda target: ["10.0.0.10"])
    journal = ReleaseJournal(tmp_path.resolve(), CONTEXT, "f" * 64, catalog()[1])
    with journal.locked():
        evidence = EvidenceSet(journal)
        yield api, journal, evidence, InstalledIsolation(evidence, {}, environment={})


def test_two_installed_image_probes_cleanup_and_original_criteria_remain_incomplete(collection):
    api, journal, evidence, collector = collection
    collector.collect(phase="run")
    progress, = evidence.progress
    assert progress["outcome"] == "partial" and progress["remaining"] == REMAINING
    assert progress["criterion"] == "TF01/AC-02" and progress["scenario"] == "TF01/SCN-02"
    assert not api.pods and len([c for c in api.calls if c[0] == "POST"]) == 2
    assert {c[0] for c in api.calls} == {"GET", "POST", "PATCH", "DELETE"}
    assert all(c[1].startswith(("/api/v1/nodes/", "/api/v1/namespaces/transform-checks/pods",
                               "/apis/networking.k8s.io/v1/namespaces/transform-checks/networkpolicies/deny-all"))
               for c in api.calls)
    report = evaluate(evidence)
    validate_catalog(report)
    assert all(row["outcome"] == "incomplete" for section in ("epic_criteria", "story_criteria", "scenarios", "live_checks")
               for row in report[section])
    assert not report["observed_criteria"]
    assert len([row for row in journal.records if row["kind"] == "executor-isolation-cleanup"]) == 2


def test_emit_reverifies_original_probes_and_absence_without_launching(collection):
    api, journal, evidence, collector = collection
    collector.collect(phase="run")
    api.calls.clear()
    other = EvidenceSet(journal)
    InstalledIsolation(other, {}, environment={}).collect(phase="emit")
    assert other.progress[0]["outcome"] == "partial"
    assert {call[0] for call in api.calls} == {"GET"}


def test_emit_cannot_create_missing_original_observations(collection):
    api, _, evidence, collector = collection
    collector.collect(phase="emit")
    assert evidence.progress[0]["outcome"] == "incomplete"
    assert {call[0] for call in api.calls} == {"GET"}


@pytest.mark.parametrize("fault", ["foreign_image", "credential_env", "incomplete_probe"])
def test_changed_or_failed_controls_never_gain_acceptance(collection, fault):
    api, _, evidence, collector = collection
    api.fault = fault
    collector.collect(phase="run")
    assert evidence.progress[0]["outcome"] == "incomplete"
    assert not evidence.verified()
    assert "never-print" not in json.dumps(evidence.progress)
    if fault == "incomplete_probe": assert not api.pods


def test_lost_create_response_reuses_exact_existing_pod(collection):
    api, journal, _, collector = collection
    api.fault = "lost_create"
    collector.collect(phase="run")
    assert len(api.pods) == 1
    evidence = EvidenceSet(journal)
    InstalledIsolation(evidence, {}, environment={}).collect(phase="run")
    assert evidence.progress[0]["outcome"] == "partial"
    assert len([call for call in api.calls if call[0] == "POST"]) == 2


def test_missing_pod_after_lost_response_does_not_launch_replacement(collection):
    api, journal, _, collector = collection
    api.fault = "lost_create"
    collector.collect(phase="run")
    api.pods.clear()
    evidence = EvidenceSet(journal)
    InstalledIsolation(evidence, {}, environment={}).collect(phase="run")
    assert evidence.progress[0]["outcome"] == "incomplete"
    assert len([call for call in api.calls if call[0] == "POST"]) == 1


def test_unknown_termination_retains_finalizer_and_never_deletes(collection, monkeypatch):
    api, _, evidence, collector = collection
    api.fault = "unknown_termination"
    ticks = iter([0, 101])
    monkeypatch.setattr(isolation.time, "monotonic", lambda: next(ticks))
    collector.collect(phase="run")
    assert evidence.progress[0]["outcome"] == "incomplete"
    assert not any(call[0] in {"DELETE", "PATCH"} for call in api.calls)
    assert next(iter(api.pods.values()))["metadata"]["finalizers"] == [isolation.FINALIZER]


@pytest.mark.parametrize("fault", ["hostNetwork", "automountServiceAccountToken", "image", "args", "lifecycle", "initContainers"])
def test_pod_validation_refuses_workload_or_security_mutation(fault):
    selection = {"protocol": PROTOCOL, "nonce": "a" * 64, "program_sha256": "b" * 64, "control_addresses": ["10.0.0.10"]}
    expected = pod_manifest(TARGET, TARGET["images"]["check"], "probe", "source", selection, "c" * 64)
    pod = deepcopy(expected)
    pod["metadata"].update(uid="pod-uid", resourceVersion="1")
    if fault in {"hostNetwork", "automountServiceAccountToken"}: pod["spec"][fault] = True
    elif fault == "initContainers": pod["spec"][fault] = [{"name": "extra"}]
    else: pod["spec"]["containers"][0][fault] = "changed"
    with pytest.raises(ValueError): checked_pod(pod, expected)


@pytest.mark.parametrize("fault", ["nonce", "program_sha256", "native_calls", "qualified", "source_sha256", "missing_probe"])
def test_result_requires_complete_original_program_bound_facts(fault):
    selection = {"protocol": PROTOCOL, "nonce": "a" * 64, "program_sha256": "b" * 64, "control_addresses": ["10.0.0.10"]}
    value = output(selection)
    if fault == "missing_probe": value["observations"].pop("metadata_denied")
    else: value[fault] = True if fault in {"native_calls", "qualified"} else "changed"
    with pytest.raises(ValueError): verified_result(json.dumps(value).encode(), selection)


def test_refused_connection_is_not_mistaken_for_enforced_network_denial(monkeypatch):
    def refused(*args, **kwargs): raise ConnectionRefusedError(111, "refused")
    monkeypatch.setattr(isolation_probe.socket, "create_connection", refused)
    with pytest.raises(ValueError, match="inconclusive"):
        isolation_probe.denied_connect("10.0.0.10", 443)


def test_probe_refuses_inherited_identity_before_any_network_or_filesystem_probe(monkeypatch):
    selection = {"protocol": PROTOCOL, "nonce": "a" * 64, "program_sha256": "b" * 64, "control_addresses": ["10.0.0.10"]}
    monkeypatch.setattr(isolation_probe.os, "getuid", lambda: 10000)
    monkeypatch.setattr(isolation_probe.os, "getgid", lambda: 10000)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-test-value")
    with pytest.raises(ValueError, match="inherited platform identity"):
        isolation_probe.run(selection)


@pytest.mark.parametrize("fault", [None, "region", "node_pin", "worker_code", "handler", "layer", "role", "endpoint", "image_tag"])
def test_real_installed_target_reader_binds_every_protected_input(monkeypatch, fault):
    import boto3
    from l5_services.adapters.transform import kubernetes
    account, region = "000000000000", "us-east-1"
    role = f"arn:aws:iam::{account}:role/adp-dev-trusted-deployment"
    worker = "adp-dev-l5-transform-worker"
    expected = {"L5_TRANSFORM_" + key: value for key, value in {
        "CLUSTER_NAME": "example", "CLUSTER_ENDPOINT": TARGET["endpoint"], "CLUSTER_CA": "certificate",
        "NODE_NAME": TARGET["node_name"], "NODE_UID": TARGET["node_uid"],
        "PROVIDER_NAMESPACE": "transform-provider", "CHECK_NAMESPACE": TARGET["namespace"],
        "PROVIDER_IMAGE": TARGET["images"]["provider"], "CHECK_IMAGE": TARGET["images"]["check"],
        "ARTIFACT_BUCKET": "private-artifacts"}.items()}
    factory = "l5_services.runtime_transform:factory"
    protected = {"environment": "dev", "account_id": account, "region": region, "role_arn": role,
        "services": {"transform": {"worker_factory": factory, "worker_environment": deepcopy(expected)}}}
    current = {"FunctionArn": f"arn:aws:lambda:{region}:{account}:function:{worker}",
        "Role": f"arn:aws:iam::{account}:role/{worker}", "CodeSha256": "worker-code", "RevisionId": "worker-revision",
        "State": "Active", "LastUpdateStatus": "Successful", "Handler": "l5_services.workers.execution.lambda_handler",
        "Runtime": "python3.12", "Environment": {"Variables": {**expected, "L5_WORKER_FACTORY": factory, "L5_SERVICE_ID": "transform"}}}
    cluster = {"arn": TARGET["cluster_arn"], "status": "ACTIVE", "endpoint": TARGET["endpoint"],
               "certificateAuthority": {"data": "certificate"}}
    if fault == "region": protected["region"] = "us-west-2"
    elif fault == "node_pin": protected["services"]["transform"]["worker_environment"]["L5_TRANSFORM_NODE_UID"] = "other"
    elif fault == "worker_code": current["CodeSha256"] = "different"
    elif fault == "handler": current["Handler"] = "l5_services.workers.recovery.lambda_handler"
    elif fault == "layer": current["Layers"] = [{"Arn": "other-layer"}]
    elif fault == "role": current["Role"] += "-other"
    elif fault == "endpoint": cluster["endpoint"] = "https://other.eks.amazonaws.com"
    elif fault == "image_tag":
        current["Environment"]["Variables"]["L5_TRANSFORM_CHECK_IMAGE"] = "image:latest"
        protected["services"]["transform"]["worker_environment"]["L5_TRANSFORM_CHECK_IMAGE"] = "image:latest"
    calls = []
    def client(name, **kwargs):
        calls.append(name)
        if name == "sts": return SimpleNamespace(get_caller_identity=lambda: {
            "Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/adp-dev-trusted-deployment/session"})
        if name == "lambda": return SimpleNamespace(get_function_configuration=lambda **kwargs: current)
        if name == "eks": return SimpleNamespace(describe_cluster=lambda **kwargs: {"cluster": cluster})
        raise AssertionError(name)
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: SimpleNamespace(client=client))
    monkeypatch.setattr(kubernetes, "TransformKubernetesAPI", lambda **kwargs: SimpleNamespace(**kwargs))
    installed = {"release": {"queue_arn": f"arn:aws:sqs:{region}:{account}:adp-dev-l5-transform-work",
        "account_id": account, "source_revision": CONTEXT["source_revision"], "role_arn": role,
        "worker_code_sha256": "worker-code"}, "selection": {"cluster_name": "example"}}
    if fault:
        with pytest.raises(ValueError):
            installed_target(installed, CONTEXT, {"L5_INFRA_CONFIG_JSON": json.dumps(protected)})
    else:
        _, result = installed_target(installed, CONTEXT, {"L5_INFRA_CONFIG_JSON": json.dumps(protected)})
        assert result == TARGET
    assert set(calls) <= {"sts", "lambda", "eks"}

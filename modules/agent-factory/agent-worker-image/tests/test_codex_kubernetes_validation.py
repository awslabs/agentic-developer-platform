"""Host Kubernetes lifecycle boundaries; these do not qualify cluster isolation."""

import copy
import hashlib
import io
import tarfile
import threading
import uuid

import pytest

from lib.codex_kubernetes_validation import FINALIZER, KubernetesValidationExecutor
from lib.codex_validation import ValidationCancelled, ValidationCheck, ValidationUnavailable


class API:
    def __init__(self):
        self.pod = None
        self.maps = {}
        self.calls = []
        self.output = b"passed\n"
        self.exit_code = 0
        self.network_allow = False
        self.lose_create = False
        self.running = False
        self.partitioned = False
        self.replace_uid = False
        self.cancel = None

    def request(self, method, path, *, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/namespaces/validation"):
            return {"metadata": {"labels": {"pod-security.kubernetes.io/enforce": "restricted", "adp.dev/validation": "true"}}}
        if path.endswith("/networkpolicies"):
            policies = [{"spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}]
            return {"items": policies + ([{"spec": {"egress": [{}]}}] if self.network_allow else [])}
        if method == "GET" and path.endswith("/configmaps"):
            return {"items": [copy.deepcopy(item) for item in self.maps.values()
                              if item["metadata"]["labels"].get("adp.dev/validation-intent") == "true"]}
        if method == "POST" and path.endswith("/configmaps"):
            self.maps[body["metadata"]["name"]] = copy.deepcopy(body)
            return body
        if method == "DELETE" and "/configmaps/" in path:
            return self.maps.pop(path.rsplit("/", 1)[1], None)
        if method == "GET" and "/configmaps/" in path:
            return copy.deepcopy(self.maps.get(path.rsplit("/", 1)[1]))
        if method == "POST" and path.endswith("/pods"):
            self.pod = copy.deepcopy(body)
            self.pod["metadata"].update(uid="pod-uid", resourceVersion="1")
            self.pod["spec"]["nodeName"] = "node-1"
            self.pod["status"] = {"containerStatuses": [{
                "name": "check", "containerID": "containerd://check",
                "imageID": "registry.example/checks@sha256:" + "a" * 64,
                "state": {"running": {}} if self.running else {"terminated": {"exitCode": self.exit_code}},
            }]}
            if self.lose_create:
                raise ValidationUnavailable("lost create acknowledgement")
            return copy.deepcopy(self.pod)
        if method == "GET" and path.endswith("/log"):
            return self.output[:kwargs["params"]["limitBytes"]]
        if method == "GET" and "/pods/" in path:
            if self.cancel:
                self.cancel.set()
            result = copy.deepcopy(self.pod)
            if self.replace_uid and result:
                result["metadata"]["uid"] = "replacement"
            return result
        if method == "DELETE" and "/pods/" in path:
            assert body["preconditions"]["uid"] == "pod-uid"
            self.pod["metadata"]["deletionTimestamp"] = "2026-09-26T00:00:00Z"
            if not self.partitioned:
                self.pod["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 137}}
            return copy.deepcopy(self.pod)
        if method == "PATCH" and "/pods/" in path:
            assert body[0] == {"op": "test", "path": "/metadata/uid", "value": "pod-uid"}
            assert "terminated" in self.pod["status"]["containerStatuses"][0]["state"]
            self.pod = None
            return {}
        raise AssertionError((method, path))


@pytest.fixture
def execution(tmp_path):
    source = tmp_path / "source.tar"
    with tarfile.open(source, "w") as stream:
        item = tarfile.TarInfo("source.txt")
        item.size = 4
        stream.addfile(item, io.BytesIO(b"code"))
    api = API()
    now = [0]
    def sleep(seconds):
        now[0] += seconds
    executor = KubernetesValidationExecutor(
        api=api, namespace="validation", task_id="tsk_" + str(uuid.uuid4()), clock=lambda: now[0], sleep=sleep,
    )
    args = dict(check=ValidationCheck("checks", "registry.example/checks@sha256:" + "a" * 64, ("/checks/test",)),
                archive=source, archive_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), commit="b" * 40)
    return executor, api, args


def test_observed_exit_and_cleanup_precede_success(execution):
    executor, api, args = execution
    result = executor.run(**args)
    assert result["status"] == "passed" and result["exitCode"] == 0
    assert result["output"] == "passed\n"
    assert api.pod is None and api.maps == {}
    pod = next(body for method, path, body in api.calls if method == "POST" and path.endswith("/pods"))
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["hostNetwork"] is False and spec["hostPID"] is False and spec["hostIPC"] is False
    assert spec["restartPolicy"] == "Never"
    assert len(spec["containers"]) == 1
    container = spec["containers"][0]
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert {entry["name"] for entry in container["env"]} == {"HOME", "BG_CONFIG_DIR", "TMPDIR"}
    assert all("hostPath" not in volume and "secret" not in volume for volume in spec["volumes"])
    assert pod["metadata"]["finalizers"] == [FINALIZER]


@pytest.mark.parametrize("mode,reason", [("failure", "process_failed"), ("output", "output_limit"), ("timeout", "timeout"), ("cancel", "cancelled")])
def test_failed_and_interrupted_checks_never_pass(execution, mode, reason):
    executor, api, args = execution
    if mode == "failure":
        api.exit_code = 7
    elif mode == "output":
        api.output = b"x" * 40000
    else:
        api.running = True
        if mode == "cancel":
            args["cancelled"] = api.cancel = threading.Event()
    result = executor.run(**args)
    assert result["status"] == "failed" and result["reason"] == reason
    assert len(result["output"].encode()) <= args["check"].max_output_bytes
    assert api.pod is None and api.maps == {}


def test_partitioned_node_retains_finalizer_source_and_unknown_outcome(execution):
    executor, api, args = execution
    api.running = api.partitioned = True
    with pytest.raises(ValidationUnavailable):
        executor.run(**args)
    assert api.pod["metadata"]["finalizers"] == [FINALIZER]
    assert api.maps
    assert not any(method == "PATCH" for method, _, _ in api.calls)
    assert executor.clock() <= args["check"].timeout_seconds + 15


def test_lost_create_response_never_blindly_retries_or_discards_intent(execution):
    executor, api, args = execution
    api.lose_create = True
    with pytest.raises(ValidationUnavailable, match="lost create"):
        executor.run(**args)
    assert len([1 for method, path, _ in api.calls if method == "POST" and path.endswith("/pods")]) == 1
    assert api.maps and api.pod is not None


def test_replacement_host_stops_persisted_work_without_rerunning_it(execution):
    executor, api, args = execution
    api.lose_create = api.running = True
    with pytest.raises(ValidationUnavailable):
        executor.run(**args)
    assert executor.recover()
    assert api.pod is None and api.maps == {}
    assert len([1 for method, path, _ in api.calls if method == "POST" and path.endswith("/pods")]) == 1


def test_missing_pod_after_uncertain_create_is_not_clean_shutdown(execution):
    executor, api, args = execution
    api.lose_create = True
    with pytest.raises(ValidationUnavailable):
        executor.run(**args)
    api.pod = None
    with pytest.raises(ValidationUnavailable, match="requires reconciliation"):
        executor.recover()
    assert api.maps


def test_cleanup_resumes_from_durable_exit_receipt_after_pod_removal(execution):
    executor, api, args = execution
    api.lose_create = True
    with pytest.raises(ValidationUnavailable):
        executor.run(**args)
    name = api.pod["metadata"]["name"]
    executor._remove(name, "pod-uid")  # Crash before source/intent cleanup.
    assert api.pod is None and api.maps
    assert executor.recover()
    assert api.maps == {}


def test_recovery_cannot_delete_a_different_tasks_pod(execution):
    from lib.codex_kubernetes_validation import LABEL
    executor, api, args = execution
    api.lose_create = True
    with pytest.raises(ValidationUnavailable):
        executor.run(**args)
    api.pod["metadata"]["labels"][LABEL] = "another-task"
    with pytest.raises(ValidationUnavailable, match="identity changed"):
        executor.recover()
    assert not any(method == "DELETE" and "/pods/" in path for method, path, _ in api.calls)


def test_replacement_pod_cannot_supply_or_release_termination_evidence(execution):
    executor, api, args = execution
    api.replace_uid = True
    with pytest.raises(ValidationUnavailable, match="identity changed"):
        executor.run(**args)
    assert not any(method == "PATCH" for method, _, _ in api.calls)
    assert api.maps


@pytest.mark.parametrize("mode", ["cancelled", "network", "image", "source"])
def test_preflight_refusal_creates_no_workload(execution, mode):
    executor, api, args = execution
    if mode == "cancelled":
        args["cancelled"] = threading.Event()
        args["cancelled"].set()
    elif mode == "network":
        api.network_allow = True
    elif mode == "image":
        args["check"] = ValidationCheck("checks", "sha256:" + "a" * 64, ("true",))
    else:
        args["archive_sha256"] = "0" * 64
    with pytest.raises((ValueError, ValidationUnavailable, ValidationCancelled)):
        executor.run(**args)
    assert not any(method == "POST" and path.endswith("/pods") for method, path, _ in api.calls)
    assert api.pod is None and not api.maps

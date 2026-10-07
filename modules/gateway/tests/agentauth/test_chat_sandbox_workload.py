"""Role-free sandbox workload proof cannot be substituted with a legacy chat token."""

from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from src.agentauth import workload

DIGEST = "sha256:" + "a" * 64
LEGACY_DIGEST = "sha256:" + "b" * 64
NAME = "chat-turn-" + "c" * 12 + "-abcde"
ORIGIN = "https://chat-sandbox-gateway.adp-gateway.svc:8443"
CA_CONFIGMAP = "chat-sandbox-gateway-ca-" + "d" * 16


def sandbox_spec():
    return {
        "serviceAccountName": "adp-chat-sandbox",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "shareProcessNamespace": False,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "hostAliases": [{"ip": "10.100.42.17", "hostnames": ["chat-sandbox-gateway.adp-gateway.svc"]}],
        "restartPolicy": "Never",
        "activeDeadlineSeconds": 900,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "fsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumes": [
            {
                "name": "sandbox-identity",
                "projected": {
                    "defaultMode": 292,
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "audience": workload.BOOTSTRAP_AUDIENCE,
                                "expirationSeconds": 600,
                                "path": "token",
                            }
                        },
                    ],
                },
            },
            {"name": "scratch", "emptyDir": {"sizeLimit": "512Mi"}},
            {"name": "gateway-ca", "configMap": {"name": CA_CONFIGMAP, "defaultMode": 292, "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
        ],
        "containers": [
            {
                "name": "chat-agent",
                "image": f"registry/chat@{DIGEST}",
                "imagePullPolicy": "IfNotPresent",
                "command": ["/app/chat-sandbox-entrypoint"],
                "workingDir": "/tmp",
                "env": [
                    {"name": name, "value": value}
                    for name, value in {
                        "ADP_CHAT_MODEL_POLICY_ENABLED": "true",
                        "ADP_CHAT_DATA_ENABLED": "true",
                        "ADP_CHAT_DATA_URL": ORIGIN,
                        "CONTEXT_STRATEGY": "gateway",
                        "MEMORY_STRATEGY": "gateway",
                        "ARTIFACT_STRATEGY": "gateway",
                        "ADP_WORKLOAD_TOKEN_FILE": "/var/run/adp-model/token",
                        "CLAUDE_CONFIG_DIR": "/tmp/workspace/.claude",
                        "HOME": "/tmp/workspace",
                        "NODE_EXTRA_CA_CERTS": "/var/run/adp-chat-ca/ca.crt",
                    }.items()
                ],
                "volumeMounts": [
                    {"name": "sandbox-identity", "mountPath": "/var/run/adp-model", "readOnly": True},
                    {"name": "scratch", "mountPath": "/tmp"},
                    {"name": "gateway-ca", "mountPath": "/var/run/adp-chat-ca", "readOnly": True},
                ],
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 10001,
                    "readOnlyRootFilesystem": True,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
            }
        ],
    }


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    state = {
        "account": "adp-chat-sandbox",
        "pod_account": "adp-chat-sandbox",
        "image": DIGEST,
        "audience": workload.BOOTSTRAP_AUDIENCE,
        "phase": "Running",
        "container_state": {"running": {}},
        "spec": sandbox_spec(),
        "labels": {"adp.io/run-hash": "c" * 64, "adp.io/chat-sandbox": "true"},
    }
    (tmp_path / "token").write_text("gateway-token")
    monkeypatch.setattr(workload, "_SA_DIRECTORY", tmp_path)
    monkeypatch.setattr(workload.ssl, "create_default_context", lambda **_: None)
    monkeypatch.setenv("ADP_CHAT_SANDBOX_IMAGE_DIGESTS", DIGEST)
    monkeypatch.setenv("ADP_CHAT_DATA_URL", ORIGIN)
    monkeypatch.setenv("ADP_CHAT_SANDBOX_CA_CONFIGMAP", CA_CONFIGMAP)
    monkeypatch.setenv("ADP_CHAT_WORKER_IMAGE_DIGESTS", LEGACY_DIGEST)
    monkeypatch.setenv("ADP_CHAT_WORKER_SERVICE_ACCOUNT", "adp-agent")

    def kubernetes(request):
        if request.method == "POST":
            assert request.url.path.endswith("/tokenreviews")
            assert request.headers["Authorization"] == "Bearer gateway-token"
            assert request.read() and request.url.path.endswith("/tokenreviews")
            return httpx.Response(
                201,
                json={
                    "status": {
                        "authenticated": True,
                        "audiences": [state["audience"]],
                        "user": {
                            "username": f"system:serviceaccount:adp-gateway-agents:{state['account']}",
                            "extra": {
                                "authentication.kubernetes.io/pod-name": [NAME],
                                "authentication.kubernetes.io/pod-uid": ["pod-uid"],
                            },
                        },
                    }
                },
            )
        assert request.url.path == f"/api/v1/namespaces/adp-gateway-agents/pods/{NAME}"
        return httpx.Response(
            200,
            json={
                "metadata": {"uid": "pod-uid", "name": NAME, "namespace": "adp-gateway-agents", "labels": state["labels"]},
                "spec": {**state["spec"], "serviceAccountName": state["pod_account"]},
                "status": {
                    "phase": state["phase"],
                    "podIP": "10.0.0.5",
                    "containerStatuses": [
                        {
                            "name": "chat-agent",
                            "imageID": f"registry/chat@{state['image']}",
                            "state": state["container_state"],
                        }
                    ],
                },
            },
        )

    client = httpx.Client(base_url="https://kubernetes.test", transport=httpx.MockTransport(kubernetes))
    monkeypatch.setattr(workload.httpx, "Client", lambda **_: client)
    runtime = workload.KubernetesWorkloadVerifier.in_cluster(chat_sandbox=True)
    runtime._gateway_token_path = tmp_path / "token"
    return runtime, state


@pytest.mark.parametrize("changed", [None, "run", "session", "image", "account"])
def test_reserved_discovery_revalidates_running_pod_scope(verifier, changed):
    runtime, state = verifier
    state["spec"].pop("activeDeadlineSeconds")
    state["labels"]["adp.io/session-hash"] = "d" * 64
    expected = {"name": NAME, "run_hash": state["labels"]["adp.io/run-hash"], "image_digest": DIGEST, "session_hash": "d" * 64}
    if changed in {"run", "session", "image"}:
        field = {"run": "run_hash", "session": "session_hash", "image": "image_digest"}[changed]
        expected[field] = "f" * 64 if changed != "image" else LEGACY_DIGEST
    elif changed == "account":
        state["pod_account"] = "adp-agent"
    if changed is None:
        assert runtime.find_reserved_sandbox(**expected).uid == "pod-uid"
    else:
        with pytest.raises(workload.WorkloadRefusedError):
            runtime.find_reserved_sandbox(**expected)


@pytest.mark.parametrize("status,valid", [(404, True), (404, False), (403, False), (503, False)])
def test_reserved_discovery_creates_only_after_exact_absence(verifier, monkeypatch, status, valid):
    runtime, state = verifier
    response = httpx.Response(
        status,
        request=httpx.Request("GET", "https://kubernetes.test/pods/reserved"),
        json={"kind": "Status", "reason": "NotFound", "details": {"name": NAME if valid else "different", "kind": "pods"}},
    )
    monkeypatch.setattr(runtime._client, "get", lambda *args, **kwargs: response)
    expected = {"name": NAME, "run_hash": state["labels"]["adp.io/run-hash"], "image_digest": DIGEST, "session_hash": "d" * 64}
    if valid:
        assert runtime.find_reserved_sandbox(**expected) is None
    else:
        with pytest.raises(workload.WorkloadUnavailableError):
            runtime.find_reserved_sandbox(**expected)


def test_sandbox_requires_fixed_service_account_and_distinct_image(verifier):
    runtime, state = verifier
    pod = runtime.verify("sandbox-token")
    assert pod.service_account == "adp-chat-sandbox"
    assert pod.uid == "pod-uid"
    assert pod.run_hash == state["labels"]["adp.io/run-hash"]
    assert runtime._digests == {DIGEST}
    state["account"] = "adp-agent"
    state["image"] = LEGACY_DIGEST
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("legacy-token")
    state["account"] = "adp-chat-sandbox"
    state["pod_account"] = "adp-agent"
    state["image"] = DIGEST
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("substituted-pod-account")
    state["pod_account"] = "adp-chat-sandbox"
    state["image"] = LEGACY_DIGEST
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("wrong-image")
    state["image"] = DIGEST
    state["audience"] = "kubernetes.default.svc"
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("wrong-audience")


def test_persistent_sandbox_requires_session_label_when_deadline_is_omitted(verifier):
    runtime, state = verifier
    state["spec"].pop("activeDeadlineSeconds")
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("unbound-pod")
    state["labels"]["adp.io/session-hash"] = "a" * 64
    assert runtime.verify("bound-pod").session_hash == "a" * 64
    state["spec"]["activeDeadlineSeconds"] = 900
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("mismatched-mode")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state["spec"].update(hostNetwork=True),
        lambda state: state["spec"].update(hostPID=True),
        lambda state: state["spec"].update(hostIPC=True),
        lambda state: state["spec"].update(automountServiceAccountToken=True),
        lambda state: state["spec"].update(initContainers=[{"name": "privileged"}]),
        lambda state: state["spec"].update(imagePullSecrets=[{"name": "platform"}]),
        lambda state: state["spec"].update(volumes=[{"name": "platform", "secret": {"secretName": "platform"}}]),
        lambda state: state["spec"]["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"].update(audience="kubernetes.default.svc"),
        lambda state: state["spec"]["containers"][0].update(command=["/app/startup.sh"]),
        lambda state: state["spec"]["containers"][0].update(envFrom=[{"secretRef": {"name": "platform"}}]),
        lambda state: state["spec"]["containers"][0]["env"].append({"name": "AWS_ROLE_ARN", "value": "platform"}),
        lambda state: state["spec"]["containers"][0]["volumeMounts"].append({"name": "platform", "mountPath": "/secrets"}),
        lambda state: state["spec"]["containers"][0]["securityContext"].update(privileged=True),
        lambda state: state["spec"]["containers"][0]["securityContext"].update(readOnlyRootFilesystem=False),
        lambda state: state["spec"]["containers"].append(deepcopy(state["spec"]["containers"][0])),
        lambda state: state["labels"].update({"adp.io/run-hash": "a" * 64}),
    ],
    ids=[
        "host-network",
        "host-pid",
        "host-ipc",
        "default-token",
        "init-container",
        "image-pull-secret",
        "secret-volume",
        "wrong-token-audience",
        "inherited-worker-command",
        "secret-env",
        "credential-env",
        "credential-mount",
        "privileged",
        "writable-root",
        "sidecar",
        "forged-run-label",
    ],
)
def test_valid_sandbox_token_cannot_admit_mutated_pod(verifier, mutate):
    runtime, state = verifier
    mutate(state)
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("valid-sandbox-token")
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify_bound(name=NAME, uid="pod-uid")


def test_missing_sandbox_image_and_ambiguous_mode_fail_closed(verifier, monkeypatch):
    legacy = workload.KubernetesWorkloadVerifier.in_cluster(chat=True)
    assert legacy._service_account == "adp-agent"
    assert legacy._digests == {LEGACY_DIGEST}
    monkeypatch.delenv("ADP_CHAT_SANDBOX_IMAGE_DIGESTS")
    with pytest.raises(workload.WorkloadRefusedError):
        workload.KubernetesWorkloadVerifier.in_cluster(chat_sandbox=True)
    with pytest.raises(workload.WorkloadRefusedError):
        workload.KubernetesWorkloadVerifier.in_cluster(chat=True, chat_sandbox=True)


def test_sandbox_requires_configured_gateway_origin(verifier, monkeypatch):
    runtime, _ = verifier
    monkeypatch.delenv("ADP_CHAT_DATA_URL")
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("valid-sandbox-token")
    monkeypatch.setenv("ADP_CHAT_DATA_URL", "https://different.example.test")
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("valid-sandbox-token")


def test_scoped_data_route_selects_sandbox_verifier():
    source = (Path(workload.__file__).parent / "chat_data_routes.py").read_text()
    legacy_model = (Path(workload.__file__).parent / "chat_model.py").read_text()
    assert "KubernetesWorkloadVerifier.in_cluster(chat_sandbox=True)" in source
    assert "KubernetesWorkloadVerifier.in_cluster(chat=True)" in legacy_model


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec.update(hostAliases=[]),
        lambda spec: spec["hostAliases"][0].update(ip="169.254.169.254"),
        lambda spec: spec["hostAliases"][0].update(hostnames=["attacker.example"]),
        lambda spec: spec["volumes"][2]["configMap"].update(name="chat-sandbox-gateway-ca-" + "e" * 16),
        lambda spec: spec["volumes"][2].update(secret={"secretName": "platform"}),
        lambda spec: spec["containers"][0]["volumeMounts"][2].update(readOnly=False),
        lambda spec: spec["containers"][0]["env"].append({"name": "NODE_TLS_REJECT_UNAUTHORIZED", "value": "0"}),
    ],
)
def test_transport_mutation_is_not_an_approved_sandbox(verifier, mutate):
    runtime, state = verifier
    mutate(state["spec"])
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("valid-sandbox-token")


def test_sandbox_requires_configured_trust(verifier, monkeypatch):
    runtime, _ = verifier
    monkeypatch.delenv("ADP_CHAT_SANDBOX_CA_CONFIGMAP")
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify("valid-sandbox-token")


def test_kubernetes_may_omit_false_host_namespace_flags(verifier):
    runtime, state = verifier
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        del state["spec"][field]
    assert runtime.verify("valid-sandbox-token").uid == "pod-uid"


def test_sandbox_exit_requires_exact_terminated_uid_template_and_digest(verifier):
    runtime, state = verifier
    assert not runtime.has_exited(name=NAME, uid="pod-uid", image_digest=DIGEST)
    state["phase"] = "Succeeded"
    state["container_state"] = {"terminated": {"exitCode": 0}}
    assert runtime.has_exited(name=NAME, uid="pod-uid", image_digest=DIGEST)
    assert not runtime.has_exited(name=NAME, uid="other-uid", image_digest=DIGEST)
    assert not runtime.has_exited(name=NAME, uid="pod-uid", image_digest=LEGACY_DIGEST)
    state["image"] = LEGACY_DIGEST
    assert not runtime.has_exited(name=NAME, uid="pod-uid", image_digest=DIGEST)
    state["image"] = DIGEST
    state["labels"]["adp.io/run-hash"] = "d" * 64
    assert not runtime.has_exited(name=NAME, uid="pod-uid", image_digest=DIGEST)
    state["labels"]["adp.io/run-hash"] = "c" * 64
    state["spec"]["containers"][0]["env"].append({"name": "AWS_ROLE_ARN", "value": "overbroad"})
    assert not runtime.has_exited(name=NAME, uid="pod-uid", image_digest=DIGEST)


@pytest.mark.parametrize("terminated", [None, {}, {"exitCode": True}, {"exitCode": "0"}])
def test_malformed_terminal_status_is_not_exit_evidence(verifier, terminated):
    runtime, state = verifier
    state["phase"] = "Succeeded"
    state["container_state"] = {"terminated": terminated}
    assert not runtime.has_exited(name=NAME, uid="pod-uid", image_digest=DIGEST)
    assert runtime.exited_sandbox_image(name=NAME, uid="pod-uid", run_hash="c" * 64) is None


def test_pre_admission_exit_requires_full_run_hash_and_does_not_enable_bootstrap(verifier):
    runtime, state = verifier
    assert runtime.exited_sandbox_image(name=NAME, uid="pod-uid", run_hash="c" * 64) is None
    state["phase"] = "Failed"
    state["container_state"] = {"terminated": {"exitCode": 1}}
    assert runtime.exited_sandbox_image(name=NAME, uid="pod-uid", run_hash="c" * 64) == DIGEST
    assert runtime.exited_sandbox_image(name=NAME, uid="foreign", run_hash="c" * 64) is None
    assert runtime.exited_sandbox_image(name=NAME, uid="pod-uid", run_hash="c" * 12 + "d" * 52) is None
    with pytest.raises(workload.WorkloadRefusedError):
        runtime.verify_bound(name=NAME, uid="pod-uid")
    assert not runtime.has_exited(name=NAME, uid="pod-uid")
    state["spec"]["hostPID"] = True
    assert runtime.exited_sandbox_image(name=NAME, uid="pod-uid", run_hash="c" * 64) is None


def test_creation_discovery_requires_positive_exit_full_run_and_expected_image(verifier):
    runtime, state = verifier
    assert runtime.find_exited_sandbox(name=NAME, run_hash="c" * 64, image_digest=DIGEST) is None
    state["phase"] = "Failed"
    state["container_state"] = {"terminated": {"exitCode": 1}}
    assert runtime.find_exited_sandbox(name=NAME, run_hash="c" * 64, image_digest=DIGEST) == "pod-uid"
    assert runtime.find_exited_sandbox(name=NAME, run_hash="c" * 12 + "d" * 52, image_digest=DIGEST) is None
    assert runtime.find_exited_sandbox(name=NAME, run_hash="c" * 64, image_digest=LEGACY_DIGEST) is None
    state["pod_account"] = "adp-agent"
    assert runtime.find_exited_sandbox(name=NAME, run_hash="c" * 64, image_digest=DIGEST) is None


@pytest.mark.parametrize("code", [403, 404, 500])
def test_unavailable_pod_is_not_pre_admission_exit_evidence(verifier, monkeypatch, code):
    runtime, _ = verifier
    monkeypatch.setattr(
        runtime._client, "get", lambda *args, **kwargs: httpx.Response(code, request=httpx.Request("GET", "https://cluster.example.test"))
    )
    assert runtime.exited_sandbox_image(name=NAME, uid="pod-uid", run_hash="c" * 64) is None


@pytest.mark.parametrize("code", [200, 202, 401, 403, 404, 500])
@pytest.mark.parametrize("valid", [True, False])
def test_absence_requires_exact_kubernetes_not_found(verifier, monkeypatch, code, valid):
    runtime, _ = verifier
    document = {"kind": "Status", "reason": "NotFound", "details": {"name": NAME, "kind": "pods"}} if valid else {}
    monkeypatch.setattr(runtime._client, "get", lambda *args, **kwargs: httpx.Response(code, json=document))
    assert runtime.is_absent(name=NAME) is (code == 404 and valid)


@pytest.mark.parametrize("field,value", [("kind", "Service"), ("reason", "Forbidden"), ("details", {"name": "foreign", "kind": "pods"})])
def test_unrelated_not_found_does_not_confirm_removal(verifier, monkeypatch, field, value):
    runtime, _ = verifier
    document = {"kind": "Status", "reason": "NotFound", "details": {"name": NAME, "kind": "pods"}, field: value}
    monkeypatch.setattr(runtime._client, "get", lambda *args, **kwargs: httpx.Response(404, json=document))
    assert not runtime.is_absent(name=NAME)


def test_absence_timeout_and_invalid_name_are_not_evidence(verifier, monkeypatch):
    runtime, _ = verifier
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise httpx.ReadTimeout("unavailable")

    monkeypatch.setattr(runtime._client, "get", unavailable)
    assert not runtime.is_absent(name="../foreign")
    assert not calls
    assert not runtime.is_absent(name=NAME)
    assert len(calls) == 1

"""Fault injection at independently read workload/routing/probe boundaries."""

import copy
import importlib.util
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from adp_tools.adapter_contracts import contract_metadata
from fastapi.testclient import TestClient

from tool_adapters.config import AdapterConfiguration
from tool_adapters.server import create_app

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("adapter_runtime_observer", ROOT / "runtime_evidence.py")
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)
render = runpy.run_path(str(ROOT / "k8s/render.py"))["render"]
NAMESPACE = "adp-tools"
ROLE = "arn:aws:iam::123456789012:role/adp-dev-tool-adapters"


@pytest.fixture
def cluster(configuration, request):
    if getattr(request, "param", "foundation") == "observation":
        configuration = AdapterConfiguration.model_validate(
            {
                **configuration.model_dump(mode="json"),
                "mode": "observation",
                "observation": {
                    "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                    "core_role_arn": "arn:aws:iam::123456789012:role/gateway",
                    "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc",
                    "edge_provenance_sha256": "c" * 64,
                    "identity_workload": "dev-atlassian-mcp-oauth-user",
                    "identity_provider": "dev-atlassian-mcp-user",
                    "config_revision": "d" * 64,
                    "subject_schema_sha256": "e" * 64,
                    "sites_schema_sha256": "f" * 64,
                },
            }
        )
    image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-dev-adapters@" + configuration.release.image_digest
    expected = render(image=image, namespace=NAMESPACE, role_arn=ROLE, configuration=configuration)
    resources = copy.deepcopy({value["kind"]: value for value in expected["items"]})
    for index, value in enumerate(resources.values()):
        value["metadata"].update(uid=f"resource-{index}", resourceVersion="123", generation=1)
    deployment = resources["Deployment"]
    deployment["status"] = {
        "observedGeneration": 1,
        "replicas": 2,
        "updatedReplicas": 2,
        "readyReplicas": 2,
        "availableReplicas": 2,
    }
    resources["Service"]["spec"]["clusterIP"] = "10.0.0.100"
    template = deployment["spec"]["template"]

    def metadata(name, uid, owner_kind, owner_uid):
        return {
            "name": name,
            "namespace": NAMESPACE,
            "uid": uid,
            "resourceVersion": "123",
            "ownerReferences": [{"kind": owner_kind, "uid": owner_uid, "controller": True}],
        }

    replica = {
        "metadata": metadata("tool-adapters-abc", "replica-1", "Deployment", deployment["metadata"]["uid"]),
        "spec": {"replicas": 2, "template": copy.deepcopy(template)},
    }
    pods = []
    for index in range(2):
        pods.append(
            {
                "metadata": {
                    **copy.deepcopy(template["metadata"]),
                    **metadata(f"tool-adapters-abc-{index}", f"pod-{index}", "ReplicaSet", "replica-1"),
                },
                "spec": copy.deepcopy(template["spec"]),
                "status": {
                    "phase": "Running",
                    "podIPs": [{"ip": f"10.0.1.{index + 1}"}],
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {
                            "name": "tool-adapters",
                            "ready": True,
                            "imageID": image,
                            "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                        }
                    ],
                },
            }
        )
    slices = [
        {
            "metadata": metadata("tool-adapters-slice", "slice-1", "Service", resources["Service"]["metadata"]["uid"]),
            "ports": [{"name": "http", "protocol": "TCP", "port": 8080}],
            "endpoints": [
                {
                    "targetRef": {
                        "kind": "Pod",
                        "namespace": NAMESPACE,
                        "uid": pod["metadata"]["uid"],
                        "name": pod["metadata"]["name"],
                    },
                    "conditions": {"ready": True, "serving": True, "terminating": False},
                    "addresses": [pod["status"]["podIPs"][0]["ip"]],
                }
                for pod in pods
            ],
        }
    ]
    state = SimpleNamespace(
        configuration=configuration,
        expected=expected,
        resources=resources,
        pods=pods,
        replica=replica,
        slices=slices,
        reads=[],
        change=None,
        continued=False,
        extra_payload=False,
        contract={"service": "adp-tool-adapters", **contract_metadata()},
    )

    def read(path):
        state.reads.append(path)
        if "/proxy/" in path:
            if path.endswith("/contract"):
                return copy.deepcopy(state.contract)
            return {**state.contract, "status": "ready", "mode": "foundation", "execution_enabled": False}
        path_only = path.split("?", 1)[0]
        for kind, (prefix, plural, name) in observer.RESOURCES.items():
            if path_only == f"{prefix}/namespaces/{NAMESPACE}/{plural}/{name}":
                result = copy.deepcopy(resources[kind])
                if state.reads.count(path) > 1 and state.change == kind:
                    result["metadata"]["resourceVersion"] = "124"
                return result
        for prefix, plural, values in (
            ("/apis/apps/v1", "replicasets", [replica]),
            ("/api/v1", "pods", pods),
            ("/apis/discovery.k8s.io/v1", "endpointslices", slices),
        ):
            base = f"{prefix}/namespaces/{NAMESPACE}/{plural}"
            if path_only == base:
                result = {"items": copy.deepcopy(values), "metadata": {"continue": "next" if state.continued else ""}}
                if state.extra_payload:
                    result["private"] = "x" * observer.MAX_RESPONSE
                return result
            for value in values:
                if path_only == base + "/" + value["metadata"]["name"]:
                    return copy.deepcopy(value)
        raise AssertionError(path)

    state.read = read
    return state


def observe(cluster):
    return observer.observe(cluster.read, cluster.expected, contract_metadata())


def test_readback_binds_running_image_config_owners_and_routes(cluster):
    evidence = observe(cluster)
    assert evidence["ready_replicas"] == evidence["routed_replicas"] == 2
    assert evidence["release"]["source_revision"] == "a" * 40
    assert evidence["release"]["image_digest"] == "sha256:" + "b" * 64
    assert evidence["execution_enabled"] is False
    assert len(json.dumps(evidence)) < 4096
    assert all("secrets" not in path and "/exec" not in path for path in cluster.reads)
    assert "10.0.1.1" not in json.dumps(evidence)


@pytest.mark.parametrize(
    "plural,prefix,name",
    [
        ("replicasets", "/apis/apps/v1", "tool-adapters-abc"),
        ("endpointslices", "/apis/discovery.k8s.io/v1", "tool-adapters-slice"),
    ],
)
def test_resource_pruned_during_observation_stays_a_retryable_observation_error(cluster, plural, prefix, name):
    """A watched resource that vanishes mid-observation must stay retryable.

    The collection read registers each identity, and the final loop re-reads it.
    Kubernetes may legitimately prune an inactive ReplicaSet through the
    Deployment's revision history, or replace an EndpointSlice, in between. The
    caller's reader surfaces the vanished path as its own transport failure, and
    deploy.py re-observes only on ObservationError, so that failure used to
    escape the deliberate retry loop and fail the foundation stage outright
    (observed in tools-platform-deploy run 40776496194). A moved target is the
    condition this loop exists to report.
    """
    pruned = f"{prefix}/namespaces/{NAMESPACE}/{plural}/{name}"
    original = cluster.read

    def read(path):
        if path == pruned:
            raise RuntimeError("the server could not find the requested resource")
        return original(path)

    with pytest.raises(observer.ObservationError) as error:
        observer.observe(read, cluster.expected, contract_metadata())
    assert str(error.value) == "adapter_runtime_changed_during_observation"


def test_a_stable_runtime_still_reads_every_watched_identity_back(cluster):
    """The relaxation must not skip any re-read: all identities are confirmed."""
    observe(cluster)
    for path in (
        f"/apis/apps/v1/namespaces/{NAMESPACE}/replicasets/tool-adapters-abc",
        f"/api/v1/namespaces/{NAMESPACE}/pods/tool-adapters-abc-0",
        f"/api/v1/namespaces/{NAMESPACE}/pods/tool-adapters-abc-1",
        f"/apis/discovery.k8s.io/v1/namespaces/{NAMESPACE}/endpointslices/tool-adapters-slice",
    ):
        assert path in cluster.reads


LEGACY_ENTRY = {"name": "ADP_TOOL_ADAPTER_LEGACY_ENABLED", "value": "true"}


def live_container_specs(cluster):
    """Every live pod spec that a server-side apply leftover would appear in."""
    return [
        cluster.resources["Deployment"]["spec"]["template"]["spec"],
        cluster.replica["spec"]["template"]["spec"],
        *(pod["spec"] for pod in cluster.pods),
    ]


def test_stale_handoff_variable_alone_is_a_real_deployment_mismatch(cluster):
    """Drive the unmocked verifier: the leftover env entry is the whole defect.

    Server-side apply merges container env by `name` and keeps an entry owned by
    another field manager, so a ready, correctly imaged runtime still fails. This
    is the exact code observed in the tools-platform-deploy foundation stage, and
    removing only that entry is what makes the strict comparison hold again.
    """
    specs = live_container_specs(cluster)
    for spec in specs:
        spec["containers"][0]["env"].append(dict(LEGACY_ENTRY))
    with pytest.raises(observer.ObservationError, match="adapter_deployment_mismatch"):
        observe(cluster)
    for spec in specs:
        spec["containers"][0]["env"] = [item for item in spec["containers"][0]["env"] if item != LEGACY_ENTRY]
    evidence = observe(cluster)
    assert evidence["ready_replicas"] == evidence["routed_replicas"] == 2
    assert evidence["release"]["image_digest"] == "sha256:" + "b" * 64


@pytest.mark.parametrize("cluster", ["foundation", "observation"], indirect=True)
@pytest.mark.parametrize("fault", [None, "mode", "execution", "status", "extra"])
def test_readiness_matches_configured_mode_and_actual_server(cluster, fault):
    configuration = cluster.configuration
    with TestClient(create_app(configuration, source_revision=configuration.release.source_revision)) as client:
        readiness = client.get("/readyz")
        assert readiness.status_code == 200
        payload = readiness.json()
    if fault == "mode":
        payload["mode"] = "foundation" if configuration.mode == "observation" else "observation"
    elif fault == "execution":
        payload["execution_enabled"] = True
    elif fault == "status":
        payload["status"] = "unavailable"
    elif fault == "extra":
        payload["unexpected"] = True
    original = cluster.read
    cluster.read = lambda path: copy.deepcopy(payload) if path.endswith("/proxy/readyz") else original(path)
    if fault:
        with pytest.raises(observer.ObservationError, match="adapter_readiness_probe_failed"):
            observe(cluster)
    else:
        assert observe(cluster)["ready_replicas"] == 2


def test_expected_eks_identity_injection_is_accepted(cluster):
    for pod in cluster.pods:
        container = pod["spec"]["containers"][0]
        container["env"] += [
            {"name": "AWS_ROLE_ARN", "value": ROLE},
            {"name": "AWS_WEB_IDENTITY_TOKEN_FILE", "value": "/var/run/secrets/eks.amazonaws.com/serviceaccount/token"},
            {"name": "AWS_REGION", "value": "us-east-1"},
        ]
        container["volumeMounts"].append(
            {
                "name": "aws-iam-token",
                "readOnly": True,
                "mountPath": "/var/run/secrets/eks.amazonaws.com/serviceaccount",
            }
        )
        pod["spec"]["volumes"].append(
            {
                "name": "aws-iam-token",
                "projected": {
                    "defaultMode": 420,
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "audience": "sts.amazonaws.com",
                                "expirationSeconds": 86400,
                                "path": "token",
                            }
                        },
                    ],
                },
            }
        )
    assert observe(cluster)["ready_replicas"] == 2


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("stale_image", "running_image_mismatch"),
        ("root", "container_user_override"),
        ("source_override", "unexpected_environment"),
        ("extra_container", "pod_extra_container"),
        ("command", "container_execution_override"),
        ("foreign_owner", "pod_owner_mismatch"),
        ("unready", "pod_not_ready"),
        ("missing_pod", "pod_count_mismatch"),
        ("secret_volume", "unexpected_volume"),
        ("wrong_role", "resource_identity_mismatch"),
        ("stale_config", "configuration_mismatch"),
        ("stale_controller", "deployment_unobserved"),
        ("external_route", "service_not_private"),
        ("foreign_endpoint", "endpoint_target_mismatch"),
        ("wrong_address", "endpoint_address_mismatch"),
        ("unready_endpoint", "endpoint_not_ready"),
        ("wrong_contract", "contract_probe_failed"),
        ("oversized", "response_invalid"),
        ("pagination", "list_limit"),
        ("probe_host", "probe_override"),
        ("subpath", "mount_override"),
        ("config_keys", "configuration_volume_override"),
    ],
)
def test_readback_refuses_false_success(cluster, fault, reason):
    pod = cluster.pods[0]
    container = pod["spec"]["containers"][0]
    if fault == "stale_image":
        pod["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "c" * 64
    elif fault == "root":
        container["securityContext"]["runAsUser"] = 0
    elif fault == "source_override":
        container["env"].append({"name": "ADP_ADAPTER_SOURCE_REVISION", "value": "a" * 40})
    elif fault == "extra_container":
        pod["spec"]["containers"].append(copy.deepcopy(container))
    elif fault == "command":
        container["command"] = ["replacement-server"]
    elif fault == "foreign_owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign-replica"
    elif fault == "unready":
        pod["status"]["conditions"][0]["status"] = "False"
    elif fault == "missing_pod":
        cluster.pods.pop()
    elif fault == "secret_volume":
        pod["spec"]["volumes"].append({"name": "foreign", "secret": {"secretName": "credential"}})
    elif fault == "wrong_role":
        cluster.resources["ServiceAccount"]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] = ROLE + "-other"
    elif fault == "stale_config":
        cluster.resources["ConfigMap"]["data"]["config.json"] = "{}"
    elif fault == "stale_controller":
        cluster.resources["Deployment"]["status"]["observedGeneration"] = 0
    elif fault == "external_route":
        cluster.resources["Service"]["spec"]["externalIPs"] = ["203.0.113.1"]
    elif fault == "foreign_endpoint":
        cluster.slices[0]["endpoints"][0]["targetRef"]["uid"] = "foreign-pod"
    elif fault == "wrong_address":
        cluster.slices[0]["endpoints"][0]["addresses"] = ["203.0.113.1"]
    elif fault == "unready_endpoint":
        cluster.slices[0]["endpoints"][0]["conditions"]["ready"] = False
    elif fault == "wrong_contract":
        cluster.contract["schema_sha256"] = "0" * 64
    elif fault == "oversized":
        cluster.extra_payload = True
    elif fault == "pagination":
        cluster.continued = True
    elif fault == "probe_host":
        container["readinessProbe"]["httpGet"]["host"] = "another-service"
    elif fault == "subpath":
        container["volumeMounts"][0]["subPath"] = "other.json"
    elif fault == "config_keys":
        pod["spec"]["volumes"][0]["configMap"]["items"] = [{"key": "other", "path": "config.json"}]
    with pytest.raises(observer.ObservationError, match="adapter_" + reason):
        observe(cluster)


@pytest.mark.parametrize("runtime", ["java", "python", "dotnet", "nodejs"])
@pytest.mark.parametrize(
    "target,reason",
    [
        ("deployment", "deployment_mismatch"),
        ("replica", "replicaset_mismatch"),
        ("pod", "pod_owner_mismatch"),
    ],
)
def test_readback_refuses_reenabled_runtime_instrumentation(cluster, runtime, target, reason):
    # The rendered release opts every language runtime out of operator
    # auto-instrumentation; an injected runtime breaks restricted PodSecurity
    # and independent execution verification. Flipping the opt-out on any
    # observed object must refuse the observation instead of reporting success.
    annotation = f"instrumentation.opentelemetry.io/inject-{runtime}"
    if target == "deployment":
        cluster.resources["Deployment"]["spec"]["template"]["metadata"]["annotations"][annotation] = "true"
    elif target == "replica":
        cluster.replica["spec"]["template"]["metadata"]["annotations"][annotation] = "true"
    else:
        cluster.pods[0]["metadata"]["annotations"][annotation] = "true"
    with pytest.raises(observer.ObservationError, match="adapter_" + reason):
        observe(cluster)


@pytest.mark.parametrize("kind", ["ServiceAccount", "ConfigMap", "Deployment", "Service", "PodDisruptionBudget"])
def test_resource_change_during_probe_invalidates_observation(cluster, kind):
    cluster.change = kind
    with pytest.raises(observer.ObservationError, match="runtime_changed_during_observation"):
        observe(cluster)

"""Exercise the gateway transport and real shared observer, with bounded API fixtures."""

import base64
import copy
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from src.orchestration import deployment_runtime_adapters as adapters
from src.orchestration import deployment_runtime_reader as reader
from src.orchestration.deployment_release_provider import ReleaseProvider
from src.orchestration.deployment_runtime_contract import ReleaseArtifact, RuntimeComponent
from src.orchestration.deployment_workflow_provider import WorkflowContext
from src.orchestration.review_cycle import CycleBlockedError

ACCOUNT = "123456789012"
NAMESPACE = "adp-tools"
CONTRACT = {"contract_version": "1.0", "schema_sha256": "c" * 64}
ARTIFACT_HASH = "d" * 64
EVIDENCE_REF = "github/actions/runs/42/artifacts/99"


@pytest.fixture(scope="module")
def certificate():
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "adapter-test.invalid")])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, None)
        .public_bytes(serialization.Encoding.PEM)
    )


@pytest.fixture(params=["legacy", "nullable-observation", "observation"])
def runtime(monkeypatch, certificate, request):
    manifest = json.loads((Path(__file__).parent / "fixtures/adapter-foundation-manifest.json").read_text())
    resources = {item["kind"]: item for item in manifest["items"]}
    configuration = json.loads(resources["ConfigMap"]["data"]["config.json"])
    if request.param != "legacy":
        configuration["observation"] = None
        if request.param == "observation":
            configuration["mode"] = "observation"
            configuration["observation"] = {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": f"arn:aws:iam::{ACCOUNT}:role/gateway",
                "assertion_key_arn": f"arn:aws:kms:us-east-1:{ACCOUNT}:key/12345678-1234-1234-1234-123456789abc",
                "edge_provenance_sha256": "c" * 64,
                "identity_workload": "dev-atlassian-mcp-oauth-user",
                "identity_provider": "dev-atlassian-mcp-user",
                "config_revision": "d" * 64,
                "subject_schema_sha256": "e" * 64,
                "sites_schema_sha256": "f" * 64,
            }
        raw = json.dumps(configuration, separators=(",", ":"))
        resources["ConfigMap"]["data"]["config.json"] = raw
        annotations = resources["Deployment"]["spec"]["template"]["metadata"]["annotations"]
        annotations["checksum/adp-adapter-contract"] = hashlib.sha256(raw.encode()).hexdigest()
    artifact = SimpleNamespace(
        component="tool-adapters",
        environment="dev",
        source_revision="a" * 40,
        image_digest="sha256:" + "b" * 64,
        account_id=ACCOUNT,
        region="us-east-1",
        resource_id="example-cluster/adp-tools/tool-adapters",
        assets={
            "adapter-manifest.json": hashlib.sha256(adapters.canonical_bytes(manifest)).hexdigest(),
            "adapter-contract.json": hashlib.sha256(adapters.canonical_bytes(CONTRACT)).hexdigest(),
        },
    )
    actual = {item["kind"]: copy.deepcopy(item) for item in manifest["items"]}
    for kind, value in actual.items():
        value["metadata"].update(uid=f"uid-{kind}", resourceVersion="1", generation=1)
    deployment = actual["Deployment"]
    deployment["status"] = {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}
    actual["Service"]["spec"]["clusterIP"] = "10.0.0.100"
    template = deployment["spec"]["template"]

    def metadata(name, uid, owner_kind, owner_uid):
        return {
            "name": name,
            "namespace": NAMESPACE,
            "uid": uid,
            "resourceVersion": "1",
            "ownerReferences": [{"kind": owner_kind, "uid": owner_uid, "controller": True}],
        }

    replica = {
        "metadata": metadata("tool-adapters-abc", "replica-1", "Deployment", "uid-Deployment"),
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
                            "imageID": template["spec"]["containers"][0]["image"],
                            "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                        }
                    ],
                },
            }
        )
    slices = [
        {
            "metadata": metadata("tool-adapters-slice", "slice-1", "Service", "uid-Service"),
            "ports": [{"name": "http", "protocol": "TCP", "port": 8080}],
            "endpoints": [
                {
                    "targetRef": {"kind": "Pod", "namespace": NAMESPACE, "uid": pod["metadata"]["uid"], "name": pod["metadata"]["name"]},
                    "conditions": {"ready": True},
                    "addresses": [pod["status"]["podIPs"][0]["ip"]],
                }
                for pod in pods
            ],
        }
    ]
    objects = {
        f"{prefix}/namespaces/{NAMESPACE}/{plural}/{name}": actual[kind] for kind, (prefix, plural, name) in adapters._observer().RESOURCES.items()
    }
    lists = {}
    for prefix, plural, values in (
        ("/apis/apps/v1", "replicasets", [replica]),
        ("/api/v1", "pods", pods),
        ("/apis/discovery.k8s.io/v1", "endpointslices", slices),
    ):
        base = f"{prefix}/namespaces/{NAMESPACE}/{plural}"
        lists[base] = {"items": values, "metadata": {}}
        objects.update({base + "/" + item["metadata"]["name"]: item for item in values})
    state = SimpleNamespace(
        manifest=manifest,
        artifact=artifact,
        actual=actual,
        pods=pods,
        slices=slices,
        calls=[],
        probe_status=200,
        readiness_mode=configuration["mode"],
        changed=None,
        contract=copy.deepcopy(CONTRACT),
    )

    def send(request):
        state.calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer adapter-test-token"
        path = request.url.path
        if "/proxy/" in path:
            value = {"service": "adp-tool-adapters", **state.contract}
            if path.endswith("/readyz"):
                value.update(status="ready", mode=state.readiness_mode, execution_enabled=False)
            return httpx.Response(state.probe_status, json=value)
        if path in lists:
            # Exercise the real KubernetesRuntime.get -> httpx transport. A
            # dropped query would read unrelated workloads and break this gate.
            assert request.url.params["limit"] == "100"
            expected_selector = (
                "kubernetes.io/service-name=tool-adapters"
                if "endpointslices" in path
                else "app.kubernetes.io/name=tool-adapters,app.kubernetes.io/part-of=adp"
            )
            assert request.url.params["labelSelector"] == expected_selector
            return httpx.Response(200, json=lists[path])
        value = copy.deepcopy(objects[path])
        if state.changed and path.endswith(state.changed) and sum(call.url.path == path for call in state.calls) > 1:
            value["metadata"]["resourceVersion"] = "2"
        return httpx.Response(200, json=value)

    monkeypatch.setattr(reader, "eks_token", lambda *_args: "adapter-test-token")
    description = {
        "name": "example-cluster",
        "arn": f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/example-cluster",
        "endpoint": "https://cluster.example.test",
        "certificateAuthority": {"data": base64.b64encode(certificate).decode()},
    }
    client = httpx.Client(transport=httpx.MockTransport(send))
    state.kube = reader.KubernetesRuntime(SimpleNamespace(region_name="us-east-1"), description, NAMESPACE, client=client)
    yield state
    client.close()


def verify(runtime):
    return adapters.verify_tool_adapters(
        runtime.kube, runtime.artifact, ARTIFACT_HASH, EVIDENCE_REF, expected_manifest=runtime.manifest, expected_contract=CONTRACT
    )


def rehash(runtime):
    runtime.artifact.assets["adapter-manifest.json"] = hashlib.sha256(adapters.canonical_bytes(runtime.manifest)).hexdigest()


def test_actual_observer_and_gateway_transport_produce_bounded_observation(runtime):
    loaded = set(sys.modules)
    observed = verify(runtime)
    assert observed == {
        "component": "tool-adapters",
        "actual_revision": "a" * 40,
        "artifact_hash": ARTIFACT_HASH,
        "image_digest": "sha256:" + "b" * 64,
        "healthy": True,
        "evidence_ref": EVIDENCE_REF,
        "observed_at": observed["observed_at"],
        "pod_uids": ["pod-0", "pod-1"],
        "adapter_probe_path": "api_server_proxy",
    }
    assert observed["observed_at"].tzinfo is not None
    assert all("secrets" not in request.url.path and "/exec" not in request.url.path for request in runtime.calls)
    assert "tool_adapters.server" not in set(sys.modules) - loaded


@pytest.mark.parametrize(
    "difference,reason",
    [
        ("missing_hash", "expectation_hashes_missing"),
        ("changed_manifest", "expectation_hash_mismatch"),
        ("foreign_namespace", "manifest_target_mismatch"),
        ("foreign_cluster", "target_mismatch"),
        ("foreign_account", "target_mismatch"),
        ("foreign_image", "manifest_image_mismatch"),
        ("stale_source", "configuration_release_mismatch"),
        ("duplicate_config", "configuration_duplicate_field"),
        ("foundation_observation", "configuration_invalid"),
        ("unknown_configuration", "configuration_invalid"),
        ("foreign_role", "identity_target_mismatch"),
    ],
)
def test_reviewed_expectation_binding_precedes_cluster_reads(runtime, difference, reason):
    resources = {item["kind"]: item for item in runtime.manifest["items"]}
    if difference == "missing_hash":
        runtime.artifact.assets.pop("adapter-contract.json")
    elif difference == "changed_manifest":
        resources["Service"]["spec"]["selector"] = {"app": "different"}
    elif difference == "foreign_namespace":
        resources["ConfigMap"]["metadata"]["namespace"] = "foreign-namespace"
        rehash(runtime)
    elif difference == "foreign_cluster":
        runtime.kube.description["name"] = "foreign-cluster"
    elif difference == "foreign_account":
        runtime.artifact.account_id = "999999999999"
    elif difference == "foreign_image":
        resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = "image:latest"
        rehash(runtime)
    elif difference == "stale_source":
        runtime.artifact.source_revision = "f" * 40
    elif difference == "duplicate_config":
        resources["ConfigMap"]["data"]["config.json"] = '{"mode":"foundation","mode":"other"}'
        rehash(runtime)
    elif difference in {"foundation_observation", "unknown_configuration"}:
        configuration = json.loads(resources["ConfigMap"]["data"]["config.json"])
        if difference == "foundation_observation":
            configuration["mode"] = "foundation"
        configuration["observation" if difference == "foundation_observation" else "unknown"] = {}
        resources["ConfigMap"]["data"]["config.json"] = json.dumps(configuration)
        rehash(runtime)
    elif difference == "foreign_role":
        resources["ServiceAccount"]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] = "arn:aws:iam::999999999999:role/foreign"
        rehash(runtime)
    with pytest.raises(CycleBlockedError, match="deployment_adapter_" + reason):
        verify(runtime)
    assert not runtime.calls


@pytest.mark.parametrize("difference", ["image", "contract", "route", "changed", "mode"])
def test_actual_readback_mismatch_cannot_become_healthy(runtime, difference):
    if difference == "image":
        runtime.pods[0]["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "f" * 64
    elif difference == "contract":
        runtime.contract["schema_sha256"] = "f" * 64
    elif difference == "route":
        runtime.slices[0]["endpoints"][0]["targetRef"]["uid"] = "foreign-pod"
    elif difference == "changed":
        runtime.changed = "/services/tool-adapters"
    elif difference == "mode":
        runtime.readiness_mode = "foundation" if runtime.readiness_mode == "observation" else "observation"
    with pytest.raises(CycleBlockedError, match="deployment_adapter_runtime_mismatch"):
        verify(runtime)


def test_real_gateway_transport_refusal_is_preserved(runtime):
    runtime.probe_status = 403
    with pytest.raises(CycleBlockedError, match="deployment_runtime_read_forbidden"):
        verify(runtime)


def test_staged_observer_is_the_exact_dependency_free_owner_source(monkeypatch, tmp_path):
    source = adapters._observer_path().read_bytes()
    path = tmp_path / "runtime_evidence.py"
    path.write_bytes(source)
    adapters._observer.cache_clear()
    monkeypatch.setattr(adapters, "_observer_path", lambda: path)
    try:
        module = adapters._observer()
        assert callable(module.observe)
        subprocess.run(
            [sys.executable, "-I", "-S", "-c", "import runpy,sys; assert callable(runpy.run_path(sys.argv[1])['observe'])", str(path)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(source).digest()
    finally:
        adapters._observer.cache_clear()


def test_missing_staged_observer_fails_closed(monkeypatch, tmp_path):
    adapters._observer.cache_clear()
    monkeypatch.setattr(adapters, "_observer_path", lambda: tmp_path / "missing.py")
    try:
        with pytest.raises(CycleBlockedError, match="observer_unavailable"):
            adapters._observer()
    finally:
        adapters._observer.cache_clear()


def release_document(runtime):
    return {
        **vars(runtime.artifact),
        "schema_version": 1,
        "repository_id": 123,
        "run_id": 42,
        "run_attempt": 1,
        "workflow_revision": "a" * 40,
        "workflow_path": ".github/workflows/tools-platform-deploy.yml",
        "produced_at": datetime.now(UTC).isoformat(),
        "adapter_manifest": copy.deepcopy(runtime.manifest),
        "adapter_contract": copy.deepcopy(CONTRACT),
    }


@pytest.fixture(params=["foundation", "observation"])
def compatibility_release(runtime, request):
    document = release_document(runtime)
    configmap = next(item for item in document["adapter_manifest"]["items"] if item["kind"] == "ConfigMap")
    configuration = json.loads(configmap["data"]["config.json"])
    configuration["mode"] = request.param
    configuration["observation"] = None
    identity = {
        "provider": "fixture-atlassian-mcp-user",
        "workload": "fixture-atlassian-mcp-oauth-user",
        "config_revision": "a" * 64,
        "subject_schema_sha256": "b" * 64,
        "sites_schema_sha256": "c" * 64,
    }
    if request.param == "observation":
        configuration["mode"] = "observation"
        configuration["observation"] = {
            "core_authorize_url": "https://core.example.invalid/internal/v1/tool-adapters/authorize",
            "core_role_arn": f"arn:aws:iam::{ACCOUNT}:role/core",
            "assertion_key_arn": f"arn:aws:kms:us-east-1:{ACCOUNT}:key/11111111-1111-4111-8111-111111111111",
            "edge_provenance_sha256": "d" * 64,
            "identity_provider": identity["provider"],
            "identity_workload": identity["workload"],
            "config_revision": identity["config_revision"],
            "subject_schema_sha256": identity["subject_schema_sha256"],
            "sites_schema_sha256": identity["sites_schema_sha256"],
        }
    configmap["data"]["config.json"] = json.dumps(configuration, separators=(",", ":"))
    document["assets"]["adapter-manifest.json"] = hashlib.sha256(adapters.canonical_bytes(document["adapter_manifest"])).hexdigest()
    document["compatibility"] = {
        "adapter": {
            "release": copy.deepcopy(configuration["release"]),
            "mode": request.param,
            "contract": copy.deepcopy(CONTRACT),
            "configuration_sha256": hashlib.sha256(configmap["data"]["config.json"].encode()).hexdigest(),
            "manifest_sha256": document["assets"]["adapter-manifest.json"],
        }
    }
    if request.param == "observation":
        document["compatibility"]["identity"] = identity
    return document


def test_adapter_compatibility_preserves_bound_metadata_and_older_receipts(compatibility_release):
    document = compatibility_release
    artifact = ReleaseArtifact.model_validate(document)
    assert artifact.model_dump(mode="json")["compatibility"] == document["compatibility"]
    document.pop("compatibility")
    assert ReleaseArtifact.model_validate(document).compatibility is None


@pytest.mark.parametrize(
    "path,value",
    [
        (("unexpected",), True),
        (("compatibility", "unexpected"), True),
        (("compatibility", "adapter", "unexpected"), True),
        (("compatibility", "adapter", "release", "unexpected"), True),
        (("compatibility", "adapter", "release", "source_revision"), "f" * 40),
        (("compatibility", "adapter", "release", "image_digest"), "sha256:" + "f" * 64),
        (("compatibility", "adapter", "release", "group"), "other-connectors"),
        (("compatibility", "adapter", "release", "group_generation"), True),
        (("compatibility", "adapter", "release", "backend_generation"), 2),
        (("compatibility", "adapter", "mode"), "other"),
        (("compatibility", "adapter", "contract", "unexpected"), True),
        (("compatibility", "adapter", "contract", "schema_sha256"), "f" * 64),
        (("compatibility", "adapter", "configuration_sha256"), "f" * 64),
        (("compatibility", "adapter", "manifest_sha256"), "f" * 64),
        (("compatibility", "identity"), {"provider": "other"}),
        (("compatibility",), {}),
    ],
)
def test_adapter_compatibility_rejects_extra_fields_and_mismatched_pins(compatibility_release, runtime, path, value):
    target = compatibility_release
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ReleaseArtifact.model_validate(compatibility_release)
    assert not runtime.calls


def test_adapter_compatibility_cannot_describe_another_component(compatibility_release):
    compatibility_release.update(component="tools-facade", assets={})
    compatibility_release.pop("adapter_manifest")
    compatibility_release.pop("adapter_contract")
    with pytest.raises(ValidationError, match="tool-adapters releases only"):
        ReleaseArtifact.model_validate(compatibility_release)


def inspect_release(runtime, artifact, *, digest=ARTIFACT_HASH, reference=EVIDENCE_REF, environment="dev"):
    def kubernetes(scoped, description, namespace):
        assert scoped is runtime.kube.scoped
        assert description is runtime.kube.description
        assert namespace == NAMESPACE
        return runtime.kube

    return reader.PilotRuntimeReader(kubernetes=kubernetes).inspect(
        runtime.kube.scoped,
        runtime.kube.description,
        NAMESPACE,
        artifact=artifact,
        artifact_hash=digest,
        evidence_ref=reference,
        environment=environment,
    )


@pytest.mark.parametrize("transport", ["github", "s3"])
@pytest.mark.parametrize("namespace_target", [False, True])
async def test_central_reader_consumes_authenticated_release_expectations(runtime, monkeypatch, transport, namespace_target):
    document = release_document(runtime)
    if namespace_target:
        document["resource_id"] = "example-cluster/adp-tools"
    context = WorkflowContext(
        **{key: value for key, value in document.items() if key in WorkflowContext.model_fields},
        resource_kind="eks-namespace",
        inputs={"environment": "dev"},
        correlation="",
    )
    run = SimpleNamespace(run_id=42, run_attempt=1, context=context)
    payload = json.dumps(document).encode()
    assert len(payload) < 65536  # Existing authenticated evidence payload bound.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("release.json", payload)
    archive_bytes = archive.getvalue()
    archive_hash = hashlib.sha256(archive_bytes).hexdigest()

    def response(request):
        assert request.headers["authorization"] == "Bearer scoped-test"
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": 99,
                            "name": "adp-release-tool-adapters-1",
                            "expired": False,
                            "size_in_bytes": len(archive_bytes),
                            "digest": "sha256:" + archive_hash,
                        }
                    ]
                },
            )
        assert request.url.path.endswith("/99/zip")
        return httpx.Response(200, content=archive_bytes)

    reads = []

    def read(**expected):
        reads.append(expected)
        return payload, hashlib.sha256(payload).hexdigest(), "s3://private-evidence/release?versionId=retained-version"

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = ReleaseProvider(client=client)
        provider.token = AsyncMock(return_value="scoped-test")
        if transport == "s3":
            run.artifact_ref = "s3://private-evidence/context?versionId=retained-context"
            # Signed-store verification is separately tested by that transport.
            # Exercise its real provider consumer and provenance arguments here.
            provider.evidence_store = SimpleNamespace(read=read)
        artifact, digest, reference = await provider.release(SimpleNamespace(repo="org/repo", provider_repository_id=123), run, "tool-adapters")

    closed = []
    monkeypatch.setattr(runtime.kube, "close", lambda: closed.append(True))
    observation = inspect_release(runtime, artifact, digest=digest, reference=reference)
    assert isinstance(observation, RuntimeComponent)
    assert observation.healthy and observation.pod_uids == ["pod-0", "pod-1"]
    assert observation.artifact_hash == digest and observation.evidence_ref == reference
    assert observation.manifests_sha256 == artifact.assets["adapter-manifest.json"]
    assert observation.adapter_contract_sha256 == artifact.assets["adapter-contract.json"]
    assert observation.adapter_probe_path == "api_server_proxy"
    assert closed == [True]
    if transport == "s3":
        assert len(reads) == 1
        assert reads[0] == {
            "account": ACCOUNT,
            "environment": "dev",
            "region": "us-east-1",
            "repository": "org/repo",
            "repository_id": 123,
            "run_id": 42,
            "attempt": 1,
            "kind": "release",
            "name": "tool-adapters",
            "workflow_path": document["workflow_path"],
            "workflow_revision": document["workflow_revision"],
        }
    else:
        assert digest == archive_hash and reference == EVIDENCE_REF
        assert not reads


@pytest.mark.parametrize(
    "difference",
    [
        "missing_manifest",
        "missing_contract",
        "manifest",
        "hash",
        "oversized",
        "nonfinite",
        "other_component",
        "compatibility_not_object",
        "unknown_field",
    ],
)
def test_unbound_or_invalid_expected_documents_fail_before_cluster_access(runtime, difference):
    document = release_document(runtime)
    if difference == "missing_manifest":
        document.pop("adapter_manifest")
    elif difference == "missing_contract":
        document.pop("adapter_contract")
    elif difference == "manifest":
        document["adapter_manifest"]["items"][0]["metadata"]["name"] = "other-name"
    elif difference == "hash":
        document["assets"] = document["assets"] | {"adapter-contract.json": "f" * 64}
    elif difference == "oversized":
        document["adapter_manifest"]["oversized"] = "x" * (24 * 1024)
        document["assets"] = document["assets"] | {
            "adapter-manifest.json": hashlib.sha256(adapters.canonical_bytes(document["adapter_manifest"])).hexdigest()
        }
    elif difference == "nonfinite":
        document["adapter_contract"]["schema_sha256"] = float("nan")
    elif difference == "compatibility_not_object":
        document["compatibility"] = "unstructured"
    elif difference == "unknown_field":
        document["unrecognized_metadata"] = {}
    else:
        document.update(component="tools-facade", assets={})
    with pytest.raises(ValidationError):
        ReleaseArtifact.model_validate(document)
    assert not runtime.calls


@pytest.mark.parametrize("difference", ["cluster", "region", "environment", "route", "forbidden"])
def test_central_reader_refuses_wrong_target_or_failed_observation(runtime, monkeypatch, difference):
    artifact = ReleaseArtifact.model_validate(release_document(runtime))
    environment = "dev"
    if difference == "cluster":
        runtime.kube.description["name"] = "other-cluster"
    elif difference == "region":
        runtime.kube.scoped.region_name = "eu-west-1"
    elif difference == "environment":
        environment = "prod"
    elif difference == "route":
        runtime.slices[0]["endpoints"][0]["targetRef"]["uid"] = "unrelated-pod"
    else:
        runtime.probe_status = 403
    closed = []
    monkeypatch.setattr(runtime.kube, "close", lambda: closed.append(True))
    with pytest.raises(CycleBlockedError):
        inspect_release(runtime, artifact, environment=environment)
    if difference in {"route", "forbidden"}:
        assert closed == [True] and runtime.calls
    else:
        assert not closed and not runtime.calls

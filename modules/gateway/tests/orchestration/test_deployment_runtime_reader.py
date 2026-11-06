"""False-green runtime cases: actual digests, schema and published bytes matter."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src.orchestration.deployment_runtime_contract import ReleaseArtifact, RuntimeComponent
from src.orchestration.deployment_runtime_reader import SCHEMA_PROBE, KubernetesRuntime, PilotRuntimeReader
from src.orchestration.review_cycle import CycleBlockedError

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
ACCOUNT = "123456789012"
REGION = "us-east-1"
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-gateway"


def artifact(component="gateway-backend", **kwargs):
    return ReleaseArtifact(
        schema_version=1,
        repository_id=17,
        run_id=42,
        run_attempt=1,
        source_revision=SHA,
        workflow_revision=SHA,
        workflow_path=".github/workflows/gateway-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id=kwargs.pop("resource_id", "cluster/namespace"),
        component=component,
        produced_at=datetime.now(UTC),
        image_digest=None if component == "gateway-frontend" else DIGEST,
        **kwargs,
    )


@pytest.fixture
def pilot():
    function = {"Configuration": {"State": "Active", "LastUpdateStatus": "Successful"}, "Code": {"ResolvedImageUri": REGISTRY + "@" + DIGEST}}
    cloudfront = {
        "ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/D1",
        "DomainName": "d123.cloudfront.net",
        "Status": "Deployed",
        "DistributionConfig": {"Enabled": True},
    }
    clients = {
        "lambda": SimpleNamespace(get_function=Mock(side_effect=lambda **kwargs: function)),
        "ssm": SimpleNamespace(get_parameter=Mock(return_value={"Parameter": {"Value": "D1"}})),
        "cloudfront": SimpleNamespace(get_distribution=Mock(side_effect=lambda **kwargs: {"Distribution": cloudfront})),
    }
    scoped = SimpleNamespace(region_name=REGION, client=lambda service, **kwargs: clients[service])
    description = {"name": "cluster", "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/cluster"}
    kube = SimpleNamespace(gateway=Mock(return_value=(["pod-uid"], "060_example")), close=Mock())
    reader = PilotRuntimeReader(kubernetes=Mock(return_value=kube))
    return SimpleNamespace(
        reader=reader, scoped=scoped, description=description, function=function, clients=clients, kube=kube, cloudfront=cloudfront
    )


def inspect(ctx, release=None):
    return ctx.reader.inspect(
        ctx.scoped,
        ctx.description,
        "namespace",
        artifact=release or artifact(),
        artifact_hash="c" * 64,
        evidence_ref="github/actions/runs/42/artifacts/77",
        environment="dev",
    )


@pytest.fixture
def qualification_api(pilot):
    release = artifact("qualification-api", resource_id="cluster/namespace/sample-api")
    deployment = {
        "metadata": {"name": "sample-api", "namespace": "namespace", "uid": "deployment-uid", "generation": 4},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "sample-api"}},
            "template": {"spec": {"containers": [{"name": "api", "image": "registry.example/sample-api@" + DIGEST}]}},
        },
        "status": {"observedGeneration": 4, "updatedReplicas": 1, "availableReplicas": 1, "readyReplicas": 1},
    }
    replicas = {"metadata": {}, "items": [{"metadata": {"uid": "replica-uid", "ownerReferences": [{"uid": "deployment-uid", "controller": True}]}}]}
    pods = {
        "metadata": {},
        "items": [
            {
                "metadata": {"uid": "pod-uid", "namespace": "namespace", "ownerReferences": [{"uid": "replica-uid", "controller": True}]},
                "spec": {"containers": [{"name": "api", "image": "registry.example/sample-api@" + DIGEST}]},
                "status": {
                    "containerStatuses": [
                        {"name": "api", "ready": True, "state": {"running": {}}, "imageID": "registry.example/sample-api@" + DIGEST}
                    ],
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
        ],
    }
    values = {
        "/apis/apps/v1/namespaces/namespace/deployments/sample-api": deployment,
        "/apis/apps/v1/namespaces/namespace/replicasets": replicas,
        "/api/v1/namespaces/namespace/pods": pods,
    }
    pilot.kube.qualification_api = Mock(side_effect=lambda candidate: KubernetesRuntime.qualification_api(pilot.kube, candidate))
    pilot.kube.description = pilot.description
    pilot.kube.namespace = "namespace"
    pilot.kube.get = Mock(side_effect=lambda path, **params: values[path])
    return SimpleNamespace(pilot=pilot, release=release, deployment=deployment, pods=pods)


@pytest.mark.parametrize(
    "selector",
    [
        {"app": "sample-api"},
        {"app.kubernetes.io/name": "adp-qualification-api"},
        {"example.com/Name_1.test": "Value_1.test"},
        {"app": ""},
        {("a" * 63 + ".") * 3 + "a" * 61 + "/" + "N" * 63: "V" * 63},
    ],
)
@pytest.mark.parametrize(
    "failure",
    [None, "wrong-image", "stale-rollout", "foreign-owner", "missing-pod", "unready", "disappeared", "changed-pod-image", "missing-pod-spec"],
)
def test_qualification_api_reads_approved_workload_and_serving_image(qualification_api, selector, failure):
    from src.orchestration.deployment_controller import ADAPTERS

    assert ADAPTERS["qualification-api-runtime-verification"] == {"qualification-api"}
    pilot, release = qualification_api.pilot, qualification_api.release
    deployment, pods = qualification_api.deployment, qualification_api.pods
    deployment["spec"]["selector"]["matchLabels"] = selector
    if failure == "wrong-image":
        pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = "registry.example/sample-api@sha256:" + "f" * 64
    elif failure == "stale-rollout":
        deployment["status"]["observedGeneration"] = 3
    elif failure == "foreign-owner":
        pods["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "different-replica"
    elif failure == "missing-pod":
        pods["items"].clear()
    elif failure == "unready":
        pods["items"][0]["status"]["containerStatuses"][0]["ready"] = False
    elif failure == "disappeared":
        deployment["metadata"]["deletionTimestamp"] = datetime.now(UTC).isoformat()
    elif failure == "changed-pod-image":
        pods["items"][0]["spec"]["containers"][0]["image"] = "registry.example/other@" + DIGEST
    elif failure == "missing-pod-spec":
        pods["items"][0].pop("spec")
    if failure is None:
        result = inspect(pilot, release)
        assert result.pod_uids == ["pod-uid"] and result.image_digest == DIGEST
        assert result.actual_revision == SHA and result.migration_head is None
        assert pilot.kube.get.call_count == 3 and pilot.kube.close.called
        key, value = next(iter(selector.items()))
        pilot.kube.get.assert_any_call("/apis/apps/v1/namespaces/namespace/replicasets", labelSelector=f"{key}={value}", limit=10)
        pilot.kube.get.assert_any_call("/api/v1/namespaces/namespace/pods", labelSelector=f"{key}={value}", limit=2)
    else:
        with pytest.raises(CycleBlockedError, match="deployment_qualification_"):
            inspect(pilot, release)


@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"app": "api", "other": "api"},
        {"": "api"},
        {"/name": "api"},
        {"example.com/": "api"},
        {"example.com/a/b": "api"},
        {"EXAMPLE.com/name": "api"},
        {"example..com/name": "api"},
        {"-example.com/name": "api"},
        {"example-.com/name": "api"},
        {"example_com/name": "api"},
        {"a" * 64 + "/name": "api"},
        {("a" * 63 + ".") * 3 + "a" * 62 + "/name": "api"},
        {"example.com/" + "a" * 64: "api"},
        {"a" * 64: "api"},
        {"_app": "api"},
        {"app.": "api"},
        {"app,other": "api"},
        {"app=name": "api"},
        {"app": "api,other=value"},
        {"app": "a/b"},
        {"app": "-api"},
        {"app": "api_"},
        {"app": "a" * 64},
        {"app": None},
        {"app": 42},
        {"app": ["api"]},
        {42: "api"},
        ["app=api"],
    ],
)
def test_qualification_api_refuses_malformed_selector_before_listing_runtime(qualification_api, selector):
    qualification_api.deployment["spec"]["selector"]["matchLabels"] = selector
    with pytest.raises(CycleBlockedError, match="deployment_qualification_rollout_mismatch"):
        inspect(qualification_api.pilot, qualification_api.release)
    assert qualification_api.pilot.kube.get.call_count == 1


@pytest.mark.parametrize("failure", ["no-image", "no-workload", "assets"])
def test_qualification_api_artifact_requires_pinned_container_delivery(failure):
    data = dict(
        schema_version=1,
        repository_id=17,
        run_id=42,
        run_attempt=1,
        source_revision=SHA,
        workflow_revision=SHA,
        workflow_path=".github/workflows/api-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id="cluster/namespace/sample-api",
        component="qualification-api",
        image_digest=DIGEST,
        produced_at=datetime.now(UTC),
    )
    if failure == "no-image":
        data["image_digest"] = None
    elif failure == "no-workload":
        data["resource_id"] = "cluster/namespace"
    else:
        data["assets"] = {"unsigned": "a" * 64}
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReleaseArtifact(**data)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "wrong-image",
        "wrong-exit",
        "foreign-owner",
        "stale-run",
        "wrong-command",
        "wrong-pod-command",
        "wrong-pod-args",
        "missing-pod-command",
        "wrong-status-name",
        "missing-pod",
        "removed-job",
    ],
)
def test_qualification_client_requires_pinned_completed_job_not_serving_api(pilot, failure):
    from src.orchestration.deployment_controller import ADAPTERS

    assert ADAPTERS["qualification-client-job-verification"] == {"qualification-client"}
    command = {"name": "client", "image": "registry.example/client@" + DIGEST, "command": ["python", "client.py"], "args": ["--health"]}
    manifest_hash = hashlib.sha256(
        json.dumps({key: command[key] for key in ("image", "command", "args")}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    release = artifact("qualification-client", qualification_job="client-run-42", cli_manifest_sha256=manifest_hash)
    job = {
        "metadata": {
            "name": "client-run-42",
            "namespace": "namespace",
            "uid": "job-uid",
            "annotations": {
                "adp/source-revision": SHA,
                "adp/workflow-revision": SHA,
                "adp/workflow-run-id": "42",
                "adp/workflow-run-attempt": "1",
            },
        },
        "spec": {"completions": 1, "parallelism": 1, "template": {"spec": {"restartPolicy": "Never", "containers": [command]}}},
        "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
    }
    pods = {
        "metadata": {},
        "items": [
            {
                "metadata": {"uid": "client-pod", "namespace": "namespace", "ownerReferences": [{"uid": "job-uid", "controller": True}]},
                "spec": {"containers": [deepcopy(command)]},
                "status": {
                    "phase": "Succeeded",
                    "containerStatuses": [
                        {"name": "client", "imageID": "registry.example/client@" + DIGEST, "state": {"terminated": {"exitCode": 0}}}
                    ],
                },
            }
        ],
    }
    if failure == "wrong-image":
        pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = "registry.example/client@sha256:" + "f" * 64
    elif failure == "wrong-exit":
        pods["items"][0]["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 1
    elif failure == "foreign-owner":
        pods["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "another-job"
    elif failure == "stale-run":
        job["metadata"]["annotations"]["adp/workflow-run-id"] = "41"
    elif failure == "wrong-command":
        job["spec"]["template"]["spec"]["containers"][0]["args"] = ["--noop"]
    elif failure == "wrong-pod-command":
        pods["items"][0]["spec"]["containers"][0]["command"] = ["python", "-c"]
    elif failure == "wrong-pod-args":
        pods["items"][0]["spec"]["containers"][0]["args"] = ["print(0)"]
    elif failure == "missing-pod-command":
        pods["items"][0]["spec"]["containers"][0].pop("command")
    elif failure == "wrong-status-name":
        pods["items"][0]["status"]["containerStatuses"][0]["name"] = "other"
    elif failure == "missing-pod":
        pods["items"].clear()
    elif failure == "removed-job":
        job["metadata"]["deletionTimestamp"] = datetime.now(UTC).isoformat()
    values = {
        "/apis/batch/v1/namespaces/namespace/jobs/client-run-42": job,
        "/api/v1/namespaces/namespace/pods": pods,
    }
    pilot.kube.qualification_client = Mock(side_effect=lambda candidate: KubernetesRuntime.qualification_client(pilot.kube, candidate))
    pilot.kube.description = pilot.description
    pilot.kube.namespace = "namespace"
    pilot.kube.get = Mock(side_effect=lambda path, **params: values[path])
    if failure is None:
        result = inspect(pilot, release)
        assert result.component == "qualification-client" and result.cli_manifest_sha256 == manifest_hash
        assert result.pod_uids == ["client-pod"] and result.image_digest == DIGEST
        assert pilot.kube.get.call_count == 2 and pilot.kube.close.called
    else:
        with pytest.raises(CycleBlockedError, match="deployment_qualification_"):
            inspect(pilot, release)


@pytest.mark.parametrize("failure", ["missing-job", "missing-manifest", "missing-image"])
def test_qualification_client_artifact_refuses_unpinned_delivery(failure):
    fields = dict(
        schema_version=1,
        repository_id=17,
        run_id=42,
        run_attempt=1,
        source_revision=SHA,
        workflow_revision=SHA,
        workflow_path=".github/workflows/client-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id="cluster/namespace",
        component="qualification-client",
        produced_at=datetime.now(UTC),
        image_digest=DIGEST,
        qualification_job="client-run-42",
        cli_manifest_sha256="f" * 64,
    )
    fields[{"missing-job": "qualification_job", "missing-manifest": "cli_manifest_sha256", "missing-image": "image_digest"}[failure]] = None
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReleaseArtifact(**fields)


def test_backend_requires_actual_pod_schema_and_tick_digest(pilot):
    result = inspect(pilot)
    assert result.image_digest == result.tick_digest == DIGEST and result.migration_head == "060_example"
    assert result.pod_uids == ["pod-uid"]
    assert (
        pilot.clients["lambda"].get_function.call_args.kwargs["FunctionName"]
        == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:adp-dev-orchestration-tick"
    )
    assert pilot.kube.close.called


@pytest.mark.parametrize("failure", ["old_tick", "unhealthy_tick", "wrong_account", "wrong_region", "wrong_cluster"])
def test_workflow_green_cannot_hide_wrong_or_partial_runtime(pilot, failure):
    if failure == "old_tick":
        pilot.function["Code"]["ResolvedImageUri"] = REGISTRY + "@sha256:" + "f" * 64
    elif failure == "unhealthy_tick":
        pilot.function["Configuration"]["LastUpdateStatus"] = "InProgress"
    elif failure == "wrong_account":
        pilot.description["arn"] = pilot.description["arn"].replace(ACCOUNT, "999999999999")
    elif failure == "wrong_region":
        pilot.scoped.region_name = "us-west-2"
    else:
        pilot.description["name"] = "other-cluster"
    with pytest.raises(CycleBlockedError):
        inspect(pilot)


def test_frontend_compares_actual_published_bytes_to_built_artifact(pilot):
    contents = {"/index.html": b"<html>release</html>", "/assets/app.js": b"the built javascript"}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=contents[request.url.path]))) as client:
        pilot.reader.public_client = client
        release = artifact("gateway-frontend", assets={name[1:]: hashlib.sha256(body).hexdigest() for name, body in contents.items()})
        result = inspect(pilot, release)
        assert result.asset_count == 2 and result.healthy
        contents["/assets/app.js"] = b"old javascript"
        with pytest.raises(CycleBlockedError, match="artifact_mismatch"):
            inspect(pilot, release)


def test_frontend_provider_identity_cannot_redirect_readback(pilot):
    pilot.cloudfront["DomainName"] = "metadata.internal"
    with pytest.raises(CycleBlockedError, match="domain_invalid"):
        inspect(pilot, artifact("gateway-frontend", assets={"index.html": "f" * 64}))


@pytest.mark.parametrize("service,method", [("ssm", "get_parameter"), ("cloudfront", "get_distribution")])
def test_frontend_observer_requires_selected_identity_permissions(pilot, service, method):
    from botocore.exceptions import ClientError

    getattr(pilot.clients[service], method).side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Synthetic denied observer"}}, method
    )
    with pytest.raises(ClientError):
        inspect(pilot, artifact("gateway-frontend", assets={"index.html": "f" * 64}))
    pilot.clients["ssm"].get_parameter.assert_called_once_with(Name="/adp/dev/gateway/cloudfront-id")
    assert not pilot.kube.gateway.called


@pytest.fixture
def cluster():
    deployment = {
        "metadata": {"uid": "deployment-uid", "generation": 7},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "bedrockgateway", "image": REGISTRY + ":" + SHA}]}}},
        "status": {"observedGeneration": 7, "updatedReplicas": 1, "availableReplicas": 1},
    }
    pod = {
        "metadata": {"name": "pod-one", "uid": "pod-uid", "ownerReferences": [{"uid": "rs-uid", "controller": True}]},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "bedrockgateway", "ready": True, "imageID": REGISTRY + "@" + DIGEST, "state": {"running": {}}}],
        },
    }
    replicas = [{"metadata": {"uid": "rs-uid", "ownerReferences": [{"uid": "deployment-uid", "controller": True}]}}]
    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "namespace"
    kube.get = Mock(side_effect=lambda *args, **kwargs: deepcopy(deployment))
    kube.resources = Mock(side_effect=lambda path: replicas if "replicasets" in path else [pod])
    kube.schema = Mock(return_value="060_example")
    return SimpleNamespace(kube=kube, deployment=deployment, pod=pod, replicas=replicas)


def gateway(ctx):
    return ctx.kube.gateway(digest=DIGEST, source_revision=SHA, account=ACCOUNT, region=REGION)


def test_all_serving_replicas_are_owned_ready_at_digest_and_schema(cluster):
    assert gateway(cluster) == (["pod-uid"], "060_example")
    cluster.kube.schema.assert_called_once_with(cluster.pod)


@pytest.mark.parametrize("wheel", ["b" * 64, None, "c" * 64])
def test_security_gateway_wheel_requires_protected_serving_pod_proof(cluster, wheel):
    cluster.kube.schema.return_value = {"database_heads": ["060_example"], "l5_wheel_sha256": wheel}
    if wheel == "b" * 64:
        assert cluster.kube.gateway(digest=DIGEST, source_revision=SHA, account=ACCOUNT, region=REGION, wheel_sha256="b" * 64) == (
            ["pod-uid"],
            "060_example",
        )
    else:
        with pytest.raises(CycleBlockedError, match="deployment_gateway_wheel_mismatch"):
            cluster.kube.gateway(digest=DIGEST, source_revision=SHA, account=ACCOUNT, region=REGION, wheel_sha256="b" * 64)
    cluster.kube.schema.assert_called_once_with(cluster.pod, product=True)


@pytest.mark.parametrize("profile_enabled", [False, True])
@pytest.mark.parametrize("onboarding_enabled", [False, True])
def test_product_release_observes_cli_and_flags_on_ready_pods(cluster, pilot, profile_enabled, onboarding_enabled):
    release = artifact(
        product_profile_enabled=profile_enabled,
        product_onboarding_enabled=onboarding_enabled,
        product_profile_schema_version="1.0",
        migration_head="060_example",
        cli_manifest_sha256="c" * 64,
    )
    cluster.deployment["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "BG_PRODUCT_PROFILE_ENABLED", "value": str(profile_enabled).lower()},
        {"name": "BG_PRODUCT_ONBOARDING_ENABLED", "value": str(onboarding_enabled).lower()},
    ]
    cluster.kube.schema.return_value = {
        "database_heads": ["060_example"],
        "product_profile_enabled": str(profile_enabled).lower(),
        "product_onboarding_enabled": str(onboarding_enabled).lower(),
        "cli_manifest_sha256": "c" * 64,
    }
    uids, head = gateway_product(cluster, release)
    assert uids == ["pod-uid"] and head == release.migration_head
    cluster.kube.schema.assert_called_once_with(cluster.pod, product=True)
    result = inspect(pilot, release)
    assert result.migration_head == release.migration_head
    assert result.cli_manifest_sha256 == release.cli_manifest_sha256
    assert result.product_profile_enabled is profile_enabled and result.product_onboarding_enabled is onboarding_enabled
    assert pilot.kube.gateway.call_args.kwargs["product"] == release


def gateway_product(cluster, release):
    return cluster.kube.gateway(
        digest=DIGEST,
        source_revision=SHA,
        account=ACCOUNT,
        region=REGION,
        product=release,
    )


@pytest.mark.parametrize("failure", ["missing_flags", "duplicate_flag", "wrong_flag", "missing_cli", "wrong_cli", "wrong_pod_flag"])
def test_product_release_rejects_unverified_served_artifact(cluster, failure):
    release = artifact(
        product_profile_enabled=True,
        product_onboarding_enabled=False,
        product_profile_schema_version="1.0",
        migration_head="060_example",
        cli_manifest_sha256="c" * 64,
    )
    env = [
        {"name": "BG_PRODUCT_PROFILE_ENABLED", "value": "true"},
        {"name": "BG_PRODUCT_ONBOARDING_ENABLED", "value": "false"},
    ]
    cluster.deployment["spec"]["template"]["spec"]["containers"][0]["env"] = env
    cluster.kube.schema.return_value = {
        "database_heads": ["060_example"],
        "product_profile_enabled": "true",
        "product_onboarding_enabled": "false",
        "cli_manifest_sha256": "c" * 64,
    }
    if failure == "missing_flags":
        env.pop()
    elif failure == "duplicate_flag":
        env.append(env[0])
    elif failure == "wrong_flag":
        env[0]["value"] = "false"
    elif failure == "missing_cli":
        cluster.kube.schema.return_value.pop("cli_manifest_sha256")
    elif failure == "wrong_cli":
        cluster.kube.schema.return_value["cli_manifest_sha256"] = "d" * 64
    else:
        cluster.kube.schema.return_value["product_profile_enabled"] = "false"
    with pytest.raises(CycleBlockedError):
        gateway_product(cluster, release)


def test_product_release_rejects_schema_drift_and_incomplete_receipt(pilot):
    release = artifact(
        product_profile_enabled=True,
        product_onboarding_enabled=False,
        product_profile_schema_version="1.0",
        migration_head="060_example",
        cli_manifest_sha256="c" * 64,
    )
    pilot.kube.gateway.return_value = (["pod-uid"], "old_schema")
    with pytest.raises(CycleBlockedError, match="product_schema_mismatch"):
        inspect(pilot, release)
    with pytest.raises(CycleBlockedError, match="product_release_incomplete"):
        inspect(pilot, artifact(product_profile_schema_version="1.0"))


@pytest.mark.parametrize(
    "failure", ["no_replicas", "old_generation", "partial_rollout", "old_spec", "old_pod", "unready", "wrong_owner", "migration", "rollout_race"]
)
def test_stale_digest_partial_migration_or_racing_rollout_refuses(cluster, failure):
    if failure == "no_replicas":
        cluster.deployment["spec"]["replicas"] = 0
    elif failure == "old_generation":
        cluster.deployment["status"]["observedGeneration"] = 6
    elif failure == "partial_rollout":
        cluster.deployment["status"]["availableReplicas"] = 0
    elif failure == "old_spec":
        cluster.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = REGISTRY + ":latest"
    elif failure == "old_pod":
        cluster.pod["status"]["containerStatuses"][0]["imageID"] = REGISTRY + "@sha256:" + "f" * 64
    elif failure == "unready":
        cluster.pod["status"]["containerStatuses"][0]["ready"] = False
    elif failure == "wrong_owner":
        cluster.pod["metadata"]["ownerReferences"][0]["uid"] = "another-deployment-rs"
    elif failure == "migration":
        cluster.kube.schema.side_effect = CycleBlockedError("deployment_migration_not_at_single_head")
    else:
        changed = deepcopy(cluster.deployment)
        changed["metadata"]["generation"] += 1
        cluster.kube.get.side_effect = [cluster.deployment, changed]
    with pytest.raises(CycleBlockedError):
        gateway(cluster)


@pytest.mark.parametrize("heads", [["old"], ["head", "other"]])
def test_schema_probe_refuses_old_or_multiple_database_heads(heads):
    kube = object.__new__(KubernetesRuntime)
    kube.namespace, kube.endpoint, kube.tls = "namespace", "https://cluster.test", object()
    kube.scoped, kube.description = object(), {"name": "cluster"}
    kube.get = Mock(return_value={"metadata": {"uid": "pod-uid"}})
    messages = iter([b"\x01" + json.dumps({"database_heads": heads, "image_heads": ["head"]}).encode(), b'\x03{"status":"Success"}'])

    class Channel:
        subprotocol = "v4.channel.k8s.io"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def recv(self, **kwargs):
            return next(messages)

    kube.websocket = Mock(return_value=Channel())
    from unittest.mock import patch

    with patch("src.orchestration.deployment_runtime_reader.eks_token", return_value="scoped-token"):
        with pytest.raises(CycleBlockedError, match="single_head"):
            kube.schema({"metadata": {"name": "pod-one", "uid": "pod-uid"}})
    assert kube.websocket.call_args.kwargs["proxy"] is None
    assert "SET TRANSACTION READ ONLY" in SCHEMA_PROBE


@pytest.mark.parametrize("missing", [None, "cli_manifest_sha256", "product_profile_enabled", "product_onboarding_enabled"])
def test_product_probe_requires_observed_flags_and_cli_bytes(missing):
    kube = object.__new__(KubernetesRuntime)
    kube.namespace, kube.endpoint, kube.tls = "namespace", "https://cluster.test", object()
    kube.scoped, kube.description = object(), {"name": "cluster"}
    kube.get = Mock(return_value={"metadata": {"uid": "pod-uid"}})
    proof = {
        "database_heads": ["head"],
        "image_heads": ["head"],
        "cli_manifest_sha256": "c" * 64,
        "product_profile_enabled": "true",
        "product_onboarding_enabled": "false",
    }
    if missing:
        proof.pop(missing)
    kube.protected_schema = Mock(return_value=proof)
    if missing:
        with pytest.raises(CycleBlockedError, match="product_probe_incomplete"):
            kube.schema({"metadata": {"name": "pod-one", "uid": "pod-uid"}}, product=True)
    else:
        assert kube.schema({"metadata": {"name": "pod-one", "uid": "pod-uid"}}, product=True) == proof
    assert "/app/cli/command-manifest.json" in SCHEMA_PROBE


def test_product_runtime_requires_complete_flags_and_served_cli_hash(pilot):
    result = inspect(pilot)
    with pytest.raises(ValueError, match="Product runtime evidence"):
        RuntimeComponent.model_validate({**result.model_dump(mode="json"), "product_profile_enabled": True})


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "two_pods",
        "retry_pod",
        "unreachable",
        "nonce",
        "stale",
        "wrong_uid",
        "redirect",
        "unavailable",
        "database",
        "cli",
        "pod_replaced",
        "old_tick",
    ],
)
def test_product_runtime_uses_protected_process_route_without_exec(cluster, pilot, monkeypatch, failure):
    import asyncio
    from datetime import timedelta

    from botocore.credentials import Credentials
    from fastapi import FastAPI

    from src.auth import caller_provenance
    from src.products import runtime_proof_routes

    release = artifact(
        product_profile_enabled=True,
        product_onboarding_enabled=False,
        product_profile_schema_version="1.0",
        migration_head="060_example",
        cli_manifest_sha256="c" * 64,
    )
    monkeypatch.setenv("ADP_RUNTIME_PROOF_INVOKE_URL", "https://example.execute-api.us-east-1.amazonaws.com/dev")
    monkeypatch.setenv("ADP_RUNTIME_PROOF_ROLE_ARNS", f"arn:aws:iam::{ACCOUNT}:role/reader")
    monkeypatch.setenv("ADP_POD_UID", "pod-uid")
    monkeypatch.setenv("ADP_POD_NAMESPACE", "namespace")
    monkeypatch.setattr(caller_provenance, "get_settings", lambda: SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret="fixture-edge"))

    async def process_proof():
        return {
            "database_heads": ["old" if failure == "database" else "060_example"],
            "image_heads": ["060_example"],
            "cli_manifest_sha256": ("d" if failure == "cli" else "c") * 64,
            "product_profile_enabled": "true",
            "product_onboarding_enabled": "false",
        }

    monkeypatch.setattr(runtime_proof_routes, "process_proof", process_proof)
    app = FastAPI()
    app.include_router(runtime_proof_routes.router)

    async def serve(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            return await client.post(
                "/internal/v1/runtime-proof/schema",
                content=request.content,
                headers={
                    "Content-Type": "application/json",
                    "X-Caller-Identity": f"arn:aws:sts::{ACCOUNT}:assumed-role/reader/session",
                    "X-Adp-Edge-Provenance": "fixture-edge",
                },
            )

    calls = []
    pods = [cluster.pod]
    if failure == "two_pods":
        second = deepcopy(cluster.pod)
        second["metadata"].update({"name": "pod-two", "uid": "pod-uid-two"})
        pods.append(second)
        cluster.deployment["spec"]["replicas"] = 2
        cluster.deployment["status"].update({"updatedReplicas": 2, "availableReplicas": 2})
        cluster.kube.resources.side_effect = lambda path: cluster.replicas if "replicasets" in path else pods

    def transport(request):
        calls.append(request)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
        if failure == "unreachable" or (failure == "retry_pod" and len(calls) == 1):
            return httpx.Response(409)
        if failure == "two_pods":
            monkeypatch.setenv("ADP_POD_UID", json.loads(request.content)["pod_uid"])
        if failure in {"redirect", "unavailable"}:
            return httpx.Response(302 if failure == "redirect" else 403, headers={"Location": "https://other.invalid"})
        result = asyncio.run(serve(request))
        payload = result.json()
        if failure == "nonce":
            payload["nonce"] = "f" * 64
        elif failure == "wrong_uid":
            payload["pod_uid"] = "foreign-uid"
        elif failure == "stale":
            payload["observed_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        return httpx.Response(result.status_code, json=payload)

    http_client = httpx.Client
    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.httpx.Client", lambda **_: http_client(transport=httpx.MockTransport(transport)))
    kube = cluster.kube
    kube.schema = KubernetesRuntime.schema.__get__(kube)
    kube.scoped = pilot.scoped
    pilot.scoped.get_credentials = lambda: Credentials("fixture-key", "fixture-secret")
    kube.close = Mock()
    kube.websocket = Mock(side_effect=AssertionError("Product must not use pod exec"))
    kube.get.side_effect = (
        lambda path: (
            {"metadata": {"uid": "replaced"}} if failure == "pod_replaced" else next(pod for pod in pods if path.endswith(pod["metadata"]["name"]))
        )
        if "/pods/" in path
        else cluster.deployment
    )
    cluster.deployment["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "BG_PRODUCT_PROFILE_ENABLED", "value": "true"},
        {"name": "BG_PRODUCT_ONBOARDING_ENABLED", "value": "false"},
    ]
    pilot.reader = PilotRuntimeReader(kubernetes=lambda *_: kube)
    if failure == "old_tick":
        pilot.function["Code"]["ResolvedImageUri"] = REGISTRY + "@sha256:" + "f" * 64
    if failure not in (None, "two_pods", "retry_pod"):
        with pytest.raises(CycleBlockedError):
            inspect(pilot, release)
    else:
        receipt = inspect(pilot, release)
        assert receipt.pod_uids == [pod["metadata"]["uid"] for pod in pods]
        assert receipt.tick_digest == DIGEST and receipt.migration_head == "060_example"
        assert receipt.cli_manifest_sha256 == "c" * 64
    assert len(calls) == (100 if failure == "unreachable" else 2 if failure in {"two_pods", "retry_pod"} else 1)
    kube.websocket.assert_not_called()


def test_runtime_model_rejects_unproven_success():
    with pytest.raises(ValueError):
        RuntimeComponent(
            component="gateway-backend",
            actual_revision=SHA,
            artifact_hash="c" * 64,
            image_digest=DIGEST,
            healthy=True,
            evidence_ref="run",
            observed_at=datetime.now(UTC),
        )


def test_eks_presign_binds_registered_credentials_and_cluster_without_network():
    import base64
    from urllib.parse import parse_qs, urlparse

    import boto3

    from src.orchestration.deployment_runtime_reader import eks_token

    scoped = boto3.Session(aws_access_key_id="ASIATEST", aws_secret_access_key="test-secret", aws_session_token="test-session", region_name=REGION)
    token = eks_token(scoped, "verified-cluster")
    encoded = token.removeprefix("k8s-aws-v1.")
    url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.hostname in {"sts.us-east-1.amazonaws.com", "sts.amazonaws.com"}
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-SignedHeaders"] == ["host;x-k8s-aws-id"]
    assert query["X-Amz-Security-Token"] == ["test-session"]
    assert query["X-Amz-Expires"] == ["60"]
    assert "ASIATEST/" in query["X-Amz-Credential"][0]


@pytest.mark.parametrize("failure", [None, "protocol", "exit", "oversize", "replaced", "text_frame"])
def test_schema_websocket_readonly_success_and_transport_failures(failure):
    from unittest.mock import patch

    kube = object.__new__(KubernetesRuntime)
    kube.namespace, kube.endpoint, kube.tls = "namespace", "https://cluster.test", object()
    kube.scoped, kube.description = object(), {"name": "cluster"}
    kube.get = Mock(return_value={"metadata": {"uid": "replaced" if failure == "replaced" else "pod-uid"}})
    frames = [b'\x01{"database_heads":["head"],"image_heads":["head"]}', b'\x03{"status":"Success"}']
    if failure == "exit":
        frames[1] = b'\x03{"status":"Failure"}'
    elif failure == "oversize":
        frames[0] = b"\x01" + b"a" * 65536
    elif failure == "text_frame":
        frames[0] = "unexpected text frame"
    channel = Mock(subprotocol="wrong" if failure == "protocol" else "v4.channel.k8s.io")
    channel.recv.side_effect = frames
    from contextlib import nullcontext

    kube.websocket = Mock(return_value=nullcontext(channel))
    with patch("src.orchestration.deployment_runtime_reader.eks_token", return_value="scoped-token"):
        if failure:
            with pytest.raises(CycleBlockedError):
                kube.schema({"metadata": {"name": "pod-one", "uid": "pod-uid"}})
        else:
            assert kube.schema({"metadata": {"name": "pod-one", "uid": "pod-uid"}}) == "head"
    kwargs = kube.websocket.call_args.kwargs
    assert kwargs["ssl"] is kube.tls and kwargs["proxy"] is None
    assert kwargs["additional_headers"] == {"Authorization": "Bearer scoped-token"}


def test_kubernetes_constructor_uses_cluster_ca_and_scoped_bearer():
    import base64
    from unittest.mock import patch

    description = {"name": "cluster", "endpoint": "https://cluster.test", "certificateAuthority": {"data": base64.b64encode(b"cluster-ca").decode()}}
    with patch("src.orchestration.deployment_runtime_reader.ssl.create_default_context", return_value="tls-context") as tls:
        with patch("src.orchestration.deployment_runtime_reader.httpx.Client") as factory:
            kube = KubernetesRuntime(object(), description, "namespace")
            tls.assert_called_once_with(cadata="cluster-ca")
            assert factory.call_args.kwargs == {"verify": "tls-context", "timeout": 10, "trust_env": False, "follow_redirects": False}
            kube.close()
            factory.return_value.close.assert_called_once()


def test_runtime_http_reader_bounds_before_buffering():
    from src.orchestration.deployment_runtime_reader import MAX_RESPONSE, read_bytes

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE + 1)))) as client:
        with pytest.raises(CycleBlockedError, match="response_limit"):
            read_bytes(client, "https://cluster.test")


def test_insights_release_requires_readback_of_bucket_and_serving_pods():
    digest = DIGEST
    target = artifact("insights", assets={"platform/workloads.tf": "f" * 64}).model_copy(update={"resource_id": "cluster/adp-insights"})
    s3 = SimpleNamespace(
        head_bucket=Mock(return_value={}),
        get_bucket_versioning=Mock(return_value={"Status": "Enabled"}),
        get_bucket_encryption=Mock(
            return_value={
                "ServerSideEncryptionConfiguration": {
                    "Rules": [
                        {
                            "ApplyServerSideEncryptionByDefault": {
                                "SSEAlgorithm": "aws:kms",
                                "KMSMasterKeyID": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/key",
                            }
                        }
                    ]
                }
            }
        ),
        get_public_access_block=Mock(
            return_value={
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            }
        ),
    )
    kube = SimpleNamespace(insights=Mock(return_value=["emitter-pod", "distiller-pod"]), close=Mock())
    scoped = SimpleNamespace(region_name=REGION, client=lambda service, **kwargs: {"s3": s3}[service])
    description = {"name": "cluster", "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/cluster"}
    reader = PilotRuntimeReader(kubernetes=Mock(return_value=kube))
    args = (scoped, description, "adp-insights")
    kwargs = dict(artifact=target, artifact_hash="c" * 64, evidence_ref="github/actions/runs/42/artifacts/77", environment="dev")
    result = reader.inspect(*args, **kwargs)
    assert result.healthy and result.image_digest == digest and result.pod_uids == ["emitter-pod", "distiller-pod"]
    s3.get_bucket_versioning.return_value = {"Status": "Suspended"}
    with pytest.raises(CycleBlockedError, match="versioning_missing"):
        reader.inspect(*args, **kwargs)
    s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
    kube.insights.side_effect = CycleBlockedError("deployment_insights_release_not_serving")
    with pytest.raises(CycleBlockedError, match="release_not_serving"):
        reader.inspect(*args, **kwargs)
    kube.insights.side_effect = None
    with pytest.raises(CycleBlockedError, match="release_image_missing"):
        reader.inspect(*args, **(kwargs | {"artifact": target.model_copy(update={"image_digest": None})}))


@pytest.fixture
def insights_runtime():
    digest = DIGEST
    registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-dev-insights-"
    deployments = {
        name: {
            "metadata": {"uid": name + "-deployment", "generation": 1},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {"serviceAccountName": "insights-" + name, "containers": [{"name": name, "image": registry + name + "@" + digest}]}
                },
            },
            "status": {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1},
        }
        for name in ("emitter", "distiller")
    }

    policies = [
        {"metadata": {"name": "deny-all"}, "spec": {"policyTypes": ["Ingress", "Egress"], "podSelector": {}}},
        {
            "metadata": {"name": "gateway-s3-github-egress"},
            "spec": {
                "policyTypes": ["Egress"],
                "podSelector": {},
                "egress": [
                    {
                        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
                        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                    },
                    {
                        "ports": [{"port": 8080, "protocol": "TCP"}],
                        "to": [
                            {
                                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "adp-gateway"}},
                                "podSelector": {"matchLabels": {"app": "bedrockgateway"}},
                            }
                        ],
                    },
                    {"ports": [{"port": 443, "protocol": "TCP"}], "to": [{"ipBlock": {"cidr": "10.0.1.7/32"}}]},
                    {"ports": [{"port": 443, "protocol": "TCP"}], "to": [{"ipBlock": {"cidr": "192.0.2.0/24"}}]},
                ],
            },
        },
    ]

    def get(path, **kwargs):
        if path.endswith("/resourcequotas/insights-budget"):
            return {"spec": {"hard": {"pods": "4"}}}
        if path.endswith("/networkpolicies"):
            return {"items": policies}
        if "/serviceaccounts/insights-" in path:
            name = path.rsplit("-", 1)[-1]
            return {"metadata": {"annotations": {"eks.amazonaws.com/role-arn": f"arn:aws:iam::{ACCOUNT}:role/adp-dev-insights-{name}"}}}
        if path.endswith("/replicasets"):
            name = kwargs["labelSelector"].removeprefix("app=insights-")
            return {"items": [{"metadata": {"uid": name + "-rs", "ownerReferences": [{"uid": name + "-deployment", "controller": True}]}}]}
        if path.endswith("/deployments/insights-emitter"):
            return deployments["emitter"]
        if path.endswith("/deployments/insights-distiller"):
            return deployments["distiller"]
        if path.endswith("/pods"):
            service = kwargs["labelSelector"].removeprefix("app=insights-")
            return {
                "items": [
                    {
                        "metadata": {"uid": service + "-pod", "ownerReferences": [{"uid": service + "-rs", "controller": True}]},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [
                                {"name": service, "ready": True, "imageID": registry + service + "@" + digest, "state": {"running": {}}}
                            ],
                        },
                    }
                ]
            }
        return {"metadata": {"name": "adp-insights", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}

    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "adp-insights"
    kube.get = Mock(side_effect=get)
    kube.description = {"resourcesVpcConfig": {"vpcId": "vpc-target"}}
    ec2 = SimpleNamespace(
        describe_prefix_lists=Mock(return_value={"PrefixLists": [{"Cidrs": ["192.0.2.0/24"]}]}),
        describe_vpc_endpoints=Mock(
            return_value={
                "VpcEndpoints": [
                    {
                        "VpcId": "vpc-target",
                        "VpcEndpointType": "Interface",
                        "PrivateDnsEnabled": True,
                        "State": "available",
                        "NetworkInterfaceIds": ["eni-sts"],
                    }
                ]
            }
        ),
        describe_network_interfaces=Mock(
            return_value={"NetworkInterfaces": [{"NetworkInterfaceId": "eni-sts", "VpcId": "vpc-target", "PrivateIpAddress": "10.0.1.7"}]}
        ),
    )
    kube.scoped = SimpleNamespace(client=Mock(return_value=ec2))
    github = SimpleNamespace(status=200, metadata={"api": ["203.0.113.0/24", "2001:db8::/48"]}, requests=[])

    def metadata(request):
        assert request.method == "GET" and str(request.url) == "https://api.github.com/meta"
        assert "authorization" not in request.headers
        github.requests.append(request)
        return httpx.Response(github.status, json=github.metadata)

    with httpx.Client(transport=httpx.MockTransport(metadata)) as public_client:
        args = dict(digest=digest, account=ACCOUNT, region=REGION, environment="dev", public_client=public_client)
        yield SimpleNamespace(kube=kube, deployments=deployments, policies=policies, ec2=ec2, args=args, github=github)


def test_insights_kubernetes_readback_rejects_zero_replicas_and_other_images(insights_runtime):
    kube, deployments, args = insights_runtime.kube, insights_runtime.deployments, insights_runtime.args
    assert kube.insights(**args) == ["distiller-pod", "emitter-pod"]
    assert insights_runtime.github.requests == []
    insights_runtime.ec2.describe_prefix_lists.assert_called_once_with(
        Filters=[{"Name": "prefix-list-name", "Values": [f"com.amazonaws.{REGION}.s3"]}]
    )
    insights_runtime.ec2.describe_vpc_endpoints.assert_called_once_with(
        Filters=[{"Name": "vpc-id", "Values": ["vpc-target"]}, {"Name": "service-name", "Values": [f"com.amazonaws.{REGION}.sts"]}]
    )
    insights_runtime.ec2.describe_network_interfaces.assert_called_once_with(NetworkInterfaceIds=["eni-sts"])
    deployments["emitter"]["spec"]["replicas"] = 0
    with pytest.raises(CycleBlockedError, match="rollout_incomplete"):
        kube.insights(**args)
    deployments["emitter"]["spec"]["replicas"] = 1
    deployments["emitter"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-dev-insights-emitter@sha256:" + "e" * 64
    )
    with pytest.raises(CycleBlockedError, match="pod_not_healthy"):
        kube.insights(**args)


@pytest.mark.parametrize(
    "peers",
    [
        [{"namespaceSelector": {}}],
        [{"ipBlock": {"cidr": "0.0.0.0/1"}}, {"ipBlock": {"cidr": "128.0.0.0/1"}}],
        [{"podSelector": {}}],
        [{}],
        [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
        [{"ipBlock": {"cidr": "::/0"}}],
        [{"ipBlock": {"cidr": "::/1"}}, {"ipBlock": {"cidr": "8000::/1"}}],
        [{"ipBlock": {"cidr": "198.51.100.0/24"}}],
        [{"ipBlock": {"cidr": "10.0.1.0/24"}}],
        [{"ipBlock": {"cidr": "10.0.1.8/32"}}],
        [{"ipBlock": {"cidr": "not-a-cidr"}}],
        [{"ipBlock": {"cidr": "192.0.2.0/24"}, "namespaceSelector": {}}],
    ],
    ids=[
        "all-namespaces",
        "split-default-route",
        "all-pods",
        "all-peers",
        "default-route",
        "ipv6-default-route",
        "ipv6-split-default-route",
        "unknown-network",
        "sts-subnet",
        "wrong-sts-interface",
        "malformed-cidr",
        "mixed-peer",
    ],
)
def test_insights_unrestricted_egress_cannot_issue_health(insights_runtime, peers):
    insights_runtime.policies[1]["spec"]["egress"][3]["to"].extend(peers)
    with pytest.raises(CycleBlockedError, match="deployment_insights_.*egress"):
        insights_runtime.kube.insights(**insights_runtime.args)


@pytest.mark.parametrize(
    "rule_index,field,value",
    [
        (0, "to", [{"namespaceSelector": {}}]),
        (0, "to", [{"namespaceSelector": {"matchLabels": {"name": "kube-system"}}}]),
        (1, "to", [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "adp-gateway"}}}]),
        (1, "to", [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"app": "bedrockgateway"}}}]),
        (2, "ports", [{"port": 443, "protocol": "UDP"}]),
        (2, "ports", [{"protocol": "TCP"}]),
        (2, "ports", [{"port": 443, "endPort": 65535, "protocol": "TCP"}]),
        (2, "ports", [{"port": "https", "protocol": "TCP"}]),
        (2, "ports", []),
        (2, "to", []),
    ],
)
def test_insights_selector_and_port_drift_cannot_issue_health(insights_runtime, rule_index, field, value):
    insights_runtime.policies[1]["spec"]["egress"][rule_index][field] = value
    with pytest.raises(CycleBlockedError, match="deployment_insights_.*egress"):
        insights_runtime.kube.insights(**insights_runtime.args)


@pytest.mark.parametrize("drift", ["extra-policy", "missing-deny", "deny-grants-traffic", "limited-deny", "missing-sts", "paginated"])
def test_insights_policy_set_must_be_complete_and_restrictive(insights_runtime, drift):
    policies = insights_runtime.policies
    if drift == "extra-policy":
        policies.append({"metadata": {"name": "allow-all"}, "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}})
    elif drift == "missing-deny":
        policies.pop(0)
    elif drift == "deny-grants-traffic":
        policies[0]["spec"]["egress"] = [{}]
    elif drift == "limited-deny":
        policies[0]["spec"]["podSelector"] = {"matchLabels": {"app": "insights-emitter"}}
    elif drift == "missing-sts":
        policies[1]["spec"]["egress"].pop(2)
    else:
        original_get = insights_runtime.kube.get.side_effect
        insights_runtime.kube.get.side_effect = lambda path, **kwargs: (
            {"items": policies, "metadata": {"continue": "more"}} if path.endswith("/networkpolicies") else original_get(path, **kwargs)
        )
    with pytest.raises(CycleBlockedError, match="deployment_insights_"):
        insights_runtime.kube.insights(**insights_runtime.args)


@pytest.mark.parametrize("cidr", ["203.0.113.0/24", "203.0.113.64/26", "2001:db8:0:1::/64"])
def test_insights_github_api_destinations_are_verified_when_enabled(insights_runtime, cidr):
    insights_runtime.policies[1]["spec"]["egress"][3]["to"].append({"ipBlock": {"cidr": cidr}})
    assert insights_runtime.kube.insights(**insights_runtime.args) == ["distiller-pod", "emitter-pod"]
    assert len(insights_runtime.github.requests) == 1


@pytest.mark.parametrize("drift", ["missing-s3", "paginated-s3", "public-sts", "wrong-vpc", "missing-interface", "unreadable"])
def test_insights_aws_destination_reads_fail_closed(insights_runtime, drift):
    ec2 = insights_runtime.ec2
    if drift == "missing-s3":
        ec2.describe_prefix_lists.return_value = {"PrefixLists": []}
    elif drift == "paginated-s3":
        ec2.describe_prefix_lists.return_value["NextToken"] = "more"
    elif drift == "public-sts":
        ec2.describe_vpc_endpoints.return_value["VpcEndpoints"][0]["PrivateDnsEnabled"] = False
    elif drift == "wrong-vpc":
        ec2.describe_network_interfaces.return_value["NetworkInterfaces"][0]["VpcId"] = "vpc-other"
    elif drift == "missing-interface":
        ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": []}
    else:
        ec2.describe_prefix_lists.side_effect = TimeoutError("EC2 unavailable")
    with pytest.raises((CycleBlockedError, TimeoutError)):
        insights_runtime.kube.insights(**insights_runtime.args)


@pytest.mark.parametrize("metadata", [{}, {"api": []}, {"api": ["0.0.0.0/0"]}, {"api": ["invalid"]}, {"web": ["203.0.113.0/24"]}])
def test_insights_github_metadata_must_supply_bounded_api_ranges(insights_runtime, metadata):
    insights_runtime.policies[1]["spec"]["egress"][3]["to"].append({"ipBlock": {"cidr": "203.0.113.0/24"}})
    insights_runtime.github.metadata = metadata
    with pytest.raises(CycleBlockedError, match="deployment_insights_.*egress"):
        insights_runtime.kube.insights(**insights_runtime.args)


def test_insights_github_metadata_failure_blocks_health(insights_runtime):
    insights_runtime.policies[1]["spec"]["egress"][3]["to"].append({"ipBlock": {"cidr": "203.0.113.0/24"}})
    insights_runtime.github.status = 503
    # A failed read-back is a named hold the operator can see, not a raw transport error (#7870).
    with pytest.raises(CycleBlockedError, match="deployment_runtime_read_failed"):
        insights_runtime.kube.insights(**insights_runtime.args)


def test_chat_network_probe_executes_inside_running_digest_pinned_sandbox(monkeypatch):
    from unittest.mock import MagicMock
    from urllib.parse import parse_qs, urlsplit

    from src.orchestration import deployment_runtime_reader

    sandbox_image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-chat-agent@{DIGEST}"
    pod = {
        "metadata": {"name": "chat-turn-abcdef123456-running", "uid": "sandbox-uid", "labels": {"adp.io/chat-sandbox": "true"}},
        "spec": {
            "serviceAccountName": "adp-chat-sandbox",
            "containers": [{"image": sandbox_image}],
            "hostAliases": [{"ip": "10.2.3.4", "hostnames": ["chat-sandbox-gateway.adp-gateway.svc"]}],
        },
        "status": {"phase": "Running", "containerStatuses": [{"ready": True, "imageID": "docker-pullable://" + sandbox_image}]},
    }
    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "adp-gateway-agents"
    kube.endpoint = "https://cluster.test"
    kube.tls = object()
    kube.scoped = object()
    kube.description = {"name": "cluster"}
    kube.get = Mock(side_effect=[{"items": [pod]}, pod])
    channel = MagicMock()
    channel.subprotocol = "v4.channel.k8s.io"
    channel.__enter__.return_value = channel
    channel.recv.side_effect = [b"\x01" + b'{"internal_allowed":true,"external_denied":true}', b"\x03" + b'{"status":"Success"}']
    kube.websocket = Mock(return_value=channel)
    monkeypatch.setattr(deployment_runtime_reader, "eks_token", lambda scoped, name: "fixture-token")

    assert kube.probe_network(sandbox_image) == ("sandbox-uid", True, True)
    url = urlsplit(kube.websocket.call_args.args[0])
    assert url.path.endswith("/pods/chat-turn-abcdef123456-running/exec")
    assert parse_qs(url.query)["command"][-1] == "10.2.3.4"
    assert kube.websocket.call_args.kwargs["additional_headers"]["Authorization"] == "Bearer fixture-token"


def test_chat_network_probe_refuses_missing_running_sandbox():
    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "adp-gateway-agents"
    kube.get = Mock(return_value={"items": []})
    with pytest.raises(CycleBlockedError, match="deployment_runtime_mismatch"):
        kube.probe_network(f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-chat-agent@{DIGEST}")


@pytest.mark.parametrize(("status", "reason"), [(401, "read_forbidden"), (403, "read_forbidden"), (404, "read_failed"), (500, "read_failed")])
def test_runtime_http_reader_names_cluster_refusals(status, reason):
    """A refused or failed cluster read-back is a named hold, never a raw httpx error (#7870).

    TLS-X02 door5: the verifier's first read of the Door Deployment was refused (403, no
    Role in agent-context); the HTTPStatusError escaped, the Actions route answered a bare
    503 and the node recorded no hold reason.
    """
    from src.orchestration.deployment_runtime_reader import read_bytes

    body = {"kind": "Status", "status": "Failure", "reason": "Forbidden" if status == 403 else "Error"}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))) as client:
        with pytest.raises(CycleBlockedError, match=reason):
            read_bytes(client, "https://cluster.test/apis/apps/v1/namespaces/agent-context/deployments/context-mcp")

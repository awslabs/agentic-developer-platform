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
        resource_id="cluster/namespace",
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

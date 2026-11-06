"""Tool runtime checks must reject a healthy but different deployed release."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from src.orchestration.deployment_runtime_tools import verify_lambda_version
from src.orchestration.review_cycle import CycleBlockedError

ACCOUNT = "123456789012"
REGION = "us-east-1"
FUNCTION = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:example-interceptor"
HASH = "a" * 43 + "="


def interceptor_artifact(**changes):
    return ReleaseArtifact(
        schema_version=1,
        repository_id=1,
        run_id=42,
        run_attempt=1,
        source_revision="b" * 40,
        workflow_revision="c" * 40,
        workflow_path=".github/workflows/tools-interceptor-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id=FUNCTION,
        component="tools-interceptor",
        lambda_version="42",
        code_sha256=HASH,
        produced_at=datetime.now(UTC),
        **changes,
    )


@pytest.fixture
def lambda_runtime():
    alias = {"FunctionVersion": "42", "RoutingConfig": {}}
    configuration = {
        "FunctionArn": FUNCTION + ":42",
        "Version": "42",
        "CodeSha256": HASH,
        "State": "Active",
        "LastUpdateStatus": "Successful",
    }
    client = SimpleNamespace(get_alias=Mock(return_value=alias), get_function=Mock(return_value={"Configuration": configuration}))
    scoped = SimpleNamespace(region_name=REGION, client=Mock(return_value=client))
    return SimpleNamespace(scoped=scoped, client=client, alias=alias, configuration=configuration)


def test_lambda_alias_version_and_code_match_release(lambda_runtime):
    observed = verify_lambda_version(lambda_runtime.scoped, interceptor_artifact(), "d" * 64, "release/artifact")
    assert (observed.lambda_version, observed.code_sha256, observed.artifact_hash) == ("42", HASH, "d" * 64)
    lambda_runtime.client.get_alias.assert_called_once_with(FunctionName=FUNCTION, Name="live")
    lambda_runtime.client.get_function.assert_called_once_with(FunctionName=FUNCTION, Qualifier="42")


@pytest.mark.parametrize("difference", ["digest", "alias", "weighted", "wrong_function", "not_ready"])
def test_lambda_alias_mismatch_blocks_runtime_verification(lambda_runtime, difference):
    if difference == "digest":
        lambda_runtime.configuration["CodeSha256"] = "e" * 43 + "="
    elif difference == "alias":
        lambda_runtime.alias["FunctionVersion"] = "41"
    elif difference == "weighted":
        lambda_runtime.alias["RoutingConfig"] = {"AdditionalVersionWeights": {"41": 0.1}}
    elif difference == "wrong_function":
        lambda_runtime.configuration["FunctionArn"] = FUNCTION.replace(ACCOUNT, "999999999999") + ":42"
    else:
        lambda_runtime.configuration["LastUpdateStatus"] = "InProgress"
    with pytest.raises(CycleBlockedError, match="deployment_runtime_mismatch"):
        verify_lambda_version(lambda_runtime.scoped, interceptor_artifact(), "d" * 64, "release/artifact")


def gateway_fixture():
    import hashlib
    from datetime import timedelta

    import httpx
    from botocore.credentials import Credentials

    from src.orchestration.deployment_runtime_tools import _revision

    gateway_id = "example"
    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{gateway_id}"
    url = f"https://{gateway_id}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"
    target = {
        "gatewayArn": gateway_arn,
        "targetId": "target-1",
        "status": "READY",
        "lastSynchronizedAt": datetime.now(UTC) - timedelta(minutes=1),
    }
    listing = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "example___Search", "inputSchema": {"type": "object"}}]}}
    digest = hashlib.sha256(json.dumps(listing["result"]["tools"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    artifact = ReleaseArtifact(
        schema_version=1,
        repository_id=1,
        run_id=42,
        run_attempt=1,
        source_revision="b" * 40,
        workflow_revision="c" * 40,
        workflow_path=".github/workflows/tools-platform-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id=gateway_arn,
        component="tools-platform",
        gateway_id=gateway_id,
        gateway_url=url,
        target_ids=["target-1"],
        target_sync_revision=_revision([target]),
        tools_list_sha256=digest,
        produced_at=datetime.now(UTC),
    )
    gateway = {"gatewayId": gateway_id, "gatewayArn": gateway_arn, "gatewayUrl": url, "status": "READY"}
    control = SimpleNamespace(
        get_gateway=Mock(return_value=gateway),
        list_gateway_targets=Mock(return_value={"items": [{"targetId": "target-1"}]}),
        get_gateway_target=Mock(return_value=target),
    )
    scoped = SimpleNamespace(
        region_name=REGION,
        client=Mock(return_value=control),
        get_credentials=Mock(return_value=Credentials("test-access", "test-secret")),
    )
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json=listing)

    client = httpx.Client(transport=httpx.MockTransport(send))
    return SimpleNamespace(
        artifact=artifact, target=target, gateway=gateway, listing=listing, control=control, scoped=scoped, client=client, calls=calls
    )


def test_gateway_reads_live_target_revision_and_authenticated_discovery():
    from src.orchestration.deployment_runtime_tools import verify_agentcore_gateway

    runtime = gateway_fixture()
    with runtime.client:
        observed = verify_agentcore_gateway(runtime.scoped, runtime.artifact, "d" * 64, "release/artifact", client=runtime.client)
    assert observed.gateway_id == runtime.artifact.gateway_id
    assert observed.target_sync_revision == runtime.artifact.target_sync_revision
    assert observed.tools_list_sha256 == runtime.artifact.tools_list_sha256
    assert len(runtime.calls) == 1 and runtime.calls[0].url == runtime.artifact.gateway_url
    assert "AWS4-HMAC-SHA256" in runtime.calls[0].headers["authorization"]
    assert json.loads(runtime.calls[0].content)["method"] == "tools/list"
    runtime.control.get_gateway.assert_called_once_with(gatewayIdentifier="example")
    runtime.control.get_gateway_target.assert_called_once_with(gatewayIdentifier="example", targetId="target-1")


@pytest.mark.parametrize("difference", ["wrong_gateway", "wrong_target", "old_sync", "wrong_list", "wrong_endpoint"])
def test_gateway_wrong_live_identity_sync_or_list_never_verifies(difference):
    from src.orchestration.deployment_runtime_tools import verify_agentcore_gateway

    runtime = gateway_fixture()
    if difference == "wrong_gateway":
        runtime.gateway["gatewayId"] = "another-gateway"
    elif difference == "wrong_target":
        runtime.control.list_gateway_targets.return_value = {"items": [{"targetId": "other"}]}
    elif difference == "old_sync":
        runtime.target["lastSynchronizedAt"] = datetime.now(UTC)
    elif difference == "wrong_list":
        runtime.listing["result"]["tools"][0]["name"] = "other___Search"
    else:
        runtime.gateway["gatewayUrl"] = "https://different.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    with runtime.client, pytest.raises(CycleBlockedError, match="deployment_runtime_mismatch"):
        verify_agentcore_gateway(runtime.scoped, runtime.artifact, "d" * 64, "release/artifact", client=runtime.client)


@pytest.mark.parametrize("component", ["tools-platform", "tools-interceptor"])
def test_registered_tool_adapter_dispatches_to_scoped_runtime(monkeypatch, component):
    from src.orchestration.deployment_controller import ADAPTERS
    from src.orchestration.deployment_runtime_reader import PilotRuntimeReader

    if component == "tools-platform":
        fixture = gateway_fixture()
        artifact = fixture.artifact
        description = fixture.gateway
        adapter = "agentcore-gateway-verification"
        method = "verify_agentcore_gateway"
    else:
        fixture = lambda_runtime.__wrapped__()
        artifact = interceptor_artifact()
        description = {"Configuration": {"FunctionArn": FUNCTION}}
        adapter = "lambda-version-verification"
        method = "verify_lambda_version"
    assert ADAPTERS[adapter] == frozenset({component})
    observed = Mock(return_value=object())
    monkeypatch.setattr("src.orchestration.deployment_runtime_tools." + method, observed)
    result = PilotRuntimeReader().inspect(
        fixture.scoped,
        description,
        None,
        artifact=artifact,
        artifact_hash="d" * 64,
        evidence_ref="release/artifact",
        environment="dev",
    )
    assert result is observed.return_value
    observed.assert_called_once_with(fixture.scoped, artifact, "d" * 64, "release/artifact")


def test_gateway_never_verifies_different_resolved_resource(monkeypatch):
    from src.orchestration.deployment_runtime_reader import PilotRuntimeReader

    fixture = gateway_fixture()
    fixture.gateway["gatewayArn"] = fixture.gateway["gatewayArn"].replace(ACCOUNT, "999999999999")
    observed = Mock()
    monkeypatch.setattr("src.orchestration.deployment_runtime_tools.verify_agentcore_gateway", observed)
    with pytest.raises(CycleBlockedError, match="deployment_runtime_target_changed"):
        PilotRuntimeReader().inspect(
            fixture.scoped,
            fixture.gateway,
            None,
            artifact=fixture.artifact,
            artifact_hash="d" * 64,
            evidence_ref="release/artifact",
            environment="dev",
        )
    observed.assert_not_called()


def test_gateway_observation_survives_deployment_receipt_validation():
    from datetime import timedelta

    from src.orchestration.deployment_controller import DeploymentServices
    from src.orchestration.deployment_runtime_tools import verify_agentcore_gateway
    from src.orchestration.execution_state import ExecutionIdentity

    runtime = gateway_fixture()
    with runtime.client:
        observed = verify_agentcore_gateway(runtime.scoped, runtime.artifact, "d" * 64, "release/artifact", client=runtime.client)
    identity = ExecutionIdentity("example-org", "node", 1, 1, "claim", 1)
    execution = SimpleNamespace(id="execution", flow_id="flow", cycle=1, accepted_plan_version=1, claim_id="claim", claim_generation=1)
    context = SimpleNamespace(identity=identity, execution=execution)
    merge = SimpleNamespace(merge_sha=runtime.artifact.source_revision, operation_key="merge")
    receipt = DeploymentServices.receipt(
        object(),
        context,
        SimpleNamespace(repo="example/repo"),
        merge,
        actual=runtime.artifact.source_revision,
        components=[observed],
        targets=[{"resource_kind": "agentcore-gateway", "resource_id": runtime.artifact.resource_id}],
        workflows=["workflow"],
        entries=["platform"],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert receipt.components[0].tools_list_sha256 == runtime.artifact.tools_list_sha256
    assert receipt.components[0].target_sync_revision == runtime.artifact.target_sync_revision
    assert receipt.model_validate(receipt.model_dump(mode="json")) == receipt


def door_fixture():
    from src.orchestration.deployment_runtime_reader import KubernetesRuntime

    image_digest = "sha256:" + "b" * 64
    keys_hash = "a" * 64
    image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-dev-agent-context-context-mcp@{image_digest}"
    artifact = ReleaseArtifact(
        schema_version=1,
        repository_id=1,
        run_id=42,
        run_attempt=1,
        source_revision="b" * 40,
        workflow_revision="c" * 40,
        workflow_path=".github/workflows/agent-context-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id="adp-dev-eks-cluster/agent-context",
        component="knowledge-door",
        image_digest=image_digest,
        door_identity_sha256=keys_hash,
        produced_at=datetime.now(UTC),
    )
    deployment = {
        "metadata": {"uid": "deployment-uid", "generation": 3},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"annotations": {"adp.dev/door-verification-keys": keys_hash}},
                "spec": {"containers": [{"name": "context-mcp", "image": image}]},
            },
        },
        "status": {"observedGeneration": 3, "updatedReplicas": 1, "availableReplicas": 1},
    }
    replicas = {"items": [{"metadata": {"uid": "replica-uid", "ownerReferences": [{"uid": "deployment-uid", "controller": True}]}}]}
    pods = {
        "items": [
            {
                "metadata": {"uid": "pod-uid", "ownerReferences": [{"uid": "replica-uid", "controller": True}]},
                "status": {
                    "containerStatuses": [{"name": "context-mcp", "ready": True, "imageID": image, "state": {"running": {}}}],
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
        ]
    }
    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "agent-context"
    kube.get = Mock(
        side_effect=lambda path, **params: deployment if path.endswith("/context-mcp") else replicas if path.endswith("/replicasets") else pods
    )
    kube.door_identity_status = Mock(return_value=(401, "cluster_network"))
    kube.close = Mock()
    return SimpleNamespace(artifact=artifact, kube=kube, deployment=deployment, pods=pods)


def test_door_matches_release_image_and_keys_and_rejects_unsigned_request():
    from src.orchestration.deployment_runtime_tools import verify_door_identity

    fixture = door_fixture()
    observed = verify_door_identity(fixture.kube, fixture.artifact, "d" * 64, "release/artifact", "dev")
    assert (observed.image_digest, observed.door_identity_sha256, observed.pod_uids) == (
        fixture.artifact.image_digest,
        fixture.artifact.door_identity_sha256,
        ["pod-uid"],
    )
    fixture.kube.door_identity_status.assert_called_once_with()
    assert observed.door_probe_path == "cluster_network"


@pytest.mark.parametrize("difference", ["wrong_image", "wrong_keys", "wrong_pod", "no_401"])
def test_door_different_live_contract_cannot_verify(difference):
    from src.orchestration.deployment_runtime_tools import verify_door_identity

    fixture = door_fixture()
    if difference == "wrong_image":
        fixture.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "example:latest"
    elif difference == "wrong_keys":
        fixture.deployment["spec"]["template"]["metadata"]["annotations"]["adp.dev/door-verification-keys"] = "c" * 64
    elif difference == "wrong_pod":
        fixture.pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = "example:old"
    else:
        fixture.kube.door_identity_status.return_value = (200, "cluster_network")
    with pytest.raises(CycleBlockedError):
        verify_door_identity(fixture.kube, fixture.artifact, "d" * 64, "release/artifact", "dev")


def test_door_identity_probe_reaches_scoped_service_proxy(monkeypatch, tmp_path):
    import httpx

    from src.orchestration.deployment_runtime_reader import KubernetesRuntime

    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "agent-context"
    kube.endpoint = "https://example.invalid"
    kube.description = {"name": "example-cluster"}
    kube.scoped = object()
    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.eks_token", lambda scoped, cluster: "scoped-token")
    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.SERVICE_ACCOUNT_CA", tmp_path / "absent-ca.crt")
    with httpx.Client(transport=httpx.MockTransport(send)) as kube.client:
        assert kube.door_identity_status() == (401, "api_server_proxy")
    assert calls[0].url.path.endswith("/services/http:context-mcp:5100/proxy/tools")
    assert calls[0].headers["x-adp-door-identity"] == "invalid"
    assert calls[0].headers["authorization"] == "Bearer scoped-token"


def test_door_identity_probe_uses_the_cluster_network_when_the_verifier_is_an_admitted_peer(monkeypatch, tmp_path):
    """#7870: the Door's ingress NetworkPolicy admits only named pod peers, so a probe through
    the API server's service proxy (a control-plane ENI) is dropped and times out. Inside the
    target cluster — proven by the pod's own CA equalling the described cluster CA — the
    verifier probes the Service over the cluster network, as the real callers do."""
    import base64

    import httpx

    from src.orchestration.deployment_runtime_reader import KubernetesRuntime

    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\ncluster-ca\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.SERVICE_ACCOUNT_CA", ca)
    monkeypatch.setattr(
        "src.orchestration.deployment_runtime_reader.eks_token", lambda scoped, cluster: pytest.fail("no API token on the cluster path")
    )
    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "agent-context"
    kube.endpoint = "https://example.invalid"
    kube.description = {"name": "example-cluster", "certificateAuthority": {"data": base64.b64encode(ca.read_bytes()).decode()}}
    kube.scoped = object()
    with httpx.Client(transport=httpx.MockTransport(send)) as kube.client:
        assert kube.door_identity_status() == (401, "cluster_network")
    assert str(calls[0].url) == "http://context-mcp.agent-context.svc.cluster.local:5100/tools"
    assert calls[0].headers["x-adp-door-identity"] == "invalid"
    assert "authorization" not in calls[0].headers

    # A different cluster's CA is not co-location, even from inside some cluster.
    kube.description = {"name": "other", "certificateAuthority": {"data": base64.b64encode(b"other-ca").decode()}}
    assert kube.runs_inside_target_cluster() is False


def test_door_identity_probe_that_cannot_reach_the_door_is_a_named_hold(monkeypatch, tmp_path):
    import httpx

    from src.orchestration.deployment_runtime_reader import KubernetesRuntime
    from src.orchestration.review_cycle import CycleBlockedError

    def send(request):
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.SERVICE_ACCOUNT_CA", tmp_path / "absent-ca.crt")
    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.eks_token", lambda scoped, cluster: "scoped-token")
    kube = object.__new__(KubernetesRuntime)
    kube.namespace, kube.endpoint, kube.description, kube.scoped = "agent-context", "https://example.invalid", {"name": "c"}, object()
    with httpx.Client(transport=httpx.MockTransport(send)) as kube.client:
        with pytest.raises(CycleBlockedError, match="deployment_door_identity_probe_unreachable"):
            kube.door_identity_status()


def facade_fixture():
    from src.orchestration.deployment_runtime_reader import KubernetesRuntime

    digest = "sha256:" + "b" * 64
    artifact = ReleaseArtifact(
        schema_version=1,
        repository_id=1,
        run_id=42,
        run_attempt=1,
        source_revision="b" * 40,
        workflow_revision="c" * 40,
        workflow_path=".github/workflows/tools-facade-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id="example-cluster/example-namespace/tools-facade",
        component="tools-facade",
        image_digest=digest,
        produced_at=datetime.now(UTC),
    )
    image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-tools-facade@{digest}"
    deployment = {
        "metadata": {"uid": "deployment-uid", "generation": 3},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "tools-facade", "image": image}]}}},
        "status": {"observedGeneration": 3, "updatedReplicas": 1, "availableReplicas": 1},
    }
    replicas = {"items": [{"metadata": {"uid": "replica-uid", "ownerReferences": [{"uid": "deployment-uid", "controller": True}]}}]}
    pods = {
        "items": [
            {
                "metadata": {"uid": "pod-uid", "ownerReferences": [{"uid": "replica-uid", "controller": True}]},
                "status": {
                    "containerStatuses": [{"name": "tools-facade", "ready": True, "imageID": image, "state": {"running": {}}}],
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
        ]
    }
    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "example-namespace"
    kube.get = Mock(
        side_effect=lambda path, **params: deployment if path.endswith("/tools-facade") else replicas if path.endswith("/replicasets") else pods
    )
    kube.facade_probe = Mock()
    kube.close = Mock()
    return SimpleNamespace(artifact=artifact, kube=kube, deployment=deployment, pods=pods)


def test_facade_serves_matching_digest_and_gateway_discovery():
    from src.orchestration.deployment_runtime_tools import verify_tools_facade

    fixture = facade_fixture()
    observed = verify_tools_facade(fixture.kube, fixture.artifact, "d" * 64, "release/artifact")
    assert observed.image_digest == fixture.artifact.image_digest and observed.pod_uids == ["pod-uid"]
    fixture.kube.facade_probe.assert_called_once_with()


@pytest.mark.parametrize("difference", ["wrong_deployment", "wrong_pod", "unhealthy", "health_failed", "rollout_race"])
def test_facade_wrong_digest_health_or_gateway_discovery_blocks(difference):
    from src.orchestration.deployment_runtime_tools import verify_tools_facade

    fixture = facade_fixture()
    if difference == "wrong_deployment":
        fixture.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "example:latest"
    elif difference == "wrong_pod":
        fixture.pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = "example:old"
    elif difference == "unhealthy":
        fixture.deployment["status"]["availableReplicas"] = 0
    elif difference == "health_failed":
        fixture.kube.facade_probe.side_effect = CycleBlockedError("deployment_facade_health_failed")
    else:
        original = fixture.kube.get.side_effect
        calls = 0

        def changed(path, **params):
            nonlocal calls
            calls += 1
            if calls == 4:
                return {**fixture.deployment, "metadata": {"uid": "replacement", "generation": 4}}
            return original(path, **params)

        fixture.kube.get.side_effect = changed
    expected = "deployment_facade_health_failed" if difference == "health_failed" else "deployment_runtime_mismatch"
    with pytest.raises(CycleBlockedError, match=expected):
        verify_tools_facade(fixture.kube, fixture.artifact, "d" * 64, "release/artifact")


@pytest.mark.parametrize("ports", [[{"name": "http", "port": 8080}], [{"port": 80, "targetPort": 8080}]], ids=["named-http", "unnamed"])
def test_facade_probe_reads_health_through_the_scoped_service_proxy(monkeypatch, ports):
    """The façade's only unauthenticated route is GET /health; discovery needs a human bearer by design."""
    import httpx

    from src.orchestration.deployment_runtime_reader import KubernetesRuntime

    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "example-namespace"
    kube.endpoint = "https://example.invalid"
    kube.description = {"name": "example-cluster"}
    kube.scoped = object()
    kube.get = Mock(return_value={"spec": {"ports": ports}})
    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.eks_token", lambda scoped, cluster: "scoped-token")
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    with httpx.Client(transport=httpx.MockTransport(send)) as kube.client:
        kube.facade_probe()
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].url.path.endswith(f"/services/http:tools-facade:{ports[0]['port']}/proxy/health")
    assert calls[0].headers["authorization"] == "Bearer scoped-token"


@pytest.mark.parametrize(
    ("ports", "status", "body"),
    [
        ([{"name": "http", "port": 8080}], 500, {"status": "ok"}),  # unhealthy
        ([{"name": "http", "port": 8080}], 200, {"status": "degraded"}),  # answered but not ok
        ([{"name": "http", "port": 8080}], 200, None),  # not the façade's health document
        ([{"port": 80}, {"name": "http", "port": 8080}], 200, {"status": "ok"}),  # two http-eligible ports: ambiguous
        ([{"name": "grpc", "port": 8080}], 200, {"status": "ok"}),  # no http port
    ],
)
def test_facade_probe_rejects_unhealthy_or_ambiguous_service(monkeypatch, ports, status, body):
    import httpx

    from src.orchestration.deployment_runtime_reader import KubernetesRuntime

    kube = object.__new__(KubernetesRuntime)
    kube.namespace = "example-namespace"
    kube.endpoint = "https://example.invalid"
    kube.description = {"name": "example-cluster"}
    kube.scoped = object()
    kube.get = Mock(return_value={"spec": {"ports": ports}})
    monkeypatch.setattr("src.orchestration.deployment_runtime_reader.eks_token", lambda scoped, cluster: "scoped-token")

    def send(request):
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="not json")

    with httpx.Client(transport=httpx.MockTransport(send)) as kube.client, pytest.raises(CycleBlockedError, match="deployment_facade_"):
        kube.facade_probe()


def test_facade_reader_dispatches_to_reviewed_deployment(monkeypatch):
    from src.orchestration.deployment_runtime_reader import PilotRuntimeReader

    fixture = facade_fixture()
    description = {"name": "example-cluster", "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/example-cluster"}
    scoped = SimpleNamespace(region_name=REGION)
    observed = Mock(return_value=object())
    monkeypatch.setattr("src.orchestration.deployment_runtime_tools.verify_tools_facade", observed)
    reader = PilotRuntimeReader(kubernetes=Mock(return_value=fixture.kube))
    result = reader.inspect(
        scoped,
        description,
        "example-namespace",
        artifact=fixture.artifact,
        artifact_hash="d" * 64,
        evidence_ref="release/artifact",
        environment="dev",
    )
    assert result is observed.return_value
    observed.assert_called_once_with(fixture.kube, fixture.artifact, "d" * 64, "release/artifact")
    fixture.kube.close.assert_called_once_with()


REAL_TOOLS_PLATFORM_RELEASE = {
    "account_id": "123456789012",
    "adp_correlation": "orch:302e2a2b-8072-5b19-8c59-de7c3212165c",
    "assertion_signing_key_arn": "arn:aws:kms:us-east-1:123456789012:key/5dd1027a-5e9a-4093-a56f-14577e5cef4a",
    "caller_role_arn": "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade",
    "component": "tools-platform",
    "definition_revision": "37a41e5a37b977c664bf42960f7867ca9eb10d1d",
    "environment": "dev",
    "execution_table_name": "adp-tools-dev-executions",
    "gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-ftgob4rcuu",
    "gateway_id": "adp-tools-ftgob4rcuu",
    "gateway_url": "https://adp-tools-ftgob4rcuu.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    "interceptor_code_sha256": "cpfiZlG+zu3X6nsZzjOUwsjeMMBM0tywCLXTn5BZPQY=",
    "interceptor_lambda_arn": "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live",
    "interceptor_version": "9",
    "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/acb1bd7d-1766-4e36-9983-357c8e33262e",
    "produced_at": "2026-10-14T16:51:05.167900+00:00",
    "region": "us-east-1",
    "repository_id": 1,
    "resource_id": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-ftgob4rcuu",
    "run_attempt": 1,
    "run_id": 42,
    "schema_version": 1,
    "source_revision": "353710ef94e5e2f56472cf752b8e88bf3774031a",
    "target_ids": ["A3URHNPDYQ", "I0FDPPUI3K", "JOH1XLAW0K"],
    "target_role_arn": "arn:aws:iam::123456789012:role/adp-tools-dev-gateway",
    "target_sync_revision": "662dc7ffb5ed68c3e8c6a7d2c2c3d0b1a88b79e700a8bcb93978a50a51ab347b",
    "targets": {"browser": "I0FDPPUI3K", "code-interpreter": "JOH1XLAW0K", "websearch": "A3URHNPDYQ"},
    "tools_list_sha256": "8c66cc6d9bc140cd56fc8a7fa1abb15f84dd801a725fe0c5f8cd9b8ccc42e663",
    "workflow_path": ".github/workflows/tools-platform-deploy.yml",
    "workflow_revision": "37a41e5a37b977c664bf42960f7867ca9eb10d1d",
}


def test_release_artifact_accepts_the_tools_platform_receipt_shape():
    """The tools-platform publisher (deployment_evidence.bind_release) carries the gateway's
    reviewed configuration beside the identity fields; the strict contract rejected the
    whole receipt (13 extra fields) so runtime verification never ran (2026-10-14)."""
    release = ReleaseArtifact.model_validate(REAL_TOOLS_PLATFORM_RELEASE)
    assert release.gateway_arn == release.resource_id
    assert sorted(release.targets.values()) == release.target_ids
    assert release.interceptor_version == "9" and release.tools_list_sha256


@pytest.mark.parametrize("change", ["other_gateway", "target_map", "definition", "half_interceptor", "wrong_component"])
def test_release_artifact_cross_checks_the_gateway_configuration_fields(change):
    raw = dict(REAL_TOOLS_PLATFORM_RELEASE)
    if change == "other_gateway":
        raw["gateway_arn"] = raw["gateway_arn"][:-1] + "x"
    elif change == "target_map":
        raw["targets"] = {**raw["targets"], "extra": "ZZZZZZZZZZ"}
    elif change == "definition":
        raw["definition_revision"] = "d" * 40
    elif change == "half_interceptor":
        raw.pop("interceptor_code_sha256")
    else:
        raw = {**raw, "component": "tools-interceptor", "lambda_version": "9", "code_sha256": raw["interceptor_code_sha256"]}
    with pytest.raises(ValueError):
        ReleaseArtifact.model_validate(raw)


def test_gateway_revision_uses_updated_at_for_unsynchronized_lambda_targets():
    """AgentCore never synchronizes Lambda targets (#7642); the live dev gateway's three
    targets carry no lastSynchronizedAt, so the old digest raised KeyError and runtime
    verification held every tools-platform deployment with deployment_runtime_mismatch."""
    import hashlib
    from datetime import timedelta

    from src.orchestration.deployment_runtime_tools import _revision, _target_timestamp

    when = datetime.now(UTC) - timedelta(minutes=2)
    lambda_target = {"targetId": "L1", "status": "READY", "updatedAt": when, "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn"}}}}
    synced_target = {"targetId": "S1", "status": "READY", "lastSynchronizedAt": when, "updatedAt": when - timedelta(hours=1)}
    assert _target_timestamp(lambda_target) == when and _target_timestamp(synced_target) == when
    # Same digest the publisher computes: sorted (id, isoformat) pairs.
    expected = hashlib.sha256(json.dumps(sorted([("L1", when.isoformat()), ("S1", when.isoformat())]), separators=(",", ":")).encode()).hexdigest()
    assert _revision([synced_target, lambda_target]) == expected
    # A non-Lambda target without a synchronization timestamp has no revision.
    with pytest.raises(CycleBlockedError, match="deployment_runtime_mismatch"):
        _revision([{"targetId": "O1", "status": "READY", "updatedAt": when, "targetConfiguration": {"mcp": {"openApiSchema": {}}}}])

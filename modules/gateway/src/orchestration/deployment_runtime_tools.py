"""Read tool deployment evidence through the registered runtime role."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .deployment_runtime_contract import RuntimeComponent
from .deployment_runtime_reader import AWS_BOUNDS, MAX_RESPONSE, require


def verify_lambda_version(scoped, artifact, artifact_hash, evidence_ref):
    function_arn = artifact.resource_id
    require(
        bool(re.fullmatch(rf"arn:aws:lambda:{re.escape(artifact.region)}:{artifact.account_id}:function:[A-Za-z0-9_-]+", function_arn)),
        "deployment_runtime_target_mismatch",
    )
    client = scoped.client("lambda", config=AWS_BOUNDS)
    alias = client.get_alias(FunctionName=function_arn, Name="live")
    require(
        alias.get("FunctionVersion") == artifact.lambda_version and not (alias.get("RoutingConfig") or {}).get("AdditionalVersionWeights"),
        "deployment_runtime_mismatch",
    )
    function = client.get_function(FunctionName=function_arn, Qualifier=artifact.lambda_version)
    configuration = function.get("Configuration", {})
    require(
        configuration.get("FunctionArn") == f"{function_arn}:{artifact.lambda_version}"
        and configuration.get("Version") == artifact.lambda_version
        and configuration.get("CodeSha256") == artifact.code_sha256
        and configuration.get("State") == "Active"
        and configuration.get("LastUpdateStatus") == "Successful",
        "deployment_runtime_mismatch",
    )
    return RuntimeComponent(
        component=artifact.component,
        actual_revision=artifact.source_revision,
        artifact_hash=artifact_hash,
        healthy=True,
        evidence_ref=evidence_ref,
        observed_at=datetime.now(UTC),
        lambda_version=artifact.lambda_version,
        code_sha256=configuration["CodeSha256"],
    )


def _target_timestamp(target):
    """The timestamp that stands for a gateway target's served tool schema.

    MCP-server and OpenAPI targets carry a synchronization timestamp. Lambda
    targets are never synchronized by AgentCore, so their last update is the
    revision of the schema the gateway serves — the same rule the tools release
    publisher applies (modules/tools/platform/scripts/smoke.py, #7642). Anything
    else has no revision and cannot verify.
    """
    synchronized = target.get("lastSynchronizedAt")
    if synchronized is not None:
        return synchronized
    if (target.get("targetConfiguration") or {}).get("mcp", {}).get("lambda") and target.get("updatedAt") is not None:
        return target["updatedAt"]
    return None


def _revision(targets):
    stamps = [(target.get("targetId"), _target_timestamp(target)) for target in targets]
    require(all(isinstance(target_id, str) and stamp is not None for target_id, stamp in stamps), "deployment_runtime_mismatch")
    values = sorted((target_id, stamp.isoformat()) for target_id, stamp in stamps)
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


class InterceptorDeniedError(Exception):
    """The gateway's fail-closed interceptor refused the verifier's unasserted request."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _interceptor_denial(status_code, payload):
    """The interceptor's refusal shape, or ``None`` for any other response.

    The tools interceptor answers a request without the façade's signed assertion
    with HTTP 403 and a JSON-RPC error (code -32000, a bare reason token). An IAM
    refusal from AgentCore itself is also 403 but carries the service's error
    document, never a JSON-RPC envelope, so the two remain distinguishable.
    """
    if status_code != 403:
        return None
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if (
        isinstance(body, dict)
        and body.get("jsonrpc") == "2.0"
        and isinstance(error, dict)
        and error.get("code") == -32000
        and isinstance(error.get("message"), str)
        and re.fullmatch(r"[a-z_]{1,64}", error["message"])
    ):
        return error["message"]
    return None


def _interceptor_lambda_arns(gateway):
    """Lambda interceptors bound at the REQUEST interception point, in gateway order."""
    arns = []
    for item in gateway.get("interceptorConfigurations") or []:
        if not isinstance(item, dict):
            continue
        arn = ((item.get("interceptor") or {}).get("lambda") or {}).get("arn")
        if isinstance(arn, str) and "REQUEST" in (item.get("interceptionPoints") or []):
            arns.append(arn)
    return arns


def _verify_interceptor_release(scoped, artifact):
    """The live interceptor alias serves exactly the version and code the release reviewed."""
    if artifact.interceptor_version is None:
        return
    function_arn, _, alias = artifact.interceptor_lambda_arn.rpartition(":")
    require(bool(alias) and ":function:" in function_arn, "deployment_runtime_mismatch")
    client = scoped.client("lambda", config=AWS_BOUNDS)
    current = client.get_alias(FunctionName=function_arn, Name=alias)
    require(
        current.get("FunctionVersion") == artifact.interceptor_version and not (current.get("RoutingConfig") or {}).get("AdditionalVersionWeights"),
        "deployment_runtime_mismatch",
    )
    configuration = client.get_function(FunctionName=function_arn, Qualifier=artifact.interceptor_version).get("Configuration", {})
    require(
        configuration.get("FunctionArn") == f"{function_arn}:{artifact.interceptor_version}"
        and configuration.get("CodeSha256") == artifact.interceptor_code_sha256
        and configuration.get("State") == "Active"
        and configuration.get("LastUpdateStatus") == "Successful",
        "deployment_runtime_mismatch",
    )


def _listing(scoped, endpoint, region, *, client=None):
    parsed = urlsplit(endpoint)
    require(
        parsed.scheme == "https"
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path == "/mcp"
        and bool(re.fullmatch(r"[a-z0-9-]+\.gateway\.bedrock-agentcore\." + re.escape(region) + r"\.amazonaws\.com", parsed.hostname or "")),
        "deployment_runtime_gateway_url_invalid",
    )
    owned = client is None
    transport = client or httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
    tools, seen, cursor = [], set(), None
    try:
        for _ in range(5):
            params = {"cursor": cursor} if cursor else {}
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params}, sort_keys=True, separators=(",", ":")).encode()
            request = AWSRequest(method="POST", url=endpoint, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
            SigV4Auth(scoped.get_credentials().get_frozen_credentials(), "bedrock-agentcore", region).add_auth(request)
            with transport.stream("POST", endpoint, content=body, headers=dict(request.headers)) as response:
                payload = bytearray()
                for chunk in response.iter_bytes():
                    require(len(payload) + len(chunk) <= MAX_RESPONSE, "deployment_runtime_response_limit")
                    payload.extend(chunk)
                denial = _interceptor_denial(response.status_code, payload)
                if denial is not None:
                    raise InterceptorDeniedError(denial)
                response.raise_for_status()
            listing = json.loads(payload)
            result = listing.get("result", {})
            page = result.get("tools")
            require(
                isinstance(page, list) and not listing.get("error") and all(isinstance(tool, dict) for tool in page),
                "deployment_runtime_tools_list_invalid",
            )
            tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                return hashlib.sha256(json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            require(isinstance(cursor, str) and len(cursor) <= 4096 and cursor not in seen, "deployment_runtime_tools_list_invalid")
            seen.add(cursor)
        require(False, "deployment_runtime_tools_list_limit")
    finally:
        if owned:
            transport.close()


def verify_agentcore_gateway(scoped, artifact, artifact_hash, evidence_ref, *, client=None):
    require(bool(re.fullmatch(r"[A-Za-z0-9-]{1,128}", artifact.gateway_id)), "deployment_runtime_target_mismatch")
    require(artifact.region == scoped.region_name, "deployment_runtime_target_mismatch")
    control = scoped.client("bedrock-agentcore-control", config=AWS_BOUNDS)
    gateway = control.get_gateway(gatewayIdentifier=artifact.gateway_id)
    require(
        gateway.get("gatewayId") == artifact.gateway_id
        and gateway.get("gatewayArn") == artifact.resource_id
        and gateway.get("status") == "READY"
        and gateway.get("gatewayUrl") == artifact.gateway_url
        and gateway.get("gatewayArn") == f"arn:aws:bedrock-agentcore:{artifact.region}:{artifact.account_id}:gateway/{artifact.gateway_id}",
        "deployment_runtime_mismatch",
    )
    targets, cursor = [], None
    for _ in range(5):
        page = control.list_gateway_targets(gatewayIdentifier=artifact.gateway_id, maxResults=100, **({"nextToken": cursor} if cursor else {}))
        require(isinstance(page.get("items"), list) and len(page["items"]) <= 100, "deployment_runtime_targets_invalid")
        targets.extend(page["items"])
        cursor = page.get("nextToken")
        if not cursor:
            break
    require(not cursor and sorted(item.get("targetId") for item in targets) == sorted(artifact.target_ids), "deployment_runtime_mismatch")
    details = [control.get_gateway_target(gatewayIdentifier=artifact.gateway_id, targetId=target_id) for target_id in artifact.target_ids]
    require(
        all(
            item.get("gatewayArn") == artifact.resource_id
            and item.get("status") == "READY"
            and item.get("targetId") in artifact.target_ids
            and _target_timestamp(item) is not None
            for item in details
        ),
        "deployment_runtime_mismatch",
    )
    revision = _revision(details)
    require(revision == artifact.target_sync_revision, "deployment_runtime_mismatch")
    interceptors = _interceptor_lambda_arns(gateway)
    if artifact.interceptor_lambda_arn:
        # A fail-closed interceptor (TLS-S15) refuses every caller that lacks the
        # façade's signed assertion — this verifier included, by design. The live
        # proof is therefore: the gateway binds exactly the reviewed interceptor at
        # REQUEST, that alias serves the reviewed version and code, and an unasserted
        # tools/list is refused by the interceptor itself (a JSON-RPC refusal), not by
        # IAM and not by an absent gateway. The served listing is then the façade's
        # release-smoke digest, which the release gate already bound to this run.
        require(interceptors == [artifact.interceptor_lambda_arn], "deployment_runtime_mismatch")
        _verify_interceptor_release(scoped, artifact)
        try:
            _listing(scoped, artifact.gateway_url, artifact.region, client=client)
        except InterceptorDeniedError as denial:
            require(denial.reason == "untrusted_assertion", "deployment_runtime_mismatch")
        except httpx.HTTPStatusError as error:
            # AgentCore refused before the interceptor ran (an IAM resource-policy or
            # identity denial): the gateway is not serving the reviewed authority shape.
            require(error.response.status_code != 403, "deployment_runtime_mismatch")
            raise
        else:
            # The interceptor answered an unasserted caller: it is not enforcing.
            require(False, "deployment_runtime_interceptor_open")
        listing_hash, source, fail_closed = artifact.tools_list_sha256, "release_smoke", True
    else:
        require(not interceptors, "deployment_runtime_mismatch")
        listing_hash = _listing(scoped, artifact.gateway_url, artifact.region, client=client)
        require(listing_hash == artifact.tools_list_sha256, "deployment_runtime_mismatch")
        source, fail_closed = "live_listing", None
    return RuntimeComponent(
        component=artifact.component,
        actual_revision=artifact.source_revision,
        artifact_hash=artifact_hash,
        healthy=True,
        evidence_ref=evidence_ref,
        observed_at=datetime.now(UTC),
        gateway_id=artifact.gateway_id,
        gateway_url=artifact.gateway_url,
        target_ids=artifact.target_ids,
        target_sync_revision=revision,
        tools_list_sha256=listing_hash,
        tools_list_source=source,
        interceptor_lambda_arn=artifact.interceptor_lambda_arn,
        interceptor_fail_closed=fail_closed,
    )


def verify_door_identity(kube, artifact, artifact_hash, evidence_ref, environment):
    from urllib.parse import quote

    namespace = quote(kube.namespace, safe="")
    deployment = kube.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/context-mcp")
    metadata, spec, status = deployment["metadata"], deployment["spec"], deployment.get("status", {})
    desired = spec.get("replicas", 0)
    images = [item.get("image") for item in spec["template"]["spec"]["containers"] if item.get("name") == "context-mcp"]
    expected = f"{artifact.account_id}.dkr.ecr.{artifact.region}.amazonaws.com/adp-{environment}-agent-context-context-mcp@{artifact.image_digest}"
    require(
        len(images) == 1
        and images[0] == expected
        and spec["template"]["metadata"].get("annotations", {}).get("adp.dev/door-verification-keys") == artifact.door_identity_sha256
        and isinstance(desired, int)
        and desired > 0
        and status.get("observedGeneration", 0) >= metadata["generation"]
        and status.get("updatedReplicas") == status.get("availableReplicas") == desired,
        "deployment_runtime_mismatch",
    )
    selector = "app.kubernetes.io/name=context-mcp"
    replicas = kube.get(f"/apis/apps/v1/namespaces/{namespace}/replicasets", labelSelector=selector, limit=100)
    pods = kube.get(f"/api/v1/namespaces/{namespace}/pods", labelSelector=selector, limit=100)
    require(not replicas.get("metadata", {}).get("continue") and not pods.get("metadata", {}).get("continue"), "deployment_runtime_list_limit")
    owners = {
        item["metadata"]["uid"]
        for item in replicas.get("items", [])
        if any(ref.get("uid") == metadata["uid"] and ref.get("controller") is True for ref in item["metadata"].get("ownerReferences", []))
    }
    require(len(pods.get("items", [])) == desired, "deployment_runtime_mismatch")
    pod_uids = []
    for pod in pods["items"]:
        current = [item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == "context-mcp"]
        require(
            not pod["metadata"].get("deletionTimestamp")
            and any(ref.get("uid") in owners and ref.get("controller") is True for ref in pod["metadata"].get("ownerReferences", []))
            and len(current) == 1
            and current[0].get("ready") is True
            and current[0].get("state", {}).get("running") is not None
            and current[0].get("imageID", "").rsplit("@", 1)[-1] == artifact.image_digest
            and any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", [])),
            "deployment_runtime_mismatch",
        )
        pod_uids.append(pod["metadata"]["uid"])
    status, probe_path = kube.door_identity_status()
    require(status == 401, "deployment_door_identity_probe_failed")
    return RuntimeComponent(
        component="knowledge-door",
        actual_revision=artifact.source_revision,
        artifact_hash=artifact_hash,
        image_digest=artifact.image_digest,
        healthy=True,
        evidence_ref=evidence_ref,
        observed_at=datetime.now(UTC),
        pod_uids=pod_uids,
        door_identity_sha256=artifact.door_identity_sha256,
        door_probe_path=probe_path,
    )


def verify_tools_facade(kube, artifact, artifact_hash, evidence_ref):
    from urllib.parse import quote

    namespace = quote(kube.namespace, safe="")
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/tools-facade"
    deployment = kube.get(path)
    metadata, spec, status = deployment["metadata"], deployment["spec"], deployment.get("status", {})
    desired = spec.get("replicas", 0)
    image = f"{artifact.account_id}.dkr.ecr.{artifact.region}.amazonaws.com/adp-tools-facade@{artifact.image_digest}"
    containers = [item for item in spec["template"]["spec"]["containers"] if item.get("name") == "tools-facade"]
    require(
        len(containers) == 1
        and containers[0].get("image") == image
        and isinstance(desired, int)
        and desired > 0
        and status.get("observedGeneration", 0) >= metadata["generation"]
        and status.get("updatedReplicas") == status.get("availableReplicas") == desired,
        "deployment_runtime_mismatch",
    )
    selector = "app=tools-facade"
    replicas = kube.get(f"/apis/apps/v1/namespaces/{namespace}/replicasets", labelSelector=selector, limit=100)
    pods = kube.get(f"/api/v1/namespaces/{namespace}/pods", labelSelector=selector, limit=100)
    require(not replicas.get("metadata", {}).get("continue") and not pods.get("metadata", {}).get("continue"), "deployment_runtime_list_limit")
    owners = {
        item["metadata"]["uid"]
        for item in replicas.get("items", [])
        if any(ref.get("uid") == metadata["uid"] and ref.get("controller") is True for ref in item["metadata"].get("ownerReferences", []))
    }
    require(len(pods.get("items", [])) == desired, "deployment_runtime_mismatch")
    pod_uids = []
    for pod in pods["items"]:
        current = [item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == "tools-facade"]
        require(
            not pod["metadata"].get("deletionTimestamp")
            and any(ref.get("uid") in owners and ref.get("controller") is True for ref in pod["metadata"].get("ownerReferences", []))
            and len(current) == 1
            and current[0].get("ready") is True
            and current[0].get("state", {}).get("running") is not None
            and current[0].get("imageID", "").rsplit("@", 1)[-1] == artifact.image_digest
            and any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", [])),
            "deployment_runtime_mismatch",
        )
        pod_uids.append(pod["metadata"]["uid"])
    kube.facade_probe()
    current = kube.get(path)
    require(
        current["metadata"]["uid"] == metadata["uid"] and current["metadata"]["generation"] == metadata["generation"],
        "deployment_runtime_mismatch",
    )
    return RuntimeComponent(
        component="tools-facade",
        actual_revision=artifact.source_revision,
        artifact_hash=artifact_hash,
        image_digest=artifact.image_digest,
        healthy=True,
        evidence_ref=evidence_ref,
        observed_at=datetime.now(UTC),
        pod_uids=pod_uids,
    )

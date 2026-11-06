"""Bounded adapter foundation observation, independent of apply command results.

The caller supplies an authenticated, target-bound Kubernetes JSON reader and
the expected release rendered from deployment-owned inputs. This observation
does not replace the native release receipt, independent evaluation, effective
IAM/network checks or connector acceptance. No secret objects are read.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

LABELS = {"app.kubernetes.io/name": "tool-adapters", "app.kubernetes.io/part-of": "adp"}
RESOURCES = {
    "ServiceAccount": ("/api/v1", "serviceaccounts", "tool-adapters"),
    "ConfigMap": ("/api/v1", "configmaps", "tool-adapters-contract"),
    "Deployment": ("/apis/apps/v1", "deployments", "tool-adapters"),
    "Service": ("/api/v1", "services", "tool-adapters"),
    "PodDisruptionBudget": ("/apis/policy/v1", "poddisruptionbudgets", "tool-adapters"),
}
MAX_RESPONSE = 2 * 1024 * 1024


class ObservationError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise ObservationError("adapter_" + reason)


def validate_configuration(configuration):
    """Keep adapter mode semantics in the component-owned verifier."""
    require(
        isinstance(configuration, dict)
        and {"schema_version", "mode", "release"}
        <= set(configuration)
        <= {"schema_version", "mode", "release", "observation"}
        and configuration["schema_version"] == "1.0",
        "configuration_invalid",
    )
    mode = configuration["mode"]
    require(isinstance(mode, str) and mode in {"foundation", "observation"}, "configuration_mode_invalid")
    require(
        configuration.get("observation") is None
        if mode == "foundation"
        else isinstance(configuration.get("observation"), dict),
        "configuration_invalid",
    )


def subset(expected, actual):
    """Allow Kubernetes defaults, while comparing every release-owned field."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and subset(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(subset(left, right) for left, right in zip(expected, actual, strict=True))
        )
    return type(expected) is type(actual) and expected == actual


def identity(value):
    metadata = value.get("metadata", {})
    uid, version = metadata.get("uid"), metadata.get("resourceVersion")
    require(isinstance(uid, str) and 0 < len(uid) <= 128, "resource_uid_missing")
    require(isinstance(version, str) and 0 < len(version) <= 128, "resource_version_missing")
    require(not metadata.get("deletionTimestamp"), "resource_terminating")
    return {"uid": uid, "resource_version": version}


def owner(value, kind, uid):
    owners = [item for item in value.get("metadata", {}).get("ownerReferences", []) if item.get("controller") is True]
    return len(owners) == 1 and owners[0].get("kind") == kind and owners[0].get("uid") == uid


def pod_spec(expected, actual, *, role, region, admission=False):
    require(isinstance(actual, dict), "pod_spec_invalid")
    require(
        not any(
            actual.get(key) for key in ("hostNetwork", "hostPID", "hostIPC", "initContainers", "ephemeralContainers")
        ),
        "pod_extra_execution",
    )
    require(len(actual.get("containers", [])) == 1, "pod_extra_container")
    wanted, container = expected["containers"][0], actual["containers"][0]
    require(
        not any(container.get(key) for key in ("command", "args", "envFrom", "lifecycle", "workingDir")),
        "container_execution_override",
    )
    security = container.get("securityContext", {})
    require(not security.get("privileged") and not security.get("capabilities", {}).get("add"), "container_privileged")
    require(security.get("runAsUser", 65532) == 65532, "container_user_override")
    require(security.get("runAsGroup", 65532) == 65532, "container_group_override")
    require(security.get("runAsNonRoot", True) is True, "container_root_override")
    require(
        security.get("seccompProfile", {"type": "RuntimeDefault"}) == {"type": "RuntimeDefault"},
        "container_seccomp_override",
    )
    require(
        not any(port.get("hostPort") or port.get("hostIP") for port in container.get("ports", [])),
        "container_host_port",
    )
    for name in ("startupProbe", "livenessProbe", "readinessProbe"):
        probe = container.get(name, {})
        request = probe.get("httpGet", {})
        require(
            not any(probe.get(key) for key in ("exec", "tcpSocket", "grpc"))
            and not request.get("host")
            and not request.get("httpHeaders")
            and request.get("scheme", "HTTP") == "HTTP",
            "probe_override",
        )
    # The EKS IRSA webhook can inject this one projected token and AWS variables.
    # A general extra-env/volume allowance would hide source or secret overrides.
    permitted = (
        {
            "AWS_ROLE_ARN": role,
            "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/eks.amazonaws.com/serviceaccount/token",
            "AWS_STS_REGIONAL_ENDPOINTS": "regional",
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
        }
        if admission
        else {}
    )
    env = container.get("env", [])
    require(len({item.get("name") for item in env}) == len(env), "duplicate_environment")
    extra_env = [item for item in env if item.get("name") not in {item["name"] for item in wanted["env"]}]
    require(
        all(
            set(item) == {"name", "value"}
            and item.get("value") == permitted.get(item.get("name"))
            and item.get("name") in permitted
            for item in extra_env
        ),
        "unexpected_environment",
    )
    extra_mounts = [
        item for item in container.get("volumeMounts", []) if item.get("name") not in {"configuration", "scratch"}
    ]
    extra_volumes = [item for item in actual.get("volumes", []) if item.get("name") not in {"configuration", "scratch"}]
    if extra_mounts or extra_volumes or extra_env:
        require(admission and len(extra_mounts) == 1 and len(extra_volumes) == 1, "unexpected_volume")
        require(
            extra_mounts[0]
            == {
                "name": "aws-iam-token",
                "mountPath": "/var/run/secrets/eks.amazonaws.com/serviceaccount",
                "readOnly": True,
            },
            "unexpected_identity_mount",
        )
        volume = extra_volumes[0]
        require(
            set(volume) == {"name", "projected"} and volume["name"] == "aws-iam-token", "unexpected_identity_volume"
        )
        projection = volume["projected"]
        sources = projection.get("sources", [])
        require(
            set(projection) <= {"sources", "defaultMode"}
            and projection.get("defaultMode", 420) == 420
            and len(sources) == 1
            and set(sources[0]) == {"serviceAccountToken"},
            "unexpected_identity_projection",
        )
        token = sources[0]["serviceAccountToken"]
        require(
            set(token) == {"audience", "expirationSeconds", "path"}
            and token["audience"] == "sts.amazonaws.com"
            and token["path"] == "token"
            and type(token["expirationSeconds"]) is int
            and 600 <= token["expirationSeconds"] <= 86400,
            "unexpected_identity_token",
        )
        values = {item["name"]: item["value"] for item in extra_env}
        require(
            values.get("AWS_ROLE_ARN") == role
            and values.get("AWS_WEB_IDENTITY_TOKEN_FILE") == permitted["AWS_WEB_IDENTITY_TOKEN_FILE"],
            "identity_environment_missing",
        )
    sanitized = {
        **actual,
        "containers": [
            {
                **container,
                "env": [item for item in env if item not in extra_env],
                "volumeMounts": [item for item in container.get("volumeMounts", []) if item not in extra_mounts],
            }
        ],
        "volumes": [item for item in actual.get("volumes", []) if item not in extra_volumes],
    }
    require(subset(expected, sanitized), "pod_configuration_mismatch")
    # Explicitly reject extra volume sources, subPath/expression overrides and
    # alternate configuration keys that dictionary subset comparison permits.
    for wanted_volume, volume in zip(expected["volumes"], sanitized["volumes"], strict=True):
        require(set(volume) == set(wanted_volume), "volume_source_override")
        if "configMap" in volume:
            require(set(volume["configMap"]) <= {"name", "defaultMode"}, "configuration_volume_override")
    require(sanitized["containers"][0]["volumeMounts"] == wanted["volumeMounts"], "mount_override")


def observe(get, expected, metadata):
    """Read, validate, probe, then reread all identities to reject moving targets."""
    items = expected.get("items", [])
    require(len(items) == 5 and {item.get("kind") for item in items} == set(RESOURCES), "expected_resources_invalid")
    wanted = {item["kind"]: item for item in items}
    namespace = wanted["Deployment"]["metadata"]["namespace"]
    require(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace) is not None, "namespace_invalid")
    watched = {}

    def read(path):
        value = get(path)
        require(isinstance(value, dict) and len(json.dumps(value).encode()) <= MAX_RESPONSE, "response_invalid")
        return value

    def resource(kind):
        prefix, plural, name = RESOURCES[kind]
        path = f"{prefix}/namespaces/{namespace}/{plural}/{name}"
        value = read(path)
        require(
            subset(wanted[kind]["metadata"], value.get("metadata", {})) and value.get("kind") == kind,
            "resource_identity_mismatch",
        )
        watched[path] = identity(value)
        return value

    def collection(prefix, plural, selector):
        path = f"{prefix}/namespaces/{namespace}/{plural}"
        page = read(path + "?" + urlencode({"labelSelector": selector, "limit": 100}))
        values = page.get("items")
        require(
            isinstance(values, list) and len(values) <= 100 and not page.get("metadata", {}).get("continue"),
            "list_limit",
        )
        require(len({value.get("metadata", {}).get("uid") for value in values}) == len(values), "duplicate_resource")
        for value in values:
            name = value.get("metadata", {}).get("name", "")
            require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name) is not None, "resource_name_invalid")
            require(value["metadata"].get("namespace") == namespace, "resource_namespace_mismatch")
            watched[path + "/" + name] = identity(value)
        return values

    account, config, deployment, service, disruption = (resource(kind) for kind in RESOURCES)
    require(
        account.get("automountServiceAccountToken") is False and not account.get("secrets"), "service_account_mismatch"
    )
    require(
        config.get("data") == wanted["ConfigMap"]["data"] and not config.get("binaryData"), "configuration_mismatch"
    )
    raw = config["data"]["config.json"]
    configuration = json.loads(raw)
    validate_configuration(configuration)
    release = configuration["release"]
    expected_spec = wanted["Deployment"]["spec"]
    spec, status = deployment.get("spec", {}), deployment.get("status", {})
    require(subset(expected_spec, spec), "deployment_mismatch")
    generation = deployment["metadata"].get("generation")
    require(
        type(generation) is int and generation > 0 and status.get("observedGeneration") == generation,
        "deployment_unobserved",
    )
    require(
        all(
            type(status.get(key)) is int and status[key] == 2
            for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
        )
        and status.get("unavailableReplicas", 0) == 0,
        "deployment_not_ready",
    )
    image = expected_spec["template"]["spec"]["containers"][0]["image"]
    match = re.fullmatch(
        r"[0-9]{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/[a-z0-9._/-]+@(sha256:[0-9a-f]{64})", image
    )
    require(match is not None and match[2] == release["image_digest"], "image_binding_invalid")
    role = account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
    template = expected_spec["template"]
    pod_spec(template["spec"], spec["template"]["spec"], role=role, region=match[1])
    selector = ",".join(f"{key}={value}" for key, value in LABELS.items())
    replicasets = collection("/apis/apps/v1", "replicasets", selector)
    active = [item for item in replicasets if item.get("spec", {}).get("replicas", 0) > 0]
    require(
        len(active) == 1 and owner(active[0], "Deployment", deployment["metadata"]["uid"]), "replicaset_owner_mismatch"
    )
    replica = active[0]
    require(
        replica["spec"]["replicas"] == 2 and subset(template, replica["spec"].get("template")), "replicaset_mismatch"
    )
    pods = collection("/api/v1", "pods", selector)
    require(len(pods) == 2, "pod_count_mismatch")
    pod_evidence = []
    for pod in pods:
        require(
            owner(pod, "ReplicaSet", replica["metadata"]["uid"]) and subset(template["metadata"], pod["metadata"]),
            "pod_owner_mismatch",
        )
        pod_spec(template["spec"], pod.get("spec"), role=role, region=match[1], admission=True)
        state = pod.get("status", {})
        require(
            state.get("phase") == "Running"
            and any(
                item.get("type") == "Ready" and item.get("status") == "True" for item in state.get("conditions", [])
            ),
            "pod_not_ready",
        )
        containers = state.get("containerStatuses", [])
        require(
            len(containers) == 1
            and containers[0].get("name") == "tool-adapters"
            and containers[0].get("ready") is True
            and bool(containers[0].get("state", {}).get("running")),
            "container_not_ready",
        )
        observed_image = containers[0].get("imageID", "")
        require(
            observed_image in {image, "docker-pullable://" + image, "containerd://" + match[2], match[2]},
            "running_image_mismatch",
        )
        pod_evidence.append({"uid": pod["metadata"]["uid"], "image_digest": match[2], "ready": True})
    service_spec = service.get("spec", {})
    require(
        subset(wanted["Service"]["spec"], service_spec) and service_spec.get("selector") == LABELS,
        "service_routing_mismatch",
    )
    require(
        service_spec.get("clusterIP") not in (None, "", "None")
        and not service_spec.get("externalIPs")
        and not service_spec.get("externalName")
        and not service_spec.get("publishNotReadyAddresses"),
        "service_not_private",
    )
    require(subset(wanted["PodDisruptionBudget"]["spec"], disruption.get("spec", {})), "disruption_budget_mismatch")
    slices = collection("/apis/discovery.k8s.io/v1", "endpointslices", "kubernetes.io/service-name=tool-adapters")
    require(0 < len(slices) <= 4, "endpoints_missing")
    pod_by_uid = {pod["metadata"]["uid"]: pod for pod in pods}
    routed = set()
    for endpoint_slice in slices:
        require(owner(endpoint_slice, "Service", service["metadata"]["uid"]), "endpoint_owner_mismatch")
        require(
            endpoint_slice.get("ports") == [{"name": "http", "protocol": "TCP", "port": 8080}], "endpoint_port_mismatch"
        )
        endpoints = endpoint_slice.get("endpoints", [])
        require(0 < len(endpoints) <= 4, "endpoint_count_invalid")
        for endpoint in endpoints:
            target, conditions = endpoint.get("targetRef", {}), endpoint.get("conditions", {})
            pod = pod_by_uid.get(target.get("uid"))
            require(
                pod is not None
                and target.get("kind") == "Pod"
                and target.get("namespace") == namespace
                and target.get("name") == pod["metadata"]["name"],
                "endpoint_target_mismatch",
            )
            addresses = {item["ip"] for item in pod["status"].get("podIPs", [])}
            require(
                bool(endpoint.get("addresses")) and set(endpoint["addresses"]) <= addresses, "endpoint_address_mismatch"
            )
            require(
                conditions.get("ready") is True
                and conditions.get("serving", True) is True
                and not conditions.get("terminating"),
                "endpoint_not_ready",
            )
            routed.add(target["uid"])
    require(routed == set(pod_by_uid), "endpoint_coverage_mismatch")
    path = f"/api/v1/namespaces/{namespace}/services/http:tool-adapters:80/proxy"
    contract = {"service": "adp-tool-adapters", **metadata}
    require(read(path + "/contract") == contract, "contract_probe_failed")
    require(
        read(path + "/readyz")
        == {"status": "ready", "mode": configuration["mode"], "execution_enabled": False, **contract},
        "readiness_probe_failed",
    )
    for path, before in watched.items():
        # A resource validated above can legitimately disappear between its
        # first read and this re-read: the Deployment's revision history retires
        # an inactive ReplicaSet, or the endpoint controller replaces an
        # EndpointSlice. The caller's reader reports a vanished path as its own
        # transport failure, which is not an ObservationError and so escapes the
        # deployer's deliberate re-observation loop, failing a release on a
        # transient blip. A target that moved during observation is exactly the
        # condition this loop exists to detect, so report it as such and let the
        # caller observe a stable runtime instead. Nothing is accepted that was
        # not accepted before: a condition that persists still fails the stage.
        try:
            current = read(path)
        except ObservationError:
            raise
        except Exception:
            current = None
        require(current is not None and identity(current) == before, "runtime_changed_during_observation")
    return {
        "schema_version": "1.0",
        "observation_kind": "adapter_foundation",
        "observed_at": datetime.now(UTC).isoformat(),
        "namespace": namespace,
        "release": release,
        "configuration_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "deployment_uid": deployment["metadata"]["uid"],
        "deployment_generation": generation,
        "service_account_uid": account["metadata"]["uid"],
        "service_uid": service["metadata"]["uid"],
        "pods": sorted(pod_evidence, key=lambda item: item["uid"]),
        "contract": metadata,
        "ready_replicas": 2,
        "routed_replicas": 2,
        "execution_enabled": False,
        "probe_path": "api_server_proxy",
    }

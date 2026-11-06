"""One source-owned Pod shape for launch and independent execution readback."""

import json
from copy import deepcopy

from l5_services.workers.execution import AuthorityRevoked


FINALIZER = "adp.dev/transform-termination-observed"
CHUNK_BYTES = 256 * 1024
MAX_PARTS = 128


def input_sources(name, parts, *, check=False):
    if type(parts) is not int or not 1 <= parts <= MAX_PARTS:
        raise AuthorityRevoked("Transform Pod input projection exceeds its bound")
    sources = [{"configMap": {"name": f"{name}-part-{index:04}"}} for index in range(parts)]
    if check:
        sources.append({"configMap": {"name": name + "-part-9999",
            "items": [{"key": "next_chunk", "path": ".upload-progress"}]}})
    return sources


def pod_manifest(*, name, namespace, labels, node_name, image, container, command, sources,
                 deadline, environment=None, annotations=None):
    workload = {"name": container, "image": image, "command": command,
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                      "limits": {"cpu": "2", "memory": "4Gi"}},
        "volumeMounts": [{"name": "input", "mountPath": "/input", "readOnly": True},
                         {"name": "work", "mountPath": "/work"}]}
    if container == "provider":
        workload["envFrom"] = [{"secretRef": {"name": name}}]
        if environment:
            workload["env"] = environment
    metadata = {"name": name, "namespace": namespace, "labels": labels, "finalizers": [FINALIZER]}
    if annotations:
        metadata["annotations"] = annotations
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata,
        "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
            "serviceAccountName": "l5-transform-sandbox", "enableServiceLinks": False,
            "nodeName": node_name, "activeDeadlineSeconds": deadline,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10000, "runAsGroup": 10000,
                "fsGroup": 10000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [workload],
            "volumes": [{"name": "input", "projected": {"sources": sources}},
                        {"name": "work", "emptyDir": {"sizeLimit": "1Gi"}}]}}


def _same(left, right):
    # Keep booleans distinct from integers when comparing API JSON.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _defaults(value, defaults):
    if not isinstance(value, dict):
        return
    for key, default in defaults.items():
        if key in value and _same(value[key], default):
            del value[key]


def _normalized(spec):
    spec = deepcopy(spec)
    if not isinstance(spec, dict):
        return spec
    _defaults(spec, {"dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler",
        "terminationGracePeriodSeconds": 30, "hostNetwork": False, "hostPID": False,
        "hostIPC": False, "shareProcessNamespace": False, "hostUsers": True,
        "serviceAccount": "l5-transform-sandbox", "priority": 0,
        "preemptionPolicy": "PreemptLowerPriority", "initContainers": [], "ephemeralContainers": []})
    tolerations = spec.get("tolerations")
    default_tolerations = [{"key": f"node.kubernetes.io/{key}", "operator": "Exists",
                           "effect": "NoExecute", "tolerationSeconds": 300}
                          for key in ("not-ready", "unreachable")]
    if tolerations == [] or (isinstance(tolerations, list) and len(tolerations) <= 2
            and all(any(_same(entry, default) for default in default_tolerations) for entry in tolerations)
            and len({json.dumps(entry, sort_keys=True) for entry in tolerations}) == len(tolerations)):
        spec.pop("tolerations", None)
    security = spec.get("securityContext")
    _defaults(security, {"supplementalGroups": [], "supplementalGroupsPolicy": "Merge",
                         "fsGroupChangePolicy": "Always"})
    for container in spec.get("containers", []) if isinstance(spec.get("containers"), list) else []:
        if not isinstance(container, dict):
            continue
        _defaults(container, {"terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File", "imagePullPolicy": "IfNotPresent",
            "stdin": False, "stdinOnce": False, "tty": False, "args": [], "env": [], "envFrom": []})
        context = container.get("securityContext")
        _defaults(context, {"privileged": False, "procMount": "Default"})
        if isinstance(context, dict):
            _defaults(context.get("capabilities"), {"add": []})
        for mount in container.get("volumeMounts", []) if isinstance(container.get("volumeMounts"), list) else []:
            _defaults(mount, {"readOnly": False, "mountPropagation": "None", "recursiveReadOnly": "Disabled"})
        for source in container.get("envFrom", []) if isinstance(container.get("envFrom"), list) else []:
            if isinstance(source, dict):
                _defaults(source.get("secretRef"), {"optional": False})
    for volume in spec.get("volumes", []) if isinstance(spec.get("volumes"), list) else []:
        if not isinstance(volume, dict) or not isinstance(volume.get("projected"), dict):
            continue
        projection = volume["projected"]
        _defaults(projection, {"defaultMode": 420})
        for source in projection.get("sources", []) if isinstance(projection.get("sources"), list) else []:
            if isinstance(source, dict):
                _defaults(source.get("configMap"), {"optional": False})
    return spec


def _difference(actual, expected, path="spec"):
    if type(actual) is not type(expected):
        return path
    if isinstance(expected, dict):
        for key, value in expected.items():
            if key not in actual:
                return path + "." + key
            difference = _difference(actual[key], value, path + "." + key)
            if difference:
                return difference
        if set(actual) != set(expected):
            return path + " (additional fields)"
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            return path
        for index, (value, wanted) in enumerate(zip(actual, expected)):
            difference = _difference(value, wanted, f"{path}[{index}]")
            if difference:
                return difference
    elif not _same(actual, expected):
        return path
    return None


def validate_pod(pod, expected):
    """Reject admission/recovery drift before reading logs or retaining results.

    Kubernetes defaults are allowed explicitly, never by dropping unknown keys.
    In particular, mounts, identity injection, execution hooks, and security
    overrides cannot hide in an otherwise matching Pod.
    """
    if not isinstance(pod, dict):
        raise AuthorityRevoked("Transform Pod readback is unavailable")
    actual, wanted = pod.get("metadata", {}), expected["metadata"]
    if (not isinstance(actual, dict) or not actual.get("uid")
            or any(not _same(actual.get(key), wanted[key])
                   for key in ("name", "namespace", "labels", "finalizers"))):
        raise AuthorityRevoked("Transform Pod ownership or termination fence changed")
    annotations = actual.get("annotations", {})
    if (not isinstance(annotations, dict)
            or any(annotations.get(key) != value for key, value in wanted.get("annotations", {}).items())
            or any(key.startswith(("seccomp.security.alpha.kubernetes.io/",
                                  "container.seccomp.security.alpha.kubernetes.io/",
                                  "container.apparmor.security.beta.kubernetes.io/")) for key in annotations)):
        raise AuthorityRevoked("Transform Pod isolation annotations changed")
    difference = _difference(_normalized(pod.get("spec")), _normalized(expected["spec"]))
    if difference:
        # Only expected source-owned field names are included, never API values
        # or untrusted extra keys which might contain credential material.
        raise AuthorityRevoked(f"Transform Pod isolation changed at {difference}; restore the admitted workload configuration")

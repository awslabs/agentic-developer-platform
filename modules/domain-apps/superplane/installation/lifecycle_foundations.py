"""Create-only, retained native lifecycle policy and durable state owner."""

import copy
import hashlib
import re

import yaml

from .config import LABEL, Refusal, digest, identity, image, require

KEY = "lifecycle_foundations"


def validate(env):
    if KEY not in env:
        return
    worker = env.get("paid_worker", {})
    require(
        worker.get("mode") == "native-lifecycle",
        "Lifecycle foundations require native lifecycle mode",
    )
    config = env[KEY]
    require(
        isinstance(config, dict) and set(config) == {"policy_json", "state"},
        "Lifecycle foundations require exact policy and state inputs",
    )
    raw = config["policy_json"]
    require(
        isinstance(raw, str) and 0 < len(raw.encode()) <= 65536,
        "Lifecycle policy must contain bounded UTF-8 JSON",
    )
    from .runtime_approval import decode_json
    from workspace_provisioning.lifecycle_policy import LifecyclePolicy

    document = decode_json(raw.encode())
    require(
        isinstance(document, dict)
        and set(document) == {"version", "tenants"}
        and type(document["version"]) is int
        and document["version"] == 1
        and isinstance(document["tenants"], dict)
        and set(document["tenants"]) == {env["org_id"]},
        "Lifecycle policy must name only this immutable domain organization",
    )
    try:
        policy = LifecyclePolicy.model_validate(document["tenants"][env["org_id"]])
    except Exception:
        raise Refusal(
            "Lifecycle policy does not satisfy the maintained runtime schema"
        ) from None
    require(
        policy.adp_org_id == env["adp_org_id"]
        and policy.management_account_id == env["account_id"]
        and policy.management_cluster == env["cluster"],
        "Lifecycle policy management identity differs from installation",
    )
    require(
        hashlib.sha256(raw.encode()).hexdigest() == worker["lifecycle_policy_sha256"],
        "Lifecycle policy bytes differ from reviewed digest",
    )
    require(
        policy.operation_max_runtime_seconds > 900
        and policy.operation_max_runtime_seconds <= worker["active_deadline_seconds"],
        "Lifecycle provider sessions need more than 900 seconds within the worker deadline",
    )
    state = config["state"]
    require(
        isinstance(state, dict) and set(state) == {"storage_class", "capacity"},
        "Lifecycle state requires exact storage class and capacity",
    )
    require(
        isinstance(state["storage_class"], str)
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", state["storage_class"]),
        "Lifecycle state requires an explicit StorageClass",
    )
    require(
        isinstance(state["capacity"], str)
        and re.fullmatch(r"[1-9][0-9]{0,2}Gi", state["capacity"]),
        "Lifecycle state capacity must be 1 through 999 Gi",
    )
    require(
        worker["max_replica_count"] == 1,
        "ReadWriteOnce lifecycle state requires one worker at a time",
    )


def documents(env):
    if KEY not in env:
        return []
    validate(env)
    worker, config = env["paid_worker"], env[KEY]

    def resource(kind, name, **fields):
        return {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "name": name,
                "namespace": env["namespace"],
                "labels": {LABEL: identity(env)},
            },
            **fields,
        }

    return [
        resource(
            "ConfigMap",
            worker["lifecycle_policy_configmap"],
            immutable=True,
            data={"lifecycle.json": config["policy_json"]},
        ),
        resource(
            "PersistentVolumeClaim",
            worker["lifecycle_state_claim"],
            spec={
                "storageClassName": config["state"]["storage_class"],
                "accessModes": ["ReadWriteOnce"],
                "volumeMode": "Filesystem",
                "resources": {"requests": {"storage": config["state"]["capacity"]}},
            },
        ),
    ]


def managed(env, doc):
    return KEY in env and (doc["kind"], doc["metadata"]["name"]) in {
        ("ConfigMap", env["paid_worker"]["lifecycle_policy_configmap"]),
        ("PersistentVolumeClaim", env["paid_worker"]["lifecycle_state_claim"]),
    }


def same(installer, actual, desired):
    from .lifecycle_worker import contains

    meta = actual.get("metadata", {})
    require(
        meta.get("name") == desired["metadata"]["name"]
        and meta.get("namespace") == installer.env["namespace"]
        and meta.get("labels", {}).get(LABEL) == installer.owner
        and meta.get("uid")
        and meta.get("resourceVersion")
        and not meta.get("deletionTimestamp"),
        "Lifecycle dependency owner or live identity changed",
    )
    previous = installer.receipt.get(KEY, {}).get(desired["kind"])
    require(
        not previous or previous["uid"] == meta["uid"],
        "Lifecycle dependency was replaced; resume refused",
    )
    if desired["kind"] == "ConfigMap":
        require(
            actual.get("immutable") is True
            and actual.get("data") == desired["data"]
            and not actual.get("binaryData"),
            "Existing lifecycle policy differs; replacement is refused",
        )
    else:
        spec = actual.get("spec", {})
        require(
            contains(spec, desired["spec"])
            and not any(
                spec.get(key) for key in ("selector", "dataSource", "dataSourceRef")
            ),
            "Existing lifecycle state differs; replacement is refused",
        )
        require(
            spec.get("resources") == desired["spec"]["resources"],
            "Lifecycle state resource requests changed",
        )
    return {key: meta[key] for key in ("uid", "resourceVersion")}


def storage_class(installer):
    name = installer.env[KEY]["state"]["storage_class"]
    current = installer.json(installer.kube("get", "storageclass", name, "-o", "json"))
    meta = current.get("metadata", {})
    require(
        meta.get("name") == name
        and meta.get("uid")
        and meta.get("resourceVersion")
        and not meta.get("deletionTimestamp")
        and current.get("reclaimPolicy") == "Retain"
        and current.get("volumeBindingMode") in {"Immediate", "WaitForFirstConsumer"}
        and current.get("provisioner")
        in {"ebs.csi.aws.com", "ebs.csi.eks.amazonaws.com"},
        "Lifecycle state requires an existing retained EBS StorageClass",
    )
    observed = {key: meta[key] for key in ("uid", "resourceVersion")}
    previous = installer.receipt.get(KEY, {}).get("StorageClass")
    require(
        not previous or previous == observed,
        "Lifecycle StorageClass identity or configuration changed",
    )
    return observed


def snapshot(installer, *, bound=True):
    if KEY not in installer.env:
        return None
    result = {"StorageClass": storage_class(installer)}
    for desired in documents(installer.env):
        actual = installer.existing(desired)
        require(actual is not None, "Owned lifecycle dependency is missing")
        result[desired["kind"]] = same(installer, actual, desired)
        if desired["kind"] == "PersistentVolumeClaim" and bound:
            require(
                actual.get("status", {}).get("phase") == "Bound",
                "Lifecycle state is not bound",
            )
            volume = actual.get("spec", {}).get("volumeName")
            require(
                isinstance(volume, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", volume),
                "Bound lifecycle state has no exact volume identity",
            )
            pv = installer.json(installer.kube("get", "pv", volume, "-o", "json"))
            spec, meta = pv.get("spec", {}), pv.get("metadata", {})
            ref = spec.get("claimRef", {})
            require(
                meta.get("name") == volume
                and meta.get("uid")
                and meta.get("resourceVersion")
                and not meta.get("deletionTimestamp")
                and spec.get("persistentVolumeReclaimPolicy") == "Retain"
                and spec.get("storageClassName")
                == installer.env[KEY]["state"]["storage_class"]
                and spec.get("volumeMode", "Filesystem") == "Filesystem"
                and spec.get("accessModes") == ["ReadWriteOnce"]
                and spec.get("capacity", {}).get("storage")
                == installer.env[KEY]["state"]["capacity"]
                and spec.get("csi", {}).get("driver")
                in {"ebs.csi.aws.com", "ebs.csi.eks.amazonaws.com"}
                and isinstance(spec.get("csi", {}).get("volumeHandle"), str)
                and re.fullmatch(r"vol-[a-f0-9]{8,32}", spec["csi"]["volumeHandle"])
                and pv.get("status", {}).get("phase") == "Bound"
                and ref.get("uid") == actual["metadata"]["uid"]
                and ref.get("name") == desired["metadata"]["name"]
                and ref.get("namespace") == installer.env["namespace"],
                "Lifecycle volume ownership or retention is inconsistent",
            )
            result["PersistentVolume"] = {
                key: meta[key] for key in ("uid", "resourceVersion")
            }
            previous = installer.receipt.get(KEY, {}).get("PersistentVolume")
            require(
                not previous or previous["uid"] == meta["uid"],
                "Lifecycle volume was replaced",
            )
    return result


def probe_job(installer, claim_uid):
    config = installer.env["paid_worker"]
    name = (
        "superplane-state-probe-"
        + digest({"claim": claim_uid, "run": installer.run_id})[:16]
    )
    labels = {
        LABEL: installer.owner,
        "app.kubernetes.io/name": "superplane-state-probe",
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": installer.env["namespace"],
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": min(installer.env["timeout_seconds"], 600),
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "superplane-state-probe",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "nodeSelector": copy.deepcopy(config["node_selector"]),
                    "securityContext": {
                        "fsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "state-probe",
                            "image": image(installer.lock, "superplane-paid-worker"),
                            "command": [
                                "/opt/executor/bin/python",
                                "-c",
                                "import os,tempfile; fd,path=tempfile.mkstemp(prefix='.adp-probe-',dir='/run/state'); os.write(fd,b'owner-storage-probe'); os.fsync(fd); os.close(fd); os.unlink(path)",
                            ],
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 65531,
                                "runAsGroup": 65532,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "250m", "memory": "128Mi"},
                            },
                            "volumeMounts": [
                                {"name": "state", "mountPath": "/run/state"}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "state",
                            "persistentVolumeClaim": {
                                "claimName": config["lifecycle_state_claim"]
                            },
                        }
                    ],
                },
            },
        },
    }


def probe_account(installer, *, create):
    service_account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "superplane-state-probe",
            "namespace": installer.env["namespace"],
            "labels": {LABEL: installer.owner},
        },
        "automountServiceAccountToken": False,
    }
    account = installer.existing(service_account)
    if account is None and create:
        installer.apply([service_account])
        account = installer.existing(service_account)
    if account is None and not create:
        require(
            not installer.receipt.get(KEY, {}).get("ProbeServiceAccount"),
            "Recorded probe service account disappeared",
        )
        return
    require(
        account is not None
        and account.get("metadata", {}).get("labels", {}).get(LABEL) == installer.owner
        and account.get("metadata", {}).get("uid")
        and not account["metadata"].get("deletionTimestamp")
        and not account["metadata"].get("annotations")
        and account.get("automountServiceAccountToken") is False
        and not account.get("secrets")
        and not account.get("imagePullSecrets"),
        "Lifecycle state probe service account has unexpected authority",
    )
    previous = installer.receipt.get(KEY, {}).get("ProbeServiceAccount")
    require(
        not previous or previous["uid"] == account["metadata"]["uid"],
        "Probe service account was replaced",
    )
    return {"uid": account["metadata"]["uid"]}


def prepare(installer):
    if KEY not in installer.env:
        return
    desired = documents(installer.env)
    evidence = installer.receipt.setdefault(KEY, {})
    observed_class = storage_class(installer)
    probe_account(installer, create=False)
    # Inspect every dependency before the first write; never adopt an unowned one.
    for doc in desired:
        actual = installer.existing(doc)
        if actual is not None:
            same(installer, actual, doc)
        else:
            require(
                doc["kind"] not in evidence, "Recorded lifecycle dependency disappeared"
            )
    evidence["StorageClass"] = observed_class
    installer.save()
    for doc in desired:
        actual = installer.existing(doc)
        if actual is None:
            actual = installer.json(
                installer.kube(
                    "create",
                    "--field-manager=superplane-installer",
                    "-f",
                    "-",
                    "-o",
                    "json",
                    data=yaml.safe_dump(doc),
                )
            )
        evidence[doc["kind"]] = same(installer, actual, doc)
        installer.save()
    evidence["ProbeServiceAccount"] = probe_account(installer, create=True)
    installer.save()
    job = probe_job(installer, evidence["PersistentVolumeClaim"]["uid"])
    current = installer.existing(job)
    if current is None:
        installer.apply([job])
    else:
        require(
            current.get("metadata", {}).get("labels", {}).get(LABEL) == installer.owner
            and not current["metadata"].get("deletionTimestamp")
            and probe_matches(current.get("spec"), job["spec"]),
            "Existing state probe differs from the owned recipe",
        )
    installer.wait_job(job)
    evidence.update(snapshot(installer))
    evidence["writable_probe"] = job["metadata"]["name"]
    evidence["retained"] = True
    installer.save()


def probe_matches(actual, desired):
    """Permit API defaults, but no additional containers, env or volume authority."""
    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            return False
        if any(
            key in actual and key not in desired
            for key in (
                "env",
                "envFrom",
                "initContainers",
                "ephemeralContainers",
                "imagePullSecrets",
                "hostNetwork",
                "hostPID",
                "hostIPC",
                "privileged",
                "procMount",
                "shareProcessNamespace",
            )
        ):
            return False
        return all(
            key in actual and probe_matches(actual[key], value)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(desired)
            and all(probe_matches(a, d) for a, d in zip(actual, desired))
        )
    return actual == desired

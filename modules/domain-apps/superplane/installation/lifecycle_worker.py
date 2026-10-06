"""Live native-worker preparation and guarded owner activation.

Receipts record evidence only. Every transition repeats live configuration and
protected Gateway reads while the API's paid admission remains disabled.
"""

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from .config import LABEL, image, require
from .paid_worker import COMPONENT, WORKER


def enabled(env):
    return env.get("paid_worker", {}).get("mode") == "native-lifecycle"


def expected_binding(env, lock):
    config = env["paid_worker"]

    def registry(role):
        return str(uuid5(NAMESPACE_URL, "adp:domain-operation-registration:v1:" + role))

    return {
        "producer_registry_id": registry(env["api_adapters"]["dispatcher"]["role_arn"]),
        "worker_registry_id": registry(config["role_arn"]),
        "worker_namespace": env["namespace"],
        "worker_service_account": WORKER,
        "worker_role_arn": config["role_arn"],
        "worker_image_digest": lock["images"][COMPONENT],
        "operation_schema": config["operation_schema"],
        "queue_arn": config["queue_arn"],
    }


def project_api(env, lock, docs):
    if not enabled(env):
        return
    config_name = WORKER + "-binding"
    labels = copy.deepcopy(docs[0]["metadata"]["labels"])
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": config_name,
                "namespace": env["namespace"],
                "labels": labels,
            },
            "data": {
                "binding.json": json.dumps(expected_binding(env, lock), sort_keys=True)
            },
        }
    )
    for doc in docs:
        if (
            doc.get("kind") != "Deployment"
            or doc["metadata"]["name"] != "superplane-api"
        ):
            continue
        pod = doc["spec"]["template"]["spec"]
        container = pod["containers"][0]
        container["env"] = [
            v
            for v in container["env"]
            if v["name"]
            not in {
                "SUPERPLANE_PAID_WORKER_BINDING_FILE",
                "SUPERPLANE_LIFECYCLE_CONFIG_FILE",
            }
        ]
        container["env"].append(
            {
                "name": "SUPERPLANE_PAID_WORKER_BINDING_FILE",
                "value": "/run/paid-worker-binding/binding.json",
            }
        )
        pod.setdefault("volumes", []).append(
            {"name": "paid-worker-binding", "configMap": {"name": config_name}}
        )
        container.setdefault("volumeMounts", []).append(
            {
                "name": "paid-worker-binding",
                "mountPath": "/run/paid-worker-binding",
                "readOnly": True,
            }
        )
        container["env"].append(
            {
                "name": "SUPERPLANE_LIFECYCLE_CONFIG_FILE",
                "value": "/run/lifecycle-policy/lifecycle.json",
            }
        )
        pod["volumes"].append(
            {
                "name": "lifecycle-policy",
                "configMap": {
                    "name": env["paid_worker"]["lifecycle_policy_configmap"],
                    "defaultMode": 0o440,
                },
            }
        )
        container["volumeMounts"].append(
            {
                "name": "lifecycle-policy",
                "mountPath": "/run/lifecycle-policy",
                "readOnly": True,
            }
        )
        doc["spec"]["template"].setdefault("metadata", {}).setdefault(
            "annotations", {}
        )["adp.aws-e.io/lifecycle-policy-sha256"] = env["paid_worker"][
            "lifecycle_policy_sha256"
        ]


def worker_documents(installer, *, active=False):
    names = {
        WORKER,
        WORKER + "-config",
        WORKER + "-queue-observer",
        WORKER + "-preparation-deny",
    }
    docs = [copy.deepcopy(d) for d in installer.docs if d["metadata"]["name"] in names]
    require(
        {d["kind"] for d in docs}
        == {
            "ServiceAccount",
            "ConfigMap",
            "TriggerAuthentication",
            "NetworkPolicy",
            "ScaledJob",
        },
        "complete native worker projection required",
    )
    for doc in docs:
        for key in ("uid", "resourceVersion", "managedFields", "creationTimestamp"):
            doc["metadata"].pop(key, None)
    if active:
        for doc in docs:
            if doc["kind"] == "ScaledJob":
                doc["metadata"]["annotations"]["autoscaling.keda.sh/paused"] = "false"
                doc["metadata"]["annotations"].pop(
                    "adp.aws-e.io/preparation-only", None
                )
                doc["spec"]["maxReplicaCount"] = installer.env["paid_worker"][
                    "max_replica_count"
                ]
            elif doc["kind"] == "NetworkPolicy":
                from . import native_egress

                if native_egress.enabled(installer.env):
                    doc["spec"]["egress"] = native_egress.rules(installer.env)
                    continue
                doc["spec"]["egress"] = [
                    {
                        "to": [{"ipBlock": {"cidr": endpoint["cidr"]}}],
                        "ports": [{"protocol": "TCP", "port": endpoint["port"]}],
                    }
                    for endpoint in installer.env["paid_worker"]["egress"].values()
                ]
                # Cluster DNS is required to resolve the reviewed service names.
                doc["spec"]["egress"].append(
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": "kube-system"
                                    }
                                },
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                            }
                        ],
                        "ports": [
                            {"protocol": protocol, "port": 53}
                            for protocol in ("TCP", "UDP")
                        ],
                    }
                )
    return docs


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


def installed_snapshot(installer, *, active=False):
    from .adapter_staging import secret_metadata
    from .lifecycle_foundations import snapshot

    env = installer.env
    observed = {}
    foundations = snapshot(installer)
    if foundations is not None:
        observed["lifecycle_foundations"] = foundations
    for desired in worker_documents(installer, active=active):
        actual = installer.existing(desired)
        require(
            actual is not None
            and actual["metadata"].get("labels", {}).get(LABEL) == installer.owner
            and contains(actual, desired),
            "installed native worker differs from reviewed projection",
        )
        if desired["kind"] == "ConfigMap":
            require(
                actual.get("data") == desired.get("data")
                and not actual.get("binaryData"),
                "native worker configuration has unreviewed values",
            )
        if desired["kind"] == "ScaledJob":
            pod = actual["spec"]["jobTargetRef"]["template"]["spec"]
            require(
                not any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC")),
                "native worker host access is not allowed",
            )
        meta = actual["metadata"]
        require(
            not meta.get("deletionTimestamp")
            and meta.get("uid")
            and meta.get("resourceVersion"),
            "native worker object identity unavailable",
        )
        observed[desired["kind"]] = {
            key: meta[key] for key in ("uid", "resourceVersion")
        }
    config = env["paid_worker"]
    policy = installer.json(
        installer.kube(
            "get",
            "configmap",
            config["lifecycle_policy_configmap"],
            "-n",
            env["namespace"],
            "-o",
            "json",
        )
    )
    claim = installer.json(
        installer.kube(
            "get",
            "pvc",
            config["lifecycle_state_claim"],
            "-n",
            env["namespace"],
            "-o",
            "json",
        )
    )
    require(
        hashlib.sha256(
            policy.get("data", {}).get("lifecycle.json", "").encode()
        ).hexdigest()
        == config["lifecycle_policy_sha256"],
        "installed lifecycle policy differs from reviewed digest",
    )
    require(
        claim.get("status", {}).get("phase") == "Bound",
        "lifecycle state storage is not bound",
    )
    for name, value in (("policy", policy), ("state", claim)):
        meta = value["metadata"]
        require(
            meta.get("namespace") == env["namespace"]
            and not meta.get("deletionTimestamp")
            and meta.get("uid")
            and meta.get("resourceVersion"),
            "lifecycle dependency identity unavailable",
        )
        observed[name] = {key: meta[key] for key in ("uid", "resourceVersion")}
    cluster = installer.json(
        installer.aws("eks", "describe-cluster", "--name", env["cluster"])
    )["cluster"]
    names = [config["database_secret"]]
    names.append(
        env["api_adapters"]["dispatcher"]["operation_database_secret_ref"]["name"]
    )
    observed["secrets"] = {
        name: secret_metadata(installer, cluster, name) for name in names
    }
    return observed


PROOF_PROGRAM = """import asyncio,json,sys
from app.adapters.operation_dispatch import ProducerTransport
from app.config import settings
async def main():
 v=json.load(sys.stdin)
 t=ProducerTransport(settings.superplane_operation_gateway_url,settings.superplane_operation_gateway_region)
 try: print(json.dumps(await t.post('/binding-proof',v)))
 finally: await t.aclose()
try: asyncio.run(main())
except Exception: raise SystemExit('native binding proof unavailable') from None
"""


def proof(installer, state):
    env = installer.env
    value = installer.json(
        installer.kube(
            "exec",
            "-i",
            "deployment/superplane-api",
            "-n",
            env["namespace"],
            "--",
            "python",
            "-c",
            PROOF_PROGRAM,
            data=json.dumps(
                {"domain": "superplane", "org_id": env["org_id"], "state": state}
            ),
        )
    )
    return validate_proof(installer, value, state)


def validate_proof(installer, value, state):
    env = installer.env
    try:
        checked = datetime.fromisoformat(value["checked_at"])
        current = checked.tzinfo is not None and datetime.now(UTC) - timedelta(
            seconds=60
        ) <= checked <= datetime.now(UTC)
    except (KeyError, ValueError, TypeError):
        current = False
    if state == "quiescent":
        require(
            current
            and value.get("version") == 1
            and value.get("state") == state
            and value.get("quiescent") is True
            and value.get("installed") is False
            and value.get("domain") == "superplane"
            and value.get("org_id") == env["org_id"]
            and value.get("adp_org_id") == env["adp_org_id"],
            "shared execution store is not quiescent",
        )
        return value
    require(
        bool(re.fullmatch(r"[a-f0-9]{64}", str(value.get("binding_sha256", ""))))
        and current
        and value.get("version") == 1
        and value.get("installed") is True
        and value.get("state") == state
        and value.get("domain") == "superplane"
        and value.get("org_id") == env["org_id"]
        and value.get("adp_org_id") == env["adp_org_id"]
        and value.get("domain_schema") == env["database"]["schema"]
        and all(
            value.get(k) == v for k, v in expected_binding(env, installer.lock).items()
        )
        and (state != "prepared" or value.get("quiescent") is True),
        "authenticated native worker proof differs from selected target",
    )
    return value


def prepare(installer):
    before = installed_snapshot(installer)
    report = proof(installer, "prepared")
    from .native_egress_probe import verify_network

    network = verify_network(installer)
    require(
        installed_snapshot(installer) == before,
        "native preparation changed during verification",
    )
    return {"snapshot": before, "proof": report, "network": network}


def activate(installer):
    prepared = installer.receipt["adapter_stage"]["native_worker"]
    require(
        installed_snapshot(installer) == prepared["snapshot"],
        "prepared native worker changed before activation",
    )
    current = proof(installer, "prepared")
    require(
        current["binding_sha256"] == prepared["proof"]["binding_sha256"],
        "shared binding changed since preparation",
    )
    from .native_egress_probe import verify_network

    verify_network(installer)
    installer.apply(worker_documents(installer, active=True))
    installed_snapshot(installer, active=True)
    executable = proof(installer, "executable")
    require(
        executable["binding_sha256"] == current["binding_sha256"],
        "shared binding changed during activation",
    )
    return executable


def pause(installer):
    installer.apply(worker_documents(installer))


def quiescence_job(installer):
    """Use the reviewed image, independent of an old or absent API process.

    This runs after the approved producer-role plan and before any worker
    resource is changed. Only namespace, producer SA, verifier network policy
    and a non-consuming bounded Job are created through the existing owner.
    """
    from .adapter_staging import role_identity

    env = installer.env
    if not enabled(env):
        return
    cluster = installer.json(
        installer.aws("eks", "describe-cluster", "--name", env["cluster"])
    )["cluster"]
    identity = role_identity(installer, cluster)
    prerequisites = [
        copy.deepcopy(d)
        for d in installer.docs
        if (d["kind"] == "Namespace" and d["metadata"]["name"] == env["namespace"])
        or (
            d["kind"] == "ServiceAccount"
            and d["metadata"]["name"] == "superplane-api"
            and d["metadata"].get("namespace") == env["namespace"]
        )
    ]
    require(
        {d["kind"] for d in prerequisites} == {"Namespace", "ServiceAccount"},
        "native verifier namespace and producer identity unavailable",
    )
    label = {"app.kubernetes.io/name": "superplane-binding-check"}
    metadata = {
        "namespace": env["namespace"],
        "labels": {LABEL: installer.owner, **label},
    }
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {**metadata, "name": "superplane-binding-check"},
        "spec": {
            "podSelector": {"matchLabels": label},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [
                        {"ipBlock": {"cidr": env["paid_worker"]["egress"][key]["cidr"]}}
                    ],
                    "ports": [
                        {
                            "protocol": "TCP",
                            "port": env["paid_worker"]["egress"][key]["port"],
                        }
                    ],
                }
                for key in ("gateway", "sts")
                if key in env["paid_worker"]["egress"]
            ]
            + [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"protocol": protocol, "port": 53}
                        for protocol in ("TCP", "UDP")
                    ],
                }
            ],
        },
    }
    from . import native_egress

    if native_egress.enabled(env):
        policy["spec"]["egress"] = native_egress.rules(env, database=False)
    dispatcher = env["api_adapters"]["dispatcher"]
    name = "superplane-binding-check-" + uuid4().hex[:12]
    program = PROOF_PROGRAM.replace(
        "import asyncio,json,sys", "import asyncio,json,sys,os"
    ).replace(
        "v=json.load(sys.stdin)",
        'v=json.loads(os.environ["SUPERPLANE_INSTALLATION_PROOF"])',
    )
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {**metadata, "name": name},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": min(env["timeout_seconds"], 300),
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {LABEL: installer.owner, **label}},
                "spec": {
                    "serviceAccountName": "superplane-api",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "verify",
                            "image": image(installer.lock, "superplane-api"),
                            "command": ["python", "-c", program],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "env": [
                                {"name": k, "value": v}
                                for k, v in {
                                    "SUPERPLANE_OPERATION_GATEWAY_URL": dispatcher[
                                        "endpoint"
                                    ],
                                    "SUPERPLANE_OPERATION_GATEWAY_REGION": env[
                                        "region"
                                    ],
                                    "AWS_REGION": env["region"],
                                    "AWS_EC2_METADATA_DISABLED": "true",
                                    "AWS_STS_REGIONAL_ENDPOINTS": "regional",
                                    "SUPERPLANE_INSTALLATION_PROOF": json.dumps(
                                        {
                                            "domain": "superplane",
                                            "org_id": env["org_id"],
                                            "state": "quiescent",
                                        }
                                    ),
                                }.items()
                            ],
                        }
                    ],
                },
            },
        },
    }
    installer.apply([*prerequisites, policy, job])
    installer.wait_job(job)
    value = installer.json(
        installer.kube("logs", "job/" + name, "-c", "verify", "-n", env["namespace"])
    )
    report = validate_proof(installer, value, "quiescent")
    require(
        role_identity(installer, cluster) == identity,
        "native verifier producer identity changed",
    )
    installer.receipt["shared_execution_quiescence"] = {
        "proof": report,
        "producer": identity,
        "job": name,
    }
    installer.save()
    return report

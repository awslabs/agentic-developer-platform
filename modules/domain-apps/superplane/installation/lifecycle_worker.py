"""Live native-worker preparation and guarded owner activation.

Receipts record evidence only. Every transition repeats live configuration and
protected Gateway reads while the API's paid admission remains disabled.
"""

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from .config import LABEL, require
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
            if v["name"] != "SUPERPLANE_PAID_WORKER_BINDING_FILE"
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

    env = installer.env
    observed = {}
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
    names = [
        config[key]
        for key in (
            "database_secret",
            "provider_secret",
            "workspace_credentials_secret",
        )
    ]
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
    require(
        installed_snapshot(installer) == before,
        "native preparation changed during verification",
    )
    return {"snapshot": before, "proof": report}


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

"""Render only the adapter foundation; the native release owns apply/readback.

IAM grants, private-edge routing and network policy are separate reviewed release
inputs. Rendering an annotated service account does not prove those permissions.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

from tool_adapters.config import AdapterConfiguration, load_configuration


def render(*, image: str, namespace: str, role_arn: str, configuration: AdapterConfiguration) -> dict:
    match = re.fullmatch(
        r"([0-9]{12})\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[a-z0-9._/-]+@(sha256:[0-9a-f]{64})", image
    )
    role = re.fullmatch(r"arn:(aws|aws-cn|aws-us-gov):iam::([0-9]{12}):role/[A-Za-z0-9_+=,.@/-]+", role_arn)
    if (
        match is None
        or role is None
        or match[1] != role[2]
        or match[2] != configuration.release.image_digest
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace) is None
    ):
        raise ValueError("Adapter image, namespace or role binding is invalid")
    labels = {"app.kubernetes.io/name": "tool-adapters", "app.kubernetes.io/part-of": "adp"}

    def resource(version, kind, name, **fields):
        return {
            "apiVersion": version,
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            **fields,
        }

    account = resource("v1", "ServiceAccount", "tool-adapters", automountServiceAccountToken=False)
    account["metadata"]["annotations"] = {"eks.amazonaws.com/role-arn": role_arn}
    config = resource(
        "v1", "ConfigMap", "tool-adapters-contract", data={"config.json": configuration.model_dump_json()}
    )
    container = {
        "name": "tool-adapters",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": 8080}],
        "env": [{"name": "ADP_TOOL_ADAPTER_CONFIG", "value": "/etc/adp-adapters/config.json"}],
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "startupProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 2, "failureThreshold": 30},
        "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10},
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}, "periodSeconds": 5},
        "volumeMounts": [
            {"name": "configuration", "mountPath": "/etc/adp-adapters", "readOnly": True},
            {"name": "scratch", "mountPath": "/tmp"},
        ],
    }
    deployment = resource(
        "apps/v1",
        "Deployment",
        "tool-adapters",
        spec={
            "replicas": 2,
            "selector": {"matchLabels": labels},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {
                        "checksum/adp-adapter-contract": hashlib.sha256(
                            configuration.model_dump_json().encode()
                        ).hexdigest(),
                        # Cluster auto-instrumentation (CloudWatch/OpenTelemetry
                        # operator) injects language runtime init containers and
                        # shared volumes into admitted pods. Those injected
                        # runtimes are rejected by this namespace's restricted
                        # PodSecurity policy and would also defeat independent
                        # execution verification, which admits exactly one
                        # container and no init container. The operator honours
                        # explicit per-workload opt-out values, so declare them
                        # here: no cluster-wide change and no weakened runtime
                        # verifier. Pod logs and metrics are unaffected.
                        "instrumentation.opentelemetry.io/inject-java": "false",
                        "instrumentation.opentelemetry.io/inject-python": "false",
                        "instrumentation.opentelemetry.io/inject-dotnet": "false",
                        "instrumentation.opentelemetry.io/inject-nodejs": "false",
                    },
                },
                "spec": {
                    "serviceAccountName": "tool-adapters",
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "fsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": labels},
                        }
                    ],
                    "containers": [container],
                    "volumes": [
                        {"name": "configuration", "configMap": {"name": "tool-adapters-contract"}},
                        {"name": "scratch", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
                    ],
                },
            },
        },
    )
    service = resource(
        "v1",
        "Service",
        "tool-adapters",
        spec={
            "type": "ClusterIP",
            "selector": labels,
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    )
    disruption = resource(
        "policy/v1",
        "PodDisruptionBudget",
        "tool-adapters",
        spec={
            "minAvailable": 1,
            "selector": {"matchLabels": labels},
        },
    )
    return {"apiVersion": "v1", "kind": "List", "items": [account, config, deployment, service, disruption]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    configuration = load_configuration(args.config)
    if configuration is None:
        raise ValueError("Adapter configuration is required")
    value = render(image=args.image, namespace=args.namespace, role_arn=args.role_arn, configuration=configuration)
    Path(args.output).write_text(json.dumps(value, indent=2) + "\n")


if __name__ == "__main__":
    main()

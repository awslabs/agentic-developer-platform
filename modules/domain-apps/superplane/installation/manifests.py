"""Render domain-only resources; inherited deployment files are never applied."""

from __future__ import annotations

import json

import yaml

from .config import (
    LABEL,
    MODULE,
    cluster_dns_address,
    control_plane_mode,
    digest,
    identity,
    image,
)


def dns_egress(env: dict) -> dict:
    # Auto Mode serves DNS on the node, outside namespace/pod selectors. Keep
    # traditional DNS peers for standard/mixed clusters and allow only the exact
    # native resolver address, discovered from EKS during target preflight.
    peers = [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
            }
        }
    ]
    address = cluster_dns_address(env)
    if address is not None:
        peers.append({"ipBlock": {"cidr": f"{address}/{address.max_prefixlen}"}})
    return {"to": peers, "ports": [{"port": 53, "protocol": p} for p in ("UDP", "TCP")]}


def runtime_annotations(release: str) -> dict[str, str]:
    # CloudWatch auto-monitoring otherwise injects its own Python packages ahead
    # of the pinned image's dependencies, breaking API and SkyPilot imports.
    # Explicit values are respected by the operator's insert-only annotator.
    return {
        "adp.aws-e.io/release": release,
        **{
            f"instrumentation.opentelemetry.io/inject-{language}": "false"
            for language in ("java", "python", "nodejs", "dotnet")
        },
    }


def render(env: dict, lock: dict, *, control_plane_only: bool = False) -> list[dict]:
    control_plane_only = control_plane_mode(env, control_plane_only)
    ns, sky_ns = env["namespace"], env["skypilot_namespace"]
    owner = identity(env)
    release = digest(lock)
    labels = {
        LABEL: owner,
        "app.kubernetes.io/part-of": "adp-superplane",
        "adp.aws-e.io/domain-app": "superplane",
    }

    def obj(kind, name, namespace, **fields):
        result = {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
            **fields,
        }
        if kind == "Deployment":
            result["apiVersion"] = "apps/v1"
        if kind == "NetworkPolicy":
            result["apiVersion"] = "networking.k8s.io/v1"
        if kind == "PodDisruptionBudget":
            result["apiVersion"] = "policy/v1"
        return result

    docs = []
    for namespace in (ns, sky_ns):
        namespace_doc = obj("Namespace", namespace, namespace)
        del namespace_doc["metadata"]["namespace"]
        docs.append(namespace_doc)
        docs.append(
            obj(
                "NetworkPolicy",
                "superplane-default-deny",
                namespace,
                spec={"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
            )
        )

    def variable(name, value):
        return {"name": name, "value": str(value)}

    def secret(name, store, key):
        return {
            "name": name,
            "valueFrom": {
                "secretKeyRef": {"name": store, "key": key, "optional": False}
            },
        }

    def deployment(
        name,
        namespace,
        port,
        health,
        variables,
        *,
        args=None,
        command=None,
        volumes=None,
        mounts=None,
    ):
        selector = {"app.kubernetes.io/name": name}
        pod = {
            "serviceAccountName": name,
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "fsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": name,
                    "image": image(lock, name),
                    "ports": [{"name": "http", "containerPort": port}],
                    "env": [
                        variable("AWS_REGION", env["region"]),
                        variable("SUPERPLANE_RELEASE_ID", release),
                        variable("SUPERPLANE_SOURCE_REVISION", lock["source_revision"]),
                        *variables,
                    ],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "readinessProbe": {
                        "httpGet": {"path": health, "port": "http"},
                        "periodSeconds": 5,
                    },
                    "livenessProbe": {
                        "httpGet": {"path": health, "port": "http"},
                        "periodSeconds": 20,
                    },
                    "startupProbe": {
                        "httpGet": {"path": health, "port": "http"},
                        "periodSeconds": 10,
                        "failureThreshold": 30,
                    },
                    "volumeMounts": [
                        {"name": "tmp", "mountPath": "/tmp"},
                        *(mounts or []),
                    ],
                }
            ],
            "volumes": [
                {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}},
                *(volumes or []),
            ],
        }
        if command:
            pod["containers"][0]["command"] = command
        if args:
            pod["containers"][0]["args"] = args
        docs.append(
            obj("ServiceAccount", name, namespace, automountServiceAccountToken=False)
        )
        docs.append(
            obj(
                "Deployment",
                name,
                namespace,
                spec={
                    "replicas": 1,
                    "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": selector},
                    "template": {
                        "metadata": {
                            "labels": {**labels, **selector},
                            "annotations": runtime_annotations(release),
                        },
                        "spec": pod,
                    },
                },
            )
        )
        docs.append(
            obj(
                "Service",
                name,
                namespace,
                spec={
                    "type": "ClusterIP",
                    "selector": selector,
                    "ports": [{"name": "http", "port": port, "targetPort": "http"}],
                },
            )
        )
        return pod

    api_env = [
        variable("DOMAIN_AUTH_ENFORCED", "true"),
        variable("COGNITO_ENABLED", "true"),
        variable("COGNITO_ISSUER", env["auth"]["issuer"]),
        variable("COGNITO_JWKS_URL", env["auth"]["issuer"] + "/.well-known/jwks.json"),
        variable(
            "DOMAIN_AUTH_ALLOWED_CLIENT_IDS", json.dumps(env["auth"]["client_ids"])
        ),
        variable("CORS_ORIGINS", json.dumps([env["origin"]])),
        variable("LEGACY_HEARTBEAT_ENABLED", "false"),
        variable("SUPERPLANE_DB_SCHEMA", env["database"]["schema"]),
        variable("SUPERPLANE_INSTALLATION_REQUIRED", "true"),
        secret("DATABASE_URL", "superplane-db", "runtime-url"),
        secret("SUPERPLANE_DATABASE_CA", "superplane-db", "ca-pem"),
        # The org-scoped token signing key (issue #5683, A04). Injected by
        # reference, like every other secret here, so the material never enters a
        # manifest, a plan, an argv or a log.
        #
        # WHY THIS LINE IS PART OF THE A04 FIX RATHER THAN A SEPARATE CHANGE.
        # `app/config.py` used to default `jwt_secret_key` to a committed
        # placeholder, and this renderer never set JWT_SECRET_KEY — so this
        # deployment ran on that placeholder, which is exactly the defect. Now that
        # a missing key is a startup refusal, NOT setting it here would turn a
        # silent weakness into a failed rollout. The two halves have to land
        # together.
        #
        # `optional: False` (the default in `secret()` above) is the fail-closed
        # half: if the key is absent from the secret, the pod never starts, rather
        # than starting with the variable unset and refusing every login.
        secret("JWT_SECRET_KEY", "superplane-observation", "jwt-signing-key"),
        secret("OBSERVATION_SUBMITTERS", "superplane-observation", "submitters"),
        secret(
            "CONTROLLER_OBSERVATION_SUBMITTER_ID",
            "superplane-observation",
            "controller-submitter-id",
        ),
    ]
    if control_plane_only:
        api_env.append(variable("SUPERPLANE_MANAGEMENT_ONLY", "true"))
    api_pod = deployment("superplane-api", ns, 8000, "/health", api_env)
    api_pod["containers"][0]["readinessProbe"]["httpGet"]["path"] = "/readyz"
    api_url = f"http://superplane-api.{ns}.svc.cluster.local:8000"
    deployment(
        "superplane-platform-monitor",
        ns,
        9090,
        "/healthz",
        [
            variable("OBSERVATION_API_URL", api_url),
            secret(
                "OBSERVATION_CREDENTIAL", "superplane-observation", "monitor-credential"
            ),
            secret(
                "OBSERVATION_SIGNING_KEY",
                "superplane-observation",
                "monitor-signing-key",
            ),
            {
                "name": "MONITOR_ID",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
            },
        ],
    )
    if control_plane_only:
        controller = deployment(
            "superplane-controller",
            ns,
            8081,
            "/readyz",
            [
                variable("CONTROL_PLANE_API_URL", api_url),
                variable("SUPERPLANE_ORG_ID", env["org_id"]),
                variable("SUPERPLANE_REGISTRY_CREDENTIAL_FILE", "/registry/credential"),
            ],
            args=["--management-only"],
            volumes=[
                {
                    "name": "registry",
                    "secret": {
                        "secretName": "superplane-observation",
                        "defaultMode": 288,
                        "items": [
                            {"key": "controller-credential", "path": "credential"}
                        ],
                    },
                }
            ],
            mounts=[{"name": "registry", "mountPath": "/registry", "readOnly": True}],
        )
        controller["containers"][0]["livenessProbe"]["httpGet"]["path"] = "/healthz"
    else:
        # The workspace controller requires workspace-specific identity fields that
        # are deferred in control-plane-only mode.  Its deployment is generated only
        # when those fields are present and workspace activation is authorized.
        controller = deployment(
            "superplane-controller",
            ns,
            8081,
            "/readyz",
            [
                variable("KUBECONFIG", "/workspace/kubeconfig"),
                variable("EKS_CLUSTER_NAME", env["workspace_cluster"]),
                variable("SUPERPLANE_LEADER_NAMESPACE", env["workspace_namespace"]),
                variable("CLUSTER_ID", env["cluster_id"]),
                variable("WORKSPACE_ID", env["workspace_id"]),
                variable("CONTROL_PLANE_API_URL", api_url),
                variable(
                    "SKYPILOT_URL",
                    f"http://skypilot-api.{sky_ns}.svc.cluster.local:46580",
                ),
                secret(
                    "OBSERVATION_CREDENTIAL",
                    "superplane-observation",
                    "controller-credential",
                ),
                secret(
                    "OBSERVATION_SIGNING_KEY",
                    "superplane-observation",
                    "controller-signing-key",
                ),
                secret("SKYPILOT_SERVICE_TOKEN", "superplane-skypilot-auth", "token"),
            ],
            args=["--leader-elect=true"],
            volumes=[
                {
                    "name": "workspace-access",
                    "secret": {
                        "secretName": "superplane-workspace-access",
                        "defaultMode": 288,
                    },
                }
            ],
            mounts=[
                {
                    "name": "workspace-access",
                    "mountPath": "/workspace",
                    "readOnly": True,
                }
            ],
        )
        # No management-cluster RBAC, host access, node joins or GPU requests.
        controller["containers"][0]["env"].append(
            variable("SUPERPLANE_INSTALLATION_REQUIRED", "true")
        )

    # Retain the tested pinned SkyPilot entrypoint, writable HOME/config mounts,
    # durable PostgreSQL setting and resource envelope from U3.
    replacements = {
        "REPLACE_WITH_SKYPILOT_NAMESPACE": sky_ns,
        "REPLACE_WITH_SKYPILOT_IMAGE": image(lock, "skypilot-api"),
        "REPLACE_WITH_AWS_REGION": env["region"],
    }
    for file in ("20-skypilot-config.yaml", "40-skypilot-api.yaml"):
        raw = (MODULE / "k8s" / file).read_text()
        for key, value in replacements.items():
            raw = raw.replace(key, value)
        for doc in yaml.safe_load_all(raw):
            if not doc:
                continue
            doc["metadata"].setdefault("labels", {}).update(labels)
            if (
                doc["kind"] == "ConfigMap"
                and doc["metadata"]["name"] == "skypilot-config"
            ):
                config = yaml.safe_load(doc["data"]["config.yaml"])
                # SkyPilot 0.12 interprets `db` as a URL string, not a backend
                # mapping. Keep the connection exclusively in the Secret-backed
                # SKYPILOT_DB_CONNECTION_URI environment variable below.
                config.pop("db", None)
                doc["data"]["config.yaml"] = "{}\n"
                doc["data"]["desired-config.yaml"] = yaml.safe_dump(config)
                doc["data"]["bootstrap.py"] = (
                    MODULE / "installation/skypilot_bootstrap.py"
                ).read_text()
            if doc["kind"] == "Deployment":
                doc["spec"]["strategy"] = {"type": "Recreate"}
                pod = doc["spec"]["template"]
                pod["metadata"].setdefault("labels", {}).update(labels)
                pod["metadata"]["annotations"] = runtime_annotations(release)
                pod["spec"]["automountServiceAccountToken"] = False
                backend = pod["spec"]["containers"][0]
                backend["command"] = ["python3", "/skypilot-bootstrap/bootstrap.py"]
                backend["args"] = [
                    "--host=127.0.0.1" if arg == "--host=0.0.0.0" else arg
                    for arg in backend["args"]
                ]
                backend["env"].extend(
                    [
                        # The pinned image has no passwd entry for non-root UID
                        # 1000. SkyPilot uses getpass.getuser() during import.
                        {"name": "USER", "value": "skypilot"},
                        {"name": "IS_SKYPILOT_SERVER", "value": "true"},
                        {"name": "PGSSLMODE", "value": "verify-full"},
                        {"name": "PGSSLROOTCERT", "value": "/database-ca/ca.pem"},
                    ]
                )
                backend["volumeMounts"].append(
                    {
                        "name": "sky-config",
                        "mountPath": "/skypilot-bootstrap",
                        "readOnly": True,
                    }
                )
                backend["volumeMounts"].append(
                    {
                        "name": "database-ca",
                        "mountPath": "/database-ca",
                        "readOnly": True,
                    }
                )
                pod["spec"]["volumes"].append(
                    {
                        "name": "database-ca",
                        "secret": {
                            "secretName": "skypilot-api-db",
                            "items": [{"key": "ca-pem", "path": "ca.pem"}],
                        },
                    }
                )
                backend["ports"] = [{"name": "backend", "containerPort": 46580}]
                for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                    backend[probe].pop("httpGet", None)
                    backend[probe]["timeoutSeconds"] = 10
                    backend[probe]["exec"] = {
                        "command": [
                            "python3",
                            "-c",
                            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:46580/api/health', timeout=5)",
                        ]
                    }
                pod["spec"]["containers"].append(
                    {
                        "name": "authenticated-transport",
                        "image": image(lock, "superplane-api"),
                        "command": ["python", "-m", "app.skypilot_proxy"],
                        "ports": [{"name": "api", "containerPort": 46581}],
                        "env": [
                            secret(
                                "SKYPILOT_SERVICE_TOKEN",
                                "superplane-skypilot-auth",
                                "token",
                            )
                        ],
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "128Mi"},
                            "limits": {"cpu": "500m", "memory": "256Mi"},
                        },
                        "securityContext": {
                            "runAsUser": 65532,
                            "runAsNonRoot": True,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "readinessProbe": {
                            "exec": {
                                "command": [
                                    "python",
                                    "-m",
                                    "app.skypilot_proxy",
                                    "--check",
                                ]
                            },
                            # The check imports the Python application and has a
                            # ten-second HTTP timeout. Kubernetes defaults to 1s.
                            "timeoutSeconds": 15,
                            "periodSeconds": 20,
                        },
                    }
                )
            docs.append(doc)
    docs.append(
        obj(
            "ServiceAccount", "skypilot-api", sky_ns, automountServiceAccountToken=False
        )
    )

    # Internal API and callbacks accept only the named domain pods. Gateway can
    # reach the API, whose proxy route inventory excludes internal endpoints.
    _np_entries = [
        ("superplane-api", ns, 8000),
        ("superplane-platform-monitor", ns, 9090),
        ("superplane-controller", ns, 8081),
        ("skypilot-api", sky_ns, 46581),
    ]
    for name, namespace, port in _np_entries:
        peers = [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": n}
                },
                "podSelector": {"matchLabels": {LABEL: owner}},
            }
            for n in (ns, sky_ns)
        ]
        if name == "superplane-api":
            peers.append(
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": env.get(
                                "gateway_namespace", "adp"
                            )
                        }
                    },
                    "podSelector": {"matchLabels": {"app": "bedrockgateway"}},
                }
            )
        docs.append(
            obj(
                "NetworkPolicy",
                name,
                namespace,
                spec={
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": name}},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [
                        {"from": peers, "ports": [{"port": port, "protocol": "TCP"}]}
                    ],
                    "egress": [
                        dns_egress(env),
                        {
                            "to": peers[:2],
                            "ports": [
                                {"port": 8000, "protocol": "TCP"},
                                {"port": 8081, "protocol": "TCP"},
                                {"port": 9090, "protocol": "TCP"},
                                {"port": 46581, "protocol": "TCP"},
                            ],
                        },
                        {
                            "to": [
                                {
                                    "ipBlock": {
                                        "cidr": "0.0.0.0/0",
                                        "except": ["169.254.0.0/16"],
                                    }
                                }
                            ],
                            "ports": [
                                {"port": 443, "protocol": "TCP"},
                                {"port": 5432, "protocol": "TCP"},
                            ],
                        },
                    ],
                },
            )
        )
    for doc in docs:
        if doc["kind"] == "Service":
            # AWS network-policy resolution matches peer podSelector labels
            # against Service selectors before allowing the pre-DNAT ClusterIP.
            # The original selector is also referenced by the Deployment.
            # Copy it: mutating it would change an immutable Deployment field
            # when upgrading an installation created before this service fix.
            doc["spec"]["selector"] = {**doc["spec"]["selector"], LABEL: owner}
        if (
            doc["kind"] == "ServiceAccount"
            and doc["metadata"]["name"] != "superplane-platform-monitor"
            and not control_plane_only
        ):
            role = (
                "skypilot-api"
                if doc["metadata"]["name"] == "skypilot-api"
                else "control-plane"
            )
            doc["metadata"]["annotations"] = {
                "eks.amazonaws.com/role-arn": f"arn:aws:iam::{env['account_id']}:role/adp-{env['environment']}-superplane-{role}"
            }
    for deployment_doc in [d for d in docs if d["kind"] == "Deployment"]:
        name = deployment_doc["metadata"]["name"]
        docs.append(
            obj(
                "PodDisruptionBudget",
                name,
                deployment_doc["metadata"]["namespace"],
                spec={
                    "minAvailable": 1,
                    "selector": {
                        "matchLabels": {"app.kubernetes.io/name": name, LABEL: owner},
                        # Jobs share API labels. The Deployment controller adds
                        # this label to service pods, but Job pods lack it.
                        "matchExpressions": [
                            {"key": "pod-template-hash", "operator": "Exists"}
                        ],
                    },
                },
            )
        )
    return docs


def migration_job(env: dict, lock: dict, run_id: str) -> dict:
    docs = render(env, lock, control_plane_only=control_plane_mode(env))
    api = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )
    pod = api["spec"]["template"]
    container = pod["spec"]["containers"][0]
    container["name"] = "migrate"
    container["command"] = ["python", "-m", "app.installation", "migrate"]
    container.pop("ports")
    for key in ("readinessProbe", "livenessProbe", "startupProbe"):
        container.pop(key)
    container["env"] = [
        {
            "name": "DATABASE_URL",
            "valueFrom": {
                "secretKeyRef": {"name": "superplane-db", "key": "migration-url"}
            },
        },
        {
            "name": "SUPERPLANE_DATABASE_CA",
            "valueFrom": {"secretKeyRef": {"name": "superplane-db", "key": "ca-pem"}},
        },
        {"name": "SUPERPLANE_DB_SCHEMA", "value": env["database"]["schema"]},
        {
            "name": "SUPERPLANE_EXPECTED_SCHEMA",
            "value": lock["schema"]["observed"]["head"],
        },
    ]
    pod["spec"]["restartPolicy"] = "Never"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"superplane-migrate-{run_id}",
            "namespace": env["namespace"],
            "labels": {LABEL: identity(env)},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": env["timeout_seconds"],
            "template": pod,
        },
    }


def bootstrap_job(env: dict, lock: dict, run_id: str) -> dict:
    job = migration_job(env, lock, run_id)
    job["metadata"]["name"] = f"superplane-bootstrap-{run_id}"
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["name"] = "bootstrap"
    container["command"] = ["python", "-m", "app.installation_bootstrap"]
    # In control-plane-only mode, workspace fields are deferred; the bootstrap job
    # creates a domain org and administrator grant without a workspace binding.
    # workspace_id, cluster_id, workspace_cluster and workspace_namespace are only
    # added when present (i.e. full installation mode).
    cp_only = control_plane_mode(env)
    always_config_keys = ("adp_org_id", "org_id", "origin")
    workspace_config_keys = (
        "workspace_id",
        "cluster_id",
        "workspace_cluster",
        "workspace_namespace",
    )
    config = {key: env[key] for key in always_config_keys}
    config["control_plane_only"] = cp_only
    if not cp_only:
        config.update({key: env[key] for key in workspace_config_keys})
        config["workspace_cluster_arn"] = (
            f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['workspace_cluster']}"
        )
    container["env"].extend(
        [
            {"name": "DOMAIN_AUTH_ENFORCED", "value": "true"},
            {"name": "COGNITO_ENABLED", "value": "true"},
            {"name": "COGNITO_ISSUER", "value": env["auth"]["issuer"]},
            {
                "name": "COGNITO_JWKS_URL",
                "value": env["auth"]["issuer"] + "/.well-known/jwks.json",
            },
            {
                "name": "DOMAIN_AUTH_ALLOWED_CLIENT_IDS",
                "value": json.dumps(env["auth"]["client_ids"]),
            },
            {"name": "SUPERPLANE_BOOTSTRAP_CONFIG", "value": json.dumps(config)},
            {
                "name": "SUPERPLANE_BOOTSTRAP_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {"name": job["metadata"]["name"], "key": "token"}
                },
            },
        ]
    )
    return job

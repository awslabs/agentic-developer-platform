"""Explicit release/projection contract for the isolated trusted executor.

Projection references select existing ADP authority; the installer never creates
an execution run, paid operation, admission lease or provider credential.
"""

import re

from .config import image, require, https_origin

COMPONENT = "superplane-executor"


def read_only_workspace_rules(status):
    """Same workspace-read boundary enforced by the Go registration manager."""
    try:
        if status.get("incomplete") is not False or status.get("evaluationError"):
            return False
        for rule in status["resourceRules"]:
            if set(rule["resources"]) & {"*", "secrets", "serviceaccounts/token"}:
                return False
            for verb in rule["verbs"]:
                if verb in {"get", "list", "watch"}:
                    allowed = {
                        "": {"nodes", "namespaces", "pods", "pods/log"},
                        "apps": {"deployments", "replicasets"},
                        "batch": {"jobs"},
                        "superplane.ai": {"nodepools", "superplanenodes"},
                    }
                    groups = rule["apiGroups"]
                    if len(groups) != 1 or not set(rule["resources"]) <= allowed.get(
                        groups[0], set()
                    ):
                        return False
                    continue
                groups, resources = rule["apiGroups"], set(rule["resources"])
                review = (
                    groups == ["authorization.k8s.io"]
                    and resources
                    <= {"selfsubjectaccessreviews", "selfsubjectrulesreviews"}
                ) or (
                    groups == ["authentication.k8s.io"]
                    and resources == {"selfsubjectreviews"}
                )
                if verb != "create" or not resources or not review:
                    return False
        return all(
            set(rule["verbs"]) <= {"get"} for rule in status.get("nonResourceRules", [])
        )
    except (AttributeError, KeyError, TypeError):
        return False


def validate_execution(env, lock):
    config = env.get("execution")
    if config is None:
        return
    require(
        isinstance(config, dict),
        "execution must contain explicit projection references",
    )
    require(
        set(config)
        == {
            "authority_endpoint",
            "role_arn",
            "provider_role_arn",
            "run_projection_secret",
            "database_secret",
            "workspace_credentials_secret",
        },
        "execution requires authority_endpoint, role_arn, provider_role_arn, run_projection_secret, database_secret and workspace_credentials_secret",
    )
    require(
        https_origin(config["authority_endpoint"]),
        "execution authority must be an HTTPS origin",
    )
    for key in (
        "run_projection_secret",
        "database_secret",
        "workspace_credentials_secret",
    ):
        require(
            isinstance(config[key], str)
            and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", config[key]),
            "execution projection must name a Kubernetes Secret",
        )
    require(
        config["workspace_credentials_secret"] != "superplane-workspace-access",
        "Executor workspace credentials must be separate from the read-only manager credentials",
    )
    for key in ("role_arn", "provider_role_arn"):
        require(
            isinstance(config[key], str)
            and re.fullmatch(
                r"arn:aws:iam::"
                + re.escape(str(env.get("account_id", "")))
                + r":role/[A-Za-z0-9+=,.@_/-]+",
                config[key],
            ),
            "execution requires an existing role in the selected account",
        )
    if lock is not None:
        source = lock.get("image_sources", {}).get(COMPONENT, {})
        require(
            COMPONENT not in lock.get("pending_images", {})
            and re.fullmatch(
                r"sha256:[a-f0-9]{64}", str(lock.get("images", {}).get(COMPONENT, ""))
            ),
            "Trusted executor release image is unresolved",
        )
        require(
            source.get("registry")
            == f"{env['account_id']}.dkr.ecr.{env['region']}.amazonaws.com"
            and source.get("repository") == "adp-" + COMPONENT
            and re.fullmatch(r"[a-f0-9]{40}", str(source.get("source_revision", ""))),
            "Trusted executor image provenance mismatch",
        )


def attach_executor(pod, docs, env, lock):
    config = env["execution"]
    for doc in docs:
        if (
            doc["kind"] == "ServiceAccount"
            and doc["metadata"]["name"] == "superplane-controller"
        ):
            doc["metadata"]["annotations"] = {
                "eks.amazonaws.com/role-arn": config["role_arn"]
            }
    shared = {
        "SUPERPLANE_EXECUTION_SOCKET": "/execution/rpc.sock",
        "SUPERPLANE_EXECUTION_CREDENTIALS_DIR": "/execution/tokens",
        "SUPERPLANE_CONTROLLER_INSTANCE_FILE": "/controller-instance/id",
    }
    pod["containers"][0]["env"].extend(
        {"name": k, "value": v} for k, v in shared.items()
    )
    pod["containers"][0]["volumeMounts"].extend(
        [
            {"name": "execution", "mountPath": "/execution", "readOnly": True},
            {"name": "controller-instance", "mountPath": "/controller-instance"},
        ]
    )
    pod["volumes"].extend(
        [
            {"name": name, "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}}
            for name in ("execution", "controller-instance", "executor-tmp")
        ]
    )
    for name, secret in (
        ("execution-run", config["run_projection_secret"]),
        ("execution-database", config["database_secret"]),
        ("execution-skypilot", "superplane-skypilot-auth"),
        ("execution-workspace", config["workspace_credentials_secret"]),
    ):
        pod["volumes"].append(
            {
                "name": name,
                "secret": {
                    "secretName": secret,
                    "defaultMode": 288,
                    "optional": True,
                },
            }
        )
    values = {
        **shared,
        "AWS_REGION": env["region"],
        "AWS_EC2_METADATA_DISABLED": "true",
        "ADP_EXECUTION_AUTHORITY_ENDPOINT": config["authority_endpoint"],
        "ADP_RUN_CREDENTIAL_FILE": "/run-authority/run-credential",
        "ADP_WORKLOAD_TOKEN_FILE": "/run-authority/workload-token",
        "SUPERPLANE_EXECUTION_OPERATION_FILE": "/run-authority/operations.json",
        "SUPERPLANE_DOMAIN_DSN_FILE": "/execution-database/domain-dsn",
        "SUPERPLANE_EXECUTION_DSN_FILE": "/execution-database/execution-dsn",
        "SUPERPLANE_WORKER_GID": "65532",
        "SUPERPLANE_WORKSPACE_CREDENTIALS_DIR": "/execution-workspace",
        "SUPERPLANE_MANAGEMENT_API_SERVER": env.get("management_api_server", ""),
        "SKYPILOT_URL": f"http://skypilot-api.{env['skypilot_namespace']}.svc.cluster.local:46580",
        "SKYPILOT_SERVICE_TOKEN_FILE": "/skypilot-credential/token",
        "SUPERPLANE_RELEASE_ID": pod["containers"][0]["env"][1]["value"],
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    pod["containers"].append(
        {
            "name": COMPONENT,
            "image": image(lock, COMPONENT),
            "env": [{"name": k, "value": v} for k, v in values.items()]
            + [
                {
                    "name": "SUPERPLANE_REGISTRY_SUBMITTER_ID",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "superplane-observation",
                            "key": "controller-submitter-id",
                        }
                    },
                }
            ],
            "securityContext": {
                "runAsUser": 65531,
                "runAsGroup": 65532,
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
            "volumeMounts": [
                {"name": "execution", "mountPath": "/execution"},
                {"name": "executor-tmp", "mountPath": "/tmp"},
                *[
                    {"name": name, "mountPath": path, "readOnly": True}
                    for name, path in (
                        ("controller-instance", "/controller-instance"),
                        ("execution-run", "/run-authority"),
                        ("execution-database", "/execution-database"),
                        ("execution-skypilot", "/skypilot-credential"),
                        ("execution-workspace", "/execution-workspace"),
                    )
                ],
            ],
        }
    )

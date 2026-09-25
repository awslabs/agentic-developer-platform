"""Explicit privileged credential-controller installation, separate from consumers."""

from copy import deepcopy
import importlib
import json
from pathlib import Path
import re
import sys

from .config import LABEL, MODULE, Refusal, digest, identity, image, require

NAME = "superplane-credential-controller"


def _read_authority(authority_id, document):
    """Use the runtime schema from this installer checkout, not an ambient wheel.

    The source installer is invoked with only the app root on Python's path.
    Bootstrap and contracts are sibling packages, also shipped in the executor
    image. Keep their dependency composition here rather than copying validation
    rules or relying on pytest/CLI environment path side effects.
    """
    packages = {
        "workspace_provisioning": MODULE / "workspace_provisioning",
        "superplane_bootstrap": MODULE / "workspace_bootstrap/superplane_bootstrap",
        "superplane_contracts": MODULE / "contracts/superplane_contracts",
    }
    for name, module in tuple(sys.modules.items()):
        for package, directory in packages.items():
            if name == package or name.startswith(package + "."):
                location = getattr(module, "__file__", None)
                require(
                    location
                    and Path(location).resolve().is_relative_to(directory.resolve()),
                    "credential validation cannot mix source checkouts or installed package versions",
                )
    paths = list(sys.path)
    try:
        sys.path[:0] = [
            str(MODULE),
            str(MODULE / "workspace_bootstrap"),
            str(MODULE / "contracts"),
        ]
        try:
            registry = importlib.import_module(
                "workspace_provisioning.credential_controller.registry"
            )
            errors = importlib.import_module("superplane_bootstrap.errors")
        except ImportError:
            raise Refusal(
                "same-checkout credential validation dependencies are unavailable"
            ) from None
        try:
            return registry.Authority.read(authority_id, json.dumps(document))
        except (errors.BootstrapRefused, TypeError, ValueError):
            raise Refusal(
                "installed credential authority document is invalid"
            ) from None
    finally:
        sys.path[:] = paths


def validate(env, lock):
    config = env.get("credential_controller")
    if config is None:
        return
    require(
        isinstance(config, dict)
        and set(config)
        == {
            "role_arn",
            "database_secret",
            "registration_database_secret",
            "authorities",
        },
        "credential_controller requires explicit installed role, distinct database projections and authorities",
    )
    require(
        config["database_secret"] != config["registration_database_secret"],
        "renewal may not mount installer authority registration credentials",
    )
    for field in ("database_secret", "registration_database_secret"):
        require(
            isinstance(config[field], str)
            and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", config[field]),
            "credential controller database projection name is invalid",
        )
    require(
        isinstance(config["authorities"], list)
        and 1 <= len(config["authorities"]) <= 64,
        "credential controller requires bounded explicit installed authorities",
    )
    seen = set()
    for entry in config["authorities"]:
        require(
            isinstance(entry, dict) and set(entry) == {"authority_id", "document"},
            "credential authority registration fields differ",
        )
        authority = _read_authority(entry["authority_id"], entry["document"])
        doc = authority.document
        require(
            authority.authority_id not in seen and doc["org_id"] == env["org_id"],
            "credential authority organization or unique identity differs",
        )
        seen.add(authority.authority_id)
        require(
            doc["controller_role_arn"] == config["role_arn"]
            and doc["management_target"]["account_id"] == env["account_id"]
            and doc["management_target"]["region"] == env["region"]
            and doc["management_target"]["cluster_name"] == env["cluster"],
            "credential authority must use the selected management account/cluster and role",
        )
        require(
            doc["projection"]["namespace"] == env["namespace"]
            and doc["projection"]["reader_secret"] == "superplane-workspace-access",
            "reader projection must be the actual installed manager mount",
        )
        if env.get("execution"):
            require(
                doc["projection"]["mutator_secret"]
                == env["execution"]["workspace_credentials_secret"],
                "mutator projection must be the installed executor mount",
            )
    if lock is not None:
        source = lock.get("image_sources", {}).get("superplane-executor", {})
        require(
            re.fullmatch(
                r"sha256:[a-f0-9]{64}",
                str(lock.get("images", {}).get("superplane-executor", "")),
            )
            and "superplane-executor" not in lock.get("pending_images", {}),
            "credential controller requires a built reviewed executor image",
        )
        require(
            source.get("registry")
            == f"{env['account_id']}.dkr.ecr.{env['region']}.amazonaws.com"
            and source.get("repository") == "adp-superplane-executor"
            and re.fullmatch(r"[a-f0-9]{40}", str(source.get("source_revision", ""))),
            "credential controller release provenance differs",
        )


def documents(env, lock):
    config = env["credential_controller"]
    owner, release = identity(env), digest(lock)
    labels = {
        LABEL: owner,
        "app.kubernetes.io/name": NAME,
        "app.kubernetes.io/part-of": "adp-superplane",
    }
    metadata = {"name": NAME, "namespace": env["namespace"], "labels": labels}
    config_name = NAME + "-" + release[:12]
    service_account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            **metadata,
            "annotations": {"eks.amazonaws.com/role-arn": config["role_arn"]},
        },
        "automountServiceAccountToken": False,
    }
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {**metadata, "name": config_name},
        "immutable": True,
        "data": {"authorities.json": json.dumps(config["authorities"], sort_keys=True)},
    }
    pod = {
        "serviceAccountName": NAME,
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsUser": 65531,
            "runAsGroup": 65532,
            "fsGroup": 65532,
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": NAME,
                "image": image(lock, "superplane-executor"),
                "command": [
                    "python",
                    "-m",
                    "workspace_provisioning.credential_controller",
                ],
                "env": [
                    {"name": name, "value": value}
                    for name, value in {
                        "SUPERPLANE_CREDENTIAL_CONTROLLER_CONFIG": "/configuration/authorities.json",
                        "SUPERPLANE_DOMAIN_DSN_FILE": "/database/domain-dsn",
                        "SUPERPLANE_DATABASE_CA_FILE": "/database/ca-pem",
                        "SUPERPLANE_DOMAIN_SCHEMA": env["database"]["schema"],
                        "AWS_REGION": env["region"],
                        "AWS_EC2_METADATA_DISABLED": "true",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONUNBUFFERED": "1",
                    }.items()
                ],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
                "readinessProbe": {
                    "exec": {
                        "command": [
                            "python",
                            "-c",
                            "from pathlib import Path; import time; assert 0 <= time.time()-float(Path('/tmp/credential-ready').read_text()) < 60",
                        ]
                    },
                    "periodSeconds": 10,
                },
                "volumeMounts": [
                    {
                        "name": "configuration",
                        "mountPath": "/configuration",
                        "readOnly": True,
                    },
                    {"name": "database", "mountPath": "/database", "readOnly": True},
                    {"name": "temporary", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {
                "name": "configuration",
                "configMap": {"name": config_name, "defaultMode": 288},
            },
            {
                "name": "database",
                "secret": {"secretName": config["database_secret"], "defaultMode": 288},
            },
            {
                "name": "temporary",
                "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"},
            },
        ],
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {"adp.aws-e.io/release": release},
                },
                "spec": pod,
            },
        },
    }
    from .manifests import dns_egress

    network = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": metadata,
        "spec": {
            "podSelector": {"matchLabels": labels},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                dns_egress(env),
                {
                    "to": [
                        {"ipBlock": {"cidr": "0.0.0.0/0", "except": ["169.254.0.0/16"]}}
                    ],
                    "ports": [
                        {"protocol": "TCP", "port": 443},
                        {"protocol": "TCP", "port": 5432},
                    ],
                },
            ],
        },
    }
    return [service_account, config_map, network, deployment]


def registration_job(env, lock, run_id):
    deployment = documents(env, lock)[-1]
    template = deepcopy(deployment["spec"]["template"])
    pod = template["spec"]
    pod["restartPolicy"] = "Never"
    pod["containers"][0]["args"] = ["--register"]
    pod["containers"][0].pop("readinessProbe")
    pod["volumes"][1]["secret"]["secretName"] = env["credential_controller"][
        "registration_database_secret"
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            **deployment["metadata"],
            "name": "sp-credential-register-" + str(run_id)[:24],
        },
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 600, "template": template},
    }

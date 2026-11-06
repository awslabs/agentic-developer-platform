"""Install a bounded reader renewer without granting the manager mutation rights."""

import base64
import json
import re

from .config import LABEL, digest, https_origin, identity, image, require

NAME = "superplane-dedicated-readers"


def validate(env, lock):
    config = env.get("dedicated_readers")
    if config is None:
        return
    require(
        env.get("control_plane_only") is True,
        "Dedicated renewal requires management installation; static full-install credentials cannot share its projection",
    )
    require(
        set(config)
        == {"role_arn", "role_id", "auth_secret", "projection", "workspaces"},
        "Dedicated reader configuration fields differ",
    )
    require(
        config["role_arn"]
        == f"arn:aws:iam::{env['account_id']}:role/adp-{env['environment']}-spr-{identity(env)}"
        and re.fullmatch(r"AROA[A-Z0-9]{17}", config["role_id"]),
        "Dedicated reader IAM identity differs",
    )
    require(
        config["auth_secret"]
        == f"adp/{env['environment']}/superplane/dedicated-reader-auth",
        "Dedicated reader auth source differs",
    )
    projection = config["projection"]
    require(
        set(projection) == {"namespace", "secret_name", "secret_uid", "endpoint"}
        and projection["namespace"] == env["namespace"]
        and projection["secret_name"] == "superplane-workspace-access"
        and projection["secret_uid"]
        and https_origin(projection["endpoint"]),
        "Dedicated reader projection must pin the real manager Secret",
    )
    require(
        isinstance(config["workspaces"], list) and 0 < len(config["workspaces"]) <= 32,
        "Dedicated readers require bounded explicit workspaces",
    )
    seen = set()
    for entry in config["workspaces"]:
        require(
            set(entry)
            == {
                "workspace_id",
                "org_id",
                "cluster_name",
                "cluster_arn",
                "endpoint",
                "certificate_authority_data",
                "namespace",
                "access_entry_arn",
                "audience",
            },
            "Dedicated reader workspace fields differ",
        )
        workspace = entry["workspace_id"]
        require(
            re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", workspace)
            and workspace not in seen
            and entry["org_id"] == env["org_id"],
            "Dedicated reader workspace scope differs",
        )
        seen.add(workspace)
        require(
            entry["cluster_arn"]
            == f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{entry['cluster_name']}"
            and entry["cluster_name"] != env["cluster"]
            and https_origin(entry["endpoint"])
            and entry["endpoint"] != projection["endpoint"]
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", entry["namespace"]),
            "Dedicated reader cluster differs",
        )
        require(
            entry["access_entry_arn"].startswith(
                f"arn:aws:eks:{env['region']}:{env['account_id']}:access-entry/{entry['cluster_name']}/role/{env['account_id']}/adp-{env['environment']}-spr-{identity(env)}/"
            )
            and entry["audience"] == "https://kubernetes.default.svc",
            "Dedicated reader access entry or audience differs",
        )
        try:
            require(
                b"BEGIN CERTIFICATE"
                in base64.b64decode(entry["certificate_authority_data"], validate=True),
                "Dedicated reader CA is invalid",
            )
        except ValueError:
            require(False, "Dedicated reader CA is invalid")


def prepare_projection(installer):
    """Explicit prerequisite under the native owner lock; no role or IAM writes."""
    installer.reconcile_lock_attempt()
    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "superplane-workspace-access",
            "namespace": installer.env["namespace"],
            "labels": {LABEL: installer.owner},
        },
        "type": "Opaque",
        "data": {},
    }
    observed = installer.existing(document)
    if observed is None:
        installer.apply([document])
        observed = installer.existing(document)
    require(
        observed["metadata"].get("labels", {}).get(LABEL) == installer.owner
        and observed["metadata"].get("uid"),
        "Reader projection belongs to another installation",
    )
    installer.receipt["dedicated_reader_projection"] = {
        key: observed["metadata"][key] for key in ("name", "namespace", "uid")
    }
    installer.save()
    return installer.receipt["dedicated_reader_projection"]


def load_material(installer):
    config = installer.env.get("dedicated_readers")
    if config is None:
        return
    metadata = installer.json(
        installer.aws(
            "secretsmanager", "describe-secret", "--secret-id", config["auth_secret"]
        )
    )
    require(
        not metadata.get("DeletedDate")
        and {"Key": "SuperplaneInstallation", "Value": installer.owner}
        in metadata.get("Tags", []),
        "Dedicated reader auth is not installation-owned",
    )
    version = installer.receipt.get("secret_versions", {}).get("dedicated_reader_auth")
    response = installer.json(
        installer.aws(
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            config["auth_secret"],
            *(["--version-id", version] if version else []),
        )
    )
    material = json.loads(response["SecretString"])
    require(
        set(material) == {"credential", "signing_key"}
        and all(isinstance(v, str) and len(v) >= 48 for v in material.values()),
        "Dedicated reader auth fields differ",
    )
    grants = json.loads(installer.secret_values["observation"]["submitters"])
    require(
        all(
            g["credential"] != material["credential"]
            and g["submitter_id"] != installer.owner + "-reader"
            for g in grants
        ),
        "Dedicated reader requires a distinct credential",
    )
    grants.append(
        {
            "submitter_id": installer.owner + "-reader",
            **material,
            "workspaces": [],
            "lease_scopes": [
                f"workspace_reader/{installer.env['org_id']}/{w['workspace_id']}"
                for w in config["workspaces"]
            ],
        }
    )
    installer.secret_values["observation"]["submitters"] = json.dumps(grants)
    installer.secret_values["dedicated_reader_auth"] = {
        "credential": material["credential"]
    }
    installer.secret_versions["dedicated_reader_auth"] = response["VersionId"]
    management = installer.json(
        installer.aws("eks", "describe-cluster", "--name", installer.env["cluster"])
    )["cluster"]
    require(
        management["endpoint"] == config["projection"]["endpoint"],
        "Dedicated reader management endpoint differs",
    )
    role = installer.json(
        installer.aws(
            "iam", "get-role", "--role-name", config["role_arn"].rsplit("/", 1)[1]
        )
    )["Role"]
    require(
        role["RoleId"] == config["role_id"] and role["Arn"] == config["role_arn"],
        "Dedicated reader role was replaced",
    )
    projection = installer.existing(
        {
            "kind": "Secret",
            "metadata": {
                "name": config["projection"]["secret_name"],
                "namespace": config["projection"]["namespace"],
            },
        }
    )
    require(
        projection is not None
        and projection["metadata"]["uid"] == config["projection"]["secret_uid"]
        and projection["metadata"].get("labels", {}).get(LABEL) == installer.owner,
        "Dedicated reader projection identity differs",
    )


def documents(env, lock):
    config = env["dedicated_readers"]
    owner = identity(env)
    labels = {
        LABEL: owner,
        "app.kubernetes.io/name": NAME,
        "app.kubernetes.io/part-of": "adp-superplane",
    }
    metadata = {"name": NAME, "namespace": env["namespace"], "labels": labels}
    runtime = {
        **{k: v for k, v in config.items() if k != "auth_secret"},
        "account_id": env["account_id"],
        "region": env["region"],
        "installation_id": owner,
        "api_url": f"http://superplane-api.{env['namespace']}.svc.cluster.local:8000",
        "credential_file": "/reader-auth/credential",
    }
    config_name = NAME + "-" + digest(runtime)[:12]
    sa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            **metadata,
            "annotations": {"eks.amazonaws.com/role-arn": config["role_arn"]},
        },
        "automountServiceAccountToken": False,
    }
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": metadata,
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "verbs": ["get", "update"],
                "resourceNames": ["superplane-workspace-access"],
            }
        ],
    }
    role_binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": metadata,
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": NAME,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": NAME, "namespace": env["namespace"]}
        ],
    }
    configuration = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {**metadata, "name": config_name},
        "immutable": True,
        "data": {"config.json": json.dumps(runtime, sort_keys=True)},
    }
    pod = {
        "serviceAccountName": NAME,
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "fsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": NAME,
                "image": image(lock, "superplane-api"),
                "command": ["python", "-m", "workspace_provisioning.dedicated_readers"],
                "env": [
                    {"name": k, "value": v}
                    for k, v in {
                        "SUPERPLANE_DEDICATED_READER_CONFIG": "/configuration/config.json",
                        "AWS_REGION": env["region"],
                        "AWS_DEFAULT_REGION": env["region"],
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
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
                "readinessProbe": {
                    "exec": {
                        "command": [
                            "python",
                            "-c",
                            "from pathlib import Path; import time; assert 0 <= time.time()-float(Path('/tmp/reader-ready').read_text()) < 75",
                        ]
                    },
                    "periodSeconds": 10,
                    "timeoutSeconds": 5,
                },
                "volumeMounts": [
                    {
                        "name": "configuration",
                        "mountPath": "/configuration",
                        "readOnly": True,
                    },
                    {"name": "auth", "mountPath": "/reader-auth", "readOnly": True},
                    {
                        "name": "management",
                        "mountPath": "/var/run/secrets/reader-management",
                        "readOnly": True,
                    },
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "configuration", "configMap": {"name": config_name}},
            {
                "name": "auth",
                "secret": {"secretName": "superplane-reader-auth", "defaultMode": 288},
            },
            {
                "name": "management",
                "projected": {
                    "defaultMode": 288,
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "path": "token",
                                "expirationSeconds": 3600,
                            }
                        },
                        {
                            "configMap": {
                                "name": "kube-root-ca.crt",
                                "items": [{"key": "ca.crt", "path": "ca.crt"}],
                            }
                        },
                    ],
                },
            },
            {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
        ],
    }
    from .manifests import dns_egress, runtime_annotations

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": runtime_annotations(digest(lock)),
                },
                "spec": pod,
            },
        },
    }
    network = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": metadata,
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                dns_egress(env),
                {"ports": [{"port": 443, "protocol": "TCP"}]},
                {
                    "to": [
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/name": "superplane-api",
                                    LABEL: owner,
                                }
                            }
                        }
                    ],
                    "ports": [{"port": 8000, "protocol": "TCP"}],
                },
            ],
        },
    }
    return [sa, role, role_binding, configuration, deployment, network]

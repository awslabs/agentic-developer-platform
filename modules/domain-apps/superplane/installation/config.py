"""Validate installation inputs before invoking any external tool."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

MODULE = Path(__file__).resolve().parents[1]
COMPONENTS = (
    "superplane-api",
    "superplane-controller",
    "superplane-platform-monitor",
    "skypilot-api",
)
LABEL = "adp.aws-e.io/installation"
SHA = re.compile(r"[0-9a-f]{40}")
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,39}")
SCHEMA = re.compile(r"[a-z][a-z0-9_]{0,62}")


class Refusal(Exception):
    """Actionable failure containing names, never secret values or tool output."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise Refusal(message)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        raise Refusal(f"Cannot read configuration file: {path.name}") from None
    require(isinstance(value, dict), f"{path.name} must contain a mapping")
    return value


def https_origin(value: object) -> bool:
    if not isinstance(value, str):
        return False
    u = urlsplit(value)
    return bool(
        u.scheme == "https"
        and u.hostname
        and not u.username
        and not u.password
        and u.path in ("", "/")
        and not u.query
        and not u.fragment
    )


def validate(env: dict, lock: dict) -> None:
    """No credentials, shell fragments, caller-selected commands or guessed targets."""
    require(env.get("version") == 1, "environment.version must be 1")
    allowed = {
        "version",
        "environment",
        "account_id",
        "region",
        "namespace",
        "skypilot_namespace",
        "cluster",
        "workspace_cluster",
        "workspace_namespace",
        "origin",
        "org_id",
        "adp_org_id",
        "workspace_id",
        "cluster_id",
        "auth",
        "database",
        "secrets",
        "timeout_seconds",
        "network_policy_enforced",
        "controller_ownership",
    }
    require(
        set(env) <= allowed,
        "Unknown environment fields; secrets belong in Secrets Manager",
    )
    require(
        re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}", str(env.get("adp_org_id", ""))
        ),
        "adp_org_id must name the actual ADP organization",
    )
    require(
        set(env.get("database", {}))
        <= {
            "identifier",
            "database",
            "schema",
            "skypilot_schema",
            "backup_id",
            "migration_owner",
            "restore_owner",
            "backup_owner",
        },
        "Unknown database fields; never place credentials in the environment file",
    )
    require(
        set(env.get("auth", {})) <= {"issuer", "client_ids"},
        "Unknown authentication fields; tokens must not be written to configuration",
    )
    for key in (
        "environment",
        "namespace",
        "skypilot_namespace",
        "cluster",
        "workspace_cluster",
        "workspace_namespace",
    ):
        require(
            isinstance(env.get(key), str) and IDENTIFIER.fullmatch(env[key]),
            f"Invalid {key}",
        )
    require(
        env["namespace"] != env["skypilot_namespace"],
        "Domain namespaces must be distinct",
    )
    require(
        not {env["namespace"], env["skypilot_namespace"]}
        & {"adp", "default", "kube-system", "kube-public", "kube-node-lease"},
        "A core namespace cannot host domain resources",
    )
    require(
        env["cluster"] != env["workspace_cluster"],
        "Workspace controller must not target the ADP management cluster",
    )
    require(
        isinstance(env.get("account_id"), str)
        and re.fullmatch(r"\d{12}", env["account_id"]),
        "account_id must be explicit",
    )
    require(
        re.fullmatch(r"[a-z]{2}-[a-z]+-\d", str(env.get("region", ""))),
        "region must be explicit",
    )
    require(
        https_origin(env.get("origin")),
        "origin must be an HTTPS origin without credentials",
    )
    for key in ("org_id", "workspace_id", "cluster_id"):
        require(
            re.fullmatch(
                r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", str(env.get(key, ""))
            ),
            f"{key} must be an immutable UUID",
        )
    auth = env.get("auth", {})
    require(
        re.fullmatch(
            r"https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9_-]+",
            str(auth.get("issuer", "")),
        ),
        "auth.issuer must name the ADP Cognito user pool",
    )
    require(
        isinstance(auth.get("client_ids"), list)
        and auth["client_ids"]
        and all(re.fullmatch(r"[A-Za-z0-9_-]+", str(x)) for x in auth["client_ids"]),
        "auth.client_ids must name the allowed ADP clients",
    )
    db = env.get("database", {})
    for key in ("schema", "skypilot_schema"):
        require(
            SCHEMA.fullmatch(str(db.get(key, "")))
            and db[key] not in {"public", "pg_catalog", "information_schema"}
            and not db[key].startswith("pg_"),
            f"database.{key} must be an isolated schema",
        )
    require(
        db["schema"] != db["skypilot_schema"], "API and SkyPilot need separate schemas"
    )
    for key in (
        "identifier",
        "database",
        "backup_id",
        "migration_owner",
        "restore_owner",
        "backup_owner",
    ):
        require(
            isinstance(db.get(key), str)
            and db[key].strip()
            and db[key].lower() not in {"tbd", "unknown", "todo"},
            f"database.{key} is required",
        )
    require(
        set(env.get("secrets", {})) == {"database", "observation", "workspace_access"},
        "Secret references must be exactly the three documented domain-owned references",
    )
    for key in ("database", "observation", "workspace_access"):
        value = env.get("secrets", {}).get(key, "")
        require(
            isinstance(value, str)
            and value.startswith(f"adp/{env['environment']}/superplane/")
            and re.fullmatch(r"[A-Za-z0-9/_+=.@-]+", value),
            f"secrets.{key} must name an environment-owned Secrets Manager secret",
        )
    for key in ("timeout_seconds",):
        require(
            type(env.get(key)) is int and 30 <= env[key] <= 3600,
            f"{key} must be 30–3600",
        )
    require(
        SHA.fullmatch(str(lock.get("source_revision", ""))),
        "release lock needs the exact maintained source_revision",
    )
    require(
        lock.get("schema", {}).get("single_head") is True,
        "release schema must have a single head",
    )
    head = lock["schema"].get("observed", {}).get("head")
    require(
        head == "015_add_adp_org_binding",
        "release schema must include U11c013, U7b014 and the U23 identity binding",
    )
    sources = lock.get("image_sources", {})
    base = load(MODULE / "releases/superplane.lock.yaml")
    for component in COMPONENTS:
        value = lock.get("images", {}).get(component)
        require(
            component not in lock.get("pending_images", {}),
            f"Release image is unresolved: {component}",
        )
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)),
            f"Immutable digest required: {component}",
        )
        source = sources.get(component, {})
        if component == "skypilot-api":
            require(
                value == base["images"][component]
                and source.get("registry")
                == base["image_sources"][component]["registry"]
                and source.get("repository")
                == base["image_sources"][component]["repository"],
                "Preserve the reviewed SkyPilot runtime pin",
            )
        else:
            require(
                source.get("registry")
                == f"{env['account_id']}.dkr.ecr.{env['region']}.amazonaws.com",
                f"Wrong ECR registry: {component}",
            )
            require(
                source.get("repository") == f"adp-{component}",
                f"Wrong ECR repository: {component}",
            )
            require(
                source.get("source_revision") == lock["source_revision"],
                f"Image provenance must bind final source: {component}",
            )
    require(
        env.get("network_policy_enforced") is True,
        "The selected cluster must enforce Kubernetes NetworkPolicy",
    )
    require(
        env.get("controller_ownership") == "single-workspace-controller",
        "Explicit single-controller ownership is required",
    )


def image(lock: dict, component: str) -> str:
    source = lock["image_sources"][component]
    return f"{source['registry']}/{source['repository']}@{lock['images'][component]}"


def identity(env: dict) -> str:
    return digest(
        {k: env[k] for k in ("account_id", "region", "environment", "cluster")}
    )[:24]

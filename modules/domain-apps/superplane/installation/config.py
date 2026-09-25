"""Validate installation inputs before invoking any external tool."""

from __future__ import annotations

import hashlib
import ipaddress
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
# EKS cluster names: 1–100 chars, alphanumeric/underscore/hyphen, alphanumeric first.
# Wider than IDENTIFIER to accept the Terraform-generated suffix form used by workspace clusters.
EKS_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
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


def control_plane_mode(env: dict, selected: bool = False) -> bool:
    require(
        type(env.get("control_plane_only", False)) is bool,
        "control_plane_only must be a boolean",
    )
    return selected or env.get("control_plane_only", False)


def cluster_dns_address(env: dict):
    """An optional exact native resolver, never an arbitrary DNS egress range."""
    if "cluster_dns_ip" not in env:
        return None
    try:
        value = env["cluster_dns_ip"]
        require(isinstance(value, str), "cluster_dns_ip must be an IP address")
        address = ipaddress.ip_address(value)
        require(
            not (
                address.is_unspecified
                or address.is_multicast
                or address.is_loopback
                or address.is_link_local
            ),
            "cluster_dns_ip must be the EKS service-network resolver",
        )
        return address
    except ValueError:
        raise Refusal(
            "cluster_dns_ip must be an IP address, without a CIDR prefix"
        ) from None


def verify_cluster_dns(env: dict, cluster: dict) -> str | None:
    """Discover the native resolver; older explicit inputs remain assertions."""
    requested = cluster_dns_address(env)
    if cluster.get("computeConfig", {}).get("enabled") is not True:
        require(requested is None, "cluster_dns_ip requires EKS Auto Mode")
        return None
    network = cluster.get("kubernetesNetworkConfig", {})
    try:
        version = {"ipv4": 4, "ipv6": 6}[network["ipFamily"]]
        cidr = ipaddress.ip_network(network[f"serviceIpv{version}Cidr"])
        address = cidr.network_address + 10
    except (KeyError, TypeError, ValueError):
        raise Refusal(
            "Cannot discover cluster_dns_ip from the EKS service network"
        ) from None
    require(
        cidr.version == version and address in cidr,
        "Cannot discover cluster_dns_ip from the EKS service network",
    )
    cluster_dns_address({"cluster_dns_ip": str(address)})
    require(
        requested is None or requested == address,
        "cluster_dns_ip must match the selected EKS Auto Mode resolver",
    )
    return str(address)


def validate(
    env: dict,
    lock: dict | None,
    *,
    control_plane_only: bool = False,
    preparation: bool = False,
) -> None:
    """No credentials, shell fragments, caller-selected commands or guessed targets.

    control_plane_only defers workspace-specific fields (workspace_cluster,
    workspace_namespace, workspace_id, cluster_id, controller_ownership and the
    workspace_access secret reference) so the management surface can be installed
    before any workspace cluster or controller credential exists.  All other fields
    remain unconditionally required.  Workspace activation later must pass the full
    validation.

    Pass lock=None to validate environment-only inputs (e.g. for --prepare-database)
    and skip all release-lock checks.
    """
    control_plane_only = control_plane_mode(env, control_plane_only) or preparation
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
        "control_plane_only",
        "image_execution",
        "gateway_namespace",
        "cluster_dns_ip",
        "execution",
        "controller_profiles",
    }
    require(
        set(env) <= allowed,
        "Unknown environment fields; secrets belong in Secrets Manager",
    )
    cluster_dns_address(env)
    from .execution import validate_execution

    validate_execution(env, lock)
    require(
        env.get("image_execution", "docker") in {"docker", "cluster"},
        "image_execution must be docker or cluster",
    )
    require(
        IDENTIFIER.fullmatch(env.get("gateway_namespace", "adp")),
        "gateway_namespace must be a namespace name",
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
    unconditional_identifiers = ("environment", "namespace", "skypilot_namespace")
    workspace_identifiers = ("workspace_namespace",)
    for key in unconditional_identifiers:
        require(
            isinstance(env.get(key), str) and IDENTIFIER.fullmatch(env[key]),
            f"Invalid {key}",
        )
    if not control_plane_only:
        for key in workspace_identifiers:
            require(
                isinstance(env.get(key), str) and IDENTIFIER.fullmatch(env[key]),
                f"Invalid {key}",
            )
    require(
        isinstance(env.get("cluster"), str) and EKS_NAME.fullmatch(env["cluster"]),
        "Invalid cluster",
    )
    if not control_plane_only:
        require(
            isinstance(env.get("workspace_cluster"), str)
            and EKS_NAME.fullmatch(env["workspace_cluster"]),
            "Invalid workspace_cluster",
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
    if not control_plane_only:
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
    uuid_always = ("org_id",)
    uuid_workspace = ("workspace_id", "cluster_id")
    for key in uuid_always:
        require(
            re.fullmatch(
                r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", str(env.get(key, ""))
            ),
            f"{key} must be an immutable UUID",
        )
    if not control_plane_only:
        for key in uuid_workspace:
            require(
                re.fullmatch(
                    r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}",
                    str(env.get(key, "")),
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
    required_secrets = (
        {"database", "observation"}
        if control_plane_only
        else {"database", "observation", "workspace_access"}
    )
    require(
        set(env.get("secrets", {})) == required_secrets
        or (
            preparation
            and set(env.get("secrets", {})) == required_secrets | {"workspace_access"}
        ),
        "Secret references must name exactly the documented domain-owned references"
        " (workspace_access is required only for full installation)",
    )
    for key in env.get("secrets", {}):
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
    if lock is not None:
        # Database preparation does not require a built release.
        require(
            SHA.fullmatch(str(lock.get("source_revision", ""))),
            "release lock needs the exact maintained source_revision",
        )
        require(
            lock.get("schema", {}).get("single_head") is True,
            "release schema must have a single head",
        )
        head = lock["schema"].get("observed", {}).get("head")
        # Exact equality, not "at least": the installer runs `alembic upgrade head` from
        # the API image and then asserts the reported revision equals this one
        # (`runner.py`), so an unrecognized head is as much a refusal as a stale one.
        # w6-10 (#5533) advances it to 017 for `workspace_bootstrap_reservations`, the
        # same way U11c advanced it to 013, U7b to 014 and U23 to 015.
        # #6048 advances it to 036 for explicit shared cluster membership.
        require(
            head == "036_shared_cluster_membership",
            "release schema must include credential-reference, replay-safe create, and workspace operation state",
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
                    SHA.fullmatch(str(source.get("source_revision", ""))),
                    f"Image provenance needs an exact source revision: {component}",
                )
    require(
        preparation or env.get("network_policy_enforced") is True,
        "The selected cluster must enforce Kubernetes NetworkPolicy",
    )
    if not control_plane_only:
        require(
            env.get("execution") is not None,
            "Full activation requires the trusted executor release and existing run projections",
        )
        require(
            env.get("controller_ownership") == "single-workspace-controller",
            "Explicit single-controller ownership is required",
        )
    from .controller_profiles import validate_profiles

    validate_profiles(env, control_plane_only=control_plane_only)


def image(lock: dict, component: str) -> str:
    source = lock["image_sources"][component]
    return f"{source['registry']}/{source['repository']}@{lock['images'][component]}"


def identity(env: dict) -> str:
    return digest(
        {k: env[k] for k in ("account_id", "region", "environment", "cluster")}
    )[:24]


def prepare_database_sql(env: dict) -> str:
    """Prepare only new or previously marked domain schemas and roles, atomically.

    This emits SQL, not credentials or a database connection. Existing unmarked
    resources are refused rather than silently adopted or transferred. Applying
    the same script again preserves identities and existing runtime data.
    """
    db = env["database"]
    schemas = (db["schema"], db["skypilot_schema"])
    require(
        all(
            isinstance(name, str)
            and SCHEMA.fullmatch(name)
            and name not in {"public", "information_schema"}
            and not name.startswith("pg_")
            for name in schemas
        )
        and schemas[0] != schemas[1],
        "Database preparation requires two distinct isolated schemas",
    )
    require(
        isinstance(env["environment"], str)
        and IDENTIFIER.fullmatch(env["environment"]),
        "Invalid environment",
    )
    require(
        isinstance(db["database"], str)
        and db["database"]
        and "\x00" not in db["database"]
        and len(db["database"].encode()) <= 63,
        "Invalid database identifier",
    )

    def literal(value):
        return "'" + value.replace("'", "''") + "'"

    def identifier(value):
        return '"' + value.replace('"', '""') + '"'

    def block(body):
        # A quoted body avoids dollar-quote delimiter injection from database names.
        return "DO " + literal("BEGIN\n" + body + "\nEND") + ";"

    api, sky = map(identifier, schemas)
    database = identifier(db["database"])
    roles = {
        key: f"superplane_{env['environment']}_{key}"
        for key in ("migration", "runtime", "skypilot")
    }
    migration, runtime, skypilot = (
        identifier(roles[key]) for key in ("migration", "runtime", "skypilot")
    )
    marker = "adp-superplane-database-v1:" + digest(
        {
            "environment": env["environment"],
            "database": db["database"],
            "schemas": schemas,
        }
    )
    lines = [
        "-- Domain-only preparation. Run against the selected database; no credentials are included.",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
        "SET LOCAL lock_timeout = '10s';",
        "SET LOCAL statement_timeout = '60s';",
        block(
            f"IF current_database() <> {literal(db['database'])} THEN\n"
            "  RAISE EXCEPTION 'Database preparation target mismatch';\nEND IF;"
        ),
        "SELECT pg_advisory_xact_lock(hashtextextended('adp-superplane-prepare:' || current_database(), 0));",
    ]
    for role in roles.values():
        name, quoted = literal(role), identifier(role)
        lines.append(
            block(f"""
IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name}) THEN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles r WHERE r.rolname = {name}
      AND shobj_description(r.oid, 'pg_authid') = {literal(marker)}
      AND r.rolcanlogin AND NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)
      AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = r.oid)
  ) THEN
    RAISE EXCEPTION 'Existing database role is unowned or has incompatible authority';
  END IF;
ELSE
  CREATE ROLE {quoted} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  COMMENT ON ROLE {quoted} IS {literal(marker)};
END IF;""")
        )
        # RDS does not apply createrole_self_grant consistently to rds_superuser.
        # The selected trusted administrator needs explicit membership to create
        # and grant objects owned by these three installation-owned roles.
        lines.append(f"GRANT {quoted} TO CURRENT_USER WITH SET TRUE, INHERIT TRUE;")
    for schema, owner in zip(
        schemas, (roles["migration"], roles["skypilot"]), strict=True
    ):
        name, quoted = literal(schema), identifier(schema)
        lines.append(
            block(f"""
IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = {name}) THEN
  IF NOT EXISTS (
    SELECT 1 FROM pg_namespace n WHERE n.nspname = {name}
      AND pg_get_userbyid(n.nspowner) = {literal(owner)}
      AND obj_description(n.oid, 'pg_namespace') = {literal(marker)}
  ) THEN
    RAISE EXCEPTION 'Existing database schema is unowned or owned by another role';
  END IF;
ELSE
  CREATE SCHEMA {quoted} AUTHORIZATION {identifier(owner)};
  COMMENT ON SCHEMA {quoted} IS {literal(marker)};
END IF;""")
        )
    lines.extend(
        [
            f"REVOKE ALL ON SCHEMA {api}, {sky} FROM PUBLIC;",
            f"GRANT CONNECT ON DATABASE {database} TO {migration}, {runtime}, {skypilot};",
            f"GRANT USAGE ON SCHEMA {api} TO {runtime};",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration} IN SCHEMA {api} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {runtime};",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration} IN SCHEMA {api} GRANT USAGE, SELECT ON SEQUENCES TO {runtime};",
            f"ALTER ROLE {migration} IN DATABASE {database} SET search_path = {api}, pg_catalog;",
            f"ALTER ROLE {runtime} IN DATABASE {database} SET search_path = {api}, pg_catalog;",
            f"ALTER ROLE {skypilot} IN DATABASE {database} SET search_path = {sky}, pg_catalog;",
        ]
    )
    for role, schema in zip(
        roles.values(), (schemas[0], schemas[0], schemas[1]), strict=True
    ):
        name = literal(role)
        lines.append(
            block(f"""
IF has_database_privilege({name}, current_database(), 'CREATE') OR EXISTS (
  SELECT 1 FROM pg_namespace n
  WHERE n.nspname <> {literal(schema)} AND n.nspname NOT LIKE 'pg_%'
    AND n.nspname <> 'information_schema'
    AND (has_schema_privilege({name}, n.oid, 'CREATE') OR EXISTS (
      SELECT 1 FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind IN ('r','p','v','m','f')
        AND has_table_privilege({name}, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
    ))
) THEN
  RAISE EXCEPTION 'Database role can mutate outside its domain schema; shared grants require separate remediation';
END IF;""")
        )
    lines.append("COMMIT;")
    return "\n\n".join(lines) + "\n"
